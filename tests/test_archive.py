"""Clip archiver against a fake Blink API: names, de-duplication, look-back, live-view pause."""
import asyncio, importlib.util, os, sys, tempfile, types, datetime as dt
ARCHIVE_PY = sys.argv[1]
calls = []
NOW = dt.datetime.now(dt.timezone.utc)
CLIPS = [
    {"id": 101, "camera_name": "FrontDoor", "created_at": (NOW - dt.timedelta(hours=2)).isoformat(), "size": 1},
    {"id": 102, "camera_name": "Backyard", "created_at": (NOW - dt.timedelta(hours=1)).isoformat(), "size": 1},
    {"id": 99, "camera_name": "FrontDoor", "created_at": (NOW - dt.timedelta(days=3)).isoformat(), "size": 1},
]
state = {"live": False, "start_live_after": None, "bad": set(), "bad_cloud": set()}
scheduled = []
CLOUD = []   # empty = no subscription (Blink returns an empty media list)

# ---- stubs: blinkpy.api + homeassistant bits ---------------------------------
api = types.ModuleType("blinkpy.api")
async def http_post(blink, url, **kw):
    calls.append(("POST", url.split("/api/v1/accounts/1")[-1]))
    if url.endswith("/manifest/request"):
        return {"id": 555, "network_id": 7}
    return {"id": 900, "network_id": 7}
async def http_get(blink, url, json=True, **kw):
    calls.append(("GET", url.split("/api/v1/accounts/1")[-1]))
    if "/media/changed" in url:
        page = int(url.rsplit("page=", 1)[1])
        return {"media": CLOUD if page == 1 else []}
    if url.endswith(".mp4"):
        if any(str(x) in url for x in state["bad_cloud"]):
            class BadC:
                status = 503
                async def read(self): return b""
            return BadC()
        class C:
            status = 200
            async def read(self): return b"CLOUD-" + url.encode()
        return C()
    if "/manifest/request/555" in url:
        return {"manifest_id": "M1", "clips": CLIPS}
    cid = int(url.rsplit("/", 1)[1])
    if cid in state["bad"]:
        class Bad:
            status = 500
            async def read(self): return b""
        return Bad()
    if state["start_live_after"] == cid:
        state["live"] = True
    class R:
        status = 200
        async def read(self): return b"MP4-" + str(cid).encode()
    return R()
async def wait_for_command(blink, resp): calls.append(("WAIT", resp.get("id"))); return True
api.http_post, api.http_get, api.wait_for_command = http_post, http_get, wait_for_command
bp = types.ModuleType("blinkpy"); bp.api = api; sys.modules["blinkpy"] = bp; sys.modules["blinkpy.api"] = api

ha = types.ModuleType("homeassistant"); core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
helpers = types.ModuleType("homeassistant.helpers"); storage = types.ModuleType("homeassistant.helpers.storage")
_DB = {}
class Store:
    def __init__(self, hass, v, key): self.key = key
    def __class_getitem__(cls, item): return cls
    async def async_load(self): return _DB.get(self.key)
    async def async_save(self, data): _DB[self.key] = data
storage.Store = Store
event = types.ModuleType("homeassistant.helpers.event")
def async_call_later(hass, delay, fn):
    scheduled.append(delay)
    return lambda: None
event.async_call_later = async_call_later
util = types.ModuleType("homeassistant.util"); dtu = types.ModuleType("homeassistant.util.dt")
dtu.UTC = dt.timezone.utc
dtu.utcnow = lambda: dt.datetime.now(dt.timezone.utc)
dtu.parse_datetime = lambda s: dt.datetime.fromisoformat(s)
dtu.as_local = lambda d: d.astimezone(dt.timezone(dt.timedelta(hours=-6)))
util.dt = dtu
for n, m in {"homeassistant": ha, "homeassistant.core": core, "homeassistant.helpers": helpers,
             "homeassistant.helpers.storage": storage, "homeassistant.helpers.event": event, "homeassistant.util": util, "homeassistant.util.dt": dtu}.items():
    sys.modules[n] = m
