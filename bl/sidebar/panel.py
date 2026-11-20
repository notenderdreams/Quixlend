"""The sidebar panel: search row, filters and the space the grid is drawn into."""

import math

import bpy
import bpy.utils.previews

from ..decal import draw_manager, editing
from ..store import icons, layout_of, prefs, session, state, wm_of
from .downloads import active_ids
from .layout import metrics


class QUIXLEND_PT_browser(bpy.types.Panel):
    bl_label = "Quixlend"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Quixlend"

    def draw_header(self, context: bpy.types.Context):
        layout_of(self).label(text="", icon_value=icons()["quixel"].icon_id)

    def draw_header_preset(self, context: bpy.types.Context):
        row = layout_of(self).row()
        plain = row.row()
        plain.emboss = "NONE"  # no background
        busy = len(active_ids())
        plain.operator(
            "quixlend.downloads", text=("9+" if busy > 9 else str(busy)) if busy else "", icon="IMPORT"
        )
        settings = plain.operator("wm.call_panel", text="", icon="PREFERENCES")
        assert settings  # an operator that exists always returns its properties
        settings.name = "QUIXLEND_PT_settings"

    def draw(self, context: bpy.types.Context):
        layout, wm = layout_of(self), wm_of(context)
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

        if box := editing(context):
            draw_manager(layout, context, box)
            return

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.prop(wm, "quixlend_query", text="", icon="VIEWZOOM", placeholder="Search Megascans")
        row.operator("quixlend.search", text="", icon="FILE_REFRESH")
        row = layout.row(align=True)
        kind = row.row(align=True)
        kind.scale_x = 0.7
        kind.prop(wm, "quixlend_type", text="")
        cat = row.row(align=True)
        cat.enabled = wm.quixlend_type not in ("ALL", "decal")
        cat.prop(wm, "quixlend_category", text="")
        row.prop(wm, "quixlend_downloaded", text="", icon="DISK_DRIVE")
        row = layout.row()
        row.label(text=state.status or "Browse the Megascans catalog", icon="ASSET_MANAGER")
        size = row.row()
        size.alignment = "RIGHT"
        size.prop(prefs(), "resolution", text="")

        # The cards are drawn by sidebar.draw_grid into this reserved empty space.
        m = metrics(context.region)
        blank = layout.column(align=True)
        for _ in range(math.ceil(m["height"] / m["unit"])):
            blank.label(text="")
        state.panel_open = True
        state.downloads_seen = True
        if not state.hits:
            box = layout.box()
            box.label(
                text="No matching assets" if state.status == "No results" else "Materials, models and plants",
                icon="VIEWZOOM",
            )
            box.label(
                text="Try another search or asset type."
                if state.status == "No results"
                else "Search by name or choose an asset type."
            )
        if state.page + 1 < state.pages:
            row = layout.row()
            row.alignment = "CENTER"
            row.label(text="Fetching more assets…", icon="SORTTIME")
