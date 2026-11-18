"""Getting a downloaded asset into the scene: loading its datablocks, placing it, copying its node group."""

from typing import cast

import bpy
import bpy.utils.previews
from bpy.props import StringProperty

from .store import Result, library


def load_templates(asset_id):
    """(objects, materials) of a downloaded asset, loaded once and reused; (None, None) if the file is missing."""
    blend = next((library() / "Quixlend").glob(f"*/{asset_id}/{asset_id}.blend"), None)
    if not blend:
        return None, None
    # Reuse datablocks from an earlier placement instead of appending duplicates.
    objects = [o for o in bpy.data.objects if o.get("quixlend_id") == asset_id and o.get("quixlend_source")]
    materials = [m for m in bpy.data.materials if m.get("quixlend_id") == asset_id]
    if not objects and not materials:
        with bpy.data.libraries.load(str(blend), assets_only=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]
            dst.objects, dst.materials = src.objects, src.materials
        objects, materials = dst.objects, dst.materials
        for block in objects + materials:
            block.asset_clear()
            block["quixlend_id"] = asset_id
        for obj in objects:
            obj["quixlend_source"] = True  # pristine template, never linked to a scene
            obj.use_fake_user = True
    return objects, materials


def reload_templates(asset_id):
    """After a rebuild: swap datablocks loaded from the old .blend for the new ones; objects keep their material."""
    old_m = sorted((m for m in bpy.data.materials if m.get("quixlend_id") == asset_id), key=lambda m: m.name)
    old_o = [o for o in bpy.data.objects if o.get("quixlend_id") == asset_id and o.get("quixlend_source")]
    if not (old_m or old_o):
        return  # never loaded this session: the next use loads the new file
    names = [m.name for m in old_m]
    groups = {
        cast(bpy.types.ShaderNodeGroup, n).node_tree
        for m in old_m
        if m.node_tree
        for n in m.node_tree.nodes
        if n.bl_idname == "ShaderNodeGroup"
    }
    groups = {g for g in groups if g}
    images = {
        cast(bpy.types.ShaderNodeTexImage, n).image
        for g in groups
        for n in g.nodes
        if n.bl_idname == "ShaderNodeTexImage"
    }
    for block in (*old_m, *old_o):
        del block["quixlend_id"]  # or load_templates would find them instead of loading the new file
    _, loaded = load_templates(asset_id)
    new_m = sorted(loaded or [], key=lambda m: m.name)
    for old, new in zip(old_m, new_m):
        old.user_remap(new)
    for old in old_m:
        bpy.data.materials.remove(old)
    for obj in old_o:
        bpy.data.objects.remove(obj)
    for new, name in zip(new_m, names):
        new.name = name
    for group in groups:
        if group.users - group.use_fake_user == 0:  # the old build's node group; the new material has its own
            bpy.data.node_groups.remove(group)
    for image in images:
        if image and image.users - image.use_fake_user == 0:
            bpy.data.images.remove(image)


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
        collection = bpy.data.collections.get("Megascans")
        if collection is None:
            collection = bpy.data.collections.new("Megascans")
        if collection not in context.scene.collection.children_recursive:
            context.scene.collection.children.link(collection)
        for source in sources:
            obj = cast(bpy.types.Object, source.copy())
            del obj["quixlend_source"]
            collection.objects.link(obj)
            obj.location = base
            obj.location.x += x
            xs = [corner[0] for corner in source.bound_box]  # dimensions are 0 on unlinked objects
            x += (max(xs) - min(xs)) * 1.2
            obj.select_set(True)
            context.view_layer.objects.active = obj
    elif materials:
        targets = (
            [target.original]
            if target
            else [o for o in context.selected_objects if hasattr(o.data, "materials")]
        )
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


class QUIXLEND_OT_copy_group(bpy.types.Operator):
    bl_idname = "quixlend.copy_group"
    bl_label = "Copy Node Group"
    bl_description = "Copy this material's node group; paste it (Ctrl+V) into any shader editor"
    asset_id: StringProperty()  # pyright: ignore[reportInvalidTypeForm]

    def execute(self, context: bpy.types.Context) -> Result:
        _, materials = load_templates(self.asset_id)
        tree = materials[0].node_tree if materials else None
        group = next((n for n in tree.nodes if n.bl_idname == "ShaderNodeGroup"), None) if tree else None
        window = context.window
        if not (tree and group and window):
            self.report({"WARNING"}, "Nothing to copy; download the asset again")
            return {"CANCELLED"}
        for n in tree.nodes:
            n.select = n == group
        # node.clipboard_copy only runs inside a node editor, so one is borrowed for a moment. Never a 3D viewport:
        # changing its type tears down its render session and Cycles would restart.
        areas = [a for a in window.screen.areas if a.type != "VIEW_3D"]
        area = next((a for a in areas if a.type == "NODE_EDITOR"), None) or min(
            areas, key=lambda a: a.width * a.height, default=context.area
        )
        if area is None:
            self.report({"WARNING"}, "No editor to borrow for copying")
            return {"CANCELLED"}
        old = area.type
        area.type = "NODE_EDITOR"
        space = cast(bpy.types.SpaceNodeEditor, area.spaces.active)
        before = (
            space.tree_type,
            space.pin,
            space.node_tree,
        )  # an editor that was already open keeps its state
        try:
            space.tree_type = "ShaderNodeTree"
            space.pin = True
            space.node_tree = tree
            region = next(r for r in area.regions if r.type == "WINDOW")
            with context.temp_override(window=window, area=area, region=region, space_data=space):
                bpy.ops.node.clipboard_copy()
        finally:
            space.pin, space.node_tree = False, None
            space.tree_type = before[0]
            if before[1]:
                space.pin, space.node_tree = before[1], before[2]
            area.type = old
        self.report({"INFO"}, "Copied node group; paste with Ctrl+V in a shader editor")
        return {"FINISHED"}
