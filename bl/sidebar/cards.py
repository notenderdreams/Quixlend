"""Draws the thumbnail cards and their buttons into the sidebar."""

import itertools
import math
import time
from typing import cast

import blf
import bpy
import bpy.utils.previews
import gpu
from gpu_extras.batch import batch_for_shader

from .. import jobs
from ..library import has_selected
from ..search import start_search, thumb_image
from ..settings import res_label
from ..store import is_material, state
from ..transfer import download_size
from . import gfx
from .interact import pointer, ticked
from .layout import DROPDOWN, DROPDOWN_COMPACT, active, buttons, cells, unavailable


def _transfer_label(asset_id):
    """Longest-first variants, e.g. '120/270 MB - 42 MB/s - 8s left'; the caller keeps the first that fits."""
    bps, eta = state.speed.get(asset_id, (None, None))
    done, total = state.transferred.get(asset_id, (0, 0))
    amount = ""
    if total and total < 1e9:
        amount = f"{done / 1e6:.0f}/{gfx.size_text(total)}"
    elif total:
        amount = f"{done / 1e9:.1f}/{gfx.size_text(total)}"
    rate = (
        ""
        if not bps
        else f"{bps / 1e6:.0f} MB/s"
        if bps >= 10e6
        else f"{bps / 1e6:.1f} MB/s"
        if bps >= 1e6
        else f"{bps / 1e3:.0f} KB/s"
    )
    time_left = f"{gfx.duration(eta)} left" if rate and eta is not None else ""
    variants = [
        " - ".join(x for x in parts if x)
        for parts in ((amount, rate, time_left), (amount, rate), (rate,), (amount,))
    ]
    return list(dict.fromkeys(v for v in variants if v))


def _progress_value(asset_id):
    st = state
    if asset_id in st.building or asset_id in st.working:
        return 1.0
    return None if st.queue.queued(asset_id) or asset_id in st.restart else st.progress.get(asset_id)


def _main_label(asset_id, hit):
    st = state
    if asset_id in st.working:
        return st.working[asset_id]
    if asset_id in st.building:
        return "Building"
    if st.queue.queued(asset_id) or asset_id in st.restart:
        return "Queued"
    if asset_id in st.progress:
        return f"{int(st.progress[asset_id] * 100)}%"
    if asset_id in st.local:
        if not has_selected(asset_id):
            return "Download"  # downloaded, but not at the chosen resolution
        return "Apply" if is_material(hit) else "Import"
    if asset_id in st.errors:
        return "Retry"
    if st.owned is not None and asset_id not in st.owned:
        return "Not in library"
    return "Download"


def _draw_download(rect, size, fill, text, hovered):
    """Download icon plus the download size; the size is dropped when the button is too narrow."""
    x, y, w, h = rect
    t, a = gfx.scale(), 3.5 * gfx.scale()
    r = 3 * t
    gfx.rrect(x, y, w, h, r, gfx.lift(fill, 0.12) if hovered else fill)
    label = gfx.size_text(size) if size else ""
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
        gfx.text(label, left + 2 * a + gap, y + (h - font) / 2 + 1.5 * t, font, text, tw + 1)


def _draw_cancel(rect, fill, text, hovered):
    x, y, w, h = rect
    gfx.rrect(x, y, w, h, 3 * gfx.scale(), gfx.lift(fill, 0.12) if hovered else fill)
    cx, cy, a, t = x + w / 2, y + h / 2, 3 * gfx.scale(), 0.7 * gfx.scale()
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


def _draw_dropdown(rect, label, colors, hovered):
    """Button showing `label` with a down arrow on the right."""
    s = gfx.scale()
    _draw_button(rect, label, colors["button"], colors["button_text"], hovered, colors, dropdown=True)
    ax, ay, a = rect[0] + rect[2] - 9 * s, rect[1] + rect[3] / 2, 2.5 * s
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", colors["button_text"])
    batch_for_shader(
        shader, "TRIS", {"pos": ((ax - a, ay + a * 0.6), (ax + a, ay + a * 0.6), (ax, ay - a * 0.8))}
    ).draw(shader)


