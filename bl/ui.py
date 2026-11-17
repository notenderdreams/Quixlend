"""Preferences, operators and the 3D Viewport sidebar panel."""

import json
import shutil
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, EnumProperty, StringProperty

from ..core import api, auth, download, http
from ..core.scheduler import Scheduler
from . import jobs

if TYPE_CHECKING:
    from bpy.stub_internal.rna_enums import OperatorReturnItems

type Result = set[Literal[OperatorReturnItems]]  # what an operator method returns

ADDON = (__package__ or "").rpartition(".")[0]
HOME = Path.home() / ".quixlend"
BUILD_SCRIPT = str(Path(__file__).with_name("build.py"))
PER_PAGE = 24
_icons: bpy.utils.previews.ImagePreviewCollection | None = None
_big: bpy.utils.previews.ImagePreviewCollection | None = None  # large previews for the details popup
TYPES = [
    ("ALL", "All Types", ""),
    ("3d", "3D Assets", ""),
    ("3dplant", "3D Plants", ""),
    ("surface", "Surfaces", ""),
    ("atlas", "Atlases", ""),
    ("brush", "Brushes", ""),
]

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
        quixlend_code: str


def wm_of(context: bpy.types.Context) -> "QuixlendWindowManager":
    return cast("QuixlendWindowManager", context.window_manager)


def icons() -> bpy.utils.previews.ImagePreviewCollection:
    assert _icons is not None, "addon is not registered"
    return _icons


def big() -> bpy.utils.previews.ImagePreviewCollection:
    assert _big is not None, "addon is not registered"
    return _big


class state:
    hits: list[dict[str, Any]] = []  # [{id, name, image}] image = thumbnail datablock (or None)
    panel_open = False  # set by the panel each time it draws; tells the grid overlay to draw
    seq = 0  # id of the latest search
    page = 0
    pages = 0
    status = ""
    loading = False  # a search/page fetch is in flight
    owned: set[str] | None = None  # set of asset ids, None until fetched
    local: set[str] = set()  # ids already built into the library
    library_bytes: int | None = None  # disk used by downloaded assets, None until measured
    progress: dict[str, float] = {}  # id -> 0..1 while downloading
    speed: dict[
        str, tuple[float | None, float | None]
    ] = {}  # id -> (bytes/s, seconds left) while downloading; None parts until known
    queue = Scheduler(2)  # at most two assets download at once; the rest wait in click order
    building: set[str] = set()  # ids past the network part, being built into the library
    cancel: dict[
        str, threading.Event
    ] = {}  # id -> threading.Event, present only while the network part can still be cancelled
    errors: dict[str, str] = {}  # id -> message
    sizes: dict[tuple[str, int], int] = {}  # (id, resolution) -> download bytes
    cancelling: set[str] = set()  # cancelled in the UI; the old worker is still stopping and cleaning up
    restart: set[str] = (
        set()
    )  # download clicked again while the old one was still stopping; starts when it has
    working: dict[str, str] = {}  # id -> "Applying"/"Importing" while place() blocks the main thread
    opening: str | None = None  # id of the card whose details are loading
    assets: dict[
        str, dict[str, Any] | None
    ] = {}  # id -> extended asset (None if the fetch failed), for size estimates


_pool = ThreadPoolExecutor(4)


def download_size(asset_id, fetch=True):
    """Bytes the Download button would fetch at the chosen resolution; None until the asset listing arrives."""
    if fetch and prefs().fetch_sizes and asset_id not in state.assets:
        state.assets[asset_id] = None

        def fetch_asset():
            try:
                state.assets[asset_id] = client.asset(asset_id)
            except Exception:
                return
            jobs.dirty.set()

        _pool.submit(fetch_asset)
    asset = state.assets.get(asset_id)
    if not asset:
        return None
    key = (asset_id, int(prefs().resolution))
    if key not in state.sizes:
        state.sizes[key] = api.payload_size(asset, api.build_payload(asset, key[1]))
    return state.sizes[key]


if TYPE_CHECKING:

    class Prefs(bpy.types.AddonPreferences):
        """Typed view of QuixlendPreferences, whose properties the stubs cannot see."""

        library: str
        resolution: str
        fetch_sizes: bool


def prefs() -> "Prefs":
    return cast("Prefs", bpy.context.preferences.addons[ADDON].preferences)


