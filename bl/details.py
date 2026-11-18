"""The asset details popup."""

import shutil

import bpy
import bpy.utils.previews
from bpy.props import IntProperty, StringProperty

from ..core import api, download
from . import catalog, jobs
from .library import refresh_asset_browsers, scan_local
from .search import big_preview
from .settings import res_label
from .store import Result, big, client, layout_of, library, state
from .transfer import download_size


def open_details(asset_id, window, area):
    """Fetch preview + asset details off the main thread (card shows a loader meanwhile), then pop up the details."""
    if state.opening:
        return
    state.opening = asset_id
    jobs.redraw()  # spinner now
    hit = next(h for h in state.hits if h["id"] == asset_id)

    def work():
        try:
            path = big_preview(hit)
        except Exception:  # offline and no local copy: the details can still come from disk
            path = None
        if not (state.assets.get(asset_id) or catalog.meta(asset_id)):
            state.assets[asset_id] = client.asset(asset_id)  # not downloaded: ask Quixel
        return path

    def done(path, error):
        state.opening = None
        if path and not error and not big().get(asset_id):
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
        asset = state.assets.get(self.asset_id) or catalog.meta(self.asset_id)
        self.info = api.asset_info(asset) if asset else [("Details unavailable", "")]
        self.size = download_size(self.asset_id, fetch=False)
        sizes = state.resolutions.get(self.asset_id, {}) if self.asset_id in state.local else {}
        self.have = [(res, f"{res // 1024}K ({n / 1e6:,.0f} MB)") for res, n in sorted(sizes.items())]
        return context.window_manager.invoke_popup(self, width=420)

    def draw(self, context: bpy.types.Context):
        layout = layout_of(self)
        if self.icon:
            layout.template_icon(icon_value=self.icon, scale=14)
        layout.label(text=self.hit["name"])
        col = layout.column(align=True)
        rows: list[tuple[int | None, str, str]] = [(None, key, value) for key, value in self.info]
        rows[:0] = [(res, "Downloaded" if i == 0 else "", line) for i, (res, line) in enumerate(self.have)]
        if self.size:
            rows.insert(0, (None, "Download size", f"{self.size / 1e6:,.0f} MB at {res_label()}"))
        for res, key, value in rows:
            row = col.split(factor=0.3)
            row.label(text=key)
            if res is None:
                row.label(text=value)
                continue
            if res not in state.resolutions.get(self.asset_id, {}):  # deleted while this card is open
                greyed = row.row()
                greyed.active = False
                greyed.label(text=f"{value}  (deleted)")
                continue
            cell = row.split(factor=0.37)  # room for the size, then the icon right after it
            cell.label(text=value)
            tail = cell.split(factor=0.14)  # the icon takes a sliver of what is left; the rest stays empty
            op = tail.operator("quixlend.delete_resolution", text="", icon="TRASH")
            tail.label(text="")
            assert op is not None
            op.asset_id, op.res = self.asset_id, res  # pyright: ignore[reportAttributeAccessIssue]

    def execute(self, context: bpy.types.Context) -> Result:
        return {"FINISHED"}


class QUIXLEND_OT_delete_resolution(bpy.types.Operator):
    bl_idname = "quixlend.delete_resolution"
    bl_label = "Delete Resolution"
    bl_description = "Delete this resolution's textures and .blend file from the library"
    asset_id: StringProperty()  # pyright: ignore[reportInvalidTypeForm]
    res: IntProperty()  # pyright: ignore[reportInvalidTypeForm]

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> Result:
        return context.window_manager.invoke_confirm(
            self,
            event,
            title=f"Delete the {self.res // 1024}K files of this asset?",
            message="Objects in the scene that use these textures will lose them. This cannot be undone.",
            confirm_text="Delete",
        )

    def execute(self, context: bpy.types.Context) -> Result:
        asset_id, res = self.asset_id, self.res
        folder = next((library() / "Quixlend").glob(f"*/{asset_id}"), None)
        if asset_id in state.progress or asset_id in state.building:
            self.report({"WARNING"}, "Wait for this asset's download to finish")
            return {"CANCELLED"}
        if folder is None:
            return {"CANCELLED"}
        sizes = state.resolutions.get(asset_id, {})
        sizes.pop(res, None)  # the cards update now; the scan below confirms
        state.built.get(asset_id, set()).discard(res)
        if not sizes:
            state.local.discard(asset_id)

        def work():
            for f in [f for f in folder.rglob("*") if f.is_file()]:
                if (
                    download.resolutions_of([f]) == {res}
                    or download.blend_resolution(f.name, asset_id) == res
                ):
                    f.unlink(missing_ok=True)
            if not download.resolutions_of(f for f in folder.rglob("*") if f.is_file()):
                shutil.rmtree(folder, ignore_errors=True)  # no texture size left: nothing worth keeping

        def done(_, error):
            scan_local()
            refresh_asset_browsers()

        jobs.run(work, done)
        jobs.redraw()
        return {"FINISHED"}
