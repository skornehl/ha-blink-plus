"""Still grabs must not keep a live view alive (or start one).

Dashboards and displays fetch a camera still every few seconds; while a live
view runs, Blink Plus grabs that frame from its own stream with ffmpeg. Those
grabs used to count as viewers, so a live view never ended. Here a real viewer
watches, real ffmpeg grabs run every second (same arguments as Home
Assistant's ffmpeg.async_get_image), the viewer leaves, and the session must
end after the linger time while the grabs go on.

    python3 test_stills.py . ../custom_components/blink_plus/immis.py
"""

import asyncio
import importlib.util
import os
import ssl
import sys
import time
import types

TEST_DIR, IMMIS_PY = sys.argv[1], sys.argv[2]
state = {"port": None, "liveview": 0}

api = types.ModuleType("blinkpy.api")


async def http_post(blink, url, data=None, **kw):
    if not url.endswith("/liveview"):
        return {}
    state["liveview"] += 1
    return {
        "command_id": 5000 + state["liveview"],
        "network_id": 42,
        "polling_interval": 2,
        "server": f"immis://127.0.0.1:{state['port']}/S{state['liveview']:03d}__IMDS_SERST?client_id=1",
        "liveview_token": "TOK",
    }


async def request_command_status(blink, net, cid):
    return {"status_code": 908, "commands": [{"id": cid, "state_condition": "running"}]}


async def request_command_done(blink, net, cid):
    return {}


api.http_post, api.request_command_status, api.request_command_done = (
    http_post, request_command_status, request_command_done,
)  # fmt: skip
pkg = types.ModuleType("blinkpy")
pkg.api = api
sys.modules["blinkpy"] = pkg
sys.modules["blinkpy.api"] = api
spec = importlib.util.spec_from_file_location("immis", IMMIS_PY)
immis = importlib.util.module_from_spec(spec)
sys.modules["immis"] = immis
spec.loader.exec_module(immis)
video = open(os.path.join(TEST_DIR, "sample.ts"), "rb").read()


async def relay(reader, writer):
    await reader.readexactly(122)

    async def drain_client():
        try:
            while await reader.read(4096):
                pass
        except Exception:  # noqa: BLE001
            pass

    rt = asyncio.create_task(drain_client())
    try:
        seq = 0
        while True:
            for off in range(0, len(video), 1316):
                p = video[off : off + 1316]
                seq += 1
                writer.write(bytes([0]) + seq.to_bytes(4, "big") + len(p).to_bytes(4, "big") + p)
                await writer.drain()
                await asyncio.sleep(0.02)
    except (ConnectionError, OSError):
        pass
    finally:
        rt.cancel()


class Cam:
    name = "Hof Kamera"
    serial = "SERST"
    network_id = "42"
    camera_id = "31"
    camera_type = ""


class Urls:
    base_url = "https://x"


class Blink:
    account_id = 1
    urls = Urls()


async def viewer(url, stop):
    host, port = url.split("//")[1].split("/")[0].split(":")
    r, w = await asyncio.open_connection(host, int(port))
    w.write(b"GET /stream.ts HTTP/1.1\r\nHost: x\r\n\r\n")
    await w.drain()
    got = 0
    try:
        while not stop.is_set():
            try:
                chunk = await asyncio.wait_for(r.read(65536), 0.5)
            except TimeoutError:
                continue
            if not chunk:
                break
            got += len(chunk)
    except ConnectionError:
        pass
    w.close()
    return got


async def grab(url):
    """One still, the way Home Assistant's ffmpeg.async_get_image does it."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", url, "-an", "-frames:v", "1",
        "-c:v", "mjpeg", "-s", "484x273", "-f", "image2pipe", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return b""
    return out if proc.returncode == 0 else b""


async def main():
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(f"{TEST_DIR}/cert.pem", f"{TEST_DIR}/key.pem")
    srv = await asyncio.start_server(relay, "127.0.0.1", 0, ssl=ctx)
    state["port"] = srv.sockets[0].getsockname()[1]
    live = immis.BlinkLiveViewServer(Blink(), Cam(), linger=2, max_minutes=10)
    await live.async_start()
    print("still url:", live.still_url)

    # 1. A still never wakes the camera.
    img = await grab(live.still_url)
    await asyncio.sleep(0.5)
    print("1. still while idle:", len(img), "bytes, liveview requests:", state["liveview"])
    assert img == b"" and state["liveview"] == 0 and not live.active

    # 2. A real viewer starts the live view; stills are grabbed every second.
    stop = asyncio.Event()
    watching = asyncio.create_task(viewer(live.url, stop))
    t0 = time.monotonic()
    while live.stats.state != "streaming":
        assert time.monotonic() - t0 < 20, live.stats.as_dict()
        await asyncio.sleep(0.1)
    grabs = []
    grabbing = True

    async def grab_loop():
        while grabbing:
            grabs.append((time.monotonic(), await grab(live.still_url)))
            await asyncio.sleep(1)

    loop = asyncio.create_task(grab_loop())
    await asyncio.sleep(4)
    stats = live.stats.as_dict()
    jpegs = [g for _, g in grabs if g[:2] == b"\xff\xd8"]
    print(f"2. streaming with a viewer: {len(jpegs)}/{len(grabs)} stills are JPEGs, "
          f"live_view_clients={stats['live_view_clients']}")
    assert jpegs, "no still from the running live view"
    assert stats["live_view_clients"] == 1, "still grabs must not count as viewers"

    # 3. The viewer leaves; stills keep coming. The session must end anyway.
    stop.set()
    got = await watching
    left = time.monotonic()
    while live.active:
        assert time.monotonic() - left < live.linger + 6, (
            "still grabs kept the live view alive: " + str(live.stats.as_dict())
        )
        await asyncio.sleep(0.2)
    ended_after = time.monotonic() - left
    print(f"3. viewer left (got {got} bytes); session ended after {ended_after:.1f}s "
          f"while stills continued ({len(grabs)} grabs so far)")

    # 4. Stills after the end: no new session, no new liveview request.
    await asyncio.sleep(4)
    grabbing = False
    await loop
    late = [g for t, g in grabs if t > left + ended_after + 0.5]
    print(f"4. after the end: {len(late)} grabs, {sum(1 for g in late if g)} got a frame, "
          f"liveview requests: {state['liveview']}, state: {live.stats.state}")
    assert late and not any(late), "a still got video after the session ended"
    assert state["liveview"] == 1 and not live.active

    await live.async_stop()
    srv.close()


asyncio.run(main())
print("PASS stills")
