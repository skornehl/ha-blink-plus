"""Blink Plus against the installed Home Assistant (CI installs the newest).

The other tests avoid Home Assistant on purpose. This one imports every module
of the integration under a real Home Assistant and calls the actions through
Home Assistant's own service registry, so API changes in Home Assistant (such
as a helper that drops its ``hass`` argument) fail here instead of at runtime.
Only the camera is faked; ffmpeg is real.

    python3 test_ha_current.py <repo root> <folder with talk_in.wav>
"""

import asyncio
import importlib
import os
import pkgutil
import sys
import tempfile
import types
from unittest.mock import MagicMock

REPO, AUDIO_DIR = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
sys.path.insert(0, REPO)

from homeassistant.const import __version__ as HA_VERSION  # noqa: E402
from homeassistant.core import Context, HomeAssistant  # noqa: E402
from homeassistant.exceptions import ServiceValidationError, Unauthorized  # noqa: E402
import voluptuous as vol  # noqa: E402

import custom_components.blink_plus as blink_plus  # noqa: E402

print("Home Assistant", HA_VERSION)
for mod in pkgutil.iter_modules(blink_plus.__path__):
    importlib.import_module(f"custom_components.blink_plus.{mod.name}")
    print("  imported", mod.name)

from custom_components.blink_plus import live_hold, services, talk_view  # noqa: E402


class FakeServer:
    """The parts of BlinkLiveViewServer that talk uses."""

    def __init__(self, ready=True):
        self.entity_id = "camera.flur"
        self.talk_lock = asyncio.Lock()
        self.talk_ready = ready
        self.frames = 0
        self.stopped = 0

    def talk_handle(self):
        return "handle"

    async def async_start_talk(self, handle):
        pass

    async def async_send_audio(self, handle, frame):
        self.frames += 1

    async def async_stop_talk(self, handle):
        self.stopped += 1


