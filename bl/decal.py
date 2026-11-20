"""Decals as a projection volume: an Empty cube you can move, rotate and scale. Whatever mesh the cube overlaps shows
the decal, because each receiving material gets a decal line (see below) whose links mask by their cube's space.
Nothing is bound to one object, and nothing needs pressing: a handler notices a box that moved, was copied or was
deleted and brings the receivers in line (see `sync`)."""

# pyright: reportOptionalSubscript=false, reportArgumentType=false
# (node sockets are looked up by name; the stubs type every lookup as optional)
from typing import cast

import bpy
from bpy.app.handlers import persistent
from mathutils import Vector

from .store import Result, state

KEY = "quixlend_decal_box"  # custom property on the box: its projector node group
HALF = 0.5  # the cube spans +-HALF in its own space, so `scale` is its size in metres
OPACITY = "Opacity"  # custom property on the box, a slider in its panel
FLIP_SIDE = "Flip Side"  # custom property on the box, a checkbox: with One Side, the side opposite its +Z
ONE_SIDE = "One Side"  # custom property on the box, a toggle: show only on the side its +Z points to
FADE = 0.6  # the decal fades out over the outer part of the box's depth: from FADE * HALF to HALF


def decal_size(source):
    """(width, height, depth) in metres of a decal template."""
    return tuple(max(c[i] for c in source.bound_box) - min(c[i] for c in source.bound_box) for i in range(3))


def make_box(source, name):
    """The projection volume for template `source`, at its size; rescale it by hand to reach deeper or wider."""
    box = bpy.data.objects.new(name, None)
    box.empty_display_type, box.empty_display_size = "CUBE", HALF
    box.scale = decal_size(source)
    box[OPACITY] = 1.0
    box[ONE_SIDE] = False
    box[FLIP_SIDE] = False
    box.id_properties_ui(OPACITY).update(min=0.0, max=1.0, subtype="FACTOR")  # pyright: ignore[reportOptionalMemberAccess]
    box[KEY] = build_projector(box, source)
    return box


def orientation(location, target):
    """Euler rotation standing a box's +Z out of the surface of `target` (a mesh) at `location`; unrotated without."""
    normal = Vector((0, 0, 1))
    if target and target.type == "MESH":
        target = target.original
        inv = target.matrix_world.inverted()
        found, _, local_normal, _ = target.closest_point_on_mesh(inv @ Vector(location))
        if found:
            normal = (inv.to_3x3().transposed() @ local_normal).normalized()
    if abs(normal.z) > 0.999:  # floor/ceiling: no "up" to follow, so just tip +Z onto the normal
        return Vector((0, 0, 1)).rotation_difference(normal).to_euler()
    return normal.to_track_quat("Z", "Y").to_euler()  # wall: keep the decal's +Y pointing up


def orient(box, location, target):
    """Centre the box on `location`, facing out of the surface of `target`."""
    box.rotation_euler = orientation(location, target)
    box.location = location


# ---- the decal line --------------------------------------------------------------------------------------------
# Every receiving material gets one line, kept apart from its own nodes:
#   Start -> Decal A -> Decal B -> ... -> Merge
# The line carries colour, roughness, normal (a tilt) and coverage (alpha). Merge takes the material's shader and the
# line and gives one shader for the Material Output: one Principled BSDF fed by the line, mixed over the material's
# shader by the coverage. So a material pays one BSDF however many decals it has, and any shader works.

LINE = (
    ("Color", "NodeSocketColor"),
    ("Roughness", "NodeSocketFloat"),
    ("Normal", "NodeSocketVector"),
    ("Alpha", "NodeSocketFloat"),
)
START, MERGE = "Quixlend Decal Start", "Quixlend Decal Merge"
TEXTURES = 13  # per material: Metal allows 16 and EEVEE keeps ~3 for itself; past it the material draws pink


def _new_group(name, inputs, outputs):
    tree = cast(bpy.types.ShaderNodeTree, bpy.data.node_groups.new(name, "ShaderNodeTree"))
    face = tree.interface
    assert face
    for label, kind in inputs:
        face.new_socket(label, in_out="INPUT", socket_type=kind)
    for label, kind in outputs:
        face.new_socket(label, in_out="OUTPUT", socket_type=kind)
    return tree, tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")