def library() -> Path:
    return Path(bpy.path.abspath(prefs().library))


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


# ---- search ----------------------------------------------------------------


def big_preview(hit):
    """Path of the large preview image, downloaded once."""
    path = HOME / "thumbs" / f"{hit['id']}_big.jpg"
    if not path.exists():
        req = urllib.request.Request(hit["big"], headers={"User-Agent": http.UA})
        with urllib.request.urlopen(req, timeout=30) as res:
            path.write_bytes(res.read())
    return path


def _thumb(hit):
    path = HOME / "thumbs" / f"{hit['id']}.jpg"
    if not path.exists():
        images = hit["previews"]["images"]
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        req = urllib.request.Request(
            images.get("thumbJpg") or images["thumb"], headers={"User-Agent": http.UA}
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            part.write_bytes(res.read())
        part.replace(path)
    return path


def res_label() -> str:
    items = cast(bpy.types.EnumProperty, prefs().bl_rna.properties["resolution"]).enum_items
    return items[prefs().resolution].name


def draw_res_menu(self: bpy.types.Menu, context: bpy.types.Context):
    layout_of(self).prop(prefs(), "resolution", expand=True)


def thumb_image(hit):
    """Thumbnail datablock; undo/redo invalidates references, so look up by name and reload if gone."""
    name = f".quixlend_{hit['id']}"  # leading dot hides it from image lists
    image = bpy.data.images.get(name)
    if image is None and hit.get("path") and Path(hit["path"]).exists():
        image = bpy.data.images.load(hit["path"])
        image.name = name
        image.use_fake_user = True
    return image


def start_search(page=0, append=False):
    """Fetch a result page. Results of a search superseded by a newer one are dropped."""
    wm = wm_of(bpy.context)
    state.seq += 1
    seq = state.seq
    query, asset_type = wm.quixlend_query, wm.quixlend_type
    state.status = "Searching…"
    state.loading = True

    def work():
        def attempt():
            res = api.search(query, page, PER_PAGE, None if asset_type == "ALL" else asset_type)
            with ThreadPoolExecutor(8) as pool:
                thumbs = list(pool.map(_thumb, res["hits"]))
            return res, thumbs

        def retrying(n):
            state.status = f"Connection dropped, retrying ({n}/2)…"
            jobs.dirty.set()

        return http.retry(attempt, on_retry=retrying, give_up=lambda: seq != state.seq)

    def done(result, error):
        if seq != state.seq:
            return
        state.loading = False
        if error:
            state.status = f"Search failed: {error}"[:120]
            return
        res, thumbs = result
        if not append:
            for old in state.hits:
                if img := bpy.data.images.get(f".quixlend_{old['id']}"):
                    bpy.data.images.remove(img)
            state.hits = []
        for hit, path in zip(res["hits"], thumbs):
            state.hits.append(
                {
                    "id": hit["id"],
                    "name": hit["name"],
                    "type": hit.get("type"),
                    "big": hit["previews"]["images"].get("preview") or hit["previews"]["images"]["thumb"],
                    "path": str(path),
                }
            )
        state.page, state.pages = page, res["nbPages"]
        state.status = f"{res['nbHits']} assets" if state.hits else "No results"
        scan_local()

    jobs.run(work, done)


class QUIXLEND_OT_search(bpy.types.Operator):
    bl_idname = "quixlend.search"
    bl_label = "Search Megascans"
    bl_description = "Search the Megascans catalog"
    more: BoolProperty(default=False, description="Append the next page instead of starting over")  # pyright: ignore[reportInvalidTypeForm]

    def execute(self, context: bpy.types.Context) -> Result:
        if self.more:
            start_search(state.page + 1, append=True)
        else:
            start_search()
        return {"FINISHED"}


# ---- download --------------------------------------------------------------


class QUIXLEND_OT_download(bpy.types.Operator):
    bl_idname = "quixlend.download"
    bl_label = "Download"
    bl_description = "Download this asset and add it to the Asset Browser"
    asset_id: StringProperty()  # pyright: ignore[reportInvalidTypeForm]
    replace: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        default=False, description="Download again at the chosen resolution, replacing the textures"
    )

    def execute(self, context: bpy.types.Context) -> Result:
        asset_id, root = self.asset_id, library()
        replacing = self.replace and asset_id in state.local
        if asset_id in state.progress or asset_id in state.restart:
            return {"FINISHED"}  # already queued, downloading or building
        if asset_id in state.cancelling:
            state.restart.add(asset_id)  # the old worker still owns the files: start once it is gone
            jobs.redraw()
            return {"FINISHED"}
        resolution, blender = int(prefs().resolution), bpy.app.binary_path
        state.progress[asset_id] = 0.0
        state.errors.pop(asset_id, None)
        if replacing:
            state.local.discard(
                asset_id
            )  # the card shows progress; the old build stays on disk until the new one works
        stop = state.cancel[asset_id] = threading.Event()

        def progress(done_bytes, total):
            if asset_id in state.cancelling:
                return  # cancelled in the UI; the dying workers must not bring the bar back
            state.progress[asset_id] = done_bytes / max(
                total, 1
            )  # the network part only; building is its own state
            jobs.dirty.set()

        def fetch():
            asset = client.asset(asset_id)
            payload = api.build_payload(asset, resolution)
            dest = download.asset_dir(root, asset)
            plan = lambda: download.plan(client.request_download(payload), dest)
            files = download.fetch_all(
                plan(),
                progress,
                refresh=plan,
                cancel=stop,
                speed=lambda bps, eta: (
                    asset_id in state.cancelling or state.speed.__setitem__(asset_id, (bps, eta))
                ),
            )
            return dest, download.resolution_of(files)

        def build(dest, res):
            thumb = HOME / "thumbs" / f"{asset_id}.jpg"  # Quixel's own catalog thumbnail
            if thumb.exists():
                shutil.copyfile(thumb, dest / "_Thumb.jpg")
            proc = subprocess.run(
                [
                    blender,
                    "-b",
                    "--factory-startup",
                    "--python",
                    BUILD_SCRIPT,
                    "--",
                    str(dest),
                    str(root),
                    *([str(res)] if res else []),
                ],
                capture_output=True,
                text=True,
            )
            if "QUIXLEND_BUILT" not in proc.stdout:
                raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[-1])
            if res:  # the new build no longer needs the textures of the resolution it replaces
                for old in download.stale_maps([p for p in dest.rglob("*") if p.is_file()], res):
                    old.unlink(missing_ok=True)

        def settle(error):
            state.progress.pop(asset_id, None)
            state.speed.pop(asset_id, None)
            state.cancelling.discard(asset_id)
            try:
                if error and replacing:
                    state.local.add(asset_id)  # the old build is still intact
                if isinstance(error, download.Cancelled):
                    return  # not a failure: no error on the card
                if error:
                    state.errors[asset_id] = download.reason(error)
                    return
                state.local.add(asset_id)
                if replacing:
                    reload_templates(asset_id)
                measure_library()
                refresh_asset_browsers()
            finally:
                if asset_id in state.restart:  # clicked Download again while this was stopping
                    state.restart.discard(asset_id)
                    # one tick later: the old job releases its scheduler slot right after settle()
                    bpy.app.timers.register(
                        lambda: bpy.ops.quixlend.download(asset_id=asset_id) and None,  # pyright: ignore[reportAttributeAccessIssue]
                        first_interval=0,
                    )

        def run(finished):
            def fetched(result, error):
                try:
                    state.cancel.pop(asset_id, None)  # nothing left to cancel
                    if not error and stop.is_set():
                        error = download.Cancelled()  # cancelled after the last byte arrived
                    if error:
                        settle(error)
                        return
                    state.building.add(asset_id)
                    jobs.run(lambda: build(*result), built)
                finally:
                    finished()  # network part over: the next queued download starts while this one builds

            def built(_, error):
                state.building.discard(asset_id)
                settle(error)

            try:
                jobs.run(fetch, fetched)
            except BaseException as e:  # never leave the card stuck, never break the job that started us
                state.cancel.pop(asset_id, None)
                settle(e)
                finished()

        state.queue.add(asset_id, run)
        jobs.redraw()  # show Queued/progress now, not on the next mouse move
        return {"FINISHED"}


