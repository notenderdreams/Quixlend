"""Preferences, operators and the 3D Viewport sidebar panel."""
import json
import math
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, EnumProperty, StringProperty

from ..core import api, auth, download, http
from . import jobs

ADDON = __package__.rpartition(".")[0]
HOME = Path.home() / ".quixlend"  # shared with cli.py: one login for both
BUILD_SCRIPT = str(Path(__file__).with_name("build.py"))
PER_PAGE = 24
_icons = None
TYPES = [("ALL", "All Types", ""), ("3d", "3D Assets", ""), ("3dplant", "3D Plants", ""),
         ("surface", "Surfaces", ""), ("atlas", "Atlases", ""), ("brush", "Brushes", "")]

session = auth.Session(HOME / "auth.json")
client = api.Client(session)


class state:
    hits = []       # [{id, name, image}] image = thumbnail datablock (or None)
    panel_open = False  # set by the panel each time it draws; tells the grid overlay to draw
    seq = 0         # id of the latest search
    page = pages = 0
    status = ""
    loading = False  # a search/page fetch is in flight
    owned = None    # set of asset ids, None until fetched
    local = set()   # ids already built into the library
    progress = {}   # id -> 0..1 while downloading
    errors = {}     # id -> message


def prefs():
    return bpy.context.preferences.addons[ADDON].preferences


def library() -> Path:
    return Path(bpy.path.abspath(prefs().library))


def scan_local():
    state.local = {p.stem for p in (library() / "Quixlend").glob("*/*/*.blend") if p.stem == p.parent.name}


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

def _thumb(hit):
    path = HOME / "thumbs" / f"{hit['id']}.jpg"
    if not path.exists():
        images = hit["previews"]["images"]
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        req = urllib.request.Request(images.get("thumbJpg") or images["thumb"], headers={"User-Agent": http.UA})
        with urllib.request.urlopen(req, timeout=30) as res:
            part.write_bytes(res.read())
        part.replace(path)
    return path


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
    wm = bpy.context.window_manager
    state.seq += 1
    seq = state.seq
    query, asset_type = wm.quixlend_query, wm.quixlend_type
    state.status = "Searching…"
    state.loading = True

    def work():
        res = api.search(query, page, PER_PAGE, None if asset_type == "ALL" else asset_type)
        with ThreadPoolExecutor(8) as pool:
            thumbs = list(pool.map(_thumb, res["hits"]))
        return res, thumbs

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
            state.hits.append({"id": hit["id"], "name": hit["name"], "path": str(path)})
        state.page, state.pages = page, res["nbPages"]
        state.status = f"{res['nbHits']} assets" if state.hits else "No results"
        scan_local()

    jobs.run(work, done)


class QUIXLEND_OT_search(bpy.types.Operator):
    bl_idname = "quixlend.search"
    bl_label = "Search Megascans"
    bl_description = "Search the Megascans catalog"
    more: BoolProperty(default=False, description="Append the next page instead of starting over")

    def execute(self, context):
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
    asset_id: StringProperty()

    def execute(self, context):
        asset_id, root = self.asset_id, library()
        resolution, blender = int(prefs().resolution), bpy.app.binary_path
        state.progress[asset_id] = 0.0
        state.errors.pop(asset_id, None)

        def progress(done_bytes, total):
            state.progress[asset_id] = 0.9 * done_bytes / max(total, 1)
            jobs.dirty.set()

        def work():
            asset = client.asset(asset_id)
            res = client.request_download(api.build_payload(asset, resolution))
            dest = download.asset_dir(root, asset)
            download.fetch_all(download.plan(res, dest), progress)
            thumb = HOME / "thumbs" / f"{asset_id}.jpg"  # Quixel's own catalog thumbnail
            if thumb.exists():
                shutil.copyfile(thumb, dest / "_Thumb.jpg")
            proc = subprocess.run([blender, "-b", "--factory-startup", "--python", BUILD_SCRIPT,
                                   "--", str(dest), str(root)], capture_output=True, text=True)
            if "QUIXLEND_BUILT" not in proc.stdout:
                raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[-1])

        def done(_, error):
            state.progress.pop(asset_id, None)
            if error:
                state.errors[asset_id] = str(error)
                return
            state.local.add(asset_id)
            refresh_asset_browsers()

        jobs.run(work, done)
        return {"FINISHED"}