class _Sections:
    """Records which nodes were made for which part of a group, so `_arrange` can lay each part out as a labelled frame."""

    def __init__(self, tree):
        self.tree, self.parts, self._open = tree, [], None

    def begin(self, label):
        self.end()
        self._open = (label, len(self.tree.nodes))

    def end(self):
        if self._open:
            label, first = self._open
            nodes = list(self.tree.nodes)[first:]
            if nodes:
                self.parts.append((label, nodes))
            self._open = None


def _height(node):
    """A drawn node's height, estimated (nodes have none until they are drawn). Generous, so nodes never touch."""
    rows = sum(1 for s in [*node.inputs, *node.outputs] if s.enabled and not s.hide)
    extra = {  # dropdowns, checkboxes and previews that are not sockets
        "ShaderNodeTexImage": 190,
        "ShaderNodeTexCoord": 110,
        "ShaderNodeMath": 80,
        "ShaderNodeVectorMath": 80,
        "ShaderNodeMix": 110,
        "ShaderNodeMapRange": 130,
        "ShaderNodeVectorRotate": 90,
        "ShaderNodeSeparateColor": 50,
        "ShaderNodeBsdfPrincipled": 420,
    }
    return 50 + 24 * rows + extra.get(node.bl_idname, 0)


def _levels(nodes):
    """{node: how many links it is from the start of `nodes`}, counting only links between them."""
    inside, depth = set(nodes), {}

    def level(n):
        if n not in depth:
            depth[n] = 0  # guards a cycle
            before = [link.from_node for i in n.inputs for link in i.links if link.from_node in inside]
            depth[n] = 1 + max((level(b) for b in before), default=-1)
        return depth[n]

    for n in nodes:
        level(n)
    return depth


def _near_inputs(column, centre):
    """`column` ordered by the average height of the already placed nodes feeding each one: keeps links short."""

    def height(n):
        feeds = [centre[link.from_node] for i in n.inputs for link in i.links if link.from_node in centre]
        return -sum(feeds) / len(feeds) if feeds else 0.0

    return sorted(column, key=height)  # stable: nodes with no placed input keep the order they were made in


def _arrange(tree, gi, go, sections):
    """Tidy layout: each part is a labelled frame; inside it, nodes sit in columns by how far they are from the
    part's start, ordered to keep links short. Parts stack top to bottom, Group Input on the left, Output on the right."""
    gap_x, gap_y, gap_part = 90, 40, 140
    tints = (
        (0.22, 0.30, 0.42),
        (0.42, 0.32, 0.20),
        (0.22, 0.38, 0.28),
        (0.34, 0.25, 0.42),
    )  # one per part, muted
    top, right = 0.0, 0.0
    for part, (label, nodes) in enumerate(sections):
        for n in nodes:  # wide enough to read the label
            n.width = max(n.width, min(260.0, 28 + 7.5 * len(n.label)))
        depth = _levels(nodes)
        columns: dict = {}
        for n in nodes:
            columns.setdefault(depth[n], []).append(n)
        x, tallest, centre = 0.0, 0.0, {}
        for d in sorted(columns):
            y = top
            for n in _near_inputs(columns[d], centre):
                n.location = (x, y)
                centre[n] = y - _height(n) / 2
                y -= _height(n) + gap_y
            tallest = max(tallest, top - y)
            x += max(n.width for n in columns[d]) + gap_x
        frame = tree.nodes.new("NodeFrame")
        frame.label, frame.label_size, frame.shrink = label, 22, True
        frame.use_custom_color, frame.color = True, tints[part % len(tints)]
        for n in nodes:
            n.parent = frame
        top -= tallest + gap_part
        right = max(right, x)
    middle = top / 2
    if gi:
        gi.location = (-gi.width - gap_part, middle)
    if go:
        go.location = (right + gap_part - gap_x, middle)