def cancel_download(asset_id):
    """Cancel at once as far as the UI goes; a running download stops and cleans up in the background."""
    event = state.cancel.get(asset_id)
    if event:
        event.set()
    if state.queue.cancel(asset_id):  # it had not started: nothing will report back, so clean up here
        state.cancel.pop(asset_id, None)
    elif event:
        state.cancelling.add(asset_id)  # settle() clears it when the workers are gone
        state.cancel.pop(asset_id, None)
        state.speed.pop(asset_id, None)
    state.progress.pop(asset_id, None)
    jobs.redraw()


def local_resolution(asset_id) -> int | None:
    """Texture size in pixels of a downloaded asset, read from its file names."""
    folder = next((library() / "Quixlend").glob(f"*/{asset_id}"), None)
    return download.resolution_of(p for p in folder.rglob("*") if p.is_file()) if folder else None


def load_templates(asset_id):
    """(objects, materials) of a downloaded asset, loaded once and reused; (None, None) if the file is missing."""
    blend = next((library() / "Quixlend").glob(f"*/{asset_id}/{asset_id}.blend"), None)
    if not blend:
        return None, None
    # Reuse datablocks from an earlier placement instead of appending duplicates.
    objects = [o for o in bpy.data.objects if o.get("quixlend_id") == asset_id and o.get("quixlend_source")]
    materials = [m for m in bpy.data.materials if m.get("quixlend_id") == asset_id]
    if not objects and not materials:
        with bpy.data.libraries.load(str(blend), assets_only=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]
            dst.objects, dst.materials = src.objects, src.materials
        objects, materials = dst.objects, dst.materials
        for block in objects + materials:
            block.asset_clear()
            block["quixlend_id"] = asset_id
        for obj in objects:
            obj["quixlend_source"] = True  # pristine template, never linked to a scene
            obj.use_fake_user = True
    return objects, materials


