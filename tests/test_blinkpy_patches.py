"""blinkpy load reductions: patched for Blink Plus, untouched for the built-in
integration, and harmless when blinkpy's internals have changed."""
import asyncio, importlib.util, sys, types

PATCHES_PY = sys.argv[1]


def load_patches():
    spec = importlib.util.spec_from_file_location("blinkpy_patches", PATCHES_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_blinkpy(full=True):
    for n in [n for n in sys.modules if n == "blinkpy" or n.startswith("blinkpy.")]:
        del sys.modules[n]
    calls = []
    sm = types.ModuleType("blinkpy.sync_module")
    cam = types.ModuleType("blinkpy.camera")

    class LocalStorageMediaItem:
        async def prepare_download(self, blink, *a, **k):
            calls.append("prepare_download"); return "orig"

    class BlinkSyncModule:
        def __init__(self, blink):
            self.blink = blink; self.network_id = 7; self._local_storage = {}
        async def update_local_storage_manifest(self, *a, **k):
            calls.append("manifest"); return "orig"
        async def get_network_info(self, *a, **k):
            calls.append("network_info"); return "orig"

    class BlinkCamera:
        def __init__(self, sync):
            self.sync = sync; self.temperature = 21
        async def get_sensor_info(self, *a, **k):
            calls.append("signals"); return "orig"

    sm.LocalStorageMediaItem = LocalStorageMediaItem
    sm.BlinkSyncModule = BlinkSyncModule
    if full:
        cam.BlinkCamera = BlinkCamera
    else:  # a future blinkpy: one class renamed, one method gone
        del BlinkSyncModule.get_network_info
        cam.BlinkCameraV2 = BlinkCamera
    bp = types.ModuleType("blinkpy")
    sys.modules.update({"blinkpy": bp, "blinkpy.sync_module": sm, "blinkpy.camera": cam})
    return sm, cam, calls, BlinkCamera


class Ours:
    blink_plus_usb_poll_minutes = 60
    blink_plus_light_local_storage = True
    homescreen = {"networks": [{"id": 7, "name": "Home", "armed": True}]}


class BuiltIn:
    homescreen = {"networks": [{"id": 7, "name": "Home", "armed": True}]}


async def main():
    # ---- current blinkpy: all four patched, gated to our instance
    sm, cam, calls, Cam = fake_blinkpy(full=True)
    p = load_patches()
    applied = p.apply_blinkpy_patches()
    print("applied:", applied)
    assert all(applied.values())
    assert p.apply_blinkpy_patches() is applied  # only once

    ours, builtin = sm.BlinkSyncModule(Ours()), sm.BlinkSyncModule(BuiltIn())
    item = sm.LocalStorageMediaItem()
    assert await item.prepare_download(Ours()) is None
    assert await item.prepare_download(BuiltIn()) == "orig"
    assert await ours.update_local_storage_manifest() == "orig"      # first read allowed
    assert await ours.update_local_storage_manifest() is None        # throttled
    assert ours._local_storage["manifest_stale"] is True
    assert await builtin.update_local_storage_manifest() == "orig"
    assert await builtin.update_local_storage_manifest() == "orig"
    assert await ours.get_network_info() is True and ours.network_info["network"]["armed"] is True
    assert await builtin.get_network_info() == "orig"
    c_ours, c_builtin = Cam(ours), Cam(builtin)
    assert await c_ours.get_sensor_info() is None and c_ours.temperature_calibrated == 21
    assert await c_builtin.get_sensor_info() == "orig"
    print("calls to blinkpy originals:", calls)
    assert calls == ["prepare_download", "manifest", "manifest", "manifest", "network_info", "signals"]

    # internals changed at runtime: no crash, just no flag
    ours._local_storage = None
    assert await ours.update_local_storage_manifest() is None

    # ---- future blinkpy: missing class / method -> skipped, the rest still patched
    sm, cam, calls, Cam = fake_blinkpy(full=False)
    p = load_patches()
    applied = p.apply_blinkpy_patches()
    print("applied with changed blinkpy:", applied)
    assert applied == {"prepare_download": True, "local_storage_manifest": True,
                       "network_info": False, "sensor_info": False}
    assert await sm.LocalStorageMediaItem().prepare_download(Ours()) is None
    print("PASS blinkpy patches")


asyncio.run(main())
