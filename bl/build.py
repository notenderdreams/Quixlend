"""Turn a downloaded Megascans folder into a .blend of marked assets.

Runs in its own background Blender so the user's session is never touched:
    blender -b --factory-startup --python build.py -- <asset_dir> <library_root>
Standalone on purpose: no imports from the rest of the add-on.
"""
import json
import re
import sys
import uuid
from pathlib import Path

import bpy

MAP_RE = re.compile(r"_(?:\d+K_)?([A-Za-z]+?)(?:_LOD(\d+))?\.(jpe?g|png|exr)$", re.I)
MESH_RE = re.compile(r"(?:_|^)LOD(\d+)[^/]*\.fbx$", re.I)
CATALOG_NS = uuid.UUID("5f1d3c1e-6c2b-4b8e-9a51-51a1e0b7c0de")
TYPE_LABEL = {"3d": "3D Assets", "3dplant": "3D Plants", "surface": "Surfaces", "atlas": "Atlases",
              "decal": "Decals", "imperfection": "Imperfections", "brush": "Brushes"}


def find_maps(folder, lod=None):
    """{role: path}. Prefers the texture baked for `lod`, then un-suffixed, and EXR for displacement."""
    best = {}
    for path in sorted(folder.rglob("*")):
        m = MAP_RE.search(path.name)
        if not m or "preview" in path.name.lower() or "billboard" in str(path.parent).lower():
            continue
        role, map_lod, ext = m.group(1).lower(), m.group(2), m.group(3).lower()
        score = (map_lod is not None and lod is not None and int(map_lod) == lod,
                 map_lod is None,
                 ext == "exr" if role == "displacement" else ext != "exr")
        if role not in best or score > best[role][0]:
            best[role] = (score, path)
    return {role: path for role, (_, path) in best.items()}