def reload_templates(asset_id):
    """After a rebuild: swap datablocks loaded from the old .blend for the new ones; objects keep their material."""
    old_m = sorted((m for m in bpy.data.materials if m.get("quixlend_id") == asset_id), key=lambda m: m.name)
    old_o = [o for o in bpy.data.objects if o.get("quixlend_id") == asset_id and o.get("quixlend_source")]
    if not (old_m or old_o):
        return  # never loaded this session: the next use loads the new file
    names = [m.name for m in old_m]
    groups = {
        cast(bpy.types.ShaderNodeGroup, n).node_tree
        for m in old_m
        if m.node_tree
        for n in m.node_tree.nodes
        if n.bl_idname == "ShaderNodeGroup"
    }
    groups = {g for g in groups if g}
    images = {
        cast(bpy.types.ShaderNodeTexImage, n).image
        for g in groups
        for n in g.nodes
        if n.bl_idname == "ShaderNodeTexImage"
    }
    for block in (*old_m, *old_o):
        del block["quixlend_id"]  # or load_templates would find them instead of loading the new file
    _, loaded = load_templates(asset_id)
    new_m = sorted(loaded or [], key=lambda m: m.name)
    for old, new in zip(old_m, new_m):
        old.user_remap(new)
    for old in old_m:
        bpy.data.materials.remove(old)
    for obj in old_o:
        bpy.data.objects.remove(obj)
    for new, name in zip(new_m, names):
        new.name = name
    for group in groups:
        if group.users - group.use_fake_user == 0:  # the old build's node group; the new material has its own
            bpy.data.node_groups.remove(group)
    for image in images:
        if image and image.users - image.use_fake_user == 0:
            bpy.data.images.remove(image)


def place(context, asset_id, location=None, target=None, pick=None):
    """Put a downloaded asset in the scene. Meshes go to `location` (default: 3D cursor);
    materials go on `target` (default: selected objects). Returns a warning message or None.
    `pick` chooses one of several plant variations; without it all are laid out in a row."""
    objects, materials = load_templates(asset_id)
    if objects is None:
        return "Asset file is missing; download it again"

    if objects:
        sources = sorted(objects, key=lambda o: o.name)
        if pick and len(sources) > 1:
            sources = [pick(sources)]
        bpy.ops.object.select_all(action="DESELECT")
        base, x = (location or context.scene.cursor.location).copy(), 0.0
        collection = bpy.data.collections.get("Megascans")
        if collection is None:
            collection = bpy.data.collections.new("Megascans")
        if collection not in context.scene.collection.children_recursive:
            context.scene.collection.children.link(collection)
        for source in sources:
            obj = cast(bpy.types.Object, source.copy())
            del obj["quixlend_source"]
            collection.objects.link(obj)
            obj.location = base
            obj.location.x += x
            xs = [corner[0] for corner in source.bound_box]  # dimensions are 0 on unlinked objects
            x += (max(xs) - min(xs)) * 1.2
            obj.select_set(True)
            context.view_layer.objects.active = obj
    elif materials:
        targets = (
            [target.original]
            if target
            else [o for o in context.selected_objects if hasattr(o.data, "materials")]
        )
        if not targets:
            return "Drop the material onto an object (or select one first)"
        for obj in targets:
            if obj.material_slots:
                obj.material_slots[obj.active_material_index].material = materials[0]
            else:
                obj.data.materials.append(materials[0])
    else:
        return "Brush asset: pick it from the brush shelf in Sculpt Mode"
    return None


