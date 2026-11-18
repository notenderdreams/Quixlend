"""Thumbnail grid inside the sidebar panel, with click-to-download and drag-and-drop.

Blender's Python UI has no drag source, so the panel only reserves empty space and
this module draws the cards into it and handles the mouse itself.
"""

import itertools
import math
import random
import time
from typing import TYPE_CHECKING, Any, cast

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils.geometry import intersect_line_plane

from . import jobs, ui

CATEGORY = "Quixlend"
HEADER_UNITS = 5.5  # panel header + search/filter/status rows, in widget units

drag: dict[str, Any] | None = None  # {"hit", "x", "y"} in window coordinates while a card is being dragged
hover: tuple[int, str, str | None] | None = None  # (region pointer, asset id, zone)
hover_since = 0.0  # when `hover` last changed
PREVIEW_KEEP = 8  # large previews kept loaded
previews: list[str] = []  # asset ids whose large image is loaded, oldest first
requested: set[str] = set()  # large previews being fetched


def scale():
    return bpy.context.preferences.system.ui_scale


def _size_text(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _min_card():
    """Narrowest card that still shows the widest download size next to the icon."""
    s = scale()
    widest = max(
        (
            _size_text(n)
            for h in ui.state.hits
            if h["id"] not in ui.state.local and (n := ui.download_size(h["id"], fetch=False))
        ),
        key=len,
        default="",
    )
    if not widest:
        return 0
    blf.size(0, 10 * s)
    return (
        3 * 4 * s + 46 * s + 8 * s + 7 * s + 4 * s + blf.dimensions(0, widest)[0]
    )  # pads, res button, margins, icon, gap, text


def metrics(region):
    """Grid geometry for a sidebar region; shared by the panel (to reserve space), drawing and hit-testing."""
    s = scale()
    left, gap, label = 10 * s, 8 * s, 30 * s
    width = max(40 * s, region.width - left - 30 * s)  # tab strip and scrollbar
    cols = max(1, int((width + gap) // (max(100 * s, _min_card()) + gap)))
    card = (width - (cols - 1) * gap) / cols
    rows = math.ceil(len(ui.state.hits) / cols)
    return {
        "left": left,
        "gap": gap,
        "label": label,
        "cols": cols,
        "card": card,
        "cell": card + label + gap,
        "height": rows * (card + label + gap),
        "unit": 20 * s,
    }


def cells(region):
    """[(hit, x, y)] with (x, y) the bottom-left of each thumbnail in region pixels."""
    m = metrics(region)
    top = region.view2d.view_to_region(0, 0, clip=False)[1] - HEADER_UNITS * m["unit"]
    out = []
    for i, hit in enumerate(ui.state.hits):
        out.append(
            (
                hit,
                m["left"] + (i % m["cols"]) * (m["card"] + m["gap"]),
                top - (i // m["cols"]) * m["cell"] - m["card"],
            )
        )
    return m, out


def buttons(m, hit, x, y):
    """(main, res) button rects (x, y, w, h); res is None once downloaded or while downloading."""
    pad = 4 * scale()
    bx, by, bh = x + pad, y - m["label"] + pad, m["label"] - 2 * pad
    st = ui.state
    if hit["id"] in st.cancel:  # downloading: the second button cancels
        cancel_w = 24 * scale()
        return (bx, by, m["card"] - 3 * pad - cancel_w, bh), (
            bx + m["card"] - 2 * pad - cancel_w,
            by,
            cancel_w,
            bh,
        )
    if hit["id"] in st.local and hit.get("type") in ("surface", "atlas"):  # Apply + copy-node-group icon
        copy_w = 24 * scale()
        return (bx, by, m["card"] - 3 * pad - copy_w, bh), (bx + m["card"] - 2 * pad - copy_w, by, copy_w, bh)
    if hit["id"] in st.local or hit["id"] in st.progress:
        return (bx, by, m["card"] - 2 * pad, bh), None
    res_w = 46 * scale()
    main_w = m["card"] - 3 * pad - res_w
    return (bx, by, main_w, bh), (bx + main_w + pad, by, res_w, bh)


def _inside(rect, px, py):
    return rect and rect[0] <= px <= rect[0] + rect[2] and rect[1] <= py <= rect[1] + rect[3]


def zone_at(region, px, py) -> tuple[dict[str, Any] | None, str | None]:
    """(hit, zone) under the pointer; zone is "main", "res", "copy" or None (the image part)."""
    m, items = cells(region)
    for hit, cx, cy in items:
        if cx <= px <= cx + m["card"] and cy - m["label"] <= py <= cy + m["card"]:
            main, res = buttons(m, hit, cx, cy)
            second = "copy" if hit["id"] in ui.state.local else "res"
            return hit, "main" if _inside(main, px, py) else second if _inside(res, px, py) else None
    return None, None


def hit_at(region, x, y):
    return zone_at(region, x, y)[0]


def active(region):
    return region.type == "UI" and region.active_panel_category == CATEGORY and ui.session.logged_in


# ---- drawing ---------------------------------------------------------------


def _fan(shader, content):
    """Triangle-fan batch; Blender accepts "TRI_FAN" though the stubs omit it."""
    return batch_for_shader(shader, "TRI_FAN", content)  # pyright: ignore[reportArgumentType]


def _rect(x, y, w, h, color):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", color)
    _fan(shader, {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h))}).draw(shader)


def _rrect(x, y, w, h, radius, color):
    """Rounded rectangle, like Blender's buttons."""
    r = min(radius, w / 2, h / 2)
    pts = []
    for cx, cy, a0 in (
        (x + w - r, y + r, -90),
        (x + w - r, y + h - r, 0),
        (x + r, y + h - r, 90),
        (x + r, y + r, 180),
    ):
        pts += [
            (cx + r * math.cos(math.radians(a0 + i * 15)), cy + r * math.sin(math.radians(a0 + i * 15)))
            for i in range(7)
        ]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", color)
    _fan(shader, {"pos": [(x + w / 2, y + h / 2), *pts, pts[0]]}).draw(shader)


def _image(image, x, y, w, h):
    shader = gpu.shader.from_builtin("IMAGE")
    shader.uniform_sampler("image", gpu.texture.from_image(image))
    _fan(
        shader,
        {
            "pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
            "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1)),
        },
    ).draw(shader)


def _text(text, x, y, size, color, max_width):
    blf.size(0, size)
    blf.color(0, *color)
    while text and blf.dimensions(0, text)[0] > max_width:
        text = text[:-2].rstrip() + "…" if len(text) > 2 else ""
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def _colors():
    ui_theme = bpy.context.preferences.themes[0].user_interface
    btn = ui_theme.wcol_regular
    return {
        "card": tuple(ui_theme.wcol_box.inner),
        "accent": tuple(ui_theme.wcol_progress.item),
        "button": (*btn.inner[:3], 1),
        "button_text": (*btn.text, 1),
        "footer": _lift((*ui_theme.wcol_box.inner[:3], 1), 0.07),
    }


def _lift(c, amount):
    return (*(a + (1 - a) * amount for a in c[:3]), c[3])


def _duration(seconds):
    seconds = int(seconds + 0.5)
    return (
        f"{seconds}s"
        if seconds < 60
        else f"{seconds // 60}m {seconds % 60:02d}s"
        if seconds < 3600
        else f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    )


def _transfer_label(asset_id):
    """Longest-first variants, e.g. '120/270 MB - 42 MB/s - 8s left'; the caller keeps the first that fits."""
    bps, eta = ui.state.speed.get(asset_id, (None, None))
    done, total = ui.state.transferred.get(asset_id, (0, 0))
    amount = ""
    if total and total < 1e9:
        amount = f"{done / 1e6:.0f}/{_size_text(total)}"
    elif total:
        amount = f"{done / 1e9:.1f}/{_size_text(total)}"
    rate = (
        ""
        if not bps
        else f"{bps / 1e6:.0f} MB/s"
        if bps >= 10e6
        else f"{bps / 1e6:.1f} MB/s"
        if bps >= 1e6
        else f"{bps / 1e3:.0f} KB/s"
    )
    time_left = f"{_duration(eta)} left" if rate and eta is not None else ""
    variants = [" - ".join(x for x in parts if x) for parts in ((amount, rate, time_left), (amount, rate), (rate,), (amount,))]
    return list(dict.fromkeys(v for v in variants if v))


def _progress_value(asset_id):
    st = ui.state
    if asset_id in st.building or asset_id in st.working:
        return 1.0
    return None if st.queue.queued(asset_id) or asset_id in st.restart else st.progress.get(asset_id)


def _main_label(asset_id, hit):
    st = ui.state
    if asset_id in st.working:
        return st.working[asset_id]
    if asset_id in st.building:
        return "Building"
    if st.queue.queued(asset_id) or asset_id in st.restart:
        return "Queued"
    if asset_id in st.progress:
        return f"{int(st.progress[asset_id] * 100)}%"
    if asset_id in st.local:
        return "Apply" if hit.get("type") in ("surface", "atlas") else "Import"
    if asset_id in st.errors:
        return "Retry"
    if st.owned is not None and asset_id not in st.owned:
        return "Not in library"
    return "Download"


def _draw_download(rect, size, fill, text, hovered):
    """Download icon plus the download size; the size is dropped when the button is too narrow."""
    x, y, w, h = rect
    t, a = scale(), 3.5 * scale()
    r = 3 * t
    _rrect(x, y, w, h, r, _lift(fill, 0.12) if hovered else fill)
    label = _size_text(size) if size else ""
    font = 10 * t
    blf.size(0, font)
    tw = blf.dimensions(0, label)[0]
    gap = 4 * t
    full = 2 * a + gap + tw
    if not label or full > w - 8 * t:
        label, full = "", 2 * a
    left = x + (w - full) / 2
    cx, cy = left + a, y + h / 2 + t
    tris = (
        (cx - a, cy),
        (cx + a, cy),
        (cx, cy - a),  # arrow head
        (cx - t / 2, cy + a),
        (cx + t / 2, cy + a),
        (cx - t / 2, cy),
        (cx + t / 2, cy + a),
        (cx + t / 2, cy),
        (cx - t / 2, cy),  # stem
        (cx - a, cy - a - t),
        (cx + a, cy - a - t),
        (cx - a, cy - a - 2 * t),  # tray
        (cx + a, cy - a - t),
        (cx + a, cy - a - 2 * t),
        (cx - a, cy - a - 2 * t),
    )
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", text)
    batch_for_shader(shader, "TRIS", {"pos": tris}).draw(shader)
    if label:
        _text(label, left + 2 * a + gap, y + (h - font) / 2 + 1.5 * t, font, text, tw + 1)


def _draw_cancel(rect, fill, text, hovered):
    x, y, w, h = rect
    _rrect(x, y, w, h, 3 * scale(), _lift(fill, 0.12) if hovered else fill)
    cx, cy, a, t = x + w / 2, y + h / 2, 3 * scale(), 0.7 * scale()
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", text)
    for sign in (1, -1):  # the two arms of the cross
        pts = (
            (cx - a, cy - sign * a + t),
            (cx - a, cy - sign * a - t),
            (cx + a, cy + sign * a - t),
            (cx - a, cy - sign * a + t),
            (cx + a, cy + sign * a - t),
            (cx + a, cy + sign * a + t),
        )
        batch_for_shader(shader, "TRIS", {"pos": pts}).draw(shader)


flashed: dict[tuple[str, str], float] = {}  # (asset id, zone) -> time of the last success, for the tick flash
TICK_SECONDS = 1.5


def flash(asset_id, zone, area):
    """Show a tick on a button for a moment."""
    flashed[asset_id, zone] = time.time()
    area.tag_redraw()
    bpy.app.timers.register(area.tag_redraw, first_interval=TICK_SECONDS + 0.05)


def _ticked(asset_id, zone):
    return time.time() - flashed.get((asset_id, zone), -TICK_SECONDS) < TICK_SECONDS


def _draw_tick(rect, fill, text):
    x, y, w, h = rect
    t = scale()
    _rrect(x, y, w, h, 3 * t, _lift(fill, 0.12))
    cx, cy, a = x + w / 2, y + h / 2, 4 * t
    p = ((cx - a, cy), (cx - a * 0.35, cy - a * 0.7), (cx + a, cy + a * 0.8))
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", text)
    for (x0, y0), (x1, y1) in itertools.pairwise(p):
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy)
        nx, ny = -dy / n * 0.8 * t, dx / n * 0.8 * t
        quad = ((x0 + nx, y0 + ny), (x0 - nx, y0 - ny), (x1 - nx, y1 - ny), (x1 + nx, y1 + ny))
        _fan(shader, {"pos": quad}).draw(shader)


def _draw_copy(rect, fill, text, hovered):
    """Two overlapping squares: the copy icon."""
    x, y, w, h = rect
    t = scale()
    _rrect(x, y, w, h, 3 * t, _lift(fill, 0.12) if hovered else fill)
    cx, cy, a = x + w / 2, y + h / 2, 4 * t
    _rrect(cx - a - t, cy - a + t, 2 * a, 2 * a, t, text)  # back sheet
    _rrect(cx - a + 2 * t, cy - a - 2 * t, 2 * a, 2 * a, t, (*text[:3], 1))
    _rrect(cx - a + 3 * t, cy - a - t, 2 * a - 2 * t, 2 * a - 2 * t, 0.5 * t, fill)  # hollow the front sheet


def _draw_button(rect, label, fill, text, hovered, colors, progress=None, dropdown=False):
    x, y, w, h = rect
    r = 3 * scale()
    _rrect(x, y, w, h, r, _lift(fill, 0.12) if hovered else fill)
    if progress is not None:
        _rrect(x, y, max(w * progress, 2 * r), h, r, colors["accent"])
    size = 10 * scale()
    blf.size(0, size)
    if isinstance(label, list):  # variants, longest first
        label = next((v for v in label if blf.dimensions(0, v)[0] <= w - 8 * scale()), label[-1])
    tw = blf.dimensions(0, label)[0]
    _text(
        label,
        x + max(4 * scale(), (w - tw) / 2 - (5 * scale() if dropdown else 0)),
        y + (h - size) / 2 + 1.5 * scale(),
        size,
        text,
        w - 8 * scale(),
    )


def _loader(x, y, card):
    """Dim the thumbnail and draw a spinner over it."""
    s = scale()
    _rect(x, y, card, card, (0, 0, 0, 0.5))
    cx, cy, radius, n = x + card / 2, y + card / 2, 10 * s, 10
    head = time.time() * 10
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    for i in range(n):
        angle = 2 * math.pi * i / n
        shader.uniform_float("color", (1, 1, 1, 0.15 + 0.85 * ((head - i) % n) / n))
        px, py, d = cx + radius * math.cos(angle), cy + radius * math.sin(angle), 1.8 * s
        _fan(
            shader,
            {"pos": ((px - d, py - d), (px + d, py - d), (px + d, py + d), (px - d, py + d))},
        ).draw(shader)
    ui.jobs.dirty.set()  # keep redrawing while it spins


def draw_grid():
    region = bpy.context.region
    if not active(region) or not ui.state.panel_open:
        return
    ui.state.panel_open = False  # set again by the panel on its next draw; stays off while collapsed
    s = scale()
    m, items = cells(region)
    colors = _colors()
    gpu.state.blend_set("ALPHA")
    for hit, x, y in items:
        if y > region.height or y + m["card"] < -m["label"]:
            continue
        asset_id, card = hit["id"], m["card"]
        _rect(x, y - m["label"], card, card + m["label"], colors["card"])
        _rect(x, y - m["label"], card, m["label"], colors["footer"])
        _rect(x, y, card, card, (0, 0, 0, 1))
        if image := ui.thumb_image(hit):
            width, height = cast("tuple[int, int]", image.size)
            fit = (card - 4 * s) / max(width, height, 1)
            w, h = width * fit, height * fit
            _image(image, x + (card - w) / 2, y + (card - h) / 2, w, h)
        if error := ui.state.errors.get(asset_id):
            _rect(x, y, card, 16 * s, (0.55, 0.1, 0.1, 0.85))
            _text(error, x + 6 * s, y + 4.5 * s, 10 * s, (1, 1, 1, 1), card - 12 * s)
        if ui.state.opening == asset_id:
            _loader(x, y, card)
        st = ui.state
        if (
            asset_id in st.progress
            and not (asset_id in st.building or asset_id in st.working or asset_id in st.restart)
            and not st.queue.queued(asset_id)
            and (variants := _transfer_label(asset_id))
        ):
            blf.size(0, 10 * s)
            text = next((v for v in variants if blf.dimensions(0, v)[0] <= card - 12 * s), variants[-1])
            _rect(x, y, card, 16 * s, (0, 0, 0, 0.75))
            _text(text, x + 6 * s, y + 4.5 * s, 10 * s, (1, 1, 1, 1), card - 12 * s)
        over = hover is not None and hover[:2] == (region.as_pointer(), asset_id)
        zone = hover[2] if hover and over else None
        _rect(x, y + card - 20 * s, card, 20 * s, (0, 0, 0, 0.6))
        _text(
            hit["name"],
            x + 6 * s,
            y + card - 14 * s,
            11 * s,
            (1, 1, 1, 1) if over else (0.8, 0.8, 0.8, 1),
            card - 12 * s,
        )
        main, res = buttons(m, hit, x, y)
        _local = asset_id in ui.state.local
        busy = asset_id in ui.state.progress or asset_id in ui.state.working
        unavailable = ui.state.owned is not None and asset_id not in ui.state.owned
        label = _main_label(asset_id, hit)
        hovered = zone == "main" and not busy and not unavailable
        if label in ("Apply", "Import") and _ticked(asset_id, "main"):
            _draw_tick(main, colors["button"], colors["button_text"])
        elif label == "Download":
            _draw_download(main, ui.download_size(asset_id), colors["button"], colors["button_text"], hovered)
        else:
            _draw_button(
                main,
                label,
                colors["button"],
                colors["button_text"],
                hovered,
                colors,
                _progress_value(asset_id),
            )
        if res and _local and _ticked(asset_id, "copy"):
            _draw_tick(res, colors["button"], colors["button_text"])
        elif res and _local:
            _draw_copy(res, colors["button"], colors["button_text"], zone == "copy")
        elif res and asset_id in ui.state.cancel:
            _draw_cancel(res, colors["button"], colors["button_text"], zone == "res")
        elif res:
            _draw_button(
                res,
                ui.res_label(),
                colors["button"],
                colors["button_text"],
                zone == "res",
                colors,
                dropdown=True,
            )
            ax, ay, a = res[0] + res[2] - 9 * s, res[1] + res[3] / 2, 2.5 * s  # dropdown arrow
            shader = gpu.shader.from_builtin("UNIFORM_COLOR")
            shader.uniform_float("color", colors["button_text"])
            batch_for_shader(
                shader, "TRIS", {"pos": ((ax - a, ay + a * 0.6), (ax + a, ay + a * 0.6), (ax, ay - a * 0.8))}
            ).draw(shader)
    gpu.state.blend_set("NONE")
    st = ui.state
    if items and not st.loading and st.page + 1 < st.pages and items[-1][2] + m["card"] >= 0:
        st.loading = True  # last row reached the viewport: fetch the next batch
        bpy.app.timers.register(lambda: ui.start_search(st.page + 1, append=True) and None, first_interval=0)


batches = {}  # (asset id, object name) -> GPU line batch of the mesh edges in object space


def preview(asset_id, max_edges=60000):
    """Pick the mesh variation that will be placed and build its wireframe once; None for materials/brushes."""
    objects, _ = ui.load_templates(asset_id)
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
    if not drag or not drag.get("wire") or not drag.get("loc"):
        return
    matrix = drag["wire"]["basis"].copy()
    matrix.translation = drag["loc"]  # place() only overrides the location
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", (*_colors()["accent"][:3], 1.0))
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_test_set("NONE")
    gpu.state.line_width_set(1.0)
    gpu.matrix.push()
    gpu.matrix.multiply_matrix(matrix)
    drag["wire"]["batch"].draw(shader)
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
    if hover is None or hover != key or key[2] is not None:
        return
    asset_id = key[1]
    hit = next((h for h in ui.state.hits if h["id"] == asset_id), None)
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

        jobs.run(lambda: ui.big_preview(hit), done)
    area.tag_redraw()


def draw_preview():
    """Large preview of the hovered card, in the viewport beside the sidebar."""
    if not ui.prefs().hover_preview or drag or not hover or hover[2] is not None:
        return
    if time.time() - hover_since < ui.prefs().hover_preview_delay * 0.9:
        return
    area, region = bpy.context.area, bpy.context.region
    sidebar = next((r for r in area.regions if r.type == "UI" and r.as_pointer() == hover[0]), None)
    hit = next((h for h in ui.state.hits if h["id"] == hover[1]), None)
    if not sidebar or not hit or not (image := _preview_image(hit["id"]) or ui.thumb_image(hit)):
        return
    s = scale()
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
    _rrect(x - 6 * s, y - 24 * s, pw + 12 * s, ph + 30 * s, 6 * s, (0, 0, 0, 0.72))
    _image(image, x, y, pw, ph)
    _text(hit["name"], x, y - 17 * s, 12 * s, (1, 1, 1, 1), pw)
    gpu.state.blend_set("NONE")


def draw_drag():
    """Thumbnail following the mouse (materials, or a mesh not over the viewport); sidebar and viewport."""
    if not drag or not (image := ui.thumb_image(drag["hit"])):
        return
    if drag["wire"] and drag["loc"]:
        return  # drawn in 3D by draw_drag_3d
    region, size = bpy.context.region, 72 * scale()
    gpu.state.blend_set("ALPHA")
    _image(image, drag["x"] - region.x - size / 2, drag["y"] - region.y - size / 2, size, size)
    gpu.state.blend_set("NONE")


# ---- mouse -----------------------------------------------------------------


class QUIXLEND_OT_hover(bpy.types.Operator):
    bl_idname = "quixlend.hover"
    bl_label = "Highlight Asset"
    bl_options = {"INTERNAL"}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> ui.Result:
        global hover, hover_since
        region = context.region
        hit, zone = (
            zone_at(region, event.mouse_region_x, event.mouse_region_y)
            if region and active(region)
            else (None, None)
        )
        current = (region.as_pointer(), hit["id"], zone) if hit else None
        if current != hover:
            hover, hover_since = current, time.time()
            context.area.tag_redraw()
            if current and current[2] is None and ui.prefs().hover_preview:
                bpy.app.timers.register(
                    lambda area=context.area, key=current: dwell(area, key),
                    first_interval=ui.prefs().hover_preview_delay,
                )
        return {"PASS_THROUGH"}


class QUIXLEND_OT_card(bpy.types.Operator):
    bl_idname = "quixlend.card"
    bl_label = "Quixlend Card"
    bl_description = "Click to download; drag a downloaded asset into the scene"
    bl_options = {"UNDO"}

    if TYPE_CHECKING:  # set in invoke(); declared here for the type checker only
        hit: dict[str, Any]
        zone: str | None
        area: bpy.types.Area
        start: tuple[int, int]

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> ui.Result:
        region = context.region
        if not region or not active(region):
            return {"PASS_THROUGH"}
        hit, self.zone = zone_at(region, event.mouse_region_x, event.mouse_region_y)
        if not hit or not context.area:
            return {"PASS_THROUGH"}
        self.hit, self.area, self.start = hit, context.area, (event.mouse_x, event.mouse_y)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context: bpy.types.Context, event: bpy.types.Event) -> ui.Result:
        global drag
        asset_id, st = self.hit["id"], ui.state
        if event.type == "MOUSEMOVE":
            moved = abs(event.mouse_x - self.start[0]) + abs(event.mouse_y - self.start[1])
            if drag or (asset_id in st.local and self.zone not in ("res", "copy") and moved > 6 * scale()):
                if not drag:
                    drag = {"hit": self.hit, "wire": preview(asset_id), "loc": None}
                drag.update(x=event.mouse_x, y=event.mouse_y)
                if drag["wire"]:
                    found = self.locate(context, event.mouse_x, event.mouse_y)
                    drag["loc"] = found[0] if found else None
                self.area.tag_redraw()
        elif event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if drag:
                chosen = drag["wire"]["name"] if drag["wire"] else None
                drag = None
                self.area.tag_redraw()
                self.drop(context, event, chosen)
            elif self.zone is None:
                ui.open_details(asset_id, context.window, self.area)
            elif self.zone == "copy":
                self.copy_group(context)
            elif self.zone == "res" and asset_id in st.cancel:
                ui.cancel_download(asset_id)
            elif self.zone == "res":
                context.window_manager.popup_menu(ui.draw_res_menu, title="Texture Resolution")
            elif self.zone == "main":
                if asset_id in st.local:
                    self.import_at_cursor(context)
                elif asset_id in st.working:
                    pass  # already applying
                elif asset_id not in st.progress and (st.owned is None or asset_id in st.owned):
                    st.errors.pop(asset_id, None)
                    bpy.ops.quixlend.download(asset_id=asset_id)  # pyright: ignore[reportAttributeAccessIssue]
            self.area.tag_redraw()
            return {"FINISHED"}
        elif event.type in {"ESC", "RIGHTMOUSE"}:
            drag = None
            self.area.tag_redraw()
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}

    def copy_group(self, context):
        """Run the copy after this modal ends: it borrows the area, which must not be in use by a running operator."""
        asset_id, window, area = self.hit["id"], context.window, self.area

        def run():
            region = next(r for r in area.regions if r.type == "WINDOW")
            with bpy.context.temp_override(window=window, area=area, region=region):
                if bpy.ops.quixlend.copy_group(asset_id=asset_id) == {"FINISHED"}:  # pyright: ignore[reportAttributeAccessIssue]
                    flash(asset_id, "copy", area)

        bpy.app.timers.register(run, first_interval=0)

    def locate(self, context, mx, my):
        """(location, target object or None) under the mouse: the surface hit, else the ground plane;
        None when the mouse is outside the viewport or over the sidebar."""
        region = next(r for r in self.area.regions if r.type == "WINDOW")
        x, y = mx - region.x, my - region.y
        sidebar = next(r for r in self.area.regions if r.type == "UI")
        over_sidebar = sidebar.x <= mx <= sidebar.x + sidebar.width
        if over_sidebar or not (0 <= x <= region.width and 0 <= y <= region.height):
            return None
        rv3d = cast(bpy.types.SpaceView3D, self.area.spaces.active).region_3d
        if rv3d is None:
            return None
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y))
        hit, location, _, _, target, _ = context.scene.ray_cast(
            context.evaluated_depsgraph_get(), origin, direction
        )
        if not hit:
            target = None
            location = (
                intersect_line_plane(origin, origin + direction, (0, 0, 0), (0, 0, 1))
                or context.scene.cursor.location
            )
        return location, target

    def place_later(self, context, **kwargs):
        """Run ui.place after a redraw: it blocks the main thread, so show "Applying" first."""
        hit, window, area = self.hit, context.window, self.area
        asset_id = hit["id"]
        ui.state.working[asset_id] = "Applying" if hit.get("type") in ("surface", "atlas") else "Importing"
        area.tag_redraw()

        def run():
            region = next(r for r in area.regions if r.type == "WINDOW")
            try:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    message = ui.place(bpy.context, asset_id, **kwargs)
                    if not message:
                        bpy.ops.ed.undo_push(message="Quixlend: " + ui.state.working[asset_id])
            finally:
                ui.state.working.pop(asset_id, None)
            if message:
                bpy.context.window_manager.popup_menu(
                    lambda menu, _context: menu.label(text=message, icon="ERROR"), title="Quixlend"
                )
            else:
                flash(asset_id, "main", area)
            area.tag_redraw()

        bpy.app.timers.register(run, first_interval=0.05)

    def import_at_cursor(self, context):
        """Import at the 3D cursor (materials go on the selected objects)."""
        self.place_later(context, pick=random.choice)

    def drop(self, context, event, chosen):
        """Place the asset exactly where the preview was: same variation, same spot."""
        found = self.locate(context, event.mouse_x, event.mouse_y)
        if not found:
            return  # released outside the viewport: cancel
        location, target = found
        pick = (
            (lambda sources: next((o for o in sources if o.name == chosen), sources[0]))
            if chosen
            else random.choice
        )
        self.place_later(context, location=location, target=target, pick=pick)


