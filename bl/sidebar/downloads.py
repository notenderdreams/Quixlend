"""The download list: a card drawn over the top of the grid, like the asset cards, so it redraws live as progress
comes in. One row per asset being fetched, queued or built, each with a thin progress bar and a cancel button."""

from typing import cast

import blf
import bpy
import gpu

from .. import catalog
from ..search import thumb_image
from ..store import Result, state
from ..transfer import cancel_download
from . import gfx
from .cards import _draw_cancel
from .layout import HEADER_UNITS, active, metrics


def active_ids():
    """Ids in flight, running first, then building, then waiting in click order."""
    return list(dict.fromkeys([*state.progress, *state.building, *state.queue.waiting, *state.restart]))


def _name(asset_id):
    hit = next((h for h in state.hits if h["id"] == asset_id), None)
    return (hit or catalog.meta(asset_id) or {}).get("name") or asset_id


def _thumb(asset_id):
    hit = next((h for h in state.hits if h["id"] == asset_id), None)
    return thumb_image(hit) if hit else bpy.data.images.get(f".quixlend_{asset_id}")


def _note(asset_id):
    if asset_id in state.progress:
        done, total = state.transferred.get(asset_id, (0, 0))
        return f"{done / 1e6:.0f} / {total / 1e6:.0f} MB" if total else f"{state.progress[asset_id]:.0%}"
    return "Building" if asset_id in state.building else "Queued"


def geometry(region):
    """((x, y, w, h) of the card, [(asset_id, row rect, cancel rect or None)]) in region pixels."""
    s, m = gfx.scale(), metrics(region)
    pad, row_h = 8 * s, 46 * s
    ids = active_ids()
    height = 2 * pad + max(1, len(ids)) * row_h
    width = region.width - m["left"] - 30 * s
    top = region.view2d.view_to_region(0, 0, clip=False)[1] - HEADER_UNITS * m["unit"]
    x, y = m["left"], top - height
    rows = []
    for i, asset_id in enumerate(ids):
        ry = y + height - pad - (i + 1) * row_h
        cancel = (
            None
            if asset_id in state.building
            else (x + width - pad - 18 * s, ry + row_h - 24 * s, 18 * s, 18 * s)
        )
        rows.append((asset_id, (x + pad, ry, width - 2 * pad, row_h), cancel))
    return (x, y, width, height), rows


def _inside(rect, px, py):
    return rect is not None and rect[0] <= px <= rect[0] + rect[2] and rect[1] <= py <= rect[1] + rect[3]


def covers(region, px, py):
    """The pointer is over the open card."""
    return state.downloads_open and _inside(geometry(region)[0], px, py)


def click(region, px, py):
    """True if the pointer is on the open card (cancelling a row if its X was hit), so the cards below ignore it."""
    if not covers(region, px, py):
        return False
    for asset_id, _, cancel in geometry(region)[1]:
        if _inside(cancel, px, py):
            cancel_download(asset_id)
    return True


def draw_downloads():
    region = bpy.context.region
    if not state.downloads_open or not active(region):
        return
    s, colors = gfx.scale(), gfx.colors()
    card, rows = geometry(region)
    pad = 8 * s
    gpu.state.blend_set("ALPHA")
    gfx.rect(*card, (*colors["footer"][:3], 1))
    if not rows:
        gfx.text(
            "No downloads",
            card[0] + pad,
            card[1] + card[3] / 2 - 4 * s,
            11 * s,
            colors["button_text"],
            card[2] - 2 * pad,
        )
    for asset_id, (rx, ry, rw, rh), cancel in rows:
        side = rh - 2 * 3 * s  # the thumbnail: a square filling the row
        gfx.rect(rx, ry + 3 * s, side, side, (0, 0, 0, 1))
        if image := _thumb(asset_id):
            width, height = cast("tuple[int, int]", image.size)
            fit = side / max(width, height, 1)
            w, h = width * fit, height * fit
            gfx.image(image, rx + (side - w) / 2, ry + 3 * s + (side - h) / 2, w, h)
        tx = rx + side + 8 * s  # text and bar start right of it
        tw = rx + rw - tx
        note = _note(asset_id)
        blf.size(0, 10 * s)
        note_w = blf.dimensions(0, note)[0]
        right = rx + rw - (24 * s if cancel else 0)
        gfx.text(note, right - note_w, ry + rh - 19 * s, 10 * s, colors["button_text"], note_w + 1)
        gfx.text(_name(asset_id), tx, ry + rh - 19 * s, 11 * s, colors["button_text"], tw - note_w - 36 * s)
        if cancel:
            _draw_cancel(cancel, colors["button"], colors["button_text"], False)
        gfx.rect(tx, ry + 8 * s, tw, 3 * s, colors["button"])  # the thin bar: track, then fill
        gfx.rect(tx, ry + 8 * s, tw * state.progress.get(asset_id, 0.0), 3 * s, colors["accent"])
    gpu.state.blend_set("NONE")


class QUIXLEND_OT_downloads(bpy.types.Operator):
    bl_idname = "quixlend.downloads"
    bl_label = "Downloads"
    bl_description = "Show or hide what is downloading"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> Result:
        state.downloads_open = not state.downloads_open
        for area in context.window.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}
