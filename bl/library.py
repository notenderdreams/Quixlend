"""The Megascans library on disk: what is downloaded, what is owned, and the Blender asset library."""

import json
from pathlib import Path

import bpy
import bpy.utils.previews

from . import jobs
from .store import HOME, client, library, state


def scan_local():
    state.local = {p.stem for p in (library() / "Quixlend").glob("*/*/*.blend") if p.stem == p.parent.name}
    measure_library()


def measure_library():
    root = library() / "Quixlend"

    def done(size, error):
        state.library_bytes = size if not error else None

    jobs.run(lambda: sum(f.stat().st_size for f in root.rglob("*") if f.is_file()), done)


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
