"""Where the cards are: grid geometry, button rectangles and hit-testing."""

import math
from typing import Any

import blf
import bpy

from ..decal import editing
from ..store import is_material, prefs, session, state
from ..transfer import download_size
from . import gfx

CATEGORY = "Quixlend"


DROPDOWN, DROPDOWN_COMPACT, MAIN_MIN = 46, 30, 64  # button widths in widget units; the main button's minimum
HEADER_UNITS = 5.5  # panel header + search/filter/status rows, in widget units


def _min_card():
    """Narrowest card that still shows the widest download size next to the icon."""
    s = gfx.scale()
    widest = max(
        (
            gfx.size_text(n)
            for h in state.hits
            if h["id"] not in state.local and (n := download_size(h["id"], fetch=False))
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
    s = gfx.scale()
    left, gap, label = 10 * s, 8 * s, 30 * s
    width = max(40 * s, region.width - left - 30 * s)  # tab strip and scrollbar
    cols = max(1, int((width + gap) // (max(100 * s, _min_card()) + gap)))
    if limit := prefs().max_columns:  # 0 = automatic
        cols = min(cols, limit)
    card = (width - (cols - 1) * gap) / cols
    rows = math.ceil(len(state.hits) / cols)
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
    for i, hit in enumerate(state.hits):
        out.append(
            (
                hit,
                m["left"] + (i % m["cols"]) * (m["card"] + m["gap"]),
                top - (i // m["cols"]) * m["cell"] - m["card"],
            )
        )
    return m, out


def unavailable(asset_id):
    """Not downloaded and not in the user's library, so it cannot be downloaded."""
    return state.owned is not None and asset_id not in state.owned and asset_id not in state.local


def buttons(m, hit, x, y):
    """(main, res, extra) button rects (x, y, w, h), None where absent. `res` is the resolution dropdown, or the
    cancel / buy button on a card that is downloading / not in the library; `extra` is the copy icon of a downloaded
    surface."""
    s, pad, asset_id = gfx.scale(), 4 * gfx.scale(), hit["id"]
    bx, by, bh = x + pad, y - m["label"] + pad, m["label"] - 2 * pad
    st = state
    if asset_id in st.cancel or unavailable(asset_id):  # cancel / open-in-browser
        widths = [24 * s]
    elif asset_id in st.building or asset_id in st.working or asset_id in st.progress:
        widths = []  # busy: one wide button with the status
    else:
        surface = asset_id in st.local and is_material(hit)  # also gets the copy icon
        widths = [DROPDOWN * s, 24 * s] if surface else [DROPDOWN * s]
        if (
            m["card"] - 2 * pad - sum(widths) - pad * len(widths) < MAIN_MIN * s
        ):  # too narrow: shrink the dropdown
            widths[0] = DROPDOWN_COMPACT * s
    rects, right = [], bx + m["card"] - 2 * pad
    for w in reversed(widths):
        right -= w
        rects.append((right, by, w, bh))
        right -= pad
    rects.reverse()
    main = (bx, by, right - bx, bh)
    res = rects[0] if rects else None
    extra = rects[1] if len(rects) > 1 else None
    return main, res, extra


def _inside(rect, px, py):
    return rect and rect[0] <= px <= rect[0] + rect[2] and rect[1] <= py <= rect[1] + rect[3]


def zone_at(region, px, py) -> tuple[dict[str, Any] | None, str | None]:
    """(hit, zone) under the pointer; zone is "main", "res", "copy", "buy" or None (the image part)."""
    m, items = cells(region)
    for hit, cx, cy in items:
        if cx <= px <= cx + m["card"] and cy - m["label"] <= py <= cy + m["card"]:
            main, res, extra = buttons(m, hit, cx, cy)
            if _inside(main, px, py):
                return hit, "main"
            if _inside(res, px, py):
                return hit, "buy" if unavailable(hit["id"]) else "res"
            return hit, "copy" if _inside(extra, px, py) else None
    return None, None


def hit_at(region, x, y):
    return zone_at(region, x, y)[0]


MIN_REGION = 120  # narrower than this (widget units) the sidebar is collapsing or closed: no cards


def active(region):
    return (
        region.type == "UI"
        and region.active_panel_category == CATEGORY
        and session.logged_in
        and editing(bpy.context) is None
        and region.width >= MIN_REGION * gfx.scale()
    )
