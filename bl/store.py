"""Shared state, paths, and typed accessors that every other module builds on."""

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import bpy
import bpy.utils.previews

from ..core import api, auth
from ..core.scheduler import Scheduler

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems


type Result = set[Literal[OperatorReturnItems]]  # what an operator method returns


ADDON = (__package__ or "").rpartition(".")[0]


HOME = Path.home() / ".quixlend"


PER_PAGE = 24


TYPES = [
    ("ALL", "All Types", ""),
    ("3d", "3D Assets", ""),
    ("3dplant", "3D Plants", ""),
    ("surface", "Surfaces", ""),
    ("atlas", "Atlases", ""),
    ("decal", "Decals", ""),  # Megascans keeps decals in the atlas type, category decals (see search.scope)
    ("brush", "Brushes", ""),
]


def is_material(hit):
    """Applied onto objects (surfaces only); atlases, decals included, are imported as planes or boxes instead."""
    return hit.get("type") == "surface"


session = auth.Session(HOME / "auth.json")


client = api.Client(session)


class _HasLayout(Protocol):
    @property
    def layout(self) -> bpy.types.UILayout | None: ...


def layout_of(ui: _HasLayout) -> bpy.types.UILayout:
    """`self.layout` is only None outside draw(); inside it, never."""
    assert ui.layout is not None
    return ui.layout


if TYPE_CHECKING:

    class QuixlendWindowManager(bpy.types.WindowManager):
        """Typed view of the properties register() adds to WindowManager."""

        quixlend_query: str
        quixlend_type: str
        quixlend_category: str
        quixlend_downloaded: bool
        quixlend_code: str


def wm_of(context: bpy.types.Context) -> "QuixlendWindowManager":
    return cast("QuixlendWindowManager", context.window_manager)


_icons: bpy.utils.previews.ImagePreviewCollection | None = None


_big: bpy.utils.previews.ImagePreviewCollection | None = None  # large previews for the details popup


def open_previews(icon_file: str):
    global _icons, _big
    _icons = bpy.utils.previews.new()
    _big = bpy.utils.previews.new()
    _icons.load("quixel", icon_file, "IMAGE")


def close_previews():
    global _icons, _big
    assert _icons is not None and _big is not None
    bpy.utils.previews.remove(_icons)
    bpy.utils.previews.remove(_big)
    _icons = _big = None


def icons() -> bpy.utils.previews.ImagePreviewCollection:
    assert _icons is not None, "addon is not registered"
    return _icons


def big() -> bpy.utils.previews.ImagePreviewCollection:
    assert _big is not None, "addon is not registered"
    return _big


class state:
    hits: list[dict[str, Any]] = []  # [{id, name, image}] image = thumbnail datablock (or None)
    back: str | None = None  # decal box the user left the manager for, until another object is active
    downloads_open = False  # the download card is showing
    panel_open = False  # set by the panel each time it draws; tells the grid overlay to draw
    downloads_seen = False  # the same, for the download card, which draws after the grid
    seq = 0  # id of the latest search
    page = 0
    pages = 0
    status = ""
    loading = False  # a search/page fetch is in flight
    owned: set[str] | None = None  # set of asset ids, None until fetched
    local: set[str] = set()  # ids already built into the library
    catalog: dict[str, dict[str, Any]] = {}  # id -> searchable record of a downloaded asset (bl/catalog.py)
    resolutions: dict[
        str, dict[int, int]
    ] = {}  # id -> {texture size in pixels: bytes on disk}, downloaded assets
    built: dict[str, set[int]] = {}  # id -> texture sizes that have a built .blend
    library_bytes: int | None = None  # disk used by downloaded assets, None until measured
    progress: dict[str, float] = {}  # id -> 0..1 while downloading
    speed: dict[
        str, tuple[float | None, float | None]
    ] = {}  # id -> (bytes/s, seconds left) while downloading; None parts until known
    transferred: dict[str, tuple[int, int]] = {}  # id -> (bytes done, bytes total) while downloading
    queue = Scheduler(4)  # kept in step with the preference; the rest wait in click order
    building: set[str] = set()  # ids past the network part, being built into the library
    cancel: dict[
        str, threading.Event
    ] = {}  # id -> threading.Event, present only while the network part can still be cancelled
    errors: dict[str, str] = {}  # id -> message
    sizes: dict[tuple[str, int], int] = {}  # (id, resolution) -> download bytes
    categories: dict[
        str, list[tuple[str, int]]
    ] = {}  # asset type -> [(category, count)], fetched once per type
    cancelling: set[str] = set()  # cancelled in the UI; the old worker is still stopping and cleaning up
    restart: set[str] = (
        set()
    )  # download clicked again while the old one was still stopping; starts when it has
    working: dict[str, str] = {}  # id -> "Applying"/"Importing" while place() blocks the main thread
    opening: str | None = None  # id of the card whose details are loading
    assets: dict[
        str, dict[str, Any] | None
    ] = {}  # id -> extended asset (None if the fetch failed), for size estimates


if TYPE_CHECKING:

    class Prefs(bpy.types.AddonPreferences):
        """Typed view of QuixlendPreferences, whose properties the stubs cannot see."""

        library: str
        resolution: str
        fetch_sizes: bool
        max_columns: int
        simultaneous_downloads: int
        asset_collections: bool
        anti_tiling: bool
        hover_preview: bool
        hover_preview_delay: float


def prefs() -> "Prefs":
    return cast("Prefs", bpy.context.preferences.addons[ADDON].preferences)


def library() -> Path:
    return Path(bpy.path.abspath(prefs().library))
