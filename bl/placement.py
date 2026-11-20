"""Getting a downloaded asset into the scene: loading its datablocks, placing it, copying its node group."""

from typing import cast

import bpy
import bpy.utils.previews
from bpy.props import StringProperty

from ..core import download
from . import decal
from .library import usable_resolution
from .store import Result, library, prefs


def load_templates(asset_id, res=None):
    """(objects, materials) of a downloaded asset at texture size `res` (default: the chosen size, else the largest
    built), loaded once and reused; (None, None) if the file is missing."""
    res = res or usable_resolution(asset_id)
    folder = next((library() / "Quixlend").glob(f"*/{asset_id}"), None)
    blend = next(
        (
            p
            for name in (f"{asset_id}_{(res or 0) // 1024}K.blend", f"{asset_id}.blend")
            if folder and (p := folder / name).exists()
        ),
        None,
    )
    if not blend:
        return None, None
    key = download.blend_resolution(blend.name, asset_id) or 0  # 0: a file without a size in its name
    # Reuse datablocks from an earlier placement instead of appending duplicates.
    stamp = int(
        blend.stat().st_mtime
    )  # a rebuilt file is loaded again, not served from the copy loaded earlier
    objects = [o for o in bpy.data.objects if _loaded(o, asset_id, key, stamp) and o.get("quixlend_source")]
    materials = [m for m in bpy.data.materials if _loaded(m, asset_id, key, stamp)]
    if not objects and not materials:
        for old in [
            o for o in bpy.data.objects if o.get("quixlend_source") and _loaded(o, asset_id, key, None)
        ]:
            bpy.data.objects.remove(
                old
            )  # a template from before a rebuild: never in a scene, kept only by fake user
        with bpy.data.libraries.load(str(blend), assets_only=True) as (src, dst):  # pyright: ignore[reportGeneralTypeIssues]
            dst.objects, dst.materials = src.objects, src.materials
        objects, materials = dst.objects, dst.materials
        for block in objects + materials:
            block.asset_clear()
            block["quixlend_id"], block["quixlend_res"], block["quixlend_stamp"] = asset_id, key, stamp
        for obj in objects:
            obj["quixlend_source"] = True  # pristine template, never linked to a scene
            obj.use_fake_user = True
    return objects, materials


def _loaded(block, asset_id, res, stamp):
    """`block` was loaded from this asset at this size (and, unless `stamp` is None, from this build of it)."""
    return (
        block.get("quixlend_id") == asset_id
        and block.get("quixlend_res", 0) == res
        and (stamp is None or block.get("quixlend_stamp") == stamp)
    )


def place(context, asset_id, location=None, target=None, pick=None, name=None):
    """Put a downloaded asset in the scene. Meshes go to `location` (default: 3D cursor);
    materials go on `target` (default: selected objects). Returns a warning message or None.
    `pick` chooses one of several plant variations; without it all are laid out in a row.
    With the "Collection per Asset" setting, meshes go in a sub-collection called `name`."""
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
        if name and prefs().asset_collections:
            parent = collection
            collection = next((c for c in parent.children if c.get("quixlend_id") == asset_id), None)
            if collection is None:
                collection = bpy.data.collections.new(name)
                collection["quixlend_id"] = asset_id
                parent.children.link(collection)
        for source in sources:
            is_decal = "quixlend_decal" in source
            obj = (
                decal.make_box(source, name or asset_id)
                if is_decal
                else cast(bpy.types.Object, source.copy())
            )
            if not is_decal:
                del obj["quixlend_source"]
            collection.objects.link(obj)
            obj.location = base
            obj.location.x += x
            if is_decal:
                decal.orient(obj, obj.location, target)
                context.view_layer.update()
                full = decal.project(
                    obj,
                    {
                        *decal.overlapping(obj, context),
                        *([target.original] if target and target.type == "MESH" else []),
                    },
                )
                if full:
                    return f"Too many decals on {full[0].name}: the GPU texture limit would turn it pink"
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
            areas, key=lambda a: a.width * a.height, default=None
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
