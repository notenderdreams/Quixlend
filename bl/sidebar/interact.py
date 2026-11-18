"""Pointer state and the success tick that flashes on a button."""

import time
from typing import Any

import bpy
import bpy.utils.previews


class pointer:
    """What the mouse is doing, shared by drawing and the mouse operators."""

    drag: dict[str, Any] | None = (
        None  # {"hit", "x", "y"} in window coordinates while a card is being dragged
    )
    hover: tuple[int, str, str | None] | None = None  # (region pointer, asset id, zone)
    hover_since = 0.0  # when `hover` last changed


flashed: dict[tuple[str, str], float] = {}  # (asset id, zone) -> time of the last success, for the tick flash


TICK_SECONDS = 1.5


def flash(asset_id, zone, area):
    """Show a tick on a button for a moment."""
    flashed[asset_id, zone] = time.time()
    area.tag_redraw()
    bpy.app.timers.register(area.tag_redraw, first_interval=TICK_SECONDS + 0.05)


def ticked(asset_id, zone):
    return time.time() - flashed.get((asset_id, zone), -TICK_SECONDS) < TICK_SECONDS
