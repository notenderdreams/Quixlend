"""Color Variation for a Quixlend material group: a Shader Editor panel that puts the shared node group, imported from
assets/nodegroups.blend, right after the selected group's Base Color."""

from pathlib import Path
from typing import cast

import bpy

from .store import Result, layout_of

NODEGROUPS = Path(__file__).parents[1] / "assets" / "nodegroups.blend"
VARIATION = "Quixlend Color Variation"
IMPORTED = "quixlend_imported"  # marks our copy, so it is reused rather than imported again


def _tree(node: bpy.types.Node | None) -> bpy.types.NodeTree | None:
    if node is None or node.bl_idname != "ShaderNodeGroup":
        return None
    return cast(bpy.types.ShaderNodeGroup, node).node_tree


def problem(node: bpy.types.Node | None) -> str | None:
    """Why `node` can't take Color Variation, or None if it can. A Quixlend material group takes a Vector and gives
    a Base Color."""
    if _tree(node) is None:
        return "Select a node group"
    assert node
    missing = [
        n for n, sockets in (("Vector", node.inputs), ("Base Color", node.outputs)) if n not in sockets
    ]
    if missing:
        return f"Not a Quixlend material group (no {', '.join(missing)})"
    after = (link.to_node for link in node.outputs["Base Color"].links or [])
    if any((t := _tree(n)) and t.name.startswith(VARIATION) for n in after):
        return "Color Variation is already attached"
    return None


def import_group() -> bpy.types.ShaderNodeTree:
    """The Color Variation group from assets/nodegroups.blend; an earlier import in this file is reused."""
    for group in bpy.data.node_groups:
        if group.get(IMPORTED):
            return cast(bpy.types.ShaderNodeTree, group)
    before = set(bpy.data.node_groups)
    with bpy.data.libraries.load(str(NODEGROUPS)) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]
        if VARIATION not in src.node_groups:
            raise FileNotFoundError(f"'{VARIATION}' is not in {NODEGROUPS.name}")
        dst.node_groups = [VARIATION]
    group = cast(bpy.types.ShaderNodeTree, next(g for g in bpy.data.node_groups if g not in before))
    group.asset_clear()  # a working copy, not an entry in the user's Asset Browser
    group[IMPORTED] = True
    return group


class QUIXLEND_OT_color_variation(bpy.types.Operator):
    bl_idname = "quixlend.color_variation"
    bl_label = "Add Color Variation"
    bl_description = "Put Color Variation after this group's Base Color"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return problem(context.active_node) is None

    def execute(self, context: bpy.types.Context) -> Result:
        group = cast(bpy.types.Node, context.active_node)
        tree = cast(bpy.types.ShaderNodeTree, group.id_data)
        try:
            variation = import_group()
        except OSError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        node = cast(bpy.types.ShaderNodeGroup, tree.nodes.new("ShaderNodeGroup"))
        node.node_tree = variation
        node.label = "Color Variation"
        gap = 40
        node.location = (group.location.x + group.width + gap, group.location.y)
        for other in (
            tree.nodes
        ):  # make room: everything to the right of the group moves over (frames carry their children)
            if other != node and other.parent is None and other.location.x >= group.location.x + group.width:
                other.location.x += node.width + gap

        feeds = group.inputs["Vector"].links
        coords = cast(bpy.types.NodeSocket | None, feeds[0].from_socket if feeds else None)
        if coords is None:  # nothing feeds the group: the noise needs the same UV the textures use
            uv = tree.nodes.new("ShaderNodeTexCoord")
            uv.location = (node.location.x, node.location.y - 260)
            coords = uv.outputs["UV"]
        targets = [link.to_socket for link in group.outputs["Base Color"].links or [] if link.to_socket]
        tree.links.new(cast(bpy.types.NodeSocket, coords), node.inputs["Vector"])
        tree.links.new(group.outputs["Base Color"], node.inputs["Color"])
        for socket in targets:  # whatever took the Base Color now takes the varied one
            tree.links.new(node.outputs["Color"], socket)
        return {"FINISHED"}


class QUIXLEND_PT_variation(bpy.types.Panel):
    bl_label = "Color Variation"
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
        layout.operator("quixlend.color_variation", icon="TEXTURE")  # greyed out by the operator's poll