def load_templates(asset_id):
    """(objects, materials) of a downloaded asset, loaded once and reused; (None, None) if the file is missing."""
    blend = next((library() / "Quixlend").glob(f"*/{asset_id}/{asset_id}.blend"), None)
    if not blend:
        return None, None
    # Reuse datablocks from an earlier placement instead of appending duplicates.
    objects = [o for o in bpy.data.objects if o.get("quixlend_id") == asset_id and o.get("quixlend_source")]
    materials = [m for m in bpy.data.materials if m.get("quixlend_id") == asset_id]
    if not objects and not materials:
        with bpy.data.libraries.load(str(blend), assets_only=True) as (src, dst):
            dst.objects, dst.materials = src.objects, src.materials
        objects, materials = dst.objects, dst.materials
        for block in objects + materials:
            block.asset_clear()
            block["quixlend_id"] = asset_id
        for obj in objects:
            obj["quixlend_source"] = True  # pristine template, never linked to a scene
            obj.use_fake_user = True
    return objects, materials


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
        for source in sources:
            obj = source.copy()
            del obj["quixlend_source"]
            context.collection.objects.link(obj)
            obj.location = base
            obj.location.x += x
            xs = [corner[0] for corner in source.bound_box]  # dimensions are 0 on unlinked objects
            x += (max(xs) - min(xs)) * 1.2
            obj.select_set(True)
            context.view_layer.objects.active = obj
    elif materials:
        targets = [target.original] if target else [o for o in context.selected_objects if hasattr(o.data, "materials")]
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

class QUIXLEND_OT_login_open(bpy.types.Operator):
    bl_idname = "quixlend.login_open"
    bl_label = "Open Epic Login"
    bl_description = "Open the Epic Games sign-in page in your browser"

    def execute(self, context):
        bpy.ops.wm.url_open(url=auth.LOGIN_URL)
        return {"FINISHED"}


class QUIXLEND_OT_login(bpy.types.Operator):
    bl_idname = "quixlend.login"
    bl_label = "Log In"
    bl_description = "Finish signing in with the pasted authorization code"

    def execute(self, context):
        wm = context.window_manager
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

    def execute(self, context):
        session.logout()
        (HOME / "owned.json").unlink(missing_ok=True)
        state.owned, state.status = None, ""
        return {"FINISHED"}


# ---- ui --------------------------------------------------------------------

class QuixlendPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON
    library: StringProperty(name="Library", subtype="DIR_PATH",
                            default=str(Path.home() / "Documents" / "Megascans Library"),
                            description="Folder downloads are stored in; registered as an asset library")
    resolution: EnumProperty(name="Texture Resolution", default="4096",
                             items=[("2048", "2K", ""), ("4096", "4K", ""), ("8192", "8K", "")])

    def draw(self, context):
        self.layout.prop(self, "library")
        self.layout.prop(self, "resolution")


