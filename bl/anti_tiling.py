"""Anti-Tiling for a Quixlend material group: a Shader Editor panel that puts the shared node group, imported from
assets/nodegroups.blend, between whatever feeds the selected group's Vector and the group itself. Surfaces get it
already when they are built (see build.add_anti_tiling); this is for the other asset types."""

from typing import cast

import bpy

from .build import ANTI_TILING, add_anti_tiling
from .store import Result, layout_of


def _tree(node: bpy.types.Node | None) -> bpy.types.NodeTree | None:
    if node is None or node.bl_idname != "ShaderNodeGroup":
        return None
    return cast(bpy.types.ShaderNodeGroup, node).node_tree


def problem(node: bpy.types.Node | None) -> str | None:
    """Why `node` can't take Anti-Tiling, or None if it can. A Quixlend material group takes a Vector."""
    if _tree(node) is None:
        return "Select a node group"
    assert node
    if "Vector" not in node.inputs:
        return "Not a Quixlend material group (no Vector)"
    before = (link.from_node for link in node.inputs["Vector"].links or [])
    if any((t := _tree(n)) and t.name.startswith(ANTI_TILING) for n in before):
        return "Anti-Tiling is already attached"
    return None


class QUIXLEND_OT_anti_tiling(bpy.types.Operator):
    bl_idname = "quixlend.anti_tiling"
    bl_label = "Add Anti-Tiling"
    bl_description = "Break up visible texture repetition: shift each cell of the UV by a random amount"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return problem(context.active_node) is None

    def execute(self, context: bpy.types.Context) -> Result:
        try:
            add_anti_tiling(cast(bpy.types.Node, context.active_node))
        except OSError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class QUIXLEND_PT_anti_tiling(bpy.types.Panel):
    bl_label = "Anti-Tiling"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Quixlend"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        space = context.space_data
        return space is not None and getattr(space, "tree_type", "") == "ShaderNodeTree"

    def draw(self, context: bpy.types.Context):
        layout = layout_of(self)
        why = problem(context.active_node)
        if why:
            layout.label(text=why, icon="INFO")
        else:
            layout.label(text=cast(bpy.types.Node, context.active_node).name, icon="NODETREE")
        layout.operator("quixlend.anti_tiling", icon="MESH_GRID")  # greyed out by the operator's poll
