"""start_live_view's hold against a real BlinkLiveViewServer and a fake relay.

The hold is a local client like a player: it must start a Blink session, keep
it running so a player that attaches later gets video at once, and end it
(STOP to the relay) when its time is up or it is stopped. One hold per camera;
a camera in cool-down is not touched at all.

    python3 test_hold.py <folder with sample.ts, cert.pem, key.pem> <immis.py> <live_hold.py>
"""

import asyncio
import importlib.util
import os
import ssl
import sys
import time
import types

TEST_DIR, IMMIS_PY, HOLD_PY = sys.argv[1], sys.argv[2], sys.argv[3]
state = {"port": None, "sessions": 0, "open": 0, "stops": 0}

api = types.ModuleType("blinkpy.api")


async def http_post(blink, url, data=None, **kw):
    if url.endswith("/done"):
        return {}
    state["sessions"] += 1
    n = state["sessions"]
    return {
        "command_id": 3000 + n, "network_id": 42, "polling_interval": 2,
        "server": f"immis://127.0.0.1:{state['port']}/H{n:03d}__IMDS_SERHOLD?client_id=1",
    }


async def request_command_status(b, net, cid):
    return {"status_code": 908, "commands": [{"id": cid, "state_condition": "running"}]}


async def request_command_done(b, net, cid):
    return {}


api.http_post = http_post
api.request_command_status = request_command_status
api.request_command_done = request_command_done
pkg = types.ModuleType("blinkpy")
pkg.api = api
sys.modules["blinkpy"] = pkg
sys.modules["blinkpy.api"] = api


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


immis = load("immis", IMMIS_PY)
live_hold = load("live_hold", HOLD_PY)
data = open(os.path.join(TEST_DIR, "sample.ts"), "rb").read()


async def relay(reader, writer):
    await reader.readexactly(122)
    state["open"] += 1

    async def read_client():
        try:
            while True:
                head = await reader.readexactly(9)
                size = int.from_bytes(head[5:9], "big")
                if size:
                    await reader.readexactly(size)
                if head[0] == 7:
                    state["stops"] += 1
        except Exception:  # noqa: BLE001
            pass

    reading = asyncio.create_task(read_client())
    try:
        seq = 0
        while True:
            for off in range(0, len(data), 1316):
                part = data[off:off + 1316]
                seq += 1
                writer.write(bytes([0]) + seq.to_bytes(4, "big") + len(part).to_bytes(4, "big") + part)
                await writer.drain()
                await asyncio.sleep(0.04)
    except (ConnectionError, OSError):
        pass
    finally:
        state["open"] -= 1
        reading.cancel()


class Cam:
    def __init__(self, name, cid):
        self.name, self.serial, self.network_id, self.camera_id, self.camera_type = name, None, "42", cid, ""


class Urls:
    base_url = "https://x"


class Blink:
    account_id = 1
    urls = Urls()


async def first_bytes(url, timeout=5.0):
    """A player that attaches: seconds until its first video bytes."""
    host, port = url.split("//")[1].split("/")[0].split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b"GET /stream.ts HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    t0 = time.monotonic()
    got = b""
    try:
        while b"\r\n\r\n" not in got or len(got.split(b"\r\n\r\n", 1)[1]) < 188:
            chunk = await asyncio.wait_for(reader.read(65536), timeout)
            if not chunk:
                return None
            got += chunk
        return round(time.monotonic() - t0, 2)
    finally:
        writer.close()


async def until(check, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        await asyncio.sleep(0.1)
    return check()


async def main():
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(f"{TEST_DIR}/cert.pem", f"{TEST_DIR}/key.pem")
    relay_srv = await asyncio.start_server(relay, "127.0.0.1", 0, ssl=ctx)
    state["port"] = relay_srv.sockets[0].getsockname()[1]

    # 1. The hold starts a session and keeps it running.
    srv = immis.BlinkLiveViewServer(Blink(), Cam("Door", "11"), linger=1)
    await srv.async_start()
    status = await live_hold.async_start_hold(srv, 4)
    print("start:", status)
    assert status["holding"] and status["seconds"] == 4 and status["cooldown_s"] == 0
    assert srv.active, "the hold did not start a session"
    assert await until(lambda: srv.stats.state == "streaming", 5), srv.stats.state
    assert state["sessions"] == 1 and state["open"] == 1

    # 2. A player attaching now finds it warm: video at once, same session.
    took = await first_bytes(srv.url)
    print("player attached to the warm stream, first video after", took, "s")
    assert took is not None and took < 1.0, took
    assert state["sessions"] == 1

    # 3. Starting again extends the one hold instead of adding a second client.
    hold = live_hold.get_hold(srv)
    await asyncio.sleep(1)
    status = await live_hold.async_start_hold(srv, 4)
    assert live_hold.get_hold(srv) is hold and status["seconds"] == 4, status
    assert len(srv._clients) == 1, srv._clients
    print("extended:", status)

    # 4. Time up: the hold leaves, the server lingers, then ends the session with STOP.
    assert await until(lambda: live_hold.get_hold(srv) is None, 6), "hold did not end"
    print("hold read", hold.bytes, "bytes")
    assert hold.bytes > 10000
    assert await until(lambda: not srv.active, 5), "session still running after the hold"
    assert await until(lambda: state["stops"] == 1 and state["open"] == 0, 3), state
    assert srv.stats.state == "idle"

    # 5. stop_hold ends it early.
    await live_hold.async_start_hold(srv, 60)
    assert await until(lambda: srv.stats.state == "streaming", 5)
    assert await live_hold.async_stop_hold(srv) is True
    assert live_hold.get_hold(srv) is None
    assert await until(lambda: not srv.active, 5), "stop did not end the session"
    assert state["sessions"] == 2 and await until(lambda: state["stops"] == 2, 3), state
    assert await live_hold.async_stop_hold(srv) is False
    print("stopped early, sessions:", state["sessions"], "STOPs:", state["stops"])

    # 6. Cool-down (e.g. another camera on the sync module just took over):
    #    nothing is opened, the answer says how long to wait.
    srv._block(5)
    status = await live_hold.async_start_hold(srv, 60)
    print("in cool-down:", status)
    assert not status["holding"] and status["cooldown_s"] >= 4 and not srv.active
    assert state["sessions"] == 2

    # 7. The hold never outlasts the live view time limit.
    short = immis.BlinkLiveViewServer(Blink(), Cam("Mini", "22"), linger=1, max_minutes=0.05)
    await short.async_start()
    status = await live_hold.async_start_hold(short, 600)
    print("time limit 3 s:", status)
    assert status["seconds"] <= 3
    await live_hold.async_stop_hold(short)

    await srv.async_stop()
    await short.async_stop()
    relay_srv.close()


asyncio.run(main())
print("PASS hold")