def _draw_external(rect, fill, text, hovered):
    """Box with an arrow leaving its top-right corner: the open-in-browser icon."""
    x, y, w, h = rect
    t = gfx.scale()
    gfx.rrect(x, y, w, h, 3 * t, gfx.lift(fill, 0.12) if hovered else fill)
    cx, cy, a = x + w / 2, y + h / 2, 3.5 * t
    bg = gfx.lift(fill, 0.12) if hovered else fill
    gfx.rrect(cx - a - t, cy - a - t, 2 * a, 2 * a, t, text)  # the box
    gfx.rrect(cx - a, cy - a, 2 * a - 2 * t, 2 * a - 2 * t, 0.5 * t, bg)  # hollowed out
    gfx.rect(cx - 0.5 * t, cy - 0.5 * t, 2 * a, 2 * a, bg)  # gap for the arrow, top right
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", text)
    d = 0.7 * t
    tip = (cx + a, cy + a)
    shaft = (
        (cx - 0.5 * t - d, cy - 0.5 * t + d),
        (cx - 0.5 * t + d, cy - 0.5 * t - d),
        (tip[0] + d, tip[1] - d),
        (tip[0] - d, tip[1] + d),
    )
    gfx.fan(shader, {"pos": shaft}).draw(shader)
    head = ((tip[0] + t, tip[1] + t), (tip[0] - 2.5 * t, tip[1] + t), (tip[0] + t, tip[1] - 2.5 * t))
    batch_for_shader(shader, "TRIS", {"pos": head}).draw(shader)


def _draw_tick(rect, fill, text):
    x, y, w, h = rect
    t = gfx.scale()
    gfx.rrect(x, y, w, h, 3 * t, gfx.lift(fill, 0.12))
    cx, cy, a = x + w / 2, y + h / 2, 4 * t
    p = ((cx - a, cy), (cx - a * 0.35, cy - a * 0.7), (cx + a, cy + a * 0.8))
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", text)
    for (x0, y0), (x1, y1) in itertools.pairwise(p):
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy)
        nx, ny = -dy / n * 0.8 * t, dx / n * 0.8 * t
        quad = ((x0 + nx, y0 + ny), (x0 - nx, y0 - ny), (x1 - nx, y1 - ny), (x1 + nx, y1 + ny))
        gfx.fan(shader, {"pos": quad}).draw(shader)


def _draw_copy(rect, fill, text, hovered):
    """Two overlapping squares: the copy icon."""
    x, y, w, h = rect
    t = gfx.scale()
    gfx.rrect(x, y, w, h, 3 * t, gfx.lift(fill, 0.12) if hovered else fill)
    cx, cy, a = x + w / 2, y + h / 2, 4 * t
    gfx.rrect(cx - a - t, cy - a + t, 2 * a, 2 * a, t, text)  # back sheet
    gfx.rrect(cx - a + 2 * t, cy - a - 2 * t, 2 * a, 2 * a, t, (*text[:3], 1))
    gfx.rrect(
        cx - a + 3 * t, cy - a - t, 2 * a - 2 * t, 2 * a - 2 * t, 0.5 * t, fill
    )  # hollow the front sheet


def _draw_button(rect, label, fill, text, hovered, colors, progress=None, dropdown=False):
    x, y, w, h = rect
    r = 3 * gfx.scale()
    gfx.rrect(x, y, w, h, r, gfx.lift(fill, 0.12) if hovered else fill)
    if progress is not None:
        gfx.rrect(x, y, max(w * progress, 2 * r), h, r, colors["accent"])
    size = 10 * gfx.scale()
    blf.size(0, size)
    if isinstance(label, list):  # variants, longest first
        label = next((v for v in label if blf.dimensions(0, v)[0] <= w - 8 * gfx.scale()), label[-1])
    tw = blf.dimensions(0, label)[0]
    gfx.text(
        label,
        x + max(4 * gfx.scale(), (w - tw) / 2 - (5 * gfx.scale() if dropdown else 0)),
        y + (h - size) / 2 + 1.5 * gfx.scale(),
        size,
        text,
        w - 8 * gfx.scale(),
    )


def _loader(x, y, card):
    """Dim the thumbnail and draw a spinner over it."""
    s = gfx.scale()
    gfx.rect(x, y, card, card, (0, 0, 0, 0.5))
    cx, cy, radius, n = x + card / 2, y + card / 2, 10 * s, 10
    head = time.time() * 10
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    for i in range(n):
        angle = 2 * math.pi * i / n
        shader.uniform_float("color", (1, 1, 1, 0.15 + 0.85 * ((head - i) % n) / n))
        px, py, d = cx + radius * math.cos(angle), cy + radius * math.sin(angle), 1.8 * s
        gfx.fan(
            shader,
            {"pos": ((px - d, py - d), (px + d, py - d), (px + d, py + d), (px - d, py + d))},
        ).draw(shader)
    jobs.dirty.set()  # keep redrawing while it spins