class FakeLiveServer:
    """The parts of BlinkLiveViewServer that start_live_view uses, with a real
    local TCP server that streams like the live view server once a client is in."""

    def __init__(self, blocked=0, max_seconds=300):
        self.entity_id = "camera.flur"
        self.name = "Flur"
        self.max_seconds = max_seconds
        self.blocked = blocked
        self.stats = types.SimpleNamespace(state="idle", last_error=None)
        self.clients = 0
        self.connections = 0

    async def async_start(self):
        self._srv = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._srv.sockets[0].getsockname()[1]

    async def async_stop(self):
        self._srv.close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/stream.ts"

    @property
    def active(self):
        return self.clients > 0

    @property
    def blocked_for(self):
        return self.blocked

    async def _handle(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        self.clients += 1
        self.connections += 1
        self.stats.state = "streaming"
        gone = asyncio.ensure_future(reader.read())
        try:
            writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
            while not gone.done():
                writer.write(b"\x47" + bytes(187))
                await writer.drain()
                await asyncio.sleep(0.05)
        except (ConnectionError, OSError):
            pass
        finally:
            gone.cancel()
            writer.close()
            self.clients -= 1
            self.stats.state = "idle"


async def wait_for(check, timeout=3):
    for _ in range(int(timeout * 20)):
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


async def check_start_live_view(hass, servers):
    """blink_plus.start_live_view through the registry, against a fake server."""

    async def start(entity_id, **data):
        return await hass.services.async_call(
            "blink_plus",
            "start_live_view",
            {"entity_id": entity_id, **data},
            blocking=True,
            return_response=True,
        )

    server = FakeLiveServer(max_seconds=20)
    await server.async_start()
    servers["camera.flur"] = server

    # Opens the live view like a player and keeps it; default 60 s, cut to the limit.
    resp = await start("camera.flur")
    print("start_live_view:", resp)
    flur = resp["camera.flur"]
    assert flur["holding"] and flur["state"] == "streaming" and flur["cooldown_s"] == 0
    assert flur["seconds"] == 20, flur
    assert server.connections == 1 and server.clients == 1
    assert await wait_for(lambda: live_hold.get_hold(server).bytes > 1000)

    # Again: one holder per camera, only the time moves.
    resp = await start("camera.flur", seconds=10)
    assert resp["camera.flur"]["holding"] and server.connections == 1, resp

    # Without a response (as from an automation) it works too.
    await hass.services.async_call(
        "blink_plus", "start_live_view", {"entity_id": "camera.flur"}, blocking=True
    )
    assert server.connections == 1

    # stop_live_view (camera.stop_live_view) ends the hold first.
    assert await live_hold.async_stop_hold(server) is True
    assert await wait_for(lambda: server.clients == 0), "hold still connected"
    print("stopped, server clients:", server.clients)

    # Cool-down: nothing is opened, the answer says how long to wait.
    server.blocked = 7
    resp = await start("camera.flur", seconds=30)
    print("in cool-down:", resp)
    assert resp["camera.flur"] == {
        "state": "idle", "holding": False, "seconds": 0, "cooldown_s": 7, "last_error": None,
    }
    assert server.connections == 1

    # Schema: seconds must be positive.
    try:
        await start("camera.flur", seconds=0)
        raise AssertionError("expected vol.Invalid")
    except vol.Invalid:
        print("seconds=0: rejected")

    # Not a Blink Plus camera.
    try:
        await start("camera.other")
        raise AssertionError("expected ServiceValidationError")
    except ServiceValidationError as err:
        print("not a Blink Plus camera:", err.translation_key)
        assert err.translation_key == "live_no_camera"

    await server.async_stop()


async def main():
    with tempfile.TemporaryDirectory() as config_dir:
        hass = HomeAssistant(config_dir)
        hass.config.allowlist_external_dirs = {AUDIO_DIR}
        services.async_setup_services(hass)
        wav = os.path.join(AUDIO_DIR, "talk_in.wav")
        servers = {"camera.flur": FakeServer()}
        talk_view.find_live_server = lambda _hass, entity_id: servers.get(entity_id)

        async def play(entity_id, context=None):
            await hass.services.async_call(
                "blink_plus",
                "play_audio",
                {"entity_id": entity_id, "file_path": wav},
                blocking=True,
                context=context,
            )

        # Through the real service registry, schema and target extraction.
        await play("camera.flur")
        server = servers["camera.flur"]
        print("play_audio:", server.frames, "frames, speaker off", server.stopped, "x")
        assert 14 <= server.frames <= 18 and server.stopped == 1

        # Not a Blink Plus camera.
        try:
            await play("camera.other")
            raise AssertionError("expected ServiceValidationError")
        except ServiceValidationError as err:
            print("not a Blink Plus camera:", err.translation_key)
            assert err.translation_key == "talk_no_camera"

        # No open live view.
        servers["camera.flur"] = FakeServer(ready=False)
        try:
            await play("camera.flur")
            raise AssertionError("expected ServiceValidationError")
        except ServiceValidationError as err:
            print("no live view:", err.translation_key)
            assert err.translation_key == "talk_no_live_view"

        # start_live_view, with its own fake server.
        talk_servers = servers
        servers = {}
        talk_view.find_live_server = lambda _hass, entity_id: servers.get(entity_id)
        await check_start_live_view(hass, servers)
        servers = talk_servers
        talk_view.find_live_server = lambda _hass, entity_id: servers.get(entity_id)

        # A user without control permission for the camera.
        servers["camera.flur"] = FakeServer()
        user = MagicMock()
        user.permissions.check_entity.return_value = False

        async def get_user(user_id):
            return user

        hass.auth = MagicMock()
        hass.auth.async_get_user = get_user
        try:
            await play("camera.flur", Context(user_id="someone"))
            raise AssertionError("expected Unauthorized")
        except Unauthorized:
            print("user without permission: Unauthorized")
        assert servers["camera.flur"].frames == 0
        try:
            await hass.services.async_call(
                "blink_plus", "start_live_view", {"entity_id": "camera.flur"},
                blocking=True, context=Context(user_id="someone"), return_response=True,
            )
            raise AssertionError("expected Unauthorized")
        except Unauthorized:
            print("start_live_view without permission: Unauthorized")

        # Older Home Assistant had async_extract_entity_ids(hass, service_call).
        real = talk_view.service.async_extract_entity_ids
        seen = []

        async def old_style(hass, service_call, expand_group=True):  # noqa: ARG001
            seen.append(hass)
            return {"camera.flur"}

        talk_view.service.async_extract_entity_ids = old_style
        try:
            ids = await talk_view.extract_entity_ids(hass, MagicMock(context=Context()))
        finally:
            talk_view.service.async_extract_entity_ids = real
        print("old helper signature:", ids, "got hass:", seen == [hass])
        assert ids == {"camera.flur"} and seen == [hass]

        await hass.async_stop(force=True)


asyncio.run(main())
print("PASS current Home Assistant")