# ---- login -----------------------------------------------------------------


def open_details(asset_id, window, area):
    """Fetch preview + asset details off the main thread (card shows a loader meanwhile), then pop up the details."""
    if state.opening:
        return
    state.opening = asset_id
    jobs.redraw()  # spinner now
    hit = next(h for h in state.hits if h["id"] == asset_id)

    def work():
        path = big_preview(hit)
        return path, state.assets.get(asset_id) or client.asset(asset_id)

    def done(result, error):
        state.opening = None
        if not error:
            path, state.assets[asset_id] = result
            if not big().get(asset_id):
                big().load(asset_id, str(path), "IMAGE")
        region = next(r for r in area.regions if r.type == "WINDOW")
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.quixlend.details("INVOKE_DEFAULT", asset_id=asset_id)  # pyright: ignore[reportAttributeAccessIssue]

    jobs.run(work, done)


class QUIXLEND_OT_details(bpy.types.Operator):
    bl_idname = "quixlend.details"
    bl_label = "Asset Details"
    bl_options = {"INTERNAL"}
    asset_id: StringProperty()  # pyright: ignore[reportInvalidTypeForm]

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> Result:
        self.hit = next(h for h in state.hits if h["id"] == self.asset_id)
        preview = big().get(self.asset_id)
        self.icon = preview.icon_id if preview else 0
        asset = state.assets.get(self.asset_id)
        self.info = api.asset_info(asset) if asset else [("Details unavailable", "")]
        self.size = download_size(self.asset_id, fetch=False)
        self.have = local_resolution(self.asset_id) if self.asset_id in state.local else None
        return context.window_manager.invoke_popup(self, width=420)

    def draw(self, context: bpy.types.Context):
        layout = layout_of(self)
        if self.icon:
            layout.template_icon(icon_value=self.icon, scale=14)
        layout.label(text=self.hit["name"])
        col = layout.column(align=True)
        rows = list(self.info)
        if self.size:
            rows.insert(0, ("Download size", f"{self.size / 1e6:,.0f} MB at {res_label()}"))
        for key, value in rows:
            row = col.split(factor=0.3)
            row.label(text=key)
            row.label(text=value)
        if self.have:
            row = layout.row()
            row.label(text=f"Downloaded at {self.have // 1024}K")
            again = row.row()
            again.enabled = self.have != int(prefs().resolution)
            op = again.operator(
                "quixlend.download", text=f"Re-download at {res_label()}", icon="FILE_REFRESH"
            )
            assert op is not None
            op.asset_id, op.replace = self.asset_id, True  # pyright: ignore[reportAttributeAccessIssue]

    def execute(self, context: bpy.types.Context) -> Result:
        return {"FINISHED"}


class QUIXLEND_OT_login_open(bpy.types.Operator):
    bl_idname = "quixlend.login_open"
    bl_label = "Open Epic Login"
    bl_description = "Open the Epic Games sign-in page in your browser"

    def execute(self, context: bpy.types.Context) -> Result:
        bpy.ops.wm.url_open(url=auth.LOGIN_URL)
        return {"FINISHED"}


class QUIXLEND_OT_login(bpy.types.Operator):
    bl_idname = "quixlend.login"
    bl_label = "Log In"
    bl_description = "Finish signing in with the pasted authorization code"

    def execute(self, context: bpy.types.Context) -> Result:
        wm = wm_of(context)
        code = wm.quixlend_code
        state.status = "Logging in…"

        def done(_, error):
            if error:
                state.status = f"Login failed: {error}"[:160]
                return
            wm.quixlend_code = ""
            load_owned()
            start_search()

        jobs.run(lambda: session.login_with_code(code), done)
        return {"FINISHED"}


