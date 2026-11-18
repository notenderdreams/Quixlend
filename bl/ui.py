"""Add-on registration and startup."""

from pathlib import Path
from typing import cast

import bpy
import bpy.utils.previews

from . import catalog, search, store
from .account import QUIXLEND_OT_login, QUIXLEND_OT_login_open, QUIXLEND_OT_logout
from .details import QUIXLEND_OT_delete_resolution, QUIXLEND_OT_details
from .library import ensure_asset_library, load_owned, scan_local
from .placement import QUIXLEND_OT_copy_group
from .search import QUIXLEND_OT_search, load_categories, start_search
from .settings import QUIXLEND_OT_clean, QUIXLEND_PT_settings, QuixlendPreferences
from .store import icons, layout_of, session, state, wm_of
from .transfer import QUIXLEND_OT_download

CLASSES = (
    QuixlendPreferences,
    QUIXLEND_OT_search,
    QUIXLEND_OT_download,
    QUIXLEND_OT_details,
    QUIXLEND_OT_delete_resolution,
    QUIXLEND_OT_login_open,
    QUIXLEND_OT_login,
    QUIXLEND_OT_logout,
    QUIXLEND_OT_copy_group,
    QUIXLEND_OT_clean,
    QUIXLEND_PT_settings,
)


def _startup():
    ensure_asset_library()
    catalog.load()
    scan_local()  # once here and after each download/delete, not on every search
    if session.logged_in:
        load_owned()
        load_categories(wm_of(bpy.context).quixlend_type)
        start_search()


@bpy.app.handlers.persistent
def _file_loaded(*_):
    """Opening a file frees the thumbnail images; drop the stale cards and search again."""
    state.hits = []
    if session.logged_in:
        start_search()


def _asset_browser_header(self: bpy.types.Menu, context: bpy.types.Context):
    space = cast(bpy.types.SpaceFileBrowser, context.space_data)
    params = space.params
    ref = getattr(params, "asset_library_reference", None) or getattr(params, "asset_library_ref", "")
    if ref == "Megascans":
        layout_of(self).label(text="Megascans", icon_value=icons()["quixel"].icon_id)


def register():
    store.open_previews(str(Path(__file__).parents[1] / "assets" / "quixel.png"))
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    search.register_properties()
    bpy.app.handlers.load_post.append(_file_loaded)
    bpy.types.ASSETBROWSER_MT_editor_menus.append(_asset_browser_header)  # pyright: ignore[reportArgumentType]
    bpy.app.timers.register(_startup, first_interval=0.5, persistent=True)


def unregister():
    bpy.app.handlers.load_post.remove(_file_loaded)
    bpy.types.ASSETBROWSER_MT_editor_menus.remove(_asset_browser_header)  # pyright: ignore[reportArgumentType]
    search.unregister_properties()
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    store.close_previews()
    for hit in state.hits:
        if img := bpy.data.images.get(f".quixlend_{hit['id']}"):
            bpy.data.images.remove(img)
    state.hits = []