def _shared(name) -> bpy.types.ShaderNodeTree:
    """The Start or Merge group, made once per file."""
    found = next((g for g in bpy.data.node_groups if g.get(KEY) == name), None)
    if found:
        return cast(bpy.types.ShaderNodeTree, found)
    if name == START:  # an empty line: nothing covered. Colour and roughness are premultiplied by coverage: 0
        tree, _, go = _new_group(name, [], LINE)
        nodes, links = tree.nodes, tree.links
        made = _Sections(tree)
        made.begin("Empty Line")
        black, rough, tilt, alpha = (
            nodes.new("ShaderNodeRGB"),
            nodes.new("ShaderNodeValue"),
            nodes.new("ShaderNodeCombineXYZ"),
            nodes.new("ShaderNodeValue"),
        )
        black.label, rough.label, tilt.label, alpha.label = (
            "No Colour",
            "No Roughness",
            "No Tilt",
            "No Coverage",
        )
        black.outputs[0].default_value = (0, 0, 0, 1)  # pyright: ignore[reportAttributeAccessIssue]
        rough.outputs[0].default_value, alpha.outputs[0].default_value = 0.0, 0.0  # pyright: ignore[reportAttributeAccessIssue]
        links.new(black.outputs[0], go.inputs["Color"])
        links.new(rough.outputs[0], go.inputs["Roughness"])
        links.new(tilt.outputs["Vector"], go.inputs["Normal"])
        links.new(alpha.outputs[0], go.inputs["Alpha"])
        made.end()
        _arrange(tree, None, go, made.parts)
    else:  # takes the material's shader and the line; gives one shader: the line's surface laid over it where it covers
        tree, gi, go = _new_group(
            name, [("Shader", "NodeSocketShader"), *LINE], [("Shader", "NodeSocketShader")]
        )
        nodes, links = tree.nodes, tree.links
        made = _Sections(tree)

        def math(operation, label, kind="ShaderNodeMath"):
            n = nodes.new(kind)
            n.operation, n.label = operation, label  # pyright: ignore[reportAttributeAccessIssue]
            return n

        # the line's colour and roughness come premultiplied by coverage (each decal is mix(below, own, a)):
        # divide it back out, or partial coverage (soft edges, opacity) would count twice
        made.begin("Undo Premultiply")
        floor = math("MAXIMUM", "Coverage, Never 0")
        links.new(gi.outputs["Alpha"], floor.inputs[0])
        floor.inputs[1].default_value = 1e-4  # pyright: ignore[reportAttributeAccessIssue]
        inverse = math("DIVIDE", "1 / Coverage")
        inverse.inputs[0].default_value = 1.0  # pyright: ignore[reportAttributeAccessIssue]
        links.new(floor.outputs[0], inverse.inputs[1])
        color = math("SCALE", "Colour / Coverage", "ShaderNodeVectorMath")
        links.new(gi.outputs["Color"], color.inputs[0])
        links.new(inverse.outputs[0], color.inputs["Scale"])
        rough = math("MULTIPLY", "Roughness / Coverage")
        links.new(gi.outputs["Roughness"], rough.inputs[0])
        links.new(inverse.outputs[0], rough.inputs[1])
        made.begin("Normal")
        tilt = math("ADD", "Surface + Decal Tilt", "ShaderNodeVectorMath")
        links.new(nodes.new("ShaderNodeNewGeometry").outputs["Normal"], tilt.inputs[0])
        links.new(gi.outputs["Normal"], tilt.inputs[1])
        unit = math("NORMALIZE", "Unit Normal", "ShaderNodeVectorMath")
        links.new(tilt.outputs["Vector"], unit.inputs[0])
        made.begin("Shader")
        bsdf, over = nodes.new("ShaderNodeBsdfPrincipled"), nodes.new("ShaderNodeMixShader")
        bsdf.label, over.label = "Decal Surface", "Decal Over Material"
        links.new(color.outputs["Vector"], bsdf.inputs["Base Color"])
        links.new(rough.outputs[0], bsdf.inputs["Roughness"])
        links.new(unit.outputs["Vector"], bsdf.inputs["Normal"])
        links.new(gi.outputs["Alpha"], over.inputs["Fac"])
        links.new(gi.outputs["Shader"], over.inputs[1])
        links.new(bsdf.outputs["BSDF"], over.inputs[2])
        links.new(over.outputs["Shader"], go.inputs["Shader"])
        made.end()
        _arrange(tree, gi, go, made.parts)
    tree[KEY] = name
    return tree


def _drive_rotation(socket, box):
    """Keep `socket` (an Euler) equal to the box's world rotation, so tangent normals turn with the box."""
    for axis in range(3):
        driver = socket.driver_add("default_value", axis).driver
        driver.type = "AVERAGE"  # a plain variable: no Python, so it runs without "Auto Run Scripts"
        var = driver.variables.new()
        var.type = "TRANSFORMS"
        target = var.targets[0]
        target.id, target.transform_type, target.transform_space = box, f"ROT_{'XYZ'[axis]}", "WORLD_SPACE"


