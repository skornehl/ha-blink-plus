"""Deleting archived USB clips against a fake Blink API.

A clip may only be deleted when it is provably archived (in the archive
memory, file present, plausible size). Covers: not archived (copy failed with
HTTP 404 / unknown clip), file missing, file too small, too young, delete
returns non-200 / no answer, live view active / starting mid-run, dry run,
per-run cap, clips saved before 0.7.0 (no recorded path), memory across
restarts and the automatic delete after an archive run.

    python3 test_usb_delete.py ../custom_components/blink_plus/archive.py
"""
import asyncio, importlib.util, os, sys, tempfile, types, datetime as dt
ARCHIVE_PY = sys.argv[1]
calls = []
NOW = dt.datetime.now(dt.timezone.utc)


def clip(cid, camera, days):
    return {"id": cid, "camera_name": camera, "created_at": (NOW - dt.timedelta(days=days)).isoformat(), "size": 1}


CLIPS = []
state = {
    "live": False,
    "bad": set(),            # clip download -> HTTP 404
    "tiny": set(),           # clip download -> a few bytes only
    "delete_status": {},     # clip id -> HTTP status of the delete (default 200)
    "delete_none": set(),    # clip id -> blinkpy returns None (no answer)
    "live_after_delete": None,  # a live view starts right after deleting this clip
}

# ---- stubs: blinkpy.api + homeassistant bits ---------------------------------
api = types.ModuleType("blinkpy.api")


class Resp:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body
    async def read(self): return self._body
    async def text(self): return self._body.decode(errors="replace")


async def http_post(blink, url, **kw):
    path = url.split("/api/v1/accounts/1")[-1]
    calls.append(("POST", path))
    if url.endswith("/manifest/request"):
        return {"id": 555, "network_id": 7}
    if "/clip/delete/" in url:
        assert kw.get("json") is False, "delete must not expect JSON (blinkpy: json=False)"
        cid = int(url.rsplit("/", 1)[1])
        if cid in state["delete_none"]:
            return None
        if state["live_after_delete"] == cid:
            state["live"] = True
        status = state["delete_status"].get(cid, 200)
        return Resp(status, b'{"message":"ok"}' if status == 200 else b'{"message":"nope"}')
    return {"id": 900, "network_id": 7}


async def http_get(blink, url, json=True, **kw):
    calls.append(("GET", url.split("/api/v1/accounts/1")[-1]))
    if "/media/changed" in url:
        return {"media": []}  # no subscription
    if "/manifest/request/555" in url:
        return {"manifest_id": "M1", "clips": list(CLIPS)}
    cid = int(url.rsplit("/", 1)[1])
    if cid in state["bad"]:
        return Resp(404)
    if cid in state["tiny"]:
        return Resp(200, b"<html>error</html>")
    return Resp(200, b"\x00\x00\x00\x18ftypmp42" + bytes(archive.MIN_ARCHIVED_BYTES + 1000))


async def wait_for_command(blink, resp):
    calls.append(("WAIT", resp.get("id")))
    return True

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
    async def async_save(self, data):
        import json
        _DB[self.key] = json.loads(json.dumps(data))  # like HA: JSON round trip

storage.Store = Store
event = types.ModuleType("homeassistant.helpers.event")
event.async_call_later = lambda hass, delay, fn: (lambda: None)
util = types.ModuleType("homeassistant.util"); dtu = types.ModuleType("homeassistant.util.dt")
dtu.UTC = dt.timezone.utc
dtu.utcnow = lambda: dt.datetime.now(dt.timezone.utc)
dtu.parse_datetime = lambda s: dt.datetime.fromisoformat(s)
dtu.as_local = lambda d: d.astimezone(dt.timezone(dt.timedelta(hours=-6)))
util.dt = dtu
for n, m in {"homeassistant": ha, "homeassistant.core": core, "homeassistant.helpers": helpers,
             "homeassistant.helpers.storage": storage, "homeassistant.helpers.event": event,
             "homeassistant.util": util, "homeassistant.util.dt": dtu}.items():
    sys.modules[n] = m
spec = importlib.util.spec_from_file_location("archive", ARCHIVE_PY)
archive = importlib.util.module_from_spec(spec); sys.modules["archive"] = archive; spec.loader.exec_module(archive)
archive.PAUSE_BETWEEN_CLIPS = 0
sleeps = []
_real_sleep = asyncio.sleep


async def _fast_sleep(t, *a, **k):
    sleeps.append(t)
    await _real_sleep(0)

archive.asyncio.sleep = _fast_sleep   # no real waiting in retries


class Bus:
    def async_fire(self, e, d): pass


class Hass:
    def __init__(self): self.bus = Bus()
    async def async_add_executor_job(self, fn, *a): return fn(*a)


class Srv:
    @property
    def active(self): return state["live"]


