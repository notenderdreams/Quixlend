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
              "brush": "Brushes"}


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


def build_group_material(name, maps, displace=False):
    """Material for any asset type: the texture work lives in a node group named like the material, exposing the tweaks
    (tint, hue, saturation, brightness, contrast, roughness, normal strength, displacement scale); the BSDF stays outside."""
    mat = bpy.data.materials.new(name)
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next(n for n in nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    out = next(n for n in nodes if n.bl_idname == "ShaderNodeOutputMaterial")
    coord, mapping = nodes.new("ShaderNodeTexCoord"), nodes.new("ShaderNodeMapping")
    coord.location, mapping.location = (-830, 0), (-610, 0)
    bsdf.location, out.location = (0, 0), (300, 0)
    links.new(coord.outputs["UV"], mapping.inputs["Vector"])

    ng = bpy.data.node_groups.new(name, "ShaderNodeTree")
    gl, gi, go = ng.links, ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
    gi.location, go.location = (-1400, 0), (1300, 0)
    face = ng.interface

    def add(label, kind, direction="INPUT", panel=None, default=None, lo=None, hi=None):
        sock = face.new_socket(label, in_out=direction, socket_type=kind, parent=panel)
        if default is not None:
            sock.default_value = default
        if lo is not None:
            sock.min_value, sock.max_value, sock.subtype = lo, hi, "FACTOR"  # a slider
        return sock

    def node(idname, x, y, **props):
        n = ng.nodes.new(idname)
        n.location = (x, y)
        for key, value in props.items():
            setattr(n, key, value)
        return n

    def mix(x, y, a, b, fac):
        n = node("ShaderNodeMix", x, y, data_type="RGBA", blend_type="MULTIPLY")
        if isinstance(fac, float):
            n.inputs["Factor"].default_value = fac
        else:
            gl.new(fac, n.inputs["Factor"])
        gl.new(a, n.inputs["A"])
        gl.new(b, n.inputs["B"])
        return n.outputs["Result"]

    add("Vector", "NodeSocketVector").hide_value = True
    color_panel = face.new_panel("Color") if "albedo" in maps else None
    surface_panel = face.new_panel("Surface") if "roughness" in maps else None
    normal_panel = face.new_panel("Normal") if "normal" in maps else None
    displace = displace and "displacement" in maps
    displace_panel = face.new_panel("Displacement") if displace else None
    row = [0]

    def tex(role, color=False):
        if role not in maps:
            return None
        n = node("ShaderNodeTexImage", -1000, 300 - 280 * row[0], label=role)
        n.image = bpy.data.images.load(str(maps[role]))
        n.image.colorspace_settings.name = "sRGB" if color else "Non-Color"
        row[0] += 1
        gl.new(gi.outputs["Vector"], n.inputs["Vector"])
        return n

    def emit(label, kind, socket):
        add(label, kind, "OUTPUT")
        gl.new(socket, go.inputs[label])

    albedo, ao = tex("albedo", color=True), tex("ao")
    if albedo:
        add("Tint", "NodeSocketColor", panel=color_panel, default=(1, 1, 1, 1))
        add("Tint Strength", "NodeSocketFloat", panel=color_panel, default=1.0, lo=0.0, hi=1.0)
        add("Hue Shift", "NodeSocketFloat", panel=color_panel, default=0.0, lo=-0.5, hi=0.5)
        add("Saturation", "NodeSocketFloat", panel=color_panel, default=1.0, lo=0.0, hi=2.0)
        add("Brightness", "NodeSocketFloat", panel=color_panel, default=0.0, lo=-1.0, hi=1.0)
        add("Contrast", "NodeSocketFloat", panel=color_panel, default=0.0, lo=-1.0, hi=1.0)
        color = albedo.outputs["Color"]
        if ao:
            color = mix(-600, 300, color, ao.outputs["Color"], 1.0)
        color = mix(-400, 300, color, gi.outputs["Tint"], gi.outputs["Tint Strength"])
        hue = node("ShaderNodeMath", -400, 100, operation="ADD")
        gl.new(gi.outputs["Hue Shift"], hue.inputs[0])
        hue.inputs[1].default_value = 0.5  # 0.5 is "no shift" for the Hue/Saturation node
        hsv = node("ShaderNodeHueSaturation", -200, 300)
        gl.new(hue.outputs["Value"], hsv.inputs["Hue"])
        gl.new(gi.outputs["Saturation"], hsv.inputs["Saturation"])
        gl.new(color, hsv.inputs["Color"])
        # Brightness: exposure-style multiply (1 + Brightness). Contrast: scale around linear mid-gray.
        # Blender's own Bright/Contrast node subtracts in linear space and crushes albedo to black.
        gain = node("ShaderNodeMath", 0, 500, operation="ADD")
        gl.new(gi.outputs["Brightness"], gain.inputs[0])
        gain.inputs[1].default_value = 1.0
        lit = mix(0, 300, hsv.outputs["Color"], gain.outputs["Value"], 1.0)
        steep = node("ShaderNodeMath", 200, 500, operation="ADD")
        gl.new(gi.outputs["Contrast"], steep.inputs[0])
        steep.inputs[1].default_value = 1.0
        below = node("ShaderNodeVectorMath", 200, 300, operation="SUBTRACT")
        gl.new(lit, below.inputs[0])
        below.inputs[1].default_value = (0.18, 0.18, 0.18)
        scaled_c = node("ShaderNodeVectorMath", 400, 300, operation="SCALE")
        gl.new(below.outputs["Vector"], scaled_c.inputs[0])
        gl.new(steep.outputs["Value"], scaled_c.inputs["Scale"])
        above = node("ShaderNodeVectorMath", 600, 300, operation="ADD")
        gl.new(scaled_c.outputs["Vector"], above.inputs[0])
        above.inputs[1].default_value = (0.18, 0.18, 0.18)
        floor = node("ShaderNodeVectorMath", 800, 300, operation="MAXIMUM")
        gl.new(above.outputs["Vector"], floor.inputs[0])
        floor.inputs[1].default_value = (0.0, 0.0, 0.0)
        emit("Base Color", "NodeSocketColor", floor.outputs["Vector"])
    roughness = tex("roughness")
    if roughness:
        add("Roughness", "NodeSocketFloat", panel=surface_panel, default=1.0, lo=0.0, hi=2.0)
        add("Roughness Bias", "NodeSocketFloat", panel=surface_panel, default=0.0, lo=-1.0, hi=1.0)
        scaled = node("ShaderNodeMath", -400, -100, operation="MULTIPLY")
        gl.new(roughness.outputs["Color"], scaled.inputs[0])
        gl.new(gi.outputs["Roughness"], scaled.inputs[1])
        biased = node("ShaderNodeMath", -200, -100, operation="ADD", use_clamp=True)
        gl.new(scaled.outputs["Value"], biased.inputs[0])
        gl.new(gi.outputs["Roughness Bias"], biased.inputs[1])
        emit("Roughness", "NodeSocketFloat", biased.outputs["Value"])
    if metal := tex("metalness"):
        emit("Metallic", "NodeSocketFloat", metal.outputs["Color"])
    if opacity := tex("opacity"):
        emit("Alpha", "NodeSocketFloat", opacity.outputs["Color"])
    if translucency := tex("translucency", color=True):
        emit("Translucency", "NodeSocketColor", translucency.outputs["Color"])
    if normal := tex("normal"):
        add("Normal Strength", "NodeSocketFloat", panel=normal_panel, default=1.0, lo=0.0, hi=2.0)
        nmap = node("ShaderNodeNormalMap", -400, -400)
        gl.new(normal.outputs["Color"], nmap.inputs["Color"])
        gl.new(gi.outputs["Normal Strength"], nmap.inputs["Strength"])
        emit("Normal", "NodeSocketVector", nmap.outputs["Normal"])
    if displace:
        height = tex("displacement")
        add("Displacement Scale", "NodeSocketFloat", panel=displace_panel, default=0.05, lo=0.0, hi=0.5)
        disp = node("ShaderNodeDisplacement", -400, -700)
        gl.new(height.outputs["Color"], disp.inputs["Height"])
        gl.new(gi.outputs["Displacement Scale"], disp.inputs["Scale"])
        emit("Displacement", "NodeSocketVector", disp.outputs["Displacement"])

    group = nodes.new("ShaderNodeGroup")
    group.node_tree, group.name, group.label, group.location = ng, name, name, (-330, 0)
    group.width = 260
    links.new(mapping.outputs["Vector"], group.inputs["Vector"])
    for label, socket in (("Base Color", "Base Color"), ("Roughness", "Roughness"), ("Metallic", "Metallic"),
                          ("Normal", "Normal")):
        if label in group.outputs:
            links.new(group.outputs[label], bsdf.inputs[socket])
    surface = bsdf.outputs["BSDF"]
    if "Translucency" in group.outputs:
        # Thin leaves: light passing through, then cut out by alpha (alpha on the BSDF alone would
        # leave the translucent lobe visible in the holes).
        trans, blend = nodes.new("ShaderNodeBsdfTranslucent"), nodes.new("ShaderNodeMixShader")
        trans.location, blend.location = (0, -300), (300, 0)
        blend.inputs["Fac"].default_value = 0.35
        links.new(group.outputs["Translucency"], trans.inputs["Color"])
        links.new(surface, blend.inputs[1])
        links.new(trans.outputs["BSDF"], blend.inputs[2])
        surface = blend.outputs["Shader"]
        if "Alpha" in group.outputs:
            clear, cut = nodes.new("ShaderNodeBsdfTransparent"), nodes.new("ShaderNodeMixShader")
            clear.location, cut.location = (300, -300), (550, 0)
            links.new(group.outputs["Alpha"], cut.inputs["Fac"])
            links.new(clear.outputs["BSDF"], cut.inputs[1])
            links.new(surface, cut.inputs[2])
            surface = cut.outputs["Shader"]
        out.location = (800, 0)
        links.new(surface, out.inputs["Surface"])
    elif "Alpha" in group.outputs:
        links.new(group.outputs["Alpha"], bsdf.inputs["Alpha"])
    if "Displacement" in group.outputs:
        links.new(group.outputs["Displacement"], out.inputs["Displacement"])
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


def catalog_id(library, asset_type, *sub):
    """Catalog uuid for Type/Category/Sub, creating it and any missing parents in the library's catalog file."""
    names = [TYPE_LABEL.get(asset_type, asset_type.title())]
    names += [str(n).strip().replace("/", "-").replace(":", "-").title() for n in sub if n and str(n).strip()]
    cats = library / "blender_assets.cats.txt"
    text = cats.read_text() if cats.exists() else ""
    if not any(line.strip() == "VERSION 1" for line in text.splitlines()):
        text = "VERSION 1\n\n" + text  # Blender ignores the whole file without this line
    known = {line.split(":")[1]: line.split(":")[0] for line in text.splitlines() if line.count(":") >= 2}
    for depth in range(1, len(names) + 1):
        path = "/".join(names[:depth])
        if path not in known:
            known[path] = str(uuid.uuid5(CATALOG_NS, path))
            text = text.rstrip("\n") + f"\n{known[path]}:{path}:{path}\n"
    cats.write_text(text)
    return known["/".join(names)]


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
        mat = build_group_material(name, maps)
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
        assets = [build_group_material(name, maps, displace=asset_type == "surface")]

    catalog = catalog_id(library, asset_type, *(meta.get("categories") or [])[1:3])
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