class QUIXLEND_OT_logout(bpy.types.Operator):
    bl_idname = "quixlend.logout"
    bl_label = "Log Out"
    bl_description = "Forget the stored Quixel session"

    def execute(self, context: bpy.types.Context) -> Result:
        session.logout()
        (HOME / "owned.json").unlink(missing_ok=True)
        state.owned, state.status = None, ""
        return {"FINISHED"}


class QUIXLEND_OT_copy_group(bpy.types.Operator):
    bl_idname = "quixlend.copy_group"
    bl_label = "Copy Node Group"
    bl_description = "Copy this material's node group; paste it (Ctrl+V) into any shader editor"
    asset_id: StringProperty()  # pyright: ignore[reportInvalidTypeForm]

    def execute(self, context: bpy.types.Context) -> Result:
        _, materials = load_templates(self.asset_id)
        tree = materials[0].node_tree if materials else None
        group = next((n for n in tree.nodes if n.bl_idname == "ShaderNodeGroup"), None) if tree else None
        window = context.window
        if not (tree and group and window):
            self.report({"WARNING"}, "Nothing to copy; download the asset again")
            return {"CANCELLED"}
        for n in tree.nodes:
            n.select = n == group
        # node.clipboard_copy only runs inside a node editor, so one is borrowed for a moment. Never a 3D viewport:
        # changing its type tears down its render session and Cycles would restart.
        areas = [a for a in window.screen.areas if a.type != "VIEW_3D"]
        area = next((a for a in areas if a.type == "NODE_EDITOR"), None) or min(
            areas, key=lambda a: a.width * a.height, default=context.area
        )
        if area is None:
            self.report({"WARNING"}, "No editor to borrow for copying")
            return {"CANCELLED"}
        old = area.type
        area.type = "NODE_EDITOR"
        space = cast(bpy.types.SpaceNodeEditor, area.spaces.active)
        before = (
            space.tree_type,
            space.pin,
            space.node_tree,
        )  # an editor that was already open keeps its state
        try:
            space.tree_type = "ShaderNodeTree"
            space.pin = True
            space.node_tree = tree
            region = next(r for r in area.regions if r.type == "WINDOW")
            with context.temp_override(window=window, area=area, region=region, space_data=space):
                bpy.ops.node.clipboard_copy()
        finally:
            space.pin, space.node_tree = False, None
            space.tree_type = before[0]
            if before[1]:
                space.pin, space.node_tree = before[1], before[2]
            area.type = old
        self.report({"INFO"}, "Copied node group; paste with Ctrl+V in a shader editor")
        return {"FINISHED"}


class QUIXLEND_OT_clean(bpy.types.Operator):
    bl_idname = "quixlend.clean"
    bl_label = "Delete Downloaded Assets"
    bl_description = "Delete every downloaded asset from the library folder"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return not (state.progress or state.building)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> Result:
        return context.window_manager.invoke_confirm(
            self,
            event,
            title="Delete all downloaded assets?",
            message=f"{len(state.local)} assets ({_size(state.library_bytes)}) will be removed from disk. "
            "This cannot be undone.",
            confirm_text="Delete",
        )

    def execute(self, context: bpy.types.Context) -> Result:
        root = library() / "Quixlend"

        def work():
            for folder in root.glob("*/*"):
                if folder.is_dir():
                    shutil.rmtree(folder, ignore_errors=True)

        def done(_, error):
            scan_local()
            refresh_asset_browsers()

        jobs.run(work, done)
        return {"FINISHED"}


def _size(n):
    if n is None:
        return "measuring…"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---- ui --------------------------------------------------------------------


class QuixlendPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON
    library: StringProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Library",
        subtype="DIR_PATH",
        default=str(Path.home() / "Documents" / "Megascans Library"),
        description="Folder downloads are stored in; registered as an asset library",
    )
    resolution: EnumProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Texture Resolution",
        default="4096",
        items=[("1024", "1K", ""), ("2048", "2K", ""), ("4096", "4K", ""), ("8192", "8K", "")],
    )
    fetch_sizes: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Fetch Download Sizes",
        default=False,
        description="Show each card's download size while browsing. This sends one request per asset as it scrolls into view. "
        "Off: sizes appear only for assets whose details you open",
    )

    def draw(self, context: bpy.types.Context):
        layout = layout_of(self)
        layout.prop(self, "library")
        layout.prop(self, "resolution")
        layout.prop(self, "fetch_sizes")


