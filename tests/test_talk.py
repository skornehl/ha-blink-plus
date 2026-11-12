"""Two-way talk against a fake Blink IMMIS relay (TLS) + real ffmpeg.

Checks the packet sequence the camera needs (token in the auth header, initial
keepalive, speaker on 0x17/3, fresh 0x18 ack, 0x05 audio frames counting from
0, speaker off 0x17/4), real-time pacing, the per-camera lock, the time limit,
format checks, and that talk refuses to run without an open live view.

    python3 test_talk.py . ../custom_components/blink_plus/immis.py ../custom_components/blink_plus/talk.py
"""

import asyncio
from contextlib import aclosing
import importlib.util
import os
import ssl
import subprocess
import sys
import time
import types

TEST_DIR, IMMIS_PY, TALK_PY = sys.argv[1], sys.argv[2], sys.argv[3]
TOKEN = "TALKTOKEN_0123456789"
state = {"port": None, "sessions": [], "ack": True}

# ---- stub blinkpy.api ------------------------------------------------------
api = types.ModuleType("blinkpy.api")
n = {"i": 0}


async def http_post(blink, url, data=None, **kw):
    if not url.endswith("/liveview"):
        return {}
    n["i"] += 1
    return {
        "command_id": 3000 + n["i"],
        "network_id": 42,
        "polling_interval": 2,
        "server": f"immis://127.0.0.1:{state['port']}/TALK{n['i']:04d}__IMDS_G8T1TALK?client_id=5",
        "liveview_token": TOKEN,
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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


immis = load("immis", IMMIS_PY)
talk = load("talk", TALK_PY)
video = open(os.path.join(TEST_DIR, "sample.ts"), "rb").read()


# ---- fake IMMIS relay ------------------------------------------------------
async def relay(reader, writer):
    session = {"header": await reader.readexactly(122), "packets": [], "raw_after_header": b""}
    state["sessions"].append(session)

    async def read_client():
        try:
            first = True
            while True:
                h = await reader.readexactly(9)
                if first:
                    session["raw_after_header"] = h
                    first = False
                ln = int.from_bytes(h[5:9], "big")
                payload = await reader.readexactly(ln) if ln else b""
                seq = int.from_bytes(h[1:5], "big")
                session["packets"].append((h[0], seq, payload))
                if h[0] == 0x17 and seq == 3 and state["ack"]:
                    await asyncio.sleep(0.05)
                    writer.write(bytes([0x18]) + bytes(8))
        except Exception:  # noqa: BLE001
            pass

    rt = asyncio.create_task(read_client())
    # A stale 0x18 before anyone asked: must not count as the talk ack.
    writer.write(bytes([0x18]) + bytes(8))
    try:
        seq = 0
        while True:
            for off in range(0, len(video), 1316):
                p = video[off : off + 1316]
                seq += 1
                writer.write(bytes([0]) + seq.to_bytes(4, "big") + len(p).to_bytes(4, "big") + p)
                await writer.drain()
                await asyncio.sleep(0.04)
    except (ConnectionError, OSError):
        pass
    finally:
        rt.cancel()


class Cam:
    name = "Front Door"
    serial = "G8T1TALK"
    network_id = "42"
    camera_id = "77"
    camera_type = "mini"


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
    try:
        while not stop.is_set():
            try:
                if not await asyncio.wait_for(r.read(65536), 0.5):
                    break
            except TimeoutError:
                pass
    except ConnectionError:
        pass
    w.close()


async def wait_ready(live, secs=20):
    t0 = time.monotonic()
    while not live.talk_ready:
        assert time.monotonic() - t0 < secs, f"live view never got ready: {live.stats.as_dict()}"
        await asyncio.sleep(0.1)


def adts(name, *args, secs=2):
    path = os.path.join(TEST_DIR, name)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
         *args, "-c:a", "aac", "-f", "adts", path],
        check=True,
    )  # fmt: skip
    return open(path, "rb").read()


async def chunked(data, size=1000, delay=0.0):
    for off in range(0, len(data), size):
        if delay:
            await asyncio.sleep(delay)
        yield data[off : off + size]


def talk_packets(session, since=0):
    return [p for p in session["packets"][since:] if p[0] in (0x05, 0x17)]


async def expect(exc, coro, label):
    try:
        await coro
    except exc as err:
        print(f"  {label}: {type(err).__name__}: {err}")
        return err
    raise AssertionError(f"{label}: expected {exc.__name__}")


