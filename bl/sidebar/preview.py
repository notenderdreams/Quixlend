"""Hover preview, and the thumbnail or wireframe that follows a dragged card."""

import math
import random
import time
from typing import Any, cast

import bpy
import bpy.utils.previews
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

from .. import jobs
from ..placement import load_templates
from ..search import big_preview, thumb_image
from ..store import prefs, state
from . import gfx
from .interact import pointer
from .layout import cells

PREVIEW_KEEP = 8  # large previews kept loaded


previews: list[str] = []  # asset ids whose large image is loaded, oldest first


requested: set[str] = set()  # large previews being fetched


batches = {}  # (asset id, object name) -> GPU line batch of the mesh edges in object space


def wireframe(asset_id, max_edges=60000):
    """Pick the mesh variation that will be placed and build its wireframe once; None for materials/brushes."""
    objects, _ = load_templates(asset_id)
    if not objects:
        return None
    obj = random.choice(sorted(objects, key=lambda o: o.name))
    key = (asset_id, obj.name)
    if key not in batches:
        import numpy as np

        mesh = cast(bpy.types.Mesh, obj.data)
        co = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", co)
        ed = np.empty(len(mesh.edges) * 2, np.int32)
        mesh.edges.foreach_get("vertices", ed)
        ed = ed.reshape(-1, 2)
        if len(ed) > max_edges:
            ed = ed[:: math.ceil(len(ed) / max_edges)]
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        batches[key] = batch_for_shader(shader, "LINES", {"pos": co.reshape(-1, 3)[ed.ravel()]})  # pyright: ignore[reportArgumentType]
    return {"name": obj.name, "batch": batches[key], "basis": obj.matrix_basis.copy()}


def draw_drag_3d():
    """The mesh as it will be placed: real size, rotation and position, drawn in the viewport."""
    if not pointer.drag or not pointer.drag.get("wire") or not pointer.drag.get("loc"):
        return
    matrix = pointer.drag["wire"]["basis"].copy()
    if rot := pointer.drag.get("rot"):  # a decal stands on the surface under the pointer
        matrix = Matrix.LocRotScale(pointer.drag["loc"], rot, None)
    matrix.translation = pointer.drag["loc"]  # place() only overrides the location
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", (*gfx.colors()["accent"][:3], 1.0))
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("NONE")
    gpu.state.line_width_set(1.0)
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    pointer.drag["wire"]["batch"].draw(shader)
    gpu.matrix.pop()
    gpu.state.blend_set("NONE")


def _preview_image(asset_id):
    """The large preview as an image datablock, or None while it has not been fetched."""
    if asset_id not in previews:
        return None
    image = bpy.data.images.get(
        f".quixlend_big_{asset_id}"
    )  # undo/redo can drop it; the file is still on disk
    if image is None:
        previews.remove(asset_id)
    return image


def dwell(area, key):
    """The pointer rested on a card's image: fetch its large preview and redraw so it shows."""
    if pointer.hover is None or pointer.hover != key or key[2] is not None:
        return
    asset_id = key[1]
    hit = next((h for h in state.hits if h["id"] == asset_id), None)
    if hit and asset_id not in requested and not _preview_image(asset_id):
        requested.add(asset_id)

        def done(path, error):
            requested.discard(asset_id)
            if error:
                return
            image = bpy.data.images.load(str(path))
            image.name = f".quixlend_big_{asset_id}"
            previews.append(asset_id)
            while len(previews) > PREVIEW_KEEP:  # keep memory bounded
                if old := bpy.data.images.get(f".quixlend_big_{previews.pop(0)}"):
                    bpy.data.images.remove(old)

        jobs.run(lambda: big_preview(hit), done)
    area.tag_redraw()


def draw_preview():
    """Large preview of the hovered card, in the viewport beside the sidebar."""
    if not prefs().hover_preview or pointer.drag or not pointer.hover or pointer.hover[2] is not None:
        return
    if state.opening:  # details are loading for a clicked card
        return
    if time.time() - pointer.hover_since < prefs().hover_preview_delay * 0.9:
        return
    area, region = bpy.context.area, bpy.context.region
    sidebar = next((r for r in area.regions if r.type == "UI" and r.as_pointer() == pointer.hover[0]), None)
    hit = next((h for h in state.hits if h["id"] == pointer.hover[1]), None)
    if not sidebar or not hit or not (image := _preview_image(hit["id"]) or thumb_image(hit)):
        return
    s = gfx.scale()
    m, items = cells(sidebar)
    card_y = next((y for h, _, y in items if h["id"] == hit["id"]), 0)
    right = sidebar.x - region.x - 12 * s  # the viewport's own region may sit under the sidebar
    side = min(480 * s, region.height - 56 * s, right - 12 * s)
    if side < 96 * s:
        return  # no room
    w, h = cast(Any, image).size
    aspect = w / h if h else 1
    pw, ph = (side, side / aspect) if aspect >= 1 else (side * aspect, side)
    x = right - pw
    mid = card_y + m["card"] / 2 + sidebar.y - region.y
    y = min(max(mid - ph / 2, 28 * s), region.height - ph - 12 * s)
    gpu.state.blend_set("ALPHA")
    gfx.rrect(x - 6 * s, y - 24 * s, pw + 12 * s, ph + 30 * s, 6 * s, (0, 0, 0, 0.72))
    gfx.image(image, x, y, pw, ph)
    gfx.text(hit["name"], x, y - 17 * s, 12 * s, (1, 1, 1, 1), pw)
    gpu.state.blend_set("NONE")


def draw_drag():
    """Thumbnail following the mouse (materials, or a mesh not over the viewport); sidebar and viewport."""
    if not pointer.drag or not (image := thumb_image(pointer.drag["hit"])):
        return
    if pointer.drag["wire"] and pointer.drag["loc"]:
        return  # drawn in 3D by draw_drag_3d
    region, size = bpy.context.region, 72 * gfx.scale()
    gpu.state.blend_set("ALPHA")
    gfx.image(
        image, pointer.drag["x"] - region.x - size / 2, pointer.drag["y"] - region.y - size / 2, size, size
    )
    gpu.state.blend_set("NONE")
