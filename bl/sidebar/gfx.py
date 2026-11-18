"""Drawing helpers: rectangles, images, text and the formatting the cards share."""

import math

import blf
import bpy
import bpy.utils.previews
import gpu
from gpu_extras.batch import batch_for_shader


def scale():
    return bpy.context.preferences.system.ui_scale


def size_text(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def fan(shader, content):
    """Triangle-fan batch; Blender accepts "TRI_FAN" though the stubs omit it."""
    return batch_for_shader(shader, "TRI_FAN", content)  # pyright: ignore[reportArgumentType]


def rect(x, y, w, h, color):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", color)
    fan(shader, {"pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h))}).draw(shader)


def rrect(x, y, w, h, radius, color):
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
    fan(shader, {"pos": [(x + w / 2, y + h / 2), *pts, pts[0]]}).draw(shader)


def image(image, x, y, w, h):
    shader = gpu.shader.from_builtin("IMAGE")
    shader.uniform_sampler("image", gpu.texture.from_image(image))
    fan(
        shader,
        {
            "pos": ((x, y), (x + w, y), (x + w, y + h), (x, y + h)),
            "texCoord": ((0, 0), (1, 0), (1, 1), (0, 1)),
        },
    ).draw(shader)


def text(text, x, y, size, color, max_width):
    blf.size(0, size)
    blf.color(0, *color)
    while text and blf.dimensions(0, text)[0] > max_width:
        text = text[:-2].rstrip() + "…" if len(text) > 2 else ""
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def colors():
    ui_theme = bpy.context.preferences.themes[0].user_interface
    btn = ui_theme.wcol_regular
    return {
        "card": tuple(ui_theme.wcol_box.inner),
        "accent": tuple(ui_theme.wcol_progress.item),
        "button": (*btn.inner[:3], 1),
        "button_text": (*btn.text, 1),
        "footer": lift((*ui_theme.wcol_box.inner[:3], 1), 0.07),
    }


def lift(c, amount):
    return (*(a + (1 - a) * amount for a in c[:3]), c[3])


def duration(seconds):
    seconds = int(seconds + 0.5)
    return (
        f"{seconds}s"
        if seconds < 60
        else f"{seconds // 60}m {seconds % 60:02d}s"
        if seconds < 3600
        else f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    )