def draw_grid():
    region = bpy.context.region
    if not active(region) or not state.panel_open:
        return
    state.panel_open = False  # set again by the panel on its next draw; stays off while collapsed
    s = gfx.scale()
    m, items = cells(region)
    colors = gfx.colors()
    gpu.state.blend_set("ALPHA")
    for hit, x, y in items:
        if y > region.height or y + m["card"] < -m["label"]:
            continue
        asset_id, card = hit["id"], m["card"]
        gfx.rect(x, y - m["label"], card, card + m["label"], colors["card"])
        gfx.rect(x, y - m["label"], card, m["label"], colors["footer"])
        gfx.rect(x, y, card, card, (0, 0, 0, 1))
        if image := thumb_image(hit):
            width, height = cast("tuple[int, int]", image.size)
            fit = (card - 4 * s) / max(width, height, 1)
            w, h = width * fit, height * fit
            gfx.image(image, x + (card - w) / 2, y + (card - h) / 2, w, h)
        if error := state.errors.get(asset_id):
            gfx.rect(x, y, card, 16 * s, (0.55, 0.1, 0.1, 0.85))
            gfx.text(error, x + 6 * s, y + 4.5 * s, 10 * s, (1, 1, 1, 1), card - 12 * s)
        if state.opening == asset_id:
            _loader(x, y, card)
        st = state
        if (
            asset_id in st.progress
            and not (asset_id in st.building or asset_id in st.working or asset_id in st.restart)
            and not st.queue.queued(asset_id)
            and (variants := _transfer_label(asset_id))
        ):
            blf.size(0, 10 * s)
            text = next((v for v in variants if blf.dimensions(0, v)[0] <= card - 12 * s), variants[-1])
            gfx.rect(x, y, card, 16 * s, (0, 0, 0, 0.75))
            gfx.text(text, x + 6 * s, y + 4.5 * s, 10 * s, (1, 1, 1, 1), card - 12 * s)
        over = pointer.hover is not None and pointer.hover[:2] == (region.as_pointer(), asset_id)
        zone = pointer.hover[2] if pointer.hover and over else None
        gfx.rect(x, y + card - 20 * s, card, 20 * s, (0, 0, 0, 0.6))
        gfx.text(
            hit["name"],
            x + 6 * s,
            y + card - 14 * s,
            11 * s,
            (1, 1, 1, 1) if over else (0.8, 0.8, 0.8, 1),
            card - 12 * s,
        )
        main, res, extra = buttons(m, hit, x, y)
        busy = asset_id in state.progress or asset_id in state.working
        label = _main_label(asset_id, hit)
        hovered = zone == "main" and not busy and not unavailable(asset_id)
        if label in ("Apply", "Import") and ticked(asset_id, "main"):
            _draw_tick(main, colors["button"], colors["button_text"])
        elif label == "Download":
            _draw_download(main, download_size(asset_id), colors["button"], colors["button_text"], hovered)
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
        if extra and ticked(asset_id, "copy"):
            _draw_tick(extra, colors["button"], colors["button_text"])
        elif extra:
            _draw_copy(extra, colors["button"], colors["button_text"], zone == "copy")
        if res and unavailable(asset_id):
            _draw_external(res, colors["button"], colors["button_text"], zone == "buy")
        elif res and asset_id in state.cancel:
            _draw_cancel(res, colors["button"], colors["button_text"], zone == "res")
        elif res:
            label = res_label()
            if (
                res[2] < (DROPDOWN + DROPDOWN_COMPACT) / 2 * s
            ):  # the compact button has room for the number only
                label = label.removesuffix("K")
            _draw_dropdown(res, label, colors, zone == "res")
    gpu.state.blend_set("NONE")
    st = state
    if items and not st.loading and st.page + 1 < st.pages and items[-1][2] + m["card"] >= 0:
        st.loading = True  # last row reached the viewport: fetch the next batch
        bpy.app.timers.register(lambda: start_search(st.page + 1, append=True) and None, first_interval=0)