def _drive_property(socket, box, prop):
    """Keep `socket` equal to the box's custom property `prop`."""
    driver = socket.driver_add("default_value").driver
    driver.type = "AVERAGE"
    var = driver.variables.new()
    var.type = "SINGLE_PROP"
    var.targets[0].id, var.targets[0].data_path = box, f'["{prop}"]'


def build_projector(box, source):
    """One link of the decal line: inside `box`'s volume it lays the decal's colour, roughness and normal over what
    came down the line, and adds its coverage. `box` is read by a Texture Coordinate node (and its rotation by
    drivers), so the group follows the box wherever it moves."""
    color, surface = (
        source.get("quixlend_decal_color"),
        source.get("quixlend_decal_surface"),
    )  # see build.pack_decal

    tree, gi, go = _new_group(f"Decal {box.name}", LINE, LINE)
    tree[KEY] = True  # tells a decal's group from any other
    nodes, links = tree.nodes, tree.links
    made = _Sections(tree)

    def node(idname, label, **props):
        n = nodes.new(idname)
        n.label = label
        for key, value in props.items():
            setattr(n, key, value)
        return n

    # --- where the box is: 1 inside it, 0 outside, fading out along its depth, times the Opacity slider
    made.begin("Box Mask")
    coord = cast(bpy.types.ShaderNodeTexCoord, node("ShaderNodeTexCoord", "Box Space"))
    coord.object = box
    absolute = node("ShaderNodeVectorMath", "Distance From Centre", operation="ABSOLUTE")
    links.new(coord.outputs["Object"], absolute.inputs[0])
    inside = node("ShaderNodeSeparateXYZ", "X, Y, Z")
    links.new(absolute.outputs["Vector"], inside.inputs["Vector"])
    cover = None
    for axis, label in (
        ("X", "Within Width"),
        ("Y", "Within Height"),
    ):  # inside the sides: |x|, |y| under HALF
        cut = node("ShaderNodeMath", label, operation="LESS_THAN")
        links.new(inside.outputs[axis], cut.inputs[0])
        cut.inputs[1].default_value = HALF  # pyright: ignore[reportAttributeAccessIssue]
        if cover:
            both = node("ShaderNodeMath", "Within Both", operation="MULTIPLY")
            links.new(cover, both.inputs[0])
            links.new(cut.outputs["Value"], both.inputs[1])
            cover = both.outputs["Value"]
        else:
            cover = cut.outputs["Value"]
    # along the depth it fades out softly instead of cutting hard where a curved surface leaves the box
    fade = node("ShaderNodeMapRange", "Depth Fade", interpolation_type="SMOOTHSTEP")
    links.new(inside.outputs["Z"], fade.inputs["Value"])
    for socket, value in (("From Min", FADE * HALF), ("From Max", HALF), ("To Min", 1.0), ("To Max", 0.0)):
        fade.inputs[socket].default_value = value  # pyright: ignore[reportAttributeAccessIssue]
    opacity = node("ShaderNodeValue", "Opacity (from the box)")
    _drive_property(opacity.outputs[0], box, OPACITY)
    # one side: Blender turns the shading normal towards the viewer, so it points the way the box's +Z does
    # only for a viewer on that side
    geometry = node("ShaderNodeNewGeometry", "Surface Side")
    axis = node("ShaderNodeVectorRotate", "Box +Z", rotation_type="EULER_XYZ")
    axis.inputs["Vector"].default_value = (0, 0, 1)  # pyright: ignore[reportAttributeAccessIssue]
    _drive_rotation(axis.inputs["Rotation"], box)
    facing = node("ShaderNodeVectorMath", "Facing the Box", operation="DOT_PRODUCT")
    links.new(geometry.outputs["Normal"], facing.inputs[0])
    links.new(axis.outputs["Vector"], facing.inputs[1])
    flip = node("ShaderNodeValue", "Flip Side (from the box)")
    _drive_property(flip.outputs[0], box, FLIP_SIDE)
    way = node("ShaderNodeMath", "Side Sign", operation="MULTIPLY_ADD")  # 1, or -1 when flipped
    links.new(flip.outputs[0], way.inputs[0])
    way.inputs[1].default_value, way.inputs[2].default_value = -2.0, 1.0  # pyright: ignore[reportAttributeAccessIssue]
    turned = node("ShaderNodeMath", "Facing x Sign", operation="MULTIPLY")
    links.new(facing.outputs["Value"], turned.inputs[0])
    links.new(way.outputs["Value"], turned.inputs[1])
    toward = node("ShaderNodeMath", "Faces the Side", operation="GREATER_THAN")
    links.new(turned.outputs["Value"], toward.inputs[0])
    toward.inputs[1].default_value = 0.0  # pyright: ignore[reportAttributeAccessIssue]
    miss = node("ShaderNodeMath", "Facing - 1", operation="SUBTRACT")
    links.new(toward.outputs["Value"], miss.inputs[0])
    miss.inputs[1].default_value = 1.0  # pyright: ignore[reportAttributeAccessIssue]
    toggle = node("ShaderNodeValue", "One Side (from the box)")
    _drive_property(toggle.outputs[0], box, ONE_SIDE)
    side = node("ShaderNodeMath", "Side Mask", operation="MULTIPLY_ADD")  # 1 - one_side * (1 - facing)
    links.new(miss.outputs["Value"], side.inputs[0])
    links.new(toggle.outputs[0], side.inputs[1])
    side.inputs[2].default_value = 1.0  # pyright: ignore[reportAttributeAccessIssue]
    for factor, label in (
        (fade.outputs["Result"], "x Depth Fade"),
        (opacity.outputs[0], "x Opacity"),
        (side.outputs["Value"], "x Side"),
    ):
        both = node("ShaderNodeMath", label, operation="MULTIPLY")
        links.new(cover, both.inputs[0])
        links.new(factor, both.inputs[1])
        cover = both.outputs["Value"]

    # --- the decal's images, laid over the box's x/y mapped onto 0..1
    made.begin("Decal Images")
    shifted = node("ShaderNodeVectorMath", "Box to UV", operation="ADD")
    links.new(coord.outputs["Object"], shifted.inputs[0])
    shifted.inputs[1].default_value = (HALF, HALF, 0)  # pyright: ignore[reportAttributeAccessIssue]

    def sample(image, label):
        n = cast(
            bpy.types.ShaderNodeTexImage, node("ShaderNodeTexImage", label, image=image, extension="CLIP")
        )
        links.new(shifted.outputs["Vector"], n.inputs["Vector"])
        return n

    colors = sample(color, "Colour + Opacity") if color else None
    surfaces = sample(surface, "Normal + Roughness") if surface else None

    def over(name, label, data_type, value):
        """`value` laid over what came down the line, by the coverage."""
        blend = node("ShaderNodeMix", label, data_type=data_type)
        a, b, out = (6, 7, 2) if data_type == "RGBA" else (2, 3, 0)  # this data type's sockets
        links.new(cover, blend.inputs[0])
        links.new(gi.outputs[name], blend.inputs[a])
        if isinstance(value, float):
            blend.inputs[b].default_value = value  # pyright: ignore[reportAttributeAccessIssue]
        else:
            links.new(value, blend.inputs[b])
        links.new(blend.outputs[out], go.inputs[name])

    # --- colour, and how much of the surface the decal covers
    made.begin("Colour and Coverage")
    if colors:
        faded = node("ShaderNodeMath", "x Image Opacity", operation="MULTIPLY")
        links.new(cover, faded.inputs[0])
        links.new(colors.outputs["Alpha"], faded.inputs[1])
        cover = faded.outputs["Value"]
        over("Color", "Colour Over", "RGBA", colors.outputs["Color"])
    else:
        links.new(gi.outputs["Color"], go.inputs["Color"])
    over("Alpha", "Coverage Adds Up", "FLOAT", 1.0)  # coverage adds up: a + below * (1 - a)

    # --- roughness, and the normal: its tilt, turned into world space by the box's rotation, added to what is there
    made.begin("Roughness and Normal")
    if surfaces:
        channels = node("ShaderNodeSeparateColor", "Unpack Normal and Roughness")
        links.new(surfaces.outputs["Color"], channels.inputs["Color"])
        over("Roughness", "Roughness Over", "FLOAT", channels.outputs["Blue"])
        flat = node("ShaderNodeCombineXYZ", "Tilt X, Y")
        links.new(channels.outputs["Red"], flat.inputs["X"])
        links.new(channels.outputs["Green"], flat.inputs["Y"])
        tangent = node("ShaderNodeVectorMath", "Map to -1..1", operation="MULTIPLY_ADD")
        links.new(flat.outputs["Vector"], tangent.inputs[0])
        tangent.inputs[1].default_value = (2, 2, 0)  # pyright: ignore[reportAttributeAccessIssue]
        tangent.inputs[2].default_value = (-1, -1, 0)  # pyright: ignore[reportAttributeAccessIssue]
        turn = node("ShaderNodeVectorRotate", "Turn With Box", rotation_type="EULER_XYZ")
        links.new(tangent.outputs["Vector"], turn.inputs["Vector"])
        _drive_rotation(turn.inputs["Rotation"], box)
        scaled = node("ShaderNodeVectorMath", "x Coverage", operation="SCALE")
        links.new(turn.outputs["Vector"], scaled.inputs[0])
        links.new(cover, scaled.inputs["Scale"])
        tilted = node(
            "ShaderNodeVectorMath", "Add to Tilt", operation="ADD"
        )  # Merge turns the tilts into a normal
        links.new(gi.outputs["Normal"], tilted.inputs[0])
        links.new(scaled.outputs["Vector"], tilted.inputs[1])
        links.new(tilted.outputs["Vector"], go.inputs["Normal"])
    else:
        links.new(gi.outputs["Roughness"], go.inputs["Roughness"])
        links.new(gi.outputs["Normal"], go.inputs["Normal"])
    made.end()
    _arrange(tree, gi, go, made.parts)
    return tree