_handles = []


class QUIXLEND_PT_browser(bpy.types.Panel):
    bl_label = "Quixlend"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Quixlend"

    def draw_header(self, context: bpy.types.Context):
        ui.layout_of(self).label(text="", icon_value=ui.icons()["quixel"].icon_id)

    def draw_header_preset(self, context: bpy.types.Context):
        ui.layout_of(self).popover(panel="QUIXLEND_PT_settings", text="", icon="PREFERENCES")

    def draw(self, context: bpy.types.Context):
        layout, wm = ui.layout_of(self), ui.wm_of(context)
        layout.use_property_decorate = False
        if not ui.session.logged_in:
            col = layout.column(align=True)
            col.label(text="Your Megascans library", icon="ASSET_MANAGER")
            col.label(text="Sign in with your Epic Games account.")
            layout.separator()
            layout.operator("quixlend.login_open", text="Sign In with Epic Games", icon="URL")
            col = layout.column(align=True)
            col.label(text="Paste the authorization code:")
            col.prop(wm, "quixlend_code", text="", placeholder="Authorization code")
            row = col.row()
            row.enabled = bool(wm.quixlend_code.strip())
            row.operator("quixlend.login", text="Connect Account", icon="CHECKMARK")
            if ui.state.status:
                layout.label(text=ui.state.status, icon="INFO")
            return

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.prop(wm, "quixlend_query", text="", icon="VIEWZOOM", placeholder="Search Megascans")
        row.operator("quixlend.search", text="", icon="FILE_REFRESH")
        row = layout.row(align=True)
        row.prop(wm, "quixlend_type", text="")
        cat = row.row(align=True)
        cat.enabled = wm.quixlend_type != "ALL"
        cat.prop(wm, "quixlend_category", text="")
        row.prop(wm, "quixlend_downloaded", text="", icon="DISK_DRIVE")
        row = layout.row()
        row.label(text=ui.state.status or "Browse the Megascans catalog", icon="ASSET_MANAGER")

        # The cards are drawn by sidebar.draw_grid into this reserved empty space.
        m = metrics(context.region)
        blank = layout.column(align=True)
        for _ in range(math.ceil(m["height"] / m["unit"])):
            blank.label(text="")
        ui.state.panel_open = True
        if not ui.state.hits:
            box = layout.box()
            box.label(
                text="No matching assets"
                if ui.state.status == "No results"
                else "Materials, models and plants",
                icon="VIEWZOOM",
            )
            box.label(
                text="Try another search or asset type."
                if ui.state.status == "No results"
                else "Search by name or choose an asset type."
            )
        if ui.state.page + 1 < ui.state.pages:
            layout.label(text="Fetching more assets…", icon="SORTTIME")
        if ui.state.hits:
            col = layout.column(align=True)
            col.enabled = False
            col.label(text="Click to download · Drag to place", icon="MOUSE_LMB")


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