async def main():
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(f"{TEST_DIR}/cert.pem", f"{TEST_DIR}/key.pem")
    srv = await asyncio.start_server(relay, "127.0.0.1", 0, ssl=ctx)
    state["port"] = srv.sockets[0].getsockname()[1]

    good = adts("talk_ok.aac", "-ac", "1", "-ar", "16000", secs=2)
    frames, rest = talk.split_adts(good)
    assert frames and not rest
    for f in frames:
        talk.check_frame(f)
    print(f"fixture: {len(frames)} ADTS frames, {len(good)} bytes")

    live = immis.BlinkLiveViewServer(Blink(), Cam(), linger=1, max_minutes=10)
    await live.async_start()

    # 1. No live view: refused, nothing sent to Blink.
    print("1. no live view")
    await expect(immis.TalkError, talk.async_talk(live, chunked(good)), "refused")
    assert not state["sessions"] and not live.talk_lock.locked()

    stop_viewer = asyncio.Event()
    view = asyncio.create_task(viewer(live.url, stop_viewer))
    await wait_ready(live)
    s1 = state["sessions"][0]

    # Auth header: real token in the 64-byte field, then the initial keepalive.
    assert s1["header"][30:34] == (64).to_bytes(4, "big")
    assert s1["header"][34:98].rstrip(b"\0") == TOKEN.encode(), s1["header"][34:98]
    assert s1["raw_after_header"] == bytes([0x0A]) + bytes(8), s1["raw_after_header"].hex()
    print("auth header carries the token, initial keepalive seq 0 follows")
    assert live.stats.as_dict()["live_view_token_used"] is True, "token_used must be set while streaming"

    # 2. Normal talk: exact sequence, real-time pacing.
    print("2. talk 2 s of audio")
    mark = len(s1["packets"])
    t0 = time.monotonic()
    sent = await talk.async_talk(live, chunked(good))
    took = time.monotonic() - t0
    await asyncio.sleep(0.3)
    pk = talk_packets(s1, mark)
    print(f"  sent {sent} frames in {took:.2f}s; relay saw {len(pk)} talk packets")
    assert sent == len(frames)
    assert pk[0] == (0x17, 3, b""), pk[0]
    assert pk[-1] == (0x17, 4, b""), pk[-1]
    audio = pk[1:-1]
    assert [p[0] for p in audio] == [0x05] * len(frames)
    assert [p[1] for p in audio] == list(range(len(frames))), "audio seq must count from 0"
    assert [p[2] for p in audio] == frames, "one ADTS frame per packet, unchanged"
    assert took >= len(frames) * talk.FRAME_SECONDS - 0.2, f"not paced: {took:.2f}s"
    assert not live.stats.as_dict()["live_view_talking"]

    # 3. A second talk again starts at seq 0; a concurrent one is refused.
    print("3. per-camera lock")
    mark = len(s1["packets"])
    first = asyncio.create_task(talk.async_talk(live, chunked(good)))
    await asyncio.sleep(0.4)
    assert live.stats.as_dict()["live_view_talking"] is True
    await expect(talk.TalkBusyError, talk.async_talk(live, chunked(good)), "second talker")
    assert await first == len(frames)
    await asyncio.sleep(0.3)
    audio = [p for p in talk_packets(s1, mark) if p[0] == 0x05]
    assert [p[1] for p in audio] == list(range(len(frames)))
    assert [p for p in talk_packets(s1, mark) if p[0] == 0x17] == [(0x17, 3, b""), (0x17, 4, b"")]

    # 4. Wrong format (44.1 kHz stereo) and non-ADTS input: speaker off, no audio.
    print("4. format checks")
    wrong = adts("talk_wrong.aac", "-ac", "2", "-ar", "44100", secs=1)
    for label, data in (("44.1 kHz stereo", wrong), ("not ADTS", b"RIFF" + bytes(5000)), ("empty", b"")):
        mark = len(s1["packets"])
        await expect(talk.AudioFormatError, talk.async_talk(live, chunked(data)), label)
        await asyncio.sleep(0.2)
        pk = talk_packets(s1, mark)
        assert pk == [(0x17, 3, b""), (0x17, 4, b"")], (label, pk)
    mark = len(s1["packets"])
    await expect(talk.AudioFormatError, talk.async_talk(live, chunked(good[:-10])), "truncated")
    await asyncio.sleep(0.2)
    assert talk_packets(s1, mark)[-1] == (0x17, 4, b"")

    # 5. Time limit: talk stops after max_seconds, speaker off.
    print("5. time limit")
    long = adts("talk_long.aac", "-ac", "1", "-ar", "16000", secs=4)
    mark = len(s1["packets"])
    t0 = time.monotonic()
    await expect(TimeoutError, talk.async_talk(live, chunked(long), max_seconds=1), "limit 1 s")
    took = time.monotonic() - t0
    await asyncio.sleep(0.2)
    pk = talk_packets(s1, mark)
    n_audio = sum(1 for p in pk if p[0] == 0x05)
    print(f"  stopped after {took:.2f}s, {n_audio} frames")
    assert took < 1.5 and 10 <= n_audio <= 20 and pk[-1] == (0x17, 4, b"")
    assert not live.talk_lock.locked()

    # 6. No ack from the camera: TalkError, no audio, speaker off.
    print("6. camera does not acknowledge")
    state["ack"] = False
    mark = len(s1["packets"])
    await expect(immis.TalkError, talk.async_talk(live, chunked(good)), "no ack")
    await asyncio.sleep(0.2)
    assert talk_packets(s1, mark) == [(0x17, 3, b""), (0x17, 4, b"")]
    state["ack"] = True

    # 7. Any audio file through ffmpeg (what the play_audio action does).
    print("7. ffmpeg transcode")
    wav = os.path.join(TEST_DIR, "talk_in.wav")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=1",
                    "-ac", "2", "-ar", "48000", wav], check=True)  # fmt: skip
    mark = len(s1["packets"])
    async with aclosing(talk.ffmpeg_adts(wav)) as chunks:
        sent = await talk.async_talk(live, chunks)
    print(f"  48 kHz stereo wav -> {sent} frames")
    assert 14 <= sent <= 18, sent
    async with aclosing(talk.ffmpeg_adts(os.path.join(TEST_DIR, "cert.pem"))) as chunks:
        await expect(talk.AudioFormatError, talk.async_talk(live, chunks), "not audio")
    async with aclosing(talk.ffmpeg_adts(os.path.join(TEST_DIR, "talk_long.aac"), max_seconds=0.5)) as chunks:
        sent = await talk.async_talk(live, chunks)
    assert sent <= 10, f"ffmpeg did not cut at max_seconds: {sent}"

    # 7b. A browser recording (WebM/Opus, what the talk card sends): not ADTS,
    # so the endpoint routes it through ffmpeg; ready ADTS goes straight on.
    print("7b. browser recording (WebM/Opus)")
    assert talk.is_adts(good) and not talk.is_adts(b"") and not talk.is_adts(b"\xff")
    webm = os.path.join(TEST_DIR, "talk_in.webm")
    made = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=500:duration=1",
                           "-ac", "1", "-ar", "48000", "-c:a", "libopus", webm])  # fmt: skip
    if made.returncode:
        print("  skipped: ffmpeg has no libopus")
    else:
        assert not talk.is_adts(open(webm, "rb").read(16)), "WebM must not look like ADTS"
        async with aclosing(talk.ffmpeg_adts(webm)) as chunks:
            sent = await talk.async_talk(live, chunks)
        print(f"  WebM/Opus -> {sent} frames")
        assert 14 <= sent <= 18, sent

    # 8. Live view ends mid-talk: the talk fails, nothing goes to a new session.
    print("8. live view ends mid-talk")
    running = asyncio.create_task(talk.async_talk(live, chunked(long)))
    await asyncio.sleep(0.6)
    stop_viewer.set()
    await live.async_stop_session()
    await expect(immis.TalkError, running, "ended")
    await view
    assert not live.active and not live.talk_ready
    await expect(immis.TalkError, talk.async_talk(live, chunked(good)), "after close")

    # A stale handle from an old session is rejected by the new one.
    stop_viewer = asyncio.Event()
    view = asyncio.create_task(viewer(live.url, stop_viewer))
    await wait_ready(live)
    s2 = state["sessions"][-1]
    assert s2 is not s1
    handle = live.talk_handle()
    stop_viewer.set()
    await live.async_stop_session()
    await view
    stop_viewer = asyncio.Event()
    view = asyncio.create_task(viewer(live.url, stop_viewer))
    await wait_ready(live)
    s3 = state["sessions"][-1]
    await expect(immis.TalkError, live.async_send_audio(handle, frames[0]), "stale handle")
    await live.async_stop_talk(handle)
    await asyncio.sleep(0.2)
    assert not talk_packets(s3), "stale talk touched a newer session"
    stop_viewer.set()
    await live.async_stop_session()
    await view
    await live.async_stop()

    # 9. A live view without the token can't talk; no initial keepalive then.
    print("9. null-token live view")
    nolive = immis.BlinkLiveViewServer(Blink(), Cam(), linger=1, token_mode=immis.TOKEN_MODE_NULL)
    await nolive.async_start()
    stop_viewer = asyncio.Event()
    view = asyncio.create_task(viewer(nolive.url, stop_viewer))
    t0 = time.monotonic()
    while nolive.stats.state != "streaming":
        assert time.monotonic() - t0 < 20
        await asyncio.sleep(0.1)
    s4 = state["sessions"][-1]
    assert s4["header"][34:98] == bytes(64)
    assert s4["raw_after_header"][0] != 0x0A or s4["raw_after_header"][1:5] != bytes(4)
    assert not nolive.talk_ready
    assert nolive.stats.as_dict()["live_view_token_used"] is False
    await expect(immis.TalkError, talk.async_talk(nolive, chunked(good)), "no token")
    await asyncio.sleep(0.2)
    assert not talk_packets(s4)
    stop_viewer.set()
    await nolive.async_stop()
    await view
    srv.close()

    # 10. Closing the command is best effort: blinkpy's ERROR logs for these
    # calls are muted (only for them), and a missing answer is retried once.
    print("10. closing the command (404 = already ended, quiet, retried)")
    import logging

    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    logging.getLogger().addHandler(Grab())
    logging.getLogger("immis").setLevel(logging.DEBUG)
    blinkpy_log = logging.getLogger("blinkpy.auth")

    class Resp:
        def __init__(self, status):
            self.status, self.released = status, False

        def release(self):
            self.released = True

    def bare_error():
        # what blinkpy logs for an aiohttp error that wraps the OS error
        err = ConnectionError()
        err.__cause__ = ConnectionResetError(104, "Connection reset by peer")
        return err

    async def close_with(*answers):
        """answers: one per request, an HTTP status or None (= network error)."""
        records.clear()
        answers, posts, resps = list(answers), [], []

        async def post(blink, url, data=None, json=True, **kw):
            assert json is False, "must ask blinkpy for the raw response"
            posts.append("v1" if "/api/v1/" in url else "legacy")
            answer = answers.pop(0)
            if answer is None:
                blinkpy_log.error(
                    "Connection error. Endpoint %s possibly down or throttled. Error: %s",
                    url, bare_error(),
                )
                return None
            resps.append(Resp(answer))
            return resps[-1]

        immis.blink_api.http_post = post
        await immis.BlinkLiveViewServer(Blink(), Cam())._command_done(42, 4711)
        assert not answers, f"unused answers {answers}"
        assert all(r.released for r in resps), "response not released"
        info = [r for r in records if r.name == "immis" and r.levelno >= logging.INFO]
        muted = [r.getMessage() for r in records if r.name == "immis" and "muted" in r.getMessage()]
        assert not [r for r in records if r.name == "blinkpy.auth"], "blinkpy ERROR leaked"
        return posts, info, muted

    # Blink already ended the command after IMMIS STOP: 404, one request, silent.
    posts, info, muted = await close_with(404)
    print(f"  404: {posts}, info lines: {len(info)}")
    assert posts == ["v1"] and not info and not muted
    assert any("already ended" in r.getMessage() for r in records)

    # Network error, then the retry gets through: no legacy call.
    posts, info, muted = await close_with(None, 200)
    print(f"  network error, then 200: {posts}")
    assert posts == ["v1", "v1"] and not info and len(muted) == 1
    print(f"  debug: {muted[0]}")
    assert "builtins.ConnectionError: ConnectionError() <- builtins.ConnectionResetError: " \
        "ConnectionResetError(104, 'Connection reset by peer')" in muted[0], muted[0]

    # Server error on the app endpoint: try the legacy one.
    posts, info, muted = await close_with(500, 200)
    print(f"  500, then legacy 200: {posts}")
    assert posts == ["v1", "legacy"] and not info

    # No answer at all: both endpoints twice, then one info line.
    posts, info, muted = await close_with(None, None, None, None)
    print(f"  no answer: {posts}, info: {info[0].getMessage() if info else None}")
    assert posts == ["v1", "v1", "legacy", "legacy"] and len(info) == 1 and len(muted) == 4

    records.clear()
    blinkpy_log.error("a real blinkpy error elsewhere")
    assert [r for r in records if r.name == "blinkpy.auth"], "filter must only mute our calls"

asyncio.run(main())
print("PASS talk")