def overlapping(box, context):
    """Visible mesh objects whose bounding box reaches into the box's volume."""
    inv = box.matrix_world.inverted_safe()  # a box scaled to 0 on some axis covers nothing, without raising
    found = []
    for obj in context.view_layer.objects:
        if obj.type != "MESH" or not obj.visible_get():
            continue
        local = [inv @ (obj.matrix_world @ Vector(c)) for c in obj.bound_box]
        if all(min(p[i] for p in local) <= HALF and max(p[i] for p in local) >= -HALF for i in range(3)):
            found.append(obj)
    return found


def _tagged(tree, tag):
    return next((n for n in tree.nodes if n.get(KEY) == tag), None)


def _merge(tree):
    """The material's Merge node, setting up Start -> Merge after its shader the first time: the shader that fed the
    Material Output goes into Merge, and Merge's one shader goes to the output."""
    merge = _tagged(tree, MERGE)
    if merge:
        return merge
    outputs = [
        n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial" and n.inputs["Surface"].links
    ]
    if not outputs:
        return None
    source = outputs[0].inputs["Surface"].links[0].from_socket
    start = cast(bpy.types.ShaderNodeGroup, tree.nodes.new("ShaderNodeGroup"))
    merge = cast(bpy.types.ShaderNodeGroup, tree.nodes.new("ShaderNodeGroup"))
    start.node_tree, merge.node_tree = _shared(START), _shared(MERGE)
    start[KEY], merge[KEY] = START, MERGE
    start.label, merge.label = "Decals", "Merge Decals"
    x, y = outputs[0].location.x, source.node.location.y  # Merge level with the shader that feeds it
    start.location, merge.location = (x - 900, y + 400), (x, y)  # the line above
    for out in outputs:  # shift the outputs right to make room
        out.location.x += 250
    tree.links.new(source, merge.inputs["Shader"])
    for out in outputs:
        if out.inputs["Surface"].links[0].from_socket == source:
            tree.links.new(merge.outputs["Shader"], out.inputs["Surface"])
    for name, _ in LINE:
        tree.links.new(start.outputs[name], merge.inputs[name])
    return merge


