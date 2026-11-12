"""Two-way talk for Home Assistant: the HTTP endpoint, the play_audio action
and start_live_view (keeps a live view open, so talk and players find it warm).

The actions are plain domain services, not entity services: entity services
of this platform run one at a time (``PARALLEL_UPDATES = 1``), so a running
``camera.record`` would hold the speaker (or the live view) back until it
finished.

HTTP endpoint:

``POST /api/blink_plus/talk/<camera entity id>`` with an audio body. Needs a
Home Assistant login with control permission for the camera, and an open live
view.

* ADTS AAC (AAC-LC, 16 kHz, mono) is sent as it arrives, so it can be a live
  stream:

      ffmpeg -i mic.wav -ac 1 -ar 16000 -c:a aac -b:a 32k -f adts - | \\
        curl -H "Authorization: Bearer $TOKEN" --data-binary @- \\
          http://homeassistant.local:8123/api/blink_plus/talk/camera.front_door

* Anything else ffmpeg can read (WebM/Opus from a browser recording, MP3, WAV,
  M4A ...) is read completely first (max. 8 MB) and then transcoded. The talk
  card that ships with the integration uses this.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
import inspect
import os
import tempfile
from typing import Any

from aiohttp import ClientError, web

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
    UnknownUser,
)
from homeassistant.helpers import service

from .const import DOMAIN
from .immis import BlinkLiveViewServer, TalkError
from .live_hold import async_start_hold
from .talk import (
    FRAME_SECONDS,
    MAX_UPLOAD,
    AudioFormatError,
    TalkBusyError,
    async_talk,
    ffmpeg_adts,
    is_adts,
)

READ_CHUNK = 8192


def find_live_server(hass: HomeAssistant, entity_id: str) -> BlinkLiveViewServer | None:
    """Return the live view server of a Blink Plus camera entity."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        for server in entry.runtime_data.live_servers.values():
            if server.entity_id == entity_id:
                return server
    return None


def ffmpeg_binary(hass: HomeAssistant) -> str:
    """Return the ffmpeg binary Home Assistant is configured with."""
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _write_temp(data: bytes) -> str:
    """Write the uploaded audio to a temporary file for ffmpeg."""
    fd, path = tempfile.mkstemp(prefix="blink_plus_talk_", suffix=".audio")
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class BlinkTalkView(HomeAssistantView):
    """Speak audio through a camera's open live view."""

    url = "/api/blink_plus/talk/{entity_id}"
    name = "api:blink_plus:talk"
    requires_auth = True

    async def post(self, request: web.Request, entity_id: str) -> web.Response:
        """Send the request body to the camera speaker."""
        hass = request.app[KEY_HASS]
        user = request["hass_user"]
        if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
            raise web.HTTPForbidden
        server = find_live_server(hass, entity_id)
        if server is None:
            raise web.HTTPNotFound(text="Unknown Blink Plus camera")
        temp: str | None = None
        try:
            head = await request.content.read(READ_CHUNK)
            if not head:
                raise web.HTTPBadRequest(text="Empty body")
            if is_adts(head):
                sent = await async_talk(server, _chain(head, request))
            else:
                data = bytearray(head)
                async for chunk in request.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > MAX_UPLOAD:
                        raise web.HTTPBadRequest(text="Audio too large")
                temp = await hass.async_add_executor_job(_write_temp, bytes(data))
                async with aclosing(ffmpeg_adts(temp, ffmpeg=ffmpeg_binary(hass))) as chunks:
                    sent = await async_talk(server, chunks)
        except TalkBusyError as err:
            raise web.HTTPConflict(text=str(err)) from None
        except AudioFormatError as err:
            raise web.HTTPBadRequest(text=str(err)) from None
        except TimeoutError:
            raise web.HTTPRequestTimeout(text="Talk exceeded its time limit") from None
        except TalkError as err:
            raise web.HTTPServiceUnavailable(text=str(err)) from None
        except (ConnectionError, ClientError):
            # The client went away mid-upload; the speaker is already off.
            raise web.HTTPBadRequest(text="Upload interrupted") from None
        finally:
            if temp:
                await hass.async_add_executor_job(_remove, temp)
        return self.json({"frames": sent, "seconds": round(sent * FRAME_SECONDS, 2)})


