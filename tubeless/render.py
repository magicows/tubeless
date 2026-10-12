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


def fit_text(draw, text: str, font, max_width: float) -> str:
    """`text`, shortened with an ellipsis if it would be wider than `max_width` pixels."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while len(text) > 1 and draw.textlength(text + "\u2026", font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + "\u2026"


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
    elif kind == "upload":
        P((0, -12), (-8, -3), (8, -3))
        R(-3, -3, 3, 5)
        L([(-10, 4), (-10, 10), (10, 10), (10, 4)], 2)
    elif kind == "wifi":
        for r in (5, 9, 13):
            d.arc((_s(cx - r), _s(cy + 8 - r), _s(cx + r), _s(cy + 8 + r)), 230, 310, fill=color, width=_s(2.5))
        d.ellipse((_s(cx - 2), _s(cy + 6), _s(cx + 2), _s(cy + 10)), fill=color)
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

    title_font, sub_font = fonts.load(fonts.SANS_BOLD, 28 * SS), fonts.load(fonts.SANS, 20 * SS)
    room = _s(W - 46) - _s(46)                                   # between the two text margins
    if subtitle:
        subtitle = fit_text(d, subtitle, sub_font, room * 0.45)
        room -= d.textlength(subtitle, font=sub_font) + _s(20)
        d.text((_s(W - 46), _s(30)), subtitle, font=sub_font, fill=DIM, anchor="ra")
    d.text((_s(46), _s(24)), fit_text(d, title, title_font, room), font=title_font, fill=ACCENT)
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


# ─────────────────────────────────────────────
#  Full-screen screens drawn straight to the framebuffer (no player running): "no videos" and upload mode
# ─────────────────────────────────────────────
MATRIX_PAPER = (0, 0, 0)

# the two buttons on the empty screen, in the landscape view's coordinates
EMPTY_BUTTONS = {"upload": (84, 332, 322, 392), "recheck": (338, 332, 556, 392)}


def empty_button_at(vx: float, vy: float) -> str:
    """Which button a tap on the 'no videos' screen is on ('' if neither). A little generous, for fingers."""
    for name, (x0, y0, x1, y1) in EMPTY_BUTTONS.items():
        if x0 - 10 <= vx <= x1 + 10 and y0 - 12 <= vy <= y1 + 12:
            return name
    return ""


def render_empty(hw: Hardware, name: str, folder: str, notice: str = "") -> Image.Image:
    img, d = _blank(hw)
    x0, y0, x1, y1 = hw.safe_rect
    left = x0 + 24
    d.rectangle(hw.safe_rect, outline=(0, 55, 20, 255), width=1)
    d.text((left, y0 + 16), fit_text(d, name.upper(), fonts.load(fonts.MONO_BOLD, 22), 300), font=fonts.load(fonts.MONO_BOLD, 22), fill=MATRIX_DIM)
    d.text((left, y0 + 52), "NO VIDEOS YET", font=fonts.load(fonts.MONO_BOLD, 38), fill=MATRIX)
    lines = [
        ("This TV plays the videos in this folder,", MATRIX_MID),
        ("one sub-folder for each channel:", MATRIX_MID),
        ("    " + folder + "/<channel>/", MATRIX),
        ("", MATRIX_MID),
        ("Copy them over the network (SFTP/scp),", MATRIX_MID),
        ("or add them from your phone or laptop.", MATRIX_MID),
    ]
    y = y0 + 112
    for text, colour in lines:
        d.text((left, y), text, font=fonts.load(fonts.MONO, 16), fill=colour)
        y += 24
    for key, label in (("upload", "UPLOAD PAGE"), ("recheck", "CHECK AGAIN")):
        bx0, by0, bx1, by1 = EMPTY_BUTTONS[key]
        solid = key == "upload"
        d.rectangle((bx0, by0, bx1, by1), fill=MATRIX if solid else (0, 0, 0, 255), outline=MATRIX, width=2)
        d.text(((bx0 + bx1) // 2, (by0 + by1) // 2), label, font=fonts.load(fonts.MONO_BOLD, 20),
               fill=(0, 20, 6, 255) if solid else MATRIX, anchor="mm")
    d.text((x0 + 24, y1 - 22), notice or "Tap a button.", font=fonts.load(fonts.MONO, 14), fill=MATRIX_DIM)
    return img


def _draw_qr(d, qr: Sequence[Sequence[bool]], left: int, top: int, size: int) -> int:
    """Dark-on-light QR code with its quiet zone, at most `size` pixels square. Returns the side length drawn."""
    n = len(qr)
    scale = max(2, size // (n + 4))
    side = scale * (n + 4)
    d.rectangle((left, top, left + side, top + side), fill=(255, 255, 255, 255))
    for r, row in enumerate(qr):
        for c, dark in enumerate(row):
            if dark:
                px, py = left + (c + 2) * scale, top + (r + 2) * scale
                d.rectangle((px, py, px + scale - 1, py + scale - 1), fill=(0, 0, 0, 255))
    return side


def render_transfer(hw: Hardware, name: str, urls: Sequence[str], pin: str, qr: Optional[Sequence[Sequence[bool]]],
                    status: Sequence[str], progress: Optional[float] = None) -> Image.Image:
    img, d = _blank(hw)
    x0, y0, x1, y1 = hw.safe_rect
    left = x0 + 24
    d.rectangle(hw.safe_rect, outline=(0, 55, 20, 255), width=1)
    d.text((left, y0 + 14), fit_text(d, name.upper(), fonts.load(fonts.MONO_BOLD, 18), 300), font=fonts.load(fonts.MONO_BOLD, 18), fill=MATRIX_DIM)
    d.text((left, y0 + 40), "UPLOAD PAGE", font=fonts.load(fonts.MONO_BOLD, 32), fill=MATRIX)

    top = y0 + 92
    size = 176
    if qr:
        side = _draw_qr(d, qr, left, top, size)
        text_x = left + side + 22
    else:
        text_x = left
    y = top
    d.text((text_x, y), "Scan, or open on your phone:" if qr else "Open on your phone or laptop:", font=fonts.load(fonts.MONO, 15), fill=MATRIX_MID)
    y += 24
    for url in urls:
        d.text((text_x, y), fit_text(d, url, fonts.load(fonts.MONO_BOLD, 16), x1 - 12 - text_x), font=fonts.load(fonts.MONO_BOLD, 16), fill=MATRIX)
        y += 24
    y += 12
    d.text((text_x, y), "PIN", font=fonts.load(fonts.MONO, 15), fill=MATRIX_MID)
    d.text((text_x + 48, y - 14), pin, font=fonts.load(fonts.MONO_BOLD, 46), fill=(220, 255, 230, 255))

    sy = max(top + (side if qr else 0) + 14, y + 52)
    for line in status[:3]:
        d.text((left, sy), fit_text(d, line, fonts.load(fonts.MONO, 16), x1 - 24 - left), font=fonts.load(fonts.MONO, 16), fill=MATRIX_MID)
        sy += 22
    if progress is not None:
        by0, by1 = sy + 4, sy + 18
        d.rectangle((left, by0, x1 - 24, by1), outline=MATRIX_DIM, width=1)
        w = int((x1 - 24 - left - 4) * max(0.0, min(1.0, progress)))
        if w > 0:
            d.rectangle((left + 2, by0 + 2, left + 2 + w, by1 - 2), fill=MATRIX)
    d.text((x0 + 24, y1 - 22), "HOLD the screen to finish and go back to the TV", font=fonts.load(fonts.MONO, 14), fill=MATRIX_DIM)
    return img


def render_setup(hw: Hardware, name: str, ssid: str, password: str, qr: Optional[Sequence[Sequence[bool]]],
                 address: str, status: Sequence[str]) -> Image.Image:
    """Wi-Fi setup: join the TV's hotspot, then pick your own network on the page that opens."""
    img, d = _blank(hw)
    x0, y0, x1, y1 = hw.safe_rect
    left = x0 + 24
    d.rectangle(hw.safe_rect, outline=(0, 55, 20, 255), width=1)
    d.text((left, y0 + 14), fit_text(d, name.upper(), fonts.load(fonts.MONO_BOLD, 18), 300), font=fonts.load(fonts.MONO_BOLD, 18), fill=MATRIX_DIM)
    d.text((left, y0 + 40), "WI-FI SETUP", font=fonts.load(fonts.MONO_BOLD, 32), fill=MATRIX)
    top, size = y0 + 92, 168
    if qr:
        side = _draw_qr(d, qr, left, top, size)
        text_x = left + side + 22
    else:
        side, text_x = 0, left
    mono, bold = fonts.load(fonts.MONO, 15), fonts.load(fonts.MONO_BOLD, 17)
    y = top
    d.text((text_x, y), "1. Join this Wi-Fi" + (" (scan me)" if qr else ""), font=mono, fill=MATRIX_MID)
    y += 22
    d.text((text_x, y), fit_text(d, ssid, bold, x1 - 12 - text_x), font=bold, fill=MATRIX)
    y += 24
    d.text((text_x, y), "password", font=mono, fill=MATRIX_DIM)
    d.text((text_x + 84, y - 2), password, font=fonts.load(fonts.MONO_BOLD, 22), fill=(220, 255, 230, 255))
    y += 38
    d.text((text_x, y), "2. Pick your network on the", font=mono, fill=MATRIX_MID)
    y += 20
    d.text((text_x, y), "page that opens, or visit", font=mono, fill=MATRIX_MID)
    y += 22
    d.text((text_x, y), address, font=bold, fill=MATRIX)
    sy = max(top + side + 14, y + 34)
    for line in status[:3]:
        d.text((left, sy), fit_text(d, line, fonts.load(fonts.MONO, 16), x1 - 24 - left), font=fonts.load(fonts.MONO, 16), fill=MATRIX_MID)
        sy += 22
    d.text((x0 + 24, y1 - 22), "HOLD the screen to cancel", font=fonts.load(fonts.MONO, 14), fill=MATRIX_DIM)
    return img
