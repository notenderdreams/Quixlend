"""The Megascans library on disk: what is downloaded, what is owned, and the Blender asset library."""

import json
from pathlib import Path

import bpy
import bpy.utils.previews

from ..core import download
from . import build, catalog, jobs
from .store import HOME, client, library, prefs, state


def scan_local():
    state.local = {
        p.parent.name
        for p in (library() / "Quixlend").glob("*/*/*.blend")
        if p.stem == p.parent.name or download.blend_resolution(p.name, p.parent.name)
    }
    measure_library()


def _scan(root):
    """(bytes on disk, {id: {texture size: bytes}}, {id: sizes that have a built .blend}) for every asset folder."""
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
        built[folder.name] = {
            res for p in folder.glob("*.blend") if (res := download.blend_resolution(p.name, folder.name))
        }
    return total, resolutions, built


def measure_library():
    """Refresh disk use and which texture sizes each downloaded asset has, off the main thread."""
    root = library() / "Quixlend"
    catalog.refresh()  # the offline search index follows the same disk changes

    def done(result, error):
        if error:
            state.library_bytes = None
            return
        state.library_bytes, state.resolutions, state.built = result

    jobs.run(lambda: _scan(root), done)


def have_resolution(asset_id, res):
    """Record that `res` is on disk and built, without waiting for the next scan."""
    state.resolutions.setdefault(asset_id, {}).setdefault(res, 0)  # bytes are filled in by the next scan
    state.built.setdefault(asset_id, set()).add(res)


def has_selected(asset_id):
    """The chosen texture size is on disk (or not scanned yet, in which case assume it is)."""
    res = int(prefs().resolution)
    return res in state.resolutions.get(asset_id, {res})


def built_at_selected(asset_id):
    """A .blend for the chosen texture size exists (or the asset is not scanned yet: assume so)."""
    res = int(prefs().resolution)
    return res in state.built.get(asset_id, {res})


def stale_decal(hit):
    """A decal whose .blend predates the current builder (or decals-as-boxes: it has no object, only a material)."""
    if not hit.get("decal"):
        return False
    path = next(
        (library() / "Quixlend").glob(f"*/{hit['id']}/*_{int(prefs().resolution) // 1024}K.blend"), None
    )
    if path is None:
        return False
    if path.stat().st_mtime < Path(build.__file__).stat().st_mtime:
        return True
    with bpy.data.libraries.load(str(path), assets_only=True) as (src, _):  # pyright: ignore[reportGeneralTypeIssues]
        return not src.objects


def usable_resolution(asset_id) -> int | None:
    """Resolution whose .blend to load: the chosen one if built, else the largest built; None if unknown or none."""
    res = int(prefs().resolution)
    built = state.built.get(asset_id)
    if built is None or res in built:
        return res
    return max(built, default=None)


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