def _images(tree):
    """Every image a node tree samples, through its groups: what counts against the per-material texture cap."""
    found = set()
    for n in tree.nodes:
        if n.bl_idname == "ShaderNodeTexImage" and getattr(n, "image", None):
            found.add(n.image)
        elif n.bl_idname == "ShaderNodeGroup" and getattr(n, "node_tree", None):
            found |= _images(n.node_tree)
    return found


def project(box, objects):
    """Add the box's decal to the end of the decal line of every material on `objects` (once per material).
    Returns the materials it left out because the decal's textures would go past the GPU's cap."""
    group = box[KEY]
    full = []
    for obj in objects:
        slots = [s for s in obj.material_slots if s.material]
        if not slots:
            obj.data.materials.append(bpy.data.materials.new(obj.name))  # pyright: ignore[reportAttributeAccessIssue]
            slots = list(obj.material_slots)
        for slot in slots:
            tree = slot.material.node_tree
            if slot.material.library or tree is None or _layers(tree, group):
                continue
            if len(_images(tree) | _images(group)) > TEXTURES:
                full.append(slot.material)
                continue
            merge = _merge(tree)
            if merge is None:
                continue
            layer = cast(bpy.types.ShaderNodeGroup, tree.nodes.new("ShaderNodeGroup"))
            layer.node_tree, layer.label = group, "Decal"
            layer[KEY] = True
            last = merge.inputs["Color"].links[0].from_node
            assert last  # the line always reaches Merge
            layer.location = (last.location.x + 220, merge.location.y + 400)
            for name, _ in LINE:
                tree.links.new(merge.inputs[name].links[0].from_socket, layer.inputs[name])
                tree.links.new(layer.outputs[name], merge.inputs[name])
    return full


