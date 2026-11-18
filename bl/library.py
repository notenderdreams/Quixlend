"""The Megascans library on disk: what is downloaded, what is owned, and the Blender asset library."""

import json
from pathlib import Path

import bpy
import bpy.utils.previews

from ..core import download
from . import jobs
from .store import HOME, client, library, prefs, state


def scan_local():
    state.local = {p.stem for p in (library() / "Quixlend").glob("*/*/*.blend") if p.stem == p.parent.name}
    measure_library()


def _scan(root):
    """(bytes on disk, {id: {texture size: bytes}}, {id: size its .blend uses}) for every asset folder."""
    total, resolutions, built = 0, {}, {}
    for folder in root.glob("*/*"):
        found = {}
        for f in (f for f in folder.rglob("*") if f.is_file()):
            size = f.stat().st_size
            total += size
            for res in download.resolutions_of([f]):
                found[res] = found.get(res, 0) + size
        if not found:
            continue
        resolutions[folder.name] = found
        try:
            built[folder.name] = json.loads((folder / "_built.json").read_text())["res"] or max(found)
        except (OSError, ValueError, KeyError):
            built[folder.name] = max(found)  # built before the size was recorded: one size was kept back then
    return total, resolutions, built


def measure_library():
    """Refresh disk use and which texture sizes each downloaded asset has, off the main thread."""
    root = library() / "Quixlend"

    def done(result, error):
        if error:
            state.library_bytes = None
            return
        state.library_bytes, state.resolutions, state.built = result

    jobs.run(lambda: _scan(root), done)


def have_resolution(asset_id, res):
    """Record that `res` is on disk and in use, without waiting for the next scan."""
    state.resolutions.setdefault(asset_id, {}).setdefault(res, 0)  # bytes are filled in by the next scan
    state.built[asset_id] = res


def has_selected(asset_id):
    """The chosen texture size is on disk (or not scanned yet, in which case assume it is)."""
    res = int(prefs().resolution)
    return res in state.resolutions.get(asset_id, {res})


def built_at_selected(asset_id):
    """The asset's .blend already uses the chosen texture size."""
    res = int(prefs().resolution)
    return state.built.get(asset_id, res) == res


def ensure_asset_library():
    """Register the Megascans folder as a Blender asset library (once)."""
    libs = bpy.context.preferences.filepaths.asset_libraries
    root = library()
    if not any(Path(lib.path) == root for lib in libs):
        root.mkdir(parents=True, exist_ok=True)
        libs.new(name="Megascans", directory=str(root))
    from .build import TYPE_LABEL, catalog_id

    for asset_type in TYPE_LABEL:  # one catalog per type, so every section shows before its first download
        catalog_id(root, asset_type)


def refresh_asset_browsers():
    """Reload asset lists in every open Asset Browser."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.ui_type != "ASSETS":
                continue
            region = next(r for r in area.regions if r.type == "WINDOW")
            try:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.asset.library_refresh()
            except RuntimeError:
                pass  # that editor cannot refresh right now


def load_owned():
    """Owned-asset ids: instant from the disk cache, then refreshed once in the background."""
    cache = HOME / "owned.json"
    if cache.exists():
        state.owned = set(json.loads(cache.read_text()))

    def work():
        owned = sorted({a["assetID"] for a in client.acquired()})
        cache.write_text(json.dumps(owned))
        return owned

    def done(owned, error):
        if not error:
            state.owned = set(owned)

    jobs.run(work, done)
