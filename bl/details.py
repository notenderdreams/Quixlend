"""The asset details popup."""

import bpy
import bpy.utils.previews
from bpy.props import StringProperty

from ..core import api
from . import jobs
from .placement import local_resolution
from .search import big_preview
from .settings import res_label
from .store import Result, big, client, layout_of, prefs, state
from .transfer import download_size


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