class QUIXLEND_PT_browser(bpy.types.Panel):
    bl_label = "Quixlend"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Quixlend"

    def draw_header(self, context):
        self.layout.label(text="", icon_value=_icons["quixel"].icon_id)

    def draw_header_preset(self, context):
        self.layout.popover(panel="QUIXLEND_PT_settings", text="", icon="PREFERENCES")

    def draw(self, context):
        layout, wm = self.layout, context.window_manager
        layout.use_property_decorate = False
        if not session.logged_in:
            col = layout.column(align=True)
            col.label(text="Your Megascans library", icon="ASSET_MANAGER")
            col.label(text="Sign in with your Epic Games account.")
            layout.separator()
            layout.operator("quixlend.login_open", text="Sign In with Epic Games", icon="URL")
            col = layout.column(align=True)
            col.label(text="Paste the authorization code:")
            col.prop(wm, "quixlend_code", text="", placeholder="Authorization code")
            row = col.row()
            row.enabled = bool(wm.quixlend_code.strip())
            row.operator("quixlend.login", text="Connect Account", icon="CHECKMARK")
            if state.status:
                layout.label(text=state.status, icon="INFO")
            return

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.prop(wm, "quixlend_query", text="", icon="VIEWZOOM", placeholder="Search Megascans")
        row.operator("quixlend.search", text="", icon="FILE_REFRESH")
        row = layout.row(align=True)
        row.prop(wm, "quixlend_type", text="")
        row.prop(prefs(), "resolution", text="")
        row = layout.row()
        row.label(text=state.status or "Browse the Megascans catalog", icon="ASSET_MANAGER")

        # The cards are drawn by sidebar.draw_grid into this reserved empty space.
        from . import sidebar
        m = sidebar.metrics(context.region)
        blank = layout.column(align=True)
        for _ in range(math.ceil(m["height"] / m["unit"])):
            blank.label(text="")
        state.panel_open = True
        if not state.hits:
            box = layout.box()
            box.label(text="No matching assets" if state.status == "No results" else "Materials, models and plants",
                      icon="VIEWZOOM")
            box.label(text="Try another search or asset type." if state.status == "No results"
                      else "Search by name or choose an asset type.")
        if state.page + 1 < state.pages:
            layout.label(text="Fetching more assets…", icon="SORTTIME")
        if state.hits:
            col = layout.column(align=True)
            col.enabled = False
            col.label(text="Click to download · Drag to place", icon="MOUSE_LMB")


class QUIXLEND_PT_settings(bpy.types.Panel):
    bl_label = "Quixlend Settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Quixlend"
    bl_options = {"INSTANCED"}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.label(text="Asset Library", icon="ASSET_MANAGER")
        layout.prop(prefs(), "library")
        layout.prop(prefs(), "resolution")
        if session.logged_in:
            layout.separator()
            layout.label(text=session.email or "Epic Games account", icon="USER")
            layout.operator("quixlend.logout", text="Sign Out", icon="QUIT")


CLASSES = (QuixlendPreferences, QUIXLEND_OT_search, QUIXLEND_OT_download, QUIXLEND_OT_login_open,
           QUIXLEND_OT_login, QUIXLEND_OT_logout, QUIXLEND_PT_settings, QUIXLEND_PT_browser)


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


def _asset_browser_header(self, context):
    params = context.space_data.params
    ref = getattr(params, "asset_library_reference", None) or getattr(params, "asset_library_ref", "")
    if ref == "Megascans":
        self.layout.label(text="Megascans", icon_value=_icons["quixel"].icon_id)


def register():
    global _icons
    _icons = bpy.utils.previews.new()
    _icons.load("quixel", str(Path(__file__).parents[1] / "assets" / "quixel.png"), "IMAGE")
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    wm.quixlend_query = StringProperty(name="Search", update=_search_update, options={"SKIP_SAVE"})
    wm.quixlend_type = EnumProperty(name="Type", items=TYPES, update=_search_update)
    wm.quixlend_code = StringProperty(name="Authorization Code", options={"SKIP_SAVE"})
    bpy.app.handlers.load_post.append(_file_loaded)
    bpy.types.ASSETBROWSER_MT_editor_menus.append(_asset_browser_header)
    bpy.app.timers.register(_startup, first_interval=0.5, persistent=True)


def unregister():
    global _icons
    bpy.app.handlers.load_post.remove(_file_loaded)
    bpy.types.ASSETBROWSER_MT_editor_menus.remove(_asset_browser_header)
    wm = bpy.types.WindowManager
    del wm.quixlend_query, wm.quixlend_type, wm.quixlend_code
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    bpy.utils.previews.remove(_icons)
    _icons = None
    for hit in state.hits:
        if img := bpy.data.images.get(f".quixlend_{hit['id']}"):
            bpy.data.images.remove(img)
    state.hits = []
