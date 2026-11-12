"""Camera never sends video: must use ONE liveview command, send STOP, close the command, then refuse reconnects."""
import asyncio, importlib.util, os, ssl, sys, types, time
TEST_DIR, IMMIS_PY = sys.argv[1], sys.argv[2]
calls = []; state = {"port": None, "tls": 0, "stops": 0}
api = types.ModuleType("blinkpy.api")
async def http_post(blink, url, data=None, **kw):
    calls.append(("POST", url.split("/api/")[-1]))
    if url.endswith("/liveview"):
        return {"command_id": 77, "network_id": 42, "polling_interval": 2, "liveview_token": "TOK",
                "server": f"immis://127.0.0.1:{state['port']}/C1__IMDS_SERB2?client_id=1"}
    return {}
async def request_command_status(b, n, c): calls.append(("STATUS", c)); return {"status_code": 908, "commands": [{"id": c, "state_condition": "running"}]}
async def request_command_done(b, n, c): calls.append(("DONE-legacy", c)); return {}
api.http_post, api.request_command_status, api.request_command_done = http_post, request_command_status, request_command_done
pkg = types.ModuleType("blinkpy"); pkg.api = api; sys.modules["blinkpy"] = pkg; sys.modules["blinkpy.api"] = api
spec = importlib.util.spec_from_file_location("immis", IMMIS_PY); immis = importlib.util.module_from_spec(spec); sys.modules["immis"] = immis; spec.loader.exec_module(immis)
immis.FIRST_DATA_TIMEOUT = 4
async def relay(reader, writer):   # accepts auth, camera never sends video
    state["tls"] += 1
    await reader.readexactly(122)
    try:
        while True:
            h = await reader.readexactly(9); ln = int.from_bytes(h[5:9], "big")
            if ln: await reader.readexactly(ln)
            if h[0] == 7: state["stops"] += 1
    except Exception: pass
class Cam: name="B2"; serial=None; network_id="42"; camera_id="5"; camera_type=""
class U: base_url = "https://x"
class B: account_id = 1; urls = U()
async def viewer(url, secs):
    host, port = url.split("//")[1].split("/")[0].split(":")
    r, w = await asyncio.open_connection(host, int(port)); w.write(b"GET /stream.ts HTTP/1.1\r\n\r\n"); await w.drain()
    got = 0; t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < secs:
            c = await asyncio.wait_for(r.read(65536), secs)
            if not c: break
            got += len(c)
    except Exception: pass
    w.close(); return got, round(time.monotonic() - t0, 1)
async def main():
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH); ctx.load_cert_chain(f"{TEST_DIR}/cert.pem", f"{TEST_DIR}/key.pem")
    srv = await asyncio.start_server(relay, "127.0.0.1", 0, ssl=ctx); state["port"] = srv.sockets[0].getsockname()[1]
    live = immis.BlinkLiveViewServer(B(), Cam(), linger=1)
    await live.async_start()
    print("viewer 1:", await viewer(live.url, 15))
    await asyncio.sleep(0.5)
    for i in range(3):   # HA's player retrying
        print(f"reconnect {i+1}:", await viewer(live.url, 2))
    print("state:", live.stats.as_dict())
    lv = [c for c in calls if c[0] == "POST" and c[1].endswith("/liveview")]
    done = [c for c in calls if c[0].startswith("DONE") or (c[0] == "POST" and str(c[1]).endswith("/done"))]
    print(f"liveview commands sent: {len(lv)} (want 1) | TLS connections: {state['tls']} (want 1) | STOP sent: {state['stops']} (want 1) | command closed: {done}")
    assert len(lv) == 1 and state["tls"] == 1 and state["stops"] == 1, "retried or no STOP"
    # The app's endpoint answered, so the legacy one isn't needed.
    assert any(c[0] == "POST" for c in done) and not any(c[0] == "DONE-legacy" for c in done)
    assert live.stats.as_dict()["live_view_cooldown_s"] > 60
    await live.async_stop(); srv.close()
asyncio.run(main())
print("PASS failure")
