"""Preferences, the settings popover and the delete-everything operator."""

import shutil
from pathlib import Path
from typing import cast

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from . import jobs
from .library import refresh_asset_browsers, scan_local
from .store import ADDON, Result, layout_of, library, prefs, session, state


def res_label() -> str:
    items = cast(bpy.types.EnumProperty, prefs().bl_rna.properties["resolution"]).enum_items
    return items[prefs().resolution].name


def draw_res_menu(self: bpy.types.Menu, context: bpy.types.Context):
    layout_of(self).prop(prefs(), "resolution", expand=True)


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
    max_columns: IntProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Max Columns",
        default=0,
        min=0,
        max=12,
        description="Most thumbnail columns to show. 0: automatic, as many as fit the panel width",
        update=lambda self, context: jobs.redraw(),
    )
    hover_preview: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Hover Preview",
        default=True,
        description="Show a large preview in the viewport when you rest the pointer on a card's image",
    )
    hover_preview_delay: FloatProperty(  # pyright: ignore[reportInvalidTypeForm]
        name="Preview Delay",
        default=0.5,
        min=0.1,
        max=2.0,
        unit="TIME",
        description="How long to rest on a card before its preview appears",
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
        layout.prop(self, "max_columns")
        layout.prop(self, "hover_preview")
        row = layout.row()
        row.enabled = self.hover_preview
        row.prop(self, "hover_preview_delay")


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
        layout.prop(prefs(), "max_columns")
        layout.prop(prefs(), "hover_preview")
        row = layout.row()
        row.enabled = prefs().hover_preview
        row.prop(prefs(), "hover_preview_delay")
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