def _coord(group):
    return cast(
        bpy.types.ShaderNodeTexCoord, next(n for n in group.nodes if n.bl_idname == "ShaderNodeTexCoord")
    )


def _layers(tree, group):
    """The decal's layer nodes in `tree`, by the group they use (a name can change, the group cannot)."""
    return [n for n in tree.nodes if n.get(KEY) is True and getattr(n, "node_tree", None) == group]


def _bridge(tree, node, pairs):
    """Remove `node`, joining what fed each input of `pairs` to what each matching output fed."""
    for into, out in pairs:
        source = node.inputs[into].links[0].from_socket if node.inputs[into].links else None
        for link in list(node.outputs[out].links):
            if source:
                tree.links.new(source, link.to_socket)
    tree.nodes.remove(node)


def unproject(material, group):
    """Take the decal out of `material`'s line; with no decal left, take the line out too."""
    tree = material.node_tree
    if tree is None:
        return
    for layer in _layers(tree, group):
        _bridge(
            tree, layer, [(i.name, i.name) for i in layer.inputs if i.name in layer.outputs]
        )  # also older layouts
    if not any(n.get(KEY) is True for n in tree.nodes):
        if merge := _tagged(tree, MERGE):
            _bridge(tree, merge, [("Shader", "Shader")])
            for out in (n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"):
                out.location.x -= 250
        if start := _tagged(tree, START):
            tree.nodes.remove(start)


def _gone(obj):
    """Deleted: the Delete operator only unlinks the box (our node still refers to it), so look for any scene."""
    return obj is None or not obj.users_scene


def own(box):
    """Make sure `box` has its own projector: a copy of a box (Shift+D) arrives pointing at the original's group."""
    group = box[KEY]
    coord = _coord(group)
    if _gone(coord.object):  # the box it was made for is gone: take it over
        coord.object = box
    elif coord.object is not box:
        box[KEY] = group = group.copy()
        group.name = f"Decal {box.name}"
        _coord(group).object = box
        for fcurve in group.animation_data.drivers if group.animation_data else []:
            for var in fcurve.driver.variables:
                var.targets[0].id = box


def sync(box, context):
    """Show the decal on exactly the meshes the box overlaps now, and nowhere else."""
    own(box)
    group = box[KEY]
    receivers = overlapping(box, context)
    full = project(box, receivers)
    here = {s.material for o in receivers for s in o.material_slots if s.material}
    for mat in bpy.data.materials:  # a box moved away from a mesh: drop its layer there
        if mat not in here and mat.node_tree and _layers(mat.node_tree, group):
            unproject(mat, group)
    return full


def sweep():
    """Remove deleted boxes' layers and groups, and the boxes themselves, which only our nodes kept alive."""
    for group in [g for g in bpy.data.node_groups if g.get(KEY) is True]:  # not Start/Merge
        box = _coord(group).object
        if not _gone(box):
            continue
        for mat in bpy.data.materials:
            unproject(mat, group)
        if box is not None and not box.users_scene:
            bpy.data.objects.remove(box)
        if not group.users:
            bpy.data.node_groups.remove(group)


_dirty: set[str | None] = set()  # boxes changed since the last sync (None: something was deleted)
_seen = [0]  # the scene's object count at the last update, to notice a deletion


def _flush():
    names = {*_dirty}
    _dirty.clear()
    if None in names:
        sweep()
    for name in names:
        box = bpy.data.objects.get(name) if name else None
        if box and KEY in box and box.name in bpy.context.view_layer.objects:
            sync(box, bpy.context)


_snap: dict = {}  # the user's snap settings, kept while a decal box is the active object


def _snap_to_surfaces(scene, view_layer):
    """While a decal box is active and selected, snap it to faces and turn it with them (G slides it over surfaces);
    give the user's own snap settings back when it no longer is."""
    obj = view_layer.objects.active
    tools = scene.tool_settings
    active = obj is not None and KEY in obj and obj.select_get(view_layer=view_layer)
    if active and not _snap:
        _snap.update(
            use_snap=tools.use_snap,
            snap_elements=set(tools.snap_elements),
            use_snap_align_rotation=tools.use_snap_align_rotation,
        )
        kinds = {i.identifier for i in tools.bl_rna.properties["snap_elements"].enum_items}  # pyright: ignore[reportAttributeAccessIssue]
        tools.use_snap, tools.use_snap_align_rotation = True, True
        tools.snap_elements = {"FACE_PROJECT" if "FACE_PROJECT" in kinds else "FACE"}
    elif not active and _snap:
        for key, value in _snap.items():
            setattr(tools, key, value)
        _snap.clear()


@persistent
def on_update(scene, depsgraph):
    """Cheap: only notes what changed, and syncs shortly after the changes stop (a drag is many updates)."""
    _snap_to_surfaces(scene, depsgraph.view_layer)
    for update in depsgraph.updates:
        obj = update.id
        if (
            isinstance(obj, bpy.types.Object)
            and KEY in obj
            and (update.is_updated_transform or update.is_updated_geometry)
        ):
            _dirty.add(obj.name)
    count = len(scene.objects)
    if count < _seen[0]:
        _dirty.add(None)
    _seen[0] = count
    if _dirty and not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush, first_interval=0.4)


