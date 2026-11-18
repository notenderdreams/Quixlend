"""Where the cards are: grid geometry, button rectangles and hit-testing."""

import math
from typing import Any

import blf

from ..store import prefs, session, state
from ..transfer import download_size
from . import gfx

CATEGORY = "Quixlend"


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
    """(main, res) button rects (x, y, w, h); res is None once downloaded or while downloading."""
    pad = 4 * gfx.scale()
    bx, by, bh = x + pad, y - m["label"] + pad, m["label"] - 2 * pad
    st = state
    if hit["id"] in st.cancel:  # downloading: the second button cancels
        cancel_w = 24 * gfx.scale()
        return (bx, by, m["card"] - 3 * pad - cancel_w, bh), (
            bx + m["card"] - 2 * pad - cancel_w,
            by,
            cancel_w,
            bh,
        )
    if hit["id"] in st.local and hit.get("type") in ("surface", "atlas"):  # Apply + copy-node-group icon
        copy_w = 24 * gfx.scale()
        return (bx, by, m["card"] - 3 * pad - copy_w, bh), (bx + m["card"] - 2 * pad - copy_w, by, copy_w, bh)
    if unavailable(hit["id"]):  # the second button opens the page where it can be bought
        buy_w = 24 * gfx.scale()
        return (bx, by, m["card"] - 3 * pad - buy_w, bh), (bx + m["card"] - 2 * pad - buy_w, by, buy_w, bh)
    if hit["id"] in st.local or hit["id"] in st.progress:
        return (bx, by, m["card"] - 2 * pad, bh), None
    res_w = 46 * gfx.scale()
    main_w = m["card"] - 3 * pad - res_w
    return (bx, by, main_w, bh), (bx + main_w + pad, by, res_w, bh)


def _inside(rect, px, py):
    return rect and rect[0] <= px <= rect[0] + rect[2] and rect[1] <= py <= rect[1] + rect[3]


def zone_at(region, px, py) -> tuple[dict[str, Any] | None, str | None]:
    """(hit, zone) under the pointer; zone is "main", "res", "copy", "buy" or None (the image part)."""
    m, items = cells(region)
    for hit, cx, cy in items:
        if cx <= px <= cx + m["card"] and cy - m["label"] <= py <= cy + m["card"]:
            main, res = buttons(m, hit, cx, cy)
            second = "copy" if hit["id"] in state.local else "buy" if unavailable(hit["id"]) else "res"
            return hit, "main" if _inside(main, px, py) else second if _inside(res, px, py) else None
    return None, None


def hit_at(region, x, y):
    return zone_at(region, x, y)[0]


def active(region):
    return region.type == "UI" and region.active_panel_category == CATEGORY and session.logged_in