class QUIXLEND_PT_settings(bpy.types.Panel):
    bl_label = "Quixlend Settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Quixlend"
    bl_options = {"INSTANCED"}
    bl_ui_units_x = 22

    def draw(self, context: bpy.types.Context):
        layout = layout_of(self)
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.label(text="Asset Library", icon="ASSET_MANAGER")
        layout.prop(prefs(), "library")
        layout.prop(prefs(), "resolution")
        layout.prop(prefs(), "fetch_sizes")
        layout.separator()
        layout.label(text="Downloaded", icon="DISK_DRIVE")
        col = layout.column(align=True)
        col.label(text=f"Assets: {len(state.local)}")
        col.label(text=f"Size on disk: {_size(state.library_bytes)}")
        busy = bool(state.progress or state.building)
        row = layout.row()
        row.enabled = bool(state.local) and not busy
        row.operator("quixlend.clean", text="Delete All Downloads", icon="TRASH")
        if busy:
            layout.label(text="Wait for active downloads to finish", icon="INFO")
        if session.logged_in:
            layout.separator()
            layout.label(text=session.email or "Epic Games account", icon="USER")
            layout.operator("quixlend.logout", text="Sign Out", icon="QUIT")


CLASSES = (
    QuixlendPreferences,
    QUIXLEND_OT_search,
    QUIXLEND_OT_download,
    QUIXLEND_OT_details,
    QUIXLEND_OT_login_open,
    QUIXLEND_OT_login,
    QUIXLEND_OT_logout,
    QUIXLEND_OT_copy_group,
    QUIXLEND_OT_clean,
    QUIXLEND_PT_settings,
)


def _startup():
    ensure_asset_library()
    if session.logged_in:
        load_owned()
        start_search()


@bpy.app.handlers.persistent
def _file_loaded(*_):
    """Opening a file frees the thumbnail images; drop the stale cards and search again."""
    state.hits = []
    if session.logged_in:
        start_search()


def _search_update(self, context):
    start_search()


def _asset_browser_header(self: bpy.types.Menu, context: bpy.types.Context):
    space = cast(bpy.types.SpaceFileBrowser, context.space_data)
    params = space.params
    ref = getattr(params, "asset_library_reference", None) or getattr(params, "asset_library_ref", "")
    if ref == "Megascans":
        layout_of(self).label(text="Megascans", icon_value=icons()["quixel"].icon_id)


def register():
    global _icons, _big
    _icons = bpy.utils.previews.new()
    _big = bpy.utils.previews.new()
    icons().load("quixel", str(Path(__file__).parents[1] / "assets" / "quixel.png"), "IMAGE")
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    wm.quixlend_query = StringProperty(name="Search", update=_search_update, options={"SKIP_SAVE"})  # pyright: ignore[reportAttributeAccessIssue]
    wm.quixlend_type = EnumProperty(name="Type", items=TYPES, update=_search_update)  # pyright: ignore[reportAttributeAccessIssue]
    wm.quixlend_code = StringProperty(name="Authorization Code", options={"SKIP_SAVE"})  # pyright: ignore[reportAttributeAccessIssue]
    bpy.app.handlers.load_post.append(_file_loaded)
    bpy.types.ASSETBROWSER_MT_editor_menus.append(_asset_browser_header)  # pyright: ignore[reportArgumentType]
    bpy.app.timers.register(_startup, first_interval=0.5, persistent=True)


def unregister():
    global _icons, _big
    bpy.app.handlers.load_post.remove(_file_loaded)
    bpy.types.ASSETBROWSER_MT_editor_menus.remove(_asset_browser_header)  # pyright: ignore[reportArgumentType]
    wm = bpy.types.WindowManager
    del wm.quixlend_query, wm.quixlend_type, wm.quixlend_code  # pyright: ignore[reportAttributeAccessIssue]
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    bpy.utils.previews.remove(_icons)
    bpy.utils.previews.remove(_big)
    _icons = _big = None
    for hit in state.hits:
        if img := bpy.data.images.get(f".quixlend_{hit['id']}"):
            bpy.data.images.remove(img)
    state.hits = []