@persistent
def on_restore(*_):
    """After undo, redo or opening a file the data can be a snapshot taken before a sweep: sweep again."""
    _dirty.add(None)
    if not bpy.app.timers.is_registered(_flush):
        bpy.app.timers.register(_flush, first_interval=0.1)


HANDLERS = (
    (bpy.app.handlers.depsgraph_update_post, on_update),
    (bpy.app.handlers.undo_post, on_restore),
    (bpy.app.handlers.redo_post, on_restore),
    (bpy.app.handlers.load_post, on_restore),
)


def editing(context):
    """The decal box the sidebar should manage: the active object, unless the user went back to the assets."""
    box = context.object
    if box is None or KEY not in box:
        state.back = None
        return None
    return None if state.back == box.name else box


def _redraw(context):
    for area in context.window.screen.areas:
        area.tag_redraw()


class QUIXLEND_OT_decal_back(bpy.types.Operator):
    bl_idname = "quixlend.decal_back"
    bl_label = "Back to Assets"
    bl_description = "Leave the decal manager and show the asset browser"

    def execute(self, context: bpy.types.Context) -> Result:
        state.back = context.object.name if context.object else None
        _redraw(context)
        return {"FINISHED"}


def draw_manager(layout, context, box):
    """The sidebar's decal manager, drawn in place of the asset browser."""
    row = layout.row(align=True)
    row.operator("quixlend.decal_back", text="", icon="BACK")
    row.label(text=box.name, icon="OUTLINER_OB_EMPTY")
    if OPACITY in box:
        layout.prop(box, f'["{OPACITY}"]', text="Opacity", slider=True)
    if ONE_SIDE in box:
        row = layout.row()
        row.prop(box, f'["{ONE_SIDE}"]', text="One Side Only")
        if FLIP_SIDE in box:
            flip = row.row()
            flip.enabled = bool(box[ONE_SIDE])  # greyed out until One Side is on
            flip.prop(box, f'["{FLIP_SIDE}"]', text="Flip Side")
