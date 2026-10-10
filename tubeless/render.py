"""
Drawing the menu (and the calibration / message screens).

mpv doesn't rotate its own OSD, and the panel is natively portrait, so everything
is drawn here as an ordinary landscape picture, turned to the panel's orientation
and handed to the player as a bitmap overlay. The menu is drawn a little smaller and
centred on the bezel's visible opening (see Hardware.bezel_*).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw

from . import fonts
from .hardware import Hardware

SS = 2                         # supersampling for smooth edges
ROW_H, ROW_GAP = 50, 4
VISIBLE_ROWS = 6
ROWS_Y0 = 78
PANEL_RECT = (22, 12, 640 - 22, 480 - 12)         # in the unscaled drawing space
ROW_X0, ROW_X1 = 36, 640 - 50
OUTSIDE = -1                  # hit_test(): the touch landed outside the menu panel

ACCENT   = (254, 217, 15, 255)
PANEL_BG = (14, 18, 30, 232)
TEXT     = (240, 242, 248, 255)
DIM      = (150, 158, 178, 255)
DARK     = (24, 26, 36, 255)

MATRIX     = (0, 255, 70, 255)
MATRIX_MID = (0, 190, 55, 255)
MATRIX_DIM = (0, 120, 38, 255)


def _s(v: float) -> int:
    return int(round(v * SS))


def _rrect(d, box, radius, fill, outline=None, width=0):
    """Rounded rectangle that also works on old Pillow (8.1 has no rounded_rectangle)."""
    def shape(b, rad, color):
        x0, y0, x1, y1 = [int(v) for v in b]
        rad = int(max(0, min(rad, (x1 - x0) // 2, (y1 - y0) // 2)))
        d.rectangle((x0 + rad, y0, x1 - rad, y1), fill=color)
        d.rectangle((x0, y0 + rad, x1, y1 - rad), fill=color)
        for ex, ey in ((x0, y0), (x1 - 2 * rad, y0), (x0, y1 - 2 * rad), (x1 - 2 * rad, y1 - 2 * rad)):
            d.ellipse((ex, ey, ex + 2 * rad, ey + 2 * rad), fill=color)
    if outline is not None:
        shape(box, radius, outline)
        x0, y0, x1, y1 = box
        shape((x0 + width, y0 + width, x1 - width, y1 - width), radius - width, fill)
    else:
        shape(box, radius, fill)


# ─────────────────────────────────────────────
#  Icons (vector, drawn at the supersampled size)
# ─────────────────────────────────────────────
def _icon(img, d, kind, cx, cy, color):
    def P(*pts):
        d.polygon([(_s(cx + x), _s(cy + y)) for x, y in pts], fill=color)

    def R(x0, y0, x1, y1):
        d.rectangle((_s(cx + x0), _s(cy + y0), _s(cx + x1), _s(cy + y1)), fill=color)

    def L(pts, w=2):
        d.line([(_s(cx + x), _s(cy + y)) for x, y in pts], fill=color, width=_s(w))

    def O(r, w=2):
        d.ellipse((_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)), outline=color, width=_s(w))

    if kind == "play":
        P((-7, -10), (-7, 10), (10, 0))
    elif kind == "pause":
        R(-8, -10, -3, 10)
        R(3, -10, 8, 10)
    elif kind == "next":
        P((-10, -10), (-10, 10), (4, 0))
        R(6, -10, 10, 10)
    elif kind == "prev":
        P((10, -10), (10, 10), (-4, 0))
        R(-10, -10, -6, 10)
    elif kind == "shuffle":
        L([(-11, 7), (-3, 7), (3, -7), (7, -7)], 3)
        L([(-11, -7), (-3, -7), (3, 7), (7, 7)], 3)
        P((7, -12), (12, -7), (7, -2))
        P((7, 2), (12, 7), (7, 12))
    elif kind == "sun":
        d.ellipse((_s(cx - 5), _s(cy - 5), _s(cx + 5), _s(cy + 5)), fill=color)
        for i in range(8):
            a = i * math.pi / 4
            L([(8 * math.cos(a), 8 * math.sin(a)), (12 * math.cos(a), 12 * math.sin(a))])
    elif kind == "tv":
        d.rectangle((_s(cx - 11), _s(cy - 8), _s(cx + 11), _s(cy + 9)), outline=color, width=_s(2))
        L([(-5, -12), (0, -8), (5, -12)])
    elif kind == "gear":
        O(6, 3)
        for i in range(8):
            a = i * math.pi / 4
            L([(8 * math.cos(a), 8 * math.sin(a)), (12 * math.cos(a), 12 * math.sin(a))], 3)
    elif kind == "back":
        L([(5, -10), (-6, 0), (5, 10)], 3)
    elif kind == "power":
        d.arc((_s(cx - 10), _s(cy - 10), _s(cx + 10), _s(cy + 10)), -60, 240, fill=color, width=_s(3))
        L([(0, -12), (0, -1)], 3)
    elif kind == "moon":
        mask = Image.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        md.ellipse((_s(cx - 10), _s(cy - 10), _s(cx + 10), _s(cy + 10)), fill=255)
        md.ellipse((_s(cx - 4), _s(cy - 13), _s(cx + 16), _s(cy + 7)), fill=0)
        img.paste(Image.new("RGBA", img.size, color), (0, 0), mask)
    elif kind == "target":
        O(8, 2)
        L([(-12, 0), (12, 0)])
        L([(0, -12), (0, 12)])
    elif kind == "reshuffle":
        d.arc((_s(cx - 9), _s(cy - 9), _s(cx + 9), _s(cy + 9)), 40, 330, fill=color, width=_s(3))
        P((6, -12), (13, -4), (3, -3))
    else:
        L([(-8, 0), (8, 0)])


# ─────────────────────────────────────────────
#  Menu
# ─────────────────────────────────────────────
def visible_window(selected: int, total: int, rows: int = VISIBLE_ROWS) -> Tuple[int, int]:
    """(start, end) of the rows to show, keeping the selection near the middle."""
    if total <= rows:
        return 0, total
    start = max(0, selected - rows // 2)
    start = min(start, total - rows)
    return start, start + rows


def clamp_top(top: int, total: int) -> int:
    return max(0, min(top, max(0, total - VISIBLE_ROWS)))


def hit_test(vx: float, vy: float, top: int, total: int, hw: Hardware) -> Optional[int]:
    """
    Map a touch (landscape view coordinates) to a menu row.
    Returns the row index, OUTSIDE (off the panel), or None (on the panel but not on a row).
    """
    cx, cy = hw.bezel_center
    dx = hw.view_w / 2 + (vx - cx) / hw.bezel_scale          # undo the shrink/centre
    dy = hw.view_h / 2 + (vy - cy) / hw.bezel_scale
    x0, y0, x1, y1 = PANEL_RECT
    if not (x0 <= dx <= x1 and y0 <= dy <= y1):
        return OUTSIDE
    if not (ROW_X0 - 8 <= dx <= x1 - 6):
        return None
    end = min(total, top + VISIBLE_ROWS)
    n = int((dy - (ROWS_Y0 - ROW_GAP / 2)) // (ROW_H + ROW_GAP))
    if 0 <= n < end - top and dy >= ROWS_Y0 - ROW_GAP / 2:
        return top + n
    return None


def render_menu_landscape(items: Sequence, selected: int, hw: Hardware, title: str = "Tubeless",
                          subtitle: str = "", top: Optional[int] = None,
                          footer: str = "TAP  choose        SWIPE  scroll        HOLD  close") -> Image.Image:
    """
    items: objects with .icon .label and optional .value / .current.
    Returns an RGBA image, hw.view_w x hw.view_h (landscape), transparent outside the menu.
    """
    W, H = hw.view_w, hw.view_h
    img = Image.new("RGBA", (_s(W), _s(H)), ACCENT[:3] + (0,))   # yellow-tinted so edges don't go dark when shrunk
    d = ImageDraw.Draw(img)

    _rrect(d, (_s(PANEL_RECT[0]), _s(PANEL_RECT[1]), _s(PANEL_RECT[2]), _s(PANEL_RECT[3])),
           _s(20), PANEL_BG, outline=ACCENT, width=_s(2))

    d.text((_s(46), _s(24)), title, font=fonts.load(fonts.SANS_BOLD, 28 * SS), fill=ACCENT)
    if subtitle:
        d.text((_s(W - 46), _s(30)), subtitle, font=fonts.load(fonts.SANS, 20 * SS), fill=DIM, anchor="ra")
    d.line([(_s(40), _s(68)), (_s(W - 40), _s(68))], fill=(60, 68, 92, 255), width=_s(1))

    total = len(items)
    if top is None:
        start, end = visible_window(selected, total)
    else:
        start = clamp_top(top, total)
        end = min(total, start + VISIBLE_ROWS)
    label_font = fonts.load(fonts.SANS_BOLD, 24 * SS)
    value_font = fonts.load(fonts.SANS_BOLD, 22 * SS)
    y = ROWS_Y0
    for i in range(start, end):
        item = items[i]
        sel = i == selected
        x0, x1 = ROW_X0, ROW_X1
        if sel:
            _rrect(d, (_s(x0), _s(y), _s(x1), _s(y + ROW_H)), _s(12), ACCENT)
        fg = DARK if sel else TEXT
        _icon(img, d, getattr(item, "icon", "tv"), x0 + 32, y + ROW_H / 2, fg)
        d.text((_s(x0 + 62), _s(y + ROW_H / 2)), item.label, font=label_font, fill=fg, anchor="lm")
        value = getattr(item, "value", None)
        if value:
            d.text((_s(x1 - 18), _s(y + ROW_H / 2)), str(value), font=value_font,
                   fill=(DARK if sel else ACCENT), anchor="rm")
        elif getattr(item, "current", False):
            cx, cy = x1 - 26, y + ROW_H / 2
            d.ellipse((_s(cx - 7), _s(cy - 7), _s(cx + 7), _s(cy + 7)), fill=(DARK if sel else ACCENT))
        y += ROW_H + ROW_GAP

    if total > VISIBLE_ROWS:                                  # scroll bar
        track_top = ROWS_Y0
        track_bot = ROWS_Y0 + VISIBLE_ROWS * (ROW_H + ROW_GAP) - ROW_GAP
        th = (track_bot - track_top) * VISIBLE_ROWS / total
        ty = track_top + (track_bot - track_top - th) * start / (total - VISIBLE_ROWS)
        sx = W - 38
        _rrect(d, (_s(sx), _s(track_top), _s(sx + 5), _s(track_bot)), _s(3), (40, 46, 66, 255))
        _rrect(d, (_s(sx), _s(ty), _s(sx + 5), _s(ty + th)), _s(3), ACCENT)

    d.text((_s(W / 2), _s(H - 32)), footer, font=fonts.load(fonts.SANS, 15 * SS), fill=DIM, anchor="mm")

    # Shrink (from the supersampled drawing, so text stays crisp) and centre on the bezel's visible opening.
    w, h = int(round(W * hw.bezel_scale)), int(round(H * hw.bezel_scale))
    small = img.resize((w, h), Image.BOX)
    out = Image.new("RGBA", (W, H), ACCENT[:3] + (0,))
    out.paste(small, (int(round(hw.bezel_center[0] - w / 2)), int(round(hw.bezel_center[1] - h / 2))))
    return out


# ─────────────────────────────────────────────
#  Full-screen screens (calibration, shutdown ...) in the boot screen's Matrix style
# ─────────────────────────────────────────────
def _blank(hw: Hardware):
    img = Image.new("RGBA", (hw.view_w, hw.view_h), (0, 0, 0, 255))
    return img, ImageDraw.Draw(img)


def render_message(hw: Hardware, title: str, lines: Sequence[str] = (), name: str = "") -> Image.Image:
    img, d = _blank(hw)
    cx, cy = hw.bezel_center
    if name:
        d.text((cx, 70), name.upper(), font=fonts.load(fonts.MONO_BOLD, 22), fill=MATRIX_DIM, anchor="mm")
    d.text((cx, cy - 20), title, font=fonts.load(fonts.MONO_BOLD, 30), fill=MATRIX, anchor="mm")
    y = cy + 28
    for line in lines:
        d.text((cx, y), line, font=fonts.load(fonts.MONO, 18), fill=MATRIX_MID, anchor="mm")
        y += 26
    return img


def calibration_targets(hw: Hardware):
    """Where the wizard asks you to tap (landscape view coordinates): four corners, then the middle."""
    cx, cy = hw.bezel_center
    dx, dy = int(hw.view_w * 0.31), int(hw.view_h * 0.31)
    return [(cx - dx, cy - dy), (cx + dx, cy - dy), (cx + dx, cy + dy), (cx - dx, cy + dy), (cx, cy)]


def render_calibration(hw: Hardware, step: int, total: int, target: Tuple[float, float],
                       message: str = "", name: str = "") -> Image.Image:
    img, d = _blank(hw)
    tx, ty = target
    for r, col in ((30, MATRIX_DIM), (18, MATRIX_MID)):
        d.ellipse((tx - r, ty - r, tx + r, ty + r), outline=col, width=2)
    d.line([(tx - 40, ty), (tx + 40, ty)], fill=MATRIX, width=2)
    d.line([(tx, ty - 40), (tx, ty + 40)], fill=MATRIX, width=2)
    d.ellipse((tx - 3, ty - 3, tx + 3, ty + 3), fill=MATRIX)
    cx, cy = hw.bezel_center
    title_y = cy - 12 if ty > cy + 30 or ty < cy - 30 else cy + 62
    d.text((cx, title_y - 16), "CALIBRATE TOUCH", font=fonts.load(fonts.MONO_BOLD, 22), fill=MATRIX, anchor="mm")
    d.text((cx, title_y + 12), message or "Tap the crosshair", font=fonts.load(fonts.MONO, 17), fill=MATRIX_MID, anchor="mm")
    d.text((cx, title_y + 38), "%d of %d" % (step + 1, total), font=fonts.load(fonts.MONO, 15), fill=MATRIX_DIM, anchor="mm")
    return img


# ─────────────────────────────────────────────
#  Hand-off to the player
# ─────────────────────────────────────────────
def to_panel_bgra(img: Image.Image, hw: Hardware) -> Tuple[bytes, int, int]:
    """
    Premultiplied BGRA bytes in the panel's native orientation (what mpv's overlay-add wants).
    Returns (bytes, width, height) of the rotated image.
    """
    # PIL does the premultiply and the turn in C: about 4x faster than numpy on a Pi Zero, identical bytes.
    transpose = getattr(Image, "Transpose", Image)
    turns = {0: None, 1: transpose.ROTATE_90, 2: transpose.ROTATE_180, 3: transpose.ROTATE_270}[hw.rot_k % 4]
    pm = img.convert("RGBA").convert("RGBa")
    if turns is not None:
        pm = pm.transpose(turns)
    r, g, b, a = pm.split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes(), pm.width, pm.height


def from_panel_bgra(raw: bytes, w: int, h: int, hw: Hardware) -> np.ndarray:
    """The inverse of to_panel_bgra(): back to a landscape premultiplied BGRA array (used by the simulator)."""
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    return np.ascontiguousarray(np.rot90(arr, -hw.rot_k))