spec = importlib.util.spec_from_file_location("archive", ARCHIVE_PY)
archive = importlib.util.module_from_spec(spec); sys.modules["archive"] = archive; spec.loader.exec_module(archive)
archive.PAUSE_BETWEEN_CLIPS = 0
_real_sleep = asyncio.sleep
async def _fast_sleep(t, *a, **k): await _real_sleep(0)
archive.asyncio.sleep = _fast_sleep   # no real waiting in retries

class Bus:
    def __init__(self): self.events = []
    def async_fire(self, e, d): self.events.append((e, d))
class Hass:
    def __init__(self): self.bus = Bus()
    async def async_add_executor_job(self, fn, *a): return fn(*a)
class Srv:
    @property
    def active(self): return state["live"]
class Sync:
    name = "Home"; sync_id = 42; network_id = 7; local_storage = True
    _names_table = {"FrontDoor": "Front Door", "Backyard": "Backyard"}
class Owl:  # Mini without USB storage: must be ignored
    name = "Mini"; sync_id = None; network_id = 8; local_storage = False
class U: base_url = "https://rest-u1.immedia-semi.com"
class Blink: account_id = 1; urls = U(); sync = {"Home": Sync(), "Mini": Owl()}
class Coord: api = Blink(); live_servers = {"a": Srv()}