def build_material(name, maps, displace):
    mat = bpy.data.materials.new(name)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next(n for n in nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    out = next(n for n in nodes if n.bl_idname == "ShaderNodeOutputMaterial")
    coord = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")
    coord.location, mapping.location = (-1300, 0), (-1100, 0)
    links.new(coord.outputs["UV"], mapping.inputs["Vector"])
    row = [0]

    def tex(role, color=False):
        if role not in maps:
            return None
        node = nodes.new("ShaderNodeTexImage")
        node.image = bpy.data.images.load(str(maps[role]))
        node.image.colorspace_settings.name = "sRGB" if color else "Non-Color"
        node.label = role
        node.location = (-800, 300 - 280 * row[0])
        row[0] += 1
        links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        return node

    albedo, ao = tex("albedo", color=True), tex("ao")
    if albedo and ao:
        mix = nodes.new("ShaderNodeMix")
        mix.data_type, mix.blend_type = "RGBA", "MULTIPLY"
        mix.inputs["Factor"].default_value = 1.0
        mix.location = (-400, 300)
        links.new(albedo.outputs["Color"], mix.inputs["A"])
        links.new(ao.outputs["Color"], mix.inputs["B"])
        links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])
    elif albedo:
        links.new(albedo.outputs["Color"], bsdf.inputs["Base Color"])
    for role, socket in (("roughness", "Roughness"), ("metalness", "Metallic")):
        node = tex(role)
        if node:
            links.new(node.outputs["Color"], bsdf.inputs[socket])
    opacity, translucency = tex("opacity"), tex("translucency", color=True)
    if translucency:
        # Thin leaves: light passing through, then cut out by opacity (alpha on the BSDF
        # alone would leave the translucent lobe visible in the holes).
        trans, mix = nodes.new("ShaderNodeBsdfTranslucent"), nodes.new("ShaderNodeMixShader")
        trans.location, mix.location = (0, -300), (300, 0)
        mix.inputs["Fac"].default_value = 0.35
        links.new(translucency.outputs["Color"], trans.inputs["Color"])
        links.new(bsdf.outputs["BSDF"], mix.inputs[1])
        links.new(trans.outputs["BSDF"], mix.inputs[2])
        surface = mix.outputs["Shader"]
        if opacity:
            clear, cut = nodes.new("ShaderNodeBsdfTransparent"), nodes.new("ShaderNodeMixShader")
            clear.location, cut.location = (300, -300), (550, 0)
            links.new(opacity.outputs["Color"], cut.inputs["Fac"])
            links.new(clear.outputs["BSDF"], cut.inputs[1])
            links.new(surface, cut.inputs[2])
            surface = cut.outputs["Shader"]
        out.location = (800, 0)
        links.new(surface, out.inputs["Surface"])
    elif opacity:
        links.new(opacity.outputs["Color"], bsdf.inputs["Alpha"])
    normal = tex("normal")
    if normal:
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.location = (-400, -300)
        links.new(normal.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    height = tex("displacement") if displace else None
    if height:
        disp = nodes.new("ShaderNodeDisplacement")
        disp.location = (-400, -600)
        disp.inputs["Scale"].default_value = 0.05
        links.new(height.outputs["Color"], disp.inputs["Height"])
        links.new(disp.outputs["Displacement"], out.inputs["Displacement"])
        mat.displacement_method = "BOTH"
    return mat


def import_meshes(folder):
    """Import the most detailed LOD of every variation. -> (objects, lod index)."""
    by_lod = {}
    for path in folder.rglob("*.fbx"):
        m = MESH_RE.search(path.name)
        by_lod.setdefault(int(m.group(1)) if m else 0, []).append(path)
    if not by_lod:
        return [], None
    lod = min(by_lod)
    objects = []
    for path in sorted(by_lod[lod]):
        before = set(bpy.data.objects)
        bpy.ops.wm.fbx_import(filepath=str(path))
        new = [o for o in bpy.data.objects if o not in before]
        bpy.context.view_layer.update()
        # The importer hangs meshes under a scaled "world_root" empty (cm -> m). Bake that
        # into the mesh so the asset is a single self-contained object at real size.
        meshes = [o for o in new if o.type == "MESH"]
        for obj in meshes:
            world = obj.matrix_world.copy()
            obj.parent = None
            obj.data.transform(world)
            obj.matrix_world.identity()
        for obj in new:
            if obj.type != "MESH":
                bpy.data.objects.remove(obj)
        objects += meshes
    return objects, lod


def catalog_id(library, asset_type):
    """Deterministic catalog uuid, appended to the library's catalog file when new."""
    path = f"Megascans/{TYPE_LABEL.get(asset_type, asset_type.title())}"
    cats = library / "blender_assets.cats.txt"
    text = cats.read_text() if cats.exists() else ""
    if not any(line.strip() == "VERSION 1" for line in text.splitlines()):
        text = "VERSION 1\n\n" + text  # Blender ignores the whole file without this line
        cats.write_text(text)
    for line in text.splitlines():  # reuse a catalog that already has this path
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == path:
            return parts[0]
    cid = str(uuid.uuid5(CATALOG_NS, path))
    cats.write_text(text.rstrip("\n") + f"\n{cid}:{path}:{path.replace('/', '-')}\n")
    return cid


def set_preview(datablock, folder):
    src = next(iter(sorted(folder.glob("_Thumb.jpg"))), None) or next(iter(sorted(folder.glob("*_Preview.*"))), None)
    if not src:
        return
    img = bpy.data.images.load(str(src))
    img.scale(256, 256)
    preview = datablock.preview_ensure()
    preview.image_size = (256, 256)
    preview.image_pixels_float.foreach_set(img.pixels[:])
    bpy.data.images.remove(img)


def build(folder, library):
    folder, library = Path(folder), Path(library)
    meta_file = next(iter(sorted(folder.glob("*.json"))), None)
    meta = json.loads(meta_file.read_text()) if meta_file else {}
    asset_id = meta.get("id") or folder.name
    name = (meta.get("name") or asset_id).strip()
    asset_type = (meta.get("categories") or [folder.parent.name])[0].lower()

    objects, lod = import_meshes(folder)
    maps = find_maps(folder, lod)
    if not maps:
        raise SystemExit(f"No texture maps found in {folder}")

    if objects:
        # One asset per variation: plants ship 5-10 meshes meant to be placed individually.
        mat = build_material(name, maps, displace=False)
        for i, obj in enumerate(objects, 1):
            obj.data.materials.clear()
            obj.data.materials.append(mat)
            obj.name = name if len(objects) == 1 else f"{name} {i:02d}"
        assets = objects
    elif set(maps) == {"brush"}:
        texture = bpy.data.textures.new(name, "IMAGE")
        texture.image = bpy.data.images.load(str(maps["brush"]))
        brush = bpy.data.brushes.new(name, mode="SCULPT")
        brush.texture = texture
        assets = [brush]
    else:
        assets = [build_material(name, maps, displace=asset_type == "surface")]

    catalog = catalog_id(library, asset_type)
    for asset in assets:
        asset.asset_mark()
        data = asset.asset_data
        data.author = "Quixel Megascans"
        data.description = f"Megascans {asset_id}"
        data.catalog_id = catalog
        for tag in dict.fromkeys([asset_id, *(meta.get("tags") or [])]):
            data.tags.new(str(tag))
        set_preview(asset, folder)

    blend = folder / f"{asset_id}.blend"
    bpy.data.libraries.write(str(blend), set(assets), path_remap="RELATIVE", fake_user=True, compress=True)
    print(f"QUIXLEND_BUILT {blend}")


if __name__ == "__main__":
    build(*sys.argv[sys.argv.index("--") + 1:][:2])