async def _chain(head: bytes, request: web.Request) -> AsyncIterator[bytes]:
    """Yield the already read first chunk, then the rest of the body."""
    yield head
    async for chunk in request.content.iter_chunked(READ_CHUNK):
        yield chunk


async def extract_entity_ids(hass: HomeAssistant, call: ServiceCall) -> set[str]:
    """Entity ids targeted by a service call, on old and new Home Assistant.

    Home Assistant dropped the ``hass`` argument of
    ``service.async_extract_entity_ids`` (it now takes the call only).
    """
    extract = service.async_extract_entity_ids
    first = next(iter(inspect.signature(extract).parameters), None)
    if first == "hass":
        return await extract(hass, call)
    return await extract(call)


async def targeted_servers(
    hass: HomeAssistant, call: ServiceCall, no_camera_key: str
) -> list[BlinkLiveViewServer]:
    """Live view servers of the targeted cameras, after the permission check.

    Raises ``no_camera_key`` if none of the targets is a Blink Plus camera.
    """
    entity_ids = await extract_entity_ids(hass, call)
    if call.context.user_id:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None:
            raise UnknownUser(context=call.context)
        for entity_id in entity_ids:
            if not user.permissions.check_entity(entity_id, POLICY_CONTROL):
                raise Unauthorized(
                    context=call.context, entity_id=entity_id, permission=POLICY_CONTROL
                )
    servers = [
        server
        for entity_id in sorted(entity_ids)
        if (server := find_live_server(hass, entity_id)) is not None
    ]
    if not servers:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=no_camera_key
        )
    return servers


async def async_handle_play_audio(hass: HomeAssistant, call: ServiceCall) -> None:
    """blink_plus.play_audio: play a file on each targeted camera's speaker."""
    for server in await targeted_servers(hass, call, "talk_no_camera"):
        await async_play_audio(hass, server, call.data["file_path"])


async def async_handle_start_live_view(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """blink_plus.start_live_view: open each targeted camera's live view and
    keep it open for ``seconds`` (or until stop_live_view).

    Returns per camera: ``state``, ``holding``, ``seconds`` (left),
    ``cooldown_s`` (> 0: the camera refuses to start for that long, nothing
    was opened) and ``last_error``.
    """
    seconds = call.data["seconds"]
    result: dict[str, Any] = {}
    for server in await targeted_servers(hass, call, "live_no_camera"):
        name = f"{DOMAIN} live view hold {server.entity_id}"
        result[str(server.entity_id)] = await async_start_hold(
            server,
            seconds,
            create_task=lambda coro, name=name: hass.async_create_background_task(
                coro, name
            ),
        )
    return result


async def async_play_audio(
    hass: HomeAssistant, server: BlinkLiveViewServer, file_path: str
) -> None:
    """Play an audio file through one camera's speaker (open live view only)."""
    if not hass.config.is_allowed_path(file_path):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_path",
            translation_placeholders={"target": file_path},
        )
    if not await hass.async_add_executor_job(os.path.isfile, file_path):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="talk_no_file",
            translation_placeholders={"target": file_path},
        )
    if not server.talk_ready:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="talk_no_live_view"
        )
    try:
        async with aclosing(ffmpeg_adts(file_path, ffmpeg=ffmpeg_binary(hass))) as chunks:
            await async_talk(server, chunks)
    except TalkBusyError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="talk_busy"
        ) from err
    except AudioFormatError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="talk_bad_audio",
            translation_placeholders={"error": str(err)},
        ) from err
    except TalkError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="talk_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    except TimeoutError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="talk_too_long"
        ) from err