async def main():
    folder = tempfile.mkdtemp()
    hass = Hass()
    a = archive.ClipArchiver(hass, Coord(), "entry1", folder); await a.async_load()

    # existing 0.3.0 install: old single memory list must NOT block the copy
    _DB["blink_plus.entry1.archive"] = {"saved": {"42": [101, 102, 99]}, "saved_total": 3}
    a = archive.ClipArchiver(hass, Coord(), "entry1", folder); await a.async_load()
    r1 = await a.async_run(hours=24)   # first run into this folder -> everything, look-back ignored
    files = sorted(os.path.relpath(os.path.join(dp, f), folder) for dp, _, fs in os.walk(folder) for f in fs)
    print("run1 (first full copy):", r1, files)
    assert r1["saved"] == 3 and "first full copy" in r1["result"], r1
    assert len(files) == 3 and all(f.endswith(".mp4") for f in files), files
    assert any(f.startswith("Front Door/") for f in files) and any(f.startswith("Backyard/") for f in files)
    assert any("99" in open(os.path.join(folder, f), "rb").read().decode() for f in files), "old clip not copied on first run"
    assert [e[1]["camera"] for e in hass.bus.events] == ["Front Door", "Front Door", "Backyard"], "not oldest-first"

    n_before = len(calls)
    r2 = await a.async_run(hours=24)
    clip_posts = [c for c in calls[n_before:] if c[0] == "POST" and "/clip/request/" in c[1]]
    print("run2:", r2, "clip uploads requested:", len(clip_posts))
    assert r2["saved"] == 0 and not clip_posts, "re-downloaded already saved clips"

    # remembered across restarts (Store)
    b = archive.ClipArchiver(hass, Coord(), "entry1", folder); await b.async_load()
    r3 = await b.async_run(hours=0)
    print("run3 (after restart, nothing new):", r3)
    assert r3["saved"] == 0 and "first full copy" not in r3["result"]

    state["live"] = True
    r4 = await b.async_run(hours=0); print("live view running:", r4)
    assert "USB skipped" in r4["result"] and r4["saved"] == 0
    state["live"] = False

    CLIPS.extend([{"id": 201, "camera_name": "Backyard", "created_at": NOW.isoformat(), "size": 1},
                  {"id": 202, "camera_name": "Backyard", "created_at": (NOW + dt.timedelta(seconds=5)).isoformat(), "size": 1}])
    state["start_live_after"] = 201
    r5 = await b.async_run(hours=24); print("live view starts mid-run:", r5)
    assert r5["result"].startswith("paused") and r5["saved"] == 1
    print("status:", {k: b.status[k] for k in ("saved_total", "last_clip_camera", "last_result")})
    assert b.status["saved_total"] == 4
    state["start_live_after"] = None

    # ---- with a subscription: cloud clips (+ USB). Cloud also works during live view.
    CLOUD.extend([
        {"id": 7001, "created_at": (NOW - dt.timedelta(minutes=30)).isoformat(), "device_name": "Driveway",
         "deleted": False, "media": "/api/v2/accounts/1/media/clip/7001.mp4"},
        {"id": 7002, "created_at": (NOW - dt.timedelta(minutes=20)).isoformat(), "device_name": "Driveway",
         "deleted": True, "media": "/api/v2/accounts/1/media/clip/7002.mp4"},
        {"id": 7003, "created_at": (NOW - dt.timedelta(minutes=10)).isoformat(), "device_name": "Front Door",
         "deleted": False, "media": "/api/v2/accounts/1/media/thumb/7003.jpg"},
    ])
    state["live"] = True
    r6 = await b.async_run(hours=24); print("subscribed, live view running:", r6)
    assert r6["saved"] == 1, "cloud clip not saved (or deleted/jpg copied)"
    assert "USB skipped" in r6["result"]
    state["live"] = False
    r7 = await b.async_run(hours=24); print("subscribed, idle:", r7)
    assert r7["saved"] == 1 and "cloud: 0 new" in r7["result"], "USB leftover not copied / cloud re-copied"
    files = sorted(os.path.relpath(os.path.join(dp, f), folder) for dp, _, fs in os.walk(folder) for f in fs)
    print("all files:", files)
    assert any(f.startswith("Driveway/") for f in files)
    assert not any(f.endswith(".part") for f in files)

    # ---- user points the archive at a NEW folder -> starts fresh, copies everything
    folder2 = tempfile.mkdtemp()
    c = archive.ClipArchiver(hass, Coord(), "entry1", folder2); await c.async_load()
    r8 = await c.async_run(hours=24); print("new folder:", r8)
    n_usb = len(CLIPS); assert r8["saved"] == n_usb + 1, f"expected {n_usb} USB + 1 cloud, got {r8}"
    r9 = await c.async_run(hours=24); assert r9["saved"] == 0
    # old folder's memory is untouched
    d = archive.ClipArchiver(hass, Coord(), "entry1", folder); await d.async_load()
    assert (await d.async_run(hours=24))["saved"] == 0, "old folder re-copied"
    # ---- reset -> copies everything again into the same folder
    await c.async_reset()
    r10 = await c.async_run(hours=24); print("after reset:", r10)
    assert r10["saved"] == n_usb + 1 and "first full copy" in r10["result"]

    # ---- a clip that keeps failing must not end the run; others still copied
    folder3 = tempfile.mkdtemp()
    e = archive.ClipArchiver(hass, Coord(), "entry1", folder3); await e.async_load()
    state["bad"] = {102}
    manifests_before = sum(1 for c in calls if c[0] == "POST" and c[1].endswith("/manifest/request"))
    r11 = await e.async_run(hours=24); print("one bad clip:", r11, "| last_error:", e.status["last_error"])
    assert r11["saved"] == n_usb, "a single failing clip stopped the run"
    assert "clip 102" in e.status["last_error"] and "HTTP 500" in e.status["last_error"]
    # ---- 5 failing in a row -> run stops with a clear error and schedules a resume
    CLIPS.extend({"id": 300 + i, "camera_name": "Backyard", "created_at": (NOW + dt.timedelta(minutes=i + 1)).isoformat(), "size": 1} for i in range(6))
    state["bad"] = {300, 301, 302, 303, 304, 305}
    scheduled.clear()
    r12 = await e.async_run(hours=24); print("5 in a row fail:", r12, "| scheduled resume in", scheduled)
    assert r12["result"] == "error" and scheduled == [archive.RESUME_AFTER_ERROR]
    manifests_after = sum(1 for c in calls if c[0] == "POST" and c[1].endswith("/manifest/request"))
    assert manifests_after - manifests_before >= 3, "clip list not refreshed after 2 failures"
    # ---- paused by live view -> resume scheduled automatically
    state["bad"] = set(); CLIPS.append({"id": 400, "camera_name": "Backyard", "created_at": (NOW + dt.timedelta(hours=1)).isoformat(), "size": 1})
    CLIPS.append({"id": 401, "camera_name": "Backyard", "created_at": (NOW + dt.timedelta(hours=2)).isoformat(), "size": 1})
    state["start_live_after"] = 300; scheduled.clear()
    r13 = await e.async_run(hours=24); print("paused:", r13, "| scheduled resume in", scheduled)
    assert r13["result"].startswith("paused") and scheduled == [archive.RESUME_AFTER_PAUSE]
    state["live"] = False; state["start_live_after"] = None
    r14 = await e.async_run(hours=24); print("resumed:", r14)
    assert r14["saved"] >= 2
    # ---- a clip that always fails is skipped after MAX_CLIP_ATTEMPTS runs, so the
    #      first full copy can complete and it no longer blocks newer clips
    folder4 = tempfile.mkdtemp()
    g = archive.ClipArchiver(hass, Coord(), "entry4", folder4); await g.async_load()
    state["bad"] = {99}; state["bad_cloud"] = {7001}
    for n in range(1, archive.MAX_CLIP_ATTEMPTS + 1):
        rr = await g.async_run(hours=24); print(f"always-bad run {n}:", rr)
        if n < archive.MAX_CLIP_ATTEMPTS:
            assert "to retry" in rr["result"] and not g._initial_done, rr
    assert g._initial_done, "first full copy never completed because of one bad clip"
    assert g.status["skipped_clips"] == 2 and "skipped after" in rr["result"], rr
    before = len(calls)
    rr = await g.async_run(hours=24)
    assert not any(c[0] == "POST" and c[1].endswith("/clip/request/99") for c in calls[before:]), "skipped clip retried"
    assert "cloud clip 7001" not in (g.status["last_error"] or ""), "skipped cloud clip retried"
    # remembered across restarts
    g2 = archive.ClipArchiver(hass, Coord(), "entry4", folder4); await g2.async_load()
    assert g2.status["skipped_clips"] == 2
    # reset forgets skipped clips too
    await g2.async_reset(); assert g2.status["skipped_clips"] == 0 and not g2._failures
    state["bad"] = set(); state["bad_cloud"] = set()

    # ---- a failing cloud clip counts as failed (full copy not marked complete)
    folder5 = tempfile.mkdtemp()
    h = archive.ClipArchiver(hass, Coord(), "entry5", folder5); await h.async_load()
    state["bad_cloud"] = {7001}
    rr = await h.async_run(hours=24); print("bad cloud clip:", rr, h.status["last_error"])
    assert not h._initial_done and "1 clip(s) to retry" in rr["result"]
    assert "cloud clip 7001" in h.status["last_error"] and "HTTP 503" in h.status["last_error"]
    state["bad_cloud"] = set()

    # ---- unload: no new run, no resume
    scheduled.clear()
    h.shutdown()
    rr = await h.async_run(hours=24); print("after shutdown:", rr)
    assert rr["result"] == "disabled" and scheduled == []

    # ---- no clip source at all: an error, but nothing to retry automatically
    class EmptyBlink: account_id = 1; urls = U(); sync = {}
    class EmptyCoord: api = EmptyBlink(); live_servers = {}
    saved_cloud = list(CLOUD); CLOUD.clear(); scheduled.clear()
    k = archive.ClipArchiver(hass, EmptyCoord(), "entry6", tempfile.mkdtemp()); await k.async_load()
    rr = await k.async_run(hours=24); print("no source:", rr, k.status["last_error"])
    assert rr["result"] == "error" and "no clips source" in k.status["last_error"] and scheduled == []
    CLOUD.extend(saved_cloud)
    print("PASS archive")

asyncio.run(main())
