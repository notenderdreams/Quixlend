"""Thumbnail grid inside the sidebar panel, with click-to-download and drag-and-drop.

Blender's Python UI has no drag source, so the panel only reserves empty space and
this module draws the cards into it and handles the mouse itself.
"""
import math
import random

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils.geometry import intersect_line_plane

from . import ui

CATEGORY = "Quixlend"
HEADER_UNITS = 6.3  # panel header + account/search/filter/status rows above the grid, in widget units
CARD, HOVER, BAR = (0.16, 0.16, 0.18, 1), (0.26, 0.3, 0.42, 1), (0.1, 0.1, 0.11, 1)
TEXT, DIM, OK, ERR, ACCENT = (0.92, 0.92, 0.92, 1), (0.6, 0.6, 0.62, 1), (0.45, 0.8, 0.5, 1), (0.95, 0.45, 0.4, 1), (0.3, 0.55, 1, 1)

drag = None  # {"hit", "x", "y"} in window coordinates while a card is being dragged


def scale():
    return bpy.context.preferences.system.ui_scale


def metrics(region):
    """Grid geometry for a sidebar region; shared by the panel (to reserve space), drawing and hit-testing."""
    s = scale()
    left, gap, label = 10 * s, 6 * s, 34 * s
    width = region.width - left - 30 * s  # room for the tab strip and scrollbar on the right
    cols = max(1, int((width + gap) // (104 * s + gap)))
    card = (width - (cols - 1) * gap) / cols
    rows = math.ceil(len(ui.state.hits) / cols)
    return {"left": left, "gap": gap, "label": label, "cols": cols, "card": card,
            "cell": card + label + gap, "height": rows * (card + label + gap), "unit": 20 * s}


def cells(region):
    """[(hit, x, y)] with (x, y) the bottom-left of each thumbnail in region pixels."""
    m = metrics(region)
    top = region.view2d.view_to_region(0, 0, clip=False)[1] - HEADER_UNITS * m["unit"]
    out = []
    for i, hit in enumerate(ui.state.hits):
        out.append((hit, m["left"] + (i % m["cols"]) * (m["card"] + m["gap"]),
                    top - (i // m["cols"]) * m["cell"] - m["card"]))
    return m, out


def hit_at(region, x, y):
    m, items = cells(region)
    for hit, cx, cy in items:
        if cx <= x <= cx + m["card"] and cy - m["label"] <= y <= cy + m["card"]:
            return hit
    return None


def active(region):
    return region.type == "UI" and region.active_panel_category == CATEGORY and ui.session.logged_in


# ---- drawing ---------------------------------------------------------------

def _rect(x, y, w, h, color):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRI_FAN", {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h))}).draw(shader)


def _image(image, x, y, w, h):
    shader = gpu.shader.from_builtin("IMAGE")
    shader.uniform_sampler("image", gpu.texture.from_image(image))
    batch_for_shader(shader, "TRI_FAN", {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
                                         "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1))}).draw(shader)


def _text(text, x, y, size, color, max_width):
    blf.size(0, size)
    blf.color(0, *color)
    while text and blf.dimensions(0, text)[0] > max_width:
        text = text[:-2].rstrip() + "…" if len(text) > 2 else ""
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def _status(asset_id):
    st = ui.state
    if asset_id in st.progress:
        return f"Downloading {int(st.progress[asset_id] * 100)}%", ACCENT
    if asset_id in st.errors:
        return st.errors[asset_id], ERR
    if asset_id in st.local:
        return "Drag into scene", OK
    if st.owned is not None and asset_id not in st.owned:
        return "Not owned", DIM
    return "Click to download", DIM


def draw_grid():
    region = bpy.context.region
    if not active(region) or not ui.state.panel_open:
        return
    ui.state.panel_open = False  # set again by the panel on its next draw; stays off while collapsed
    s = scale()
    m, items = cells(region)
    gpu.state.blend_set("ALPHA")
    for hit, x, y in items:
        if y > region.height or y + m["card"] < -m["label"]:
            continue
        asset_id, card = hit["id"], m["card"]
        _rect(x, y - m["label"], card, card + m["label"], HOVER if drag and drag["hit"] is hit else CARD)
        if hit["image"]:
            _image(hit["image"], x + 2 * s, y + 2 * s, card - 4 * s, card - 4 * s)
        if asset_id in ui.state.progress:
            _rect(x, y, card, 4 * s, BAR)
            _rect(x, y, card * ui.state.progress[asset_id], 4 * s, ACCENT)
        _text(hit["name"], x + 4 * s, y - 13 * s, 11 * s, TEXT, card - 8 * s)
        status, color = _status(asset_id)
        _text(status, x + 4 * s, y - 28 * s, 10 * s, color, card - 8 * s)
    gpu.state.blend_set("NONE")


def draw_drag():
    """Thumbnail following the mouse; registered for both the sidebar and the viewport region."""
    if not drag or not drag["hit"]["image"]:
        return
    region, size = bpy.context.region, 72 * scale()
    gpu.state.blend_set("ALPHA")
    _image(drag["hit"]["image"], drag["x"] - region.x - size / 2, drag["y"] - region.y - size / 2, size, size)
    gpu.state.blend_set("NONE")


# ---- mouse -----------------------------------------------------------------

class QUIXLEND_OT_card(bpy.types.Operator):
    bl_idname = "quixlend.card"
    bl_label = "Quixlend Card"
    bl_description = "Click to download; drag a downloaded asset into the scene"
    bl_options = {"UNDO"}

    def invoke(self, context, event):
        region = context.region
        if not region or not active(region):
            return {"PASS_THROUGH"}
        self.hit = hit_at(region, event.mouse_region_x, event.mouse_region_y)
        if not self.hit:
            return {"PASS_THROUGH"}
        self.area, self.start = context.area, (event.mouse_x, event.mouse_y)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global drag
        asset_id, st = self.hit["id"], ui.state
        if event.type == "MOUSEMOVE":
            moved = abs(event.mouse_x - self.start[0]) + abs(event.mouse_y - self.start[1])
            if drag or (asset_id in st.local and moved > 6 * scale()):
                drag = {"hit": self.hit, "x": event.mouse_x, "y": event.mouse_y}
                self.area.tag_redraw()
        elif event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if drag:
                drag = None
                self.area.tag_redraw()
                self.drop(context, event)
            elif asset_id not in st.local and asset_id not in st.progress and (st.owned is None or asset_id in st.owned):
                bpy.ops.quixlend.download(asset_id=asset_id)
            return {"FINISHED"}
        elif event.type in {"ESC", "RIGHTMOUSE"}:
            drag = None
            self.area.tag_redraw()
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def drop(self, context, event):
        """Place the asset under the mouse: on the surface hit, else on the ground plane."""
        region = next(r for r in self.area.regions if r.type == "WINDOW")
        x, y = event.mouse_x - region.x, event.mouse_y - region.y
        sidebar = next(r for r in self.area.regions if r.type == "UI")
        over_sidebar = sidebar.x <= event.mouse_x <= sidebar.x + sidebar.width
        if over_sidebar or not (0 <= x <= region.width and 0 <= y <= region.height):
            return  # released outside the viewport: cancel
        rv3d = self.area.spaces.active.region_3d
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y))
        hit, location, _, _, target, _ = context.scene.ray_cast(context.evaluated_depsgraph_get(), origin, direction)
        if not hit:
            target = None
            location = intersect_line_plane(origin, origin + direction, (0, 0, 0), (0, 0, 1)) \
                or context.scene.cursor.location
        with context.temp_override(area=self.area, region=region):
            message = ui.place(context, self.hit["id"], location, target, pick=random.choice)
        if message:
            self.report({"WARNING"}, message)


_handles = []


def register():
    bpy.utils.register_class(QUIXLEND_OT_card)
    space = bpy.types.SpaceView3D
    _handles.extend([(space.draw_handler_add(draw_grid, (), "UI", "POST_PIXEL"), "UI"),
                     (space.draw_handler_add(draw_drag, (), "UI", "POST_PIXEL"), "UI"),
                     (space.draw_handler_add(draw_drag, (), "WINDOW", "POST_PIXEL"), "WINDOW")])
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:  # absent in background mode
        keymap = keyconfig.keymaps.new(name="3D View Generic", space_type="VIEW_3D")
        keymap.keymap_items.new("quixlend.card", "LEFTMOUSE", "PRESS")


def unregister():
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig and "3D View Generic" in keyconfig.keymaps:
        keymap = keyconfig.keymaps["3D View Generic"]
        for item in [i for i in keymap.keymap_items if i.idname == "quixlend.card"]:
            keymap.keymap_items.remove(item)
    for handle, region in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(handle, region)
    _handles.clear()
    bpy.utils.unregister_class(QUIXLEND_OT_card)