class Sync:
    name = "Home"; sync_id = 42; network_id = 7; local_storage = True
    _names_table = {"FrontDoor": "Front Door", "Backyard": "Backyard"}


class U: base_url = "https://rest-u1.immedia-semi.com"
class Blink: account_id = 1; urls = U(); sync = {"Home": Sync()}
class Coord: api = Blink(); live_servers = {"a": Srv()}


def deletes(since=0):
    return [int(c[1].rsplit("/", 1)[1]) for c in calls[since:] if c[0] == "POST" and "/clip/delete/" in c[1]]


def manifest_requests(since=0):
    return sum(1 for c in calls[since:] if c[0] == "POST" and c[1].endswith("/manifest/request"))


def ids(items):
    return [i["clip_id"] for i in items]


def reasons(report):
    return {i["clip_id"]: i["reason"] for i in report["skipped"]}


async def main():
    hass = Hass()
    folder = tempfile.mkdtemp()
    CLIPS[:] = [
        clip(1001, "FrontDoor", 40),   # archived -> deleted (happy path)
        clip(1002, "Backyard", 35),    # archived -> deleted
        clip(1003, "FrontDoor", 45),   # copy fails with HTTP 404 -> never deleted
        clip(1004, "Backyard", 38),    # archived, file removed later -> kept
        clip(1005, "FrontDoor", 33),   # "archived" but only an error page -> kept
        clip(1006, "Backyard", 2),     # archived, but too young -> kept, not even listed
        clip(1007, "FrontDoor", 31),   # archived, delete answers HTTP 500 -> error, kept
    ]
    state["bad"] = {1003}; state["tiny"] = {1005}
    a = archive.ClipArchiver(hass, Coord(), "entry1", folder); await a.async_load()
    r = await a.async_run(hours=24); print("archive run:", r)
    assert r["saved"] == 6 and "usb_delete" not in r, "delete must be off by default"
    assert not deletes(), "something was deleted with the option off"
    # The archiver remembers where each USB clip went (relative to the folder).
    rel = a._paths["42"]["1004"]
    assert not os.path.isabs(rel) and rel.startswith("Backyard/"), rel
    os.remove(os.path.join(folder, rel))  # file missing
    CLIPS.append(clip(1008, "Backyard", 50))  # never copied: unknown to the archive memory

    # ---- dry run: deletes nothing, reports what it would do
    n = len(calls)
    d = await a.async_delete_usb(30, dry_run=True); print("dry run:", d["result"])
    assert d["dry_run"] is True and not deletes(n), "dry run deleted something"
    assert ids(d["deleted"]) == [1001, 1002, 1007], d["deleted"]  # oldest first
    assert all(i["path"].startswith(folder) and i["camera"] for i in d["deleted"])
    why = reasons(d)
    print("  skipped:", why)
    assert set(why) == {1003, 1004, 1005, 1008}, why
    assert "not archived" in why[1003] and "copy failed 1 time" in why[1003]   # HTTP 404 copy
    assert "missing" in why[1004]
    assert "too small" in why[1005] and str(archive.MIN_ARCHIVED_BYTES) in why[1005]
    assert "not in the archive memory" in why[1008]
    assert 1006 not in why and 1006 not in ids(d["deleted"]), "too young clip listed"
    assert a.status["deleted_total"] == 0 and a.status["deleted_last_run"] == 0
    assert a.status["state"] == "idle"

    # ---- live view active: nothing starts (not even the clip list request)
    state["live"] = True
    n = len(calls)
    d = await a.async_delete_usb(30); print("live view active:", d["result"])
    assert d["result"] == "skipped: live view running" and calls[n:] == [], calls[n:]
    state["live"] = False

    # ---- per-run cap: one clip, the rest next run
    n = len(calls)
    d = await a.async_delete_usb(30, max_clips=1); print("cap 1:", d["result"])
    assert deletes(n) == [1001] and ids(d["deleted"]) == [1001] and d["remaining"] == 2, d
    assert a.status["deleted_total"] == 1 and a.status["deleted_last_run"] == 1
    assert a.status["last_delete_error"] is None
    assert "1001" not in a._paths["42"], "path of a deleted clip kept"
    url = [c[1] for c in calls[n:] if c[0] == "POST" and "/clip/delete/" in c[1]][0]
    assert url == "/networks/7/sync_modules/42/local_storage/manifest/M1/clip/delete/1001", url

    # ---- clip saved before 0.7.0 (no recorded path): the archiver's file name is used
    a._paths["42"].pop("1002")
    # ---- delete answers HTTP 500: error, retried, not recorded, nothing else breaks
    state["delete_status"] = {1007: 500}
    n = len(calls); sleeps.clear()
    d = await a.async_delete_usb(30); print("non-200:", d["result"], d["errors"])
    assert deletes(n) == [1002] + [1007] * archive.DELETE_RETRIES, deletes(n)
    assert ids(d["deleted"]) == [1002] and ids(d["errors"]) == [1007]
    assert d["errors"][0]["error"] == "delete HTTP 500"
    assert a.status["deleted_total"] == 2 and a.status["deleted_last_run"] == 1
    assert "clip 1007" in a.status["last_delete_error"] and "HTTP 500" in a.status["last_delete_error"]
    assert 1001 not in deletes(n), "deleted clip tried again"
    assert [3, 6] == [s for s in sleeps if s in (3, 6)][:2], f"no backoff between retries: {sleeps}"
    # no answer at all (blinkpy returns None) is an error too, never a success
    state["delete_status"] = {}; state["delete_none"] = {1007}
    d = await a.async_delete_usb(30); print("no answer:", d["errors"])
    assert d["errors"][0]["error"] == "no response from Blink" and not d["deleted"]
    assert 1007 not in a._deleted.get("42", [])
    state["delete_none"] = set()

    # ---- remembered across restarts: deleted clips are never tried again
    #      (the fake Blink still lists them), counters survive
    b = archive.ClipArchiver(hass, Coord(), "entry1", folder); await b.async_load()
    assert b.status["deleted_total"] == 2 and b._deleted["42"] == [1001, 1002]
    n = len(calls)
    d = await b.async_delete_usb(30); print("after restart:", d["result"])
    assert deletes(n) == [1007] and ids(d["deleted"]) == [1007] and b.status["deleted_total"] == 3

    # ---- live view starts mid-run: stop cleanly after the current clip
    CLIPS.extend([clip(1101, "FrontDoor", 60), clip(1102, "FrontDoor", 59), clip(1103, "Backyard", 58)])
    r = await b.async_run(hours=0); assert r["saved"] == 4, r   # + 1008, still missing so far
    state["live_after_delete"] = 1101
    n = len(calls)
    d = await b.async_delete_usb(30); print("live view mid-run:", d["result"])
    assert d["result"] == "paused: a live view started" and deletes(n) == [1101], deletes(n)
    assert b.status["deleted_total"] == 4 and b.status["state"] == "idle"
    assert _DB["blink_plus.entry1.archive"]["folders"][folder]["deleted_total"] == 4, "progress not saved"
    state["live"] = False; state["live_after_delete"] = None

    # ---- a clip whose copy keeps failing (404) is never deleted, also once skipped for good
    for _ in range(archive.MAX_CLIP_ATTEMPTS):
        await b.async_run(hours=0)
    d = await b.async_delete_usb(30, dry_run=True)
    assert "skipped for good" in reasons(d)[1003], reasons(d)
    assert 1003 not in ids(d["deleted"])

    # ---- reset keeps what was deleted from the stick
    await b.async_reset()
    assert b.status["deleted_total"] == 4
    assert b._deleted["42"] and not b._paths
    d = await b.async_delete_usb(30, dry_run=True)
    assert not d["deleted"], "clips deleted although the archive memory was reset"

    # ---- the option: delete automatically after a complete archive run
    folder2 = tempfile.mkdtemp()
    CLIPS[:] = [clip(2001, "FrontDoor", 40), clip(2002, "Backyard", 1)]
    state["bad"] = set(); state["tiny"] = set()
    c = archive.ClipArchiver(hass, Coord(), "entry2", folder2); await c.async_load()
    c.delete_after_days = 30
    n = len(calls)
    r = await c.async_run(hours=24); print("archive + auto delete:", r)
    assert r["saved"] == 2 and r["deleted"] == 1 and deletes(n) == [2001], (r, deletes(n))
    assert manifest_requests(n) == 1, "delete should reuse the clip list of the run"
    assert c.status["deleted_last_run"] == 1 and c.status["deleted_total"] == 1
    # the archive copy stays
    assert sorted(os.listdir(os.path.join(folder2, "Front Door"))), "archive file gone"
    # a run paused by a live view does not delete
    CLIPS.extend([clip(2003, "FrontDoor", 50), clip(2004, "FrontDoor", 49)])
    state["live"] = True
    n = len(calls)
    r = await c.async_run(hours=0); print("live view, auto delete:", r)
    assert "usb_delete" not in r or r["usb_delete"] == "skipped: live view running", r
    assert not deletes(n)
    state["live"] = False
    r = await c.async_run(hours=0); print("next run:", r)
    assert r["deleted"] == 2 and c.status["deleted_total"] == 3

    # ---- bad input / disabled
    d = await c.async_delete_usb(0); assert d["result"].startswith("error"), d
    c.shutdown()
    d = await c.async_delete_usb(30); assert d["result"] == "disabled", d
    print("PASS usb delete")

asyncio.run(main())
