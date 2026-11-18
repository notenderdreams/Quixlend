"""Mouse handling: hover highlight, click, and drag-and-drop of cards."""

import random
import time
from typing import TYPE_CHECKING, Any, cast

import bpy
import bpy.utils.previews
from bpy_extras import view3d_utils
from mathutils.geometry import intersect_line_plane

from ...core import api
from ..details import open_details
from ..library import built_at_selected, has_selected
from ..placement import place
from ..settings import draw_res_menu
from ..store import Result, prefs, state
from ..transfer import cancel_download, switch_resolution
from . import gfx
from .interact import flash, pointer
from .layout import active, zone_at
from .preview import dwell, wireframe


class QUIXLEND_OT_hover(bpy.types.Operator):
    bl_idname = "quixlend.hover"
    bl_label = "Highlight Asset"
    bl_options = {"INTERNAL"}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> Result:
        region = context.region
        hit, zone = (
            zone_at(region, event.mouse_region_x, event.mouse_region_y)
            if region and active(region)
            else (None, None)
        )
        current = (region.as_pointer(), hit["id"], zone) if hit else None
        if current != pointer.hover:
            pointer.hover, pointer.hover_since = current, time.time()
            context.area.tag_redraw()
            if current and current[2] is None and prefs().hover_preview:
                bpy.app.timers.register(
                    lambda area=context.area, key=current: dwell(area, key),
                    first_interval=prefs().hover_preview_delay,
                )
        return {"PASS_THROUGH"}


