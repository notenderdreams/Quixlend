"""Registers the sidebar panel, the mouse operators, the draw handlers and the keymap."""

import bpy

from .cards import draw_grid
from .mouse import QUIXLEND_OT_card, QUIXLEND_OT_hover
from .panel import QUIXLEND_PT_browser
from .preview import draw_drag, draw_drag_3d, draw_preview

_handles = []


def register():
    bpy.utils.register_class(QUIXLEND_PT_browser)
    bpy.utils.register_class(QUIXLEND_OT_hover)
    bpy.utils.register_class(QUIXLEND_OT_card)
    space = bpy.types.SpaceView3D
    _handles.extend(
        [
            (space.draw_handler_add(draw_grid, (), "UI", "POST_PIXEL"), "UI"),
            (space.draw_handler_add(draw_drag, (), "UI", "POST_PIXEL"), "UI"),
            (space.draw_handler_add(draw_drag, (), "WINDOW", "POST_PIXEL"), "WINDOW"),
            (space.draw_handler_add(draw_preview, (), "WINDOW", "POST_PIXEL"), "WINDOW"),
            (space.draw_handler_add(draw_drag_3d, (), "WINDOW", "POST_VIEW"), "WINDOW"),
        ]
    )
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:  # absent in background mode
        keymap = keyconfig.keymaps.new(name="3D View Generic", space_type="VIEW_3D")
        keymap.keymap_items.new("quixlend.card", "LEFTMOUSE", "PRESS")
        keymap.keymap_items.new("quixlend.hover", "MOUSEMOVE", "ANY")


def unregister():
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig and "3D View Generic" in keyconfig.keymaps:
        keymap = keyconfig.keymaps["3D View Generic"]
        for item in [i for i in keymap.keymap_items if i.idname in {"quixlend.card", "quixlend.hover"}]:
            keymap.keymap_items.remove(item)
    for handle, region in _handles:
        bpy.types.SpaceView3D.draw_handler_remove(handle, region)
    _handles.clear()
    bpy.utils.unregister_class(QUIXLEND_OT_card)
    bpy.utils.unregister_class(QUIXLEND_OT_hover)
    bpy.utils.unregister_class(QUIXLEND_PT_browser)