class QUIXLEND_OT_card(bpy.types.Operator):
    bl_idname = "quixlend.card"
    bl_label = "Quixlend Card"
    bl_description = "Click to download; drag a downloaded asset into the scene"
    bl_options = {"UNDO"}

    if TYPE_CHECKING:  # set in invoke(); declared here for the type checker only
        hit: dict[str, Any]
        zone: str | None
        area: bpy.types.Area
        start: tuple[int, int]

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> Result:
        region = context.region
        if not region or not active(region):
            return {"PASS_THROUGH"}
        hit, self.zone = zone_at(region, event.mouse_region_x, event.mouse_region_y)
        if not hit or not context.area:
            return {"PASS_THROUGH"}
        self.hit, self.area, self.start = hit, context.area, (event.mouse_x, event.mouse_y)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> Result:
        asset_id, st = self.hit["id"], state
        if event.type == "MOUSEMOVE":
            moved = abs(event.mouse_x - self.start[0]) + abs(event.mouse_y - self.start[1])
            if pointer.drag or (
                asset_id in st.local and self.zone not in ("res", "copy") and moved > 6 * gfx.scale()
            ):
                if not pointer.drag:
                    pointer.drag = {"hit": self.hit, "wire": wireframe(asset_id), "loc": None}
                pointer.drag.update(x=event.mouse_x, y=event.mouse_y)
                if pointer.drag["wire"]:
                    found = self.locate(context, event.mouse_x, event.mouse_y)
                    pointer.drag["loc"] = found[0] if found else None
                self.area.tag_redraw()
        elif event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if pointer.drag:
                chosen = pointer.drag["wire"]["name"] if pointer.drag["wire"] else None
                pointer.drag = None
                self.area.tag_redraw()
                self.drop(context, event, chosen)
            elif self.zone is None:
                pointer.hover = None  # the details popup replaces the hover preview
                open_details(asset_id, context.window, self.area)
            elif self.zone == "copy":
                self.copy_group(context)
            elif self.zone == "buy":
                bpy.ops.wm.url_open(url=api.purchase_url(self.hit["name"]))
            elif self.zone == "res" and asset_id in st.cancel:
                cancel_download(asset_id)
            elif self.zone == "res":
                context.window_manager.popup_menu(draw_res_menu, title="Texture Resolution")
            elif self.zone == "main":
                if asset_id in st.local and not has_selected(asset_id):
                    bpy.ops.quixlend.download(asset_id=asset_id, replace=True)  # pyright: ignore[reportAttributeAccessIssue]
                elif asset_id in st.local:
                    self.import_at_cursor(context)
                elif asset_id in st.working:
                    pass  # already applying
                elif asset_id not in st.progress and (st.owned is None or asset_id in st.owned):
                    st.errors.pop(asset_id, None)
                    bpy.ops.quixlend.download(asset_id=asset_id)  # pyright: ignore[reportAttributeAccessIssue]
            self.area.tag_redraw()
            return {"FINISHED"}
        elif event.type in {"ESC", "RIGHTMOUSE"}:
            pointer.drag = None
            self.area.tag_redraw()
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def copy_group(self, context):
        """Run the copy after this modal ends: it borrows the area, which must not be in use by a running operator."""
        asset_id, window, area = self.hit["id"], context.window, self.area

        def run():
            region = next(r for r in area.regions if r.type == "WINDOW")
            with bpy.context.temp_override(window=window, area=area, region=region):
                if bpy.ops.quixlend.copy_group(asset_id=asset_id) == {"FINISHED"}:  # pyright: ignore[reportAttributeAccessIssue]
                    flash(asset_id, "copy", area)

        bpy.app.timers.register(run, first_interval=0)

    def locate(self, context, mx, my):
        """(location, target object or None) under the mouse: the surface hit, else the ground plane;
        None when the mouse is outside the viewport or over the sidebar."""
        region = next(r for r in self.area.regions if r.type == "WINDOW")
        x, y = mx - region.x, my - region.y
        sidebar = next(r for r in self.area.regions if r.type == "UI")
        over_sidebar = sidebar.x <= mx <= sidebar.x + sidebar.width
        if over_sidebar or not (0 <= x <= region.width and 0 <= y <= region.height):
            return None
        rv3d = cast(bpy.types.SpaceView3D, self.area.spaces.active).region_3d
        if rv3d is None:
            return None
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y))
        hit, location, _, _, target, _ = context.scene.ray_cast(
            context.evaluated_depsgraph_get(), origin, direction
        )
        if not hit:
            target = None
            location = (
                intersect_line_plane(origin, origin + direction, (0, 0, 0), (0, 0, 1))
                or context.scene.cursor.location
            )
        return location, target

    def place_later(self, context, **kwargs):
        """Run place after a redraw: it blocks the main thread, so show "Applying" first."""
        hit, window, area = self.hit, context.window, self.area
        asset_id = hit["id"]

        def run():
            region = next(r for r in area.regions if r.type == "WINDOW")
            try:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    message = place(bpy.context, asset_id, **kwargs)
                    if not message:
                        bpy.ops.ed.undo_push(message="Quixlend: " + state.working[asset_id])
            finally:
                state.working.pop(asset_id, None)
            if message:
                bpy.context.window_manager.popup_menu(
                    lambda menu, _context: menu.label(text=message, icon="ERROR"), title="Quixlend"
                )
            else:
                flash(asset_id, "main", area)
            area.tag_redraw()

        def start():
            state.working[asset_id] = "Applying" if hit.get("type") in ("surface", "atlas") else "Importing"
            area.tag_redraw()
            bpy.app.timers.register(run, first_interval=0.05)

        if built_at_selected(asset_id):
            start()
        else:  # the textures of the chosen resolution are on disk but the asset still uses another: rebuild first
            switch_resolution(asset_id, int(prefs().resolution), start)

    def import_at_cursor(self, context):
        """Import at the 3D cursor (materials go on the selected objects)."""
        self.place_later(context, pick=random.choice)

    def drop(self, context, event, chosen):
        """Place the asset exactly where the preview was: same variation, same spot."""
        found = self.locate(context, event.mouse_x, event.mouse_y)
        if not found:
            return  # released outside the viewport: cancel
        location, target = found
        pick = (
            (lambda sources: next((o for o in sources if o.name == chosen), sources[0]))
            if chosen
            else random.choice
        )
        self.place_later(context, location=location, target=target, pick=pick)
