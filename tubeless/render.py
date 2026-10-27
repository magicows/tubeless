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

from . import fonts, themes
from .hardware import Hardware

SS = 2                         # supersampling for smooth edges
ROW_H, ROW_GAP = 50, 4
VISIBLE_ROWS = 6
ROWS_Y0 = 78
PANEL_RECT = (22, 12, 640 - 22, 480 - 12)         # in the unscaled drawing space
ROW_X0, ROW_X1 = 36, 640 - 50
OUTSIDE = -1                  # hit_test(): the touch landed outside the menu panel
BUTTON_SIZE, BUTTON_GAP, BUTTON_Y0 = 42, 10, 20      # the icon buttons at the top right of the menu panel
BUTTON_X1 = PANEL_RECT[2] - 24
HEADER_BUTTONS = ("guide", "upload", "power")        # left to right

# The colours come from the active theme (themes.py); the boot screen keeps its own.


def _rgba(c, alpha: int = 255) -> Tuple[int, int, int, int]:
    return (c[0], c[1], c[2], alpha)


def _sc():
    """The active theme's colours for the full-screen info screens, as RGBA:
    (background, bright, mid, faint, border, text on bright, strong)."""
    return tuple(_rgba(c) for c in themes.active().screen_colours())


def _frame() -> int:
    """Border width of the info screens' frame: thick in the neobrutalist style."""
    return 3 if themes.active().brutal else 1


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
    elif kind == "guide":
        for y in (-8, 0, 8):
            R(-12, y - 2, -8, y + 2)
            L([(-4, y), (12, y)], 3)
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


def button_rect(name: str) -> Tuple[int, int, int, int]:
    """Where a header button sits in the unscaled drawing space."""
    i = len(HEADER_BUTTONS) - 1 - HEADER_BUTTONS.index(name)         # counted from the right
    x1 = BUTTON_X1 - i * (BUTTON_SIZE + BUTTON_GAP)
    return (x1 - BUTTON_SIZE, BUTTON_Y0, x1, BUTTON_Y0 + BUTTON_SIZE)


def header_hit(vx: float, vy: float, hw: Hardware) -> Optional[str]:
    """The header button under a touch (landscape view coordinates), or None."""
    cx, cy = hw.bezel_center
    dx = hw.view_w / 2 + (vx - cx) / hw.bezel_scale
    dy = hw.view_h / 2 + (vy - cy) / hw.bezel_scale
    for name in HEADER_BUTTONS:
        x0, y0, x1, y1 = button_rect(name)
        if x0 - 5 <= dx <= x1 + 5 and y0 - 5 <= dy <= y1 + 5:
            return name
    return None


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
                          footer: str = "TAP  choose        SWIPE  scroll        HOLD  close",
                          armed: Sequence[str] = (), lit: Sequence[str] = ()) -> Image.Image:
    """
    items: objects with .icon .label and optional .value / .current / .number (drawn in place of the icon) / .detail (a small line under the label).
    armed: names of header buttons waiting for their second tap (drawn filled); lit: ones that are switched on (drawn the same way).
    The TV name, a small gap, then the channel name sit at the left; the icon buttons at the right.
    Colours and shapes come from the active theme.
    """
    t = themes.active()
    accent, ink, text, dim = _rgba(t.accent), _rgba(t.outline_colour), _rgba(t.text), _rgba(t.dim)
    panel, on_accent, title_col = _rgba(t.panel, t.panel_alpha), _rgba(t.on_accent), _rgba(t.accent_text)
    W, H = hw.view_w, hw.view_h
    img = Image.new("RGBA", (_s(W), _s(H)), accent[:3] + (0,))    # accent-tinted so edges don't go dark when shrunk
    d = ImageDraw.Draw(img)

    px0, py0, px1, py1 = PANEL_RECT
    if t.shadow:
        _rrect(d, (_s(px0 + t.shadow), _s(py0 + t.shadow), _s(px1 + t.shadow), _s(py1 + t.shadow)), _s(t.panel_radius), ink)
    _rrect(d, (_s(px0), _s(py0), _s(px1), _s(py1)), _s(t.panel_radius), panel, outline=ink, width=_s(t.outline))

    room = _s(button_rect(HEADER_BUTTONS[0])[0] - 16) - _s(46)    # from the left margin to the first button
    gap = _s(14)
    # Both names at full size if they fit; otherwise a little smaller before anything is cut short (the folder name can be long).
    for title_px, sub_px in ((28, 20), (24, 18), (21, 16)):
        title_font, sub_font = fonts.load(fonts.SANS_BOLD, title_px * SS), fonts.load(fonts.SANS, sub_px * SS)
        need = d.textlength(title, font=title_font) + (gap + d.textlength(subtitle, font=sub_font) if subtitle else 0)
        if need <= room:
            break
    if subtitle:
        tw, sw = d.textlength(title, font=title_font), d.textlength(subtitle, font=sub_font)
        if tw + gap + sw > room:                                  # still too long at the smallest size: the title keeps what it needs
            title_room = min(tw, max(room * 0.5, room - gap - sw))
            title = fit_text(d, title, title_font, title_room)
            subtitle = fit_text(d, subtitle, sub_font, room - gap - d.textlength(title, font=title_font))
    else:
        title = fit_text(d, title, title_font, room)
    d.text((_s(46), _s(52)), title, font=title_font, fill=title_col, anchor="ls")
    if subtitle:
        d.text((_s(46) + d.textlength(title, font=title_font) + gap, _s(52)), subtitle, font=sub_font, fill=dim, anchor="ls")
    btn_w = 3 if t.brutal else 2
    for name in HEADER_BUTTONS:
        x0, y0, x1, y1 = button_rect(name)
        on = name in armed or name in lit
        _rrect(d, (_s(x0), _s(y0), _s(x1), _s(y1)), _s(0 if t.brutal else 10), accent if on else panel, outline=ink, width=_s(btn_w))
        _icon(img, d, name, (x0 + x1) / 2, (y0 + y1) / 2, on_accent if on else (ink if t.brutal else title_col))
    rule = ink if t.brutal else _rgba(t.line)
    d.line([(_s(40), _s(68)), (_s(W - 40), _s(68))], fill=rule, width=_s(3 if t.brutal else 1))

    total = len(items)
    if top is None:
        start, end = visible_window(selected, total)
    else:
        start = clamp_top(top, total)
        end = min(total, start + VISIBLE_ROWS)
    label_font = fonts.load(fonts.SANS_BOLD, 24 * SS)
    value_font = fonts.load(fonts.SANS_BOLD, 22 * SS)
    detail_font = fonts.load(fonts.SANS, 15 * SS)
    y = ROWS_Y0
    for i in range(start, end):
        item = items[i]
        sel = i == selected
        x0, x1 = ROW_X0, ROW_X1
        if t.row_outline:
            _rrect(d, (_s(x0), _s(y), _s(x1), _s(y + ROW_H)), _s(t.radius), accent if sel else panel, outline=ink, width=_s(t.row_outline))
        elif sel:
            _rrect(d, (_s(x0), _s(y), _s(x1), _s(y + ROW_H)), _s(t.radius), accent)
        fg = on_accent if sel else text
        number, detail = getattr(item, "number", None), getattr(item, "detail", "")
        if number is None:
            _icon(img, d, getattr(item, "icon", "tv"), x0 + 32, y + ROW_H / 2, fg)
        else:
            d.text((_s(x0 + 32), _s(y + ROW_H / 2)), str(number), font=label_font, fill=(on_accent if sel else title_col), anchor="mm")
        if detail:                                                 # two lines: the name, and under it what is on
            d.text((_s(x0 + 62), _s(y + 17)), fit_text(d, item.label, label_font, _s(x1 - x0 - 62 - 44)), font=label_font, fill=fg, anchor="lm")
            d.text((_s(x0 + 62), _s(y + 38)), fit_text(d, detail, detail_font, _s(x1 - x0 - 62 - 44)), font=detail_font,
                   fill=(on_accent if sel else dim), anchor="lm")
        else:
            d.text((_s(x0 + 62), _s(y + ROW_H / 2)), item.label, font=label_font, fill=fg, anchor="lm")
        value = getattr(item, "value", None)
        if value:
            d.text((_s(x1 - 18), _s(y + ROW_H / 2)), str(value), font=value_font,
                   fill=(on_accent if sel else title_col), anchor="rm")
        elif getattr(item, "current", False):
            cx, cy = x1 - 26, y + ROW_H / 2
            d.ellipse((_s(cx - 7), _s(cy - 7), _s(cx + 7), _s(cy + 7)), fill=(on_accent if sel else title_col))
        y += ROW_H + ROW_GAP

    if total > VISIBLE_ROWS:                                  # scroll bar
        track_top = ROWS_Y0
        track_bot = ROWS_Y0 + VISIBLE_ROWS * (ROW_H + ROW_GAP) - ROW_GAP
        th = (track_bot - track_top) * VISIBLE_ROWS / total
        ty = track_top + (track_bot - track_top - th) * start / (total - VISIBLE_ROWS)
        sx = W - 38
        _rrect(d, (_s(sx), _s(track_top), _s(sx + 5), _s(track_bot)), _s(3), _rgba(themes.mix(t.panel, t.text, 0.14)))
        _rrect(d, (_s(sx), _s(ty), _s(sx + 5), _s(ty + th)), _s(3), title_col)

    d.text((_s(W / 2), _s(H - 32)), footer, font=fonts.load(fonts.SANS, 15 * SS), fill=dim, anchor="mm")

    # Shrink (from the supersampled drawing, so text stays crisp) and centre on the bezel's visible opening.
    w, h = int(round(W * hw.bezel_scale)), int(round(H * hw.bezel_scale))
    small = img.resize((w, h), Image.BOX)
    out = Image.new("RGBA", (W, H), accent[:3] + (0,))
    out.paste(small, (int(round(hw.bezel_center[0] - w / 2)), int(round(hw.bezel_center[1] - h / 2))))
    return out


# ─────────────────────────────────────────────
#  Full-screen screens (calibration, shutdown ...) in the boot screen's Matrix style
# ─────────────────────────────────────────────
def _blank(hw: Hardware):
    img = Image.new("RGBA", (hw.view_w, hw.view_h), _sc()[0])
    return img, ImageDraw.Draw(img)


def render_message(hw: Hardware, title: str, lines: Sequence[str] = (), name: str = "") -> Image.Image:
    BG, BR, MID, FAINT, BORDER, ONBR, STRONG = _sc()
    img, d = _blank(hw)
    cx, cy = hw.bezel_center
    if name:
        d.text((cx, 70), name.upper(), font=fonts.load(fonts.MONO_BOLD, 22), fill=FAINT, anchor="mm")
    d.text((cx, cy - 20), title, font=fonts.load(fonts.MONO_BOLD, 30), fill=BR, anchor="mm")
    y = cy + 28
    for line in lines:
        d.text((cx, y), line, font=fonts.load(fonts.MONO, 18), fill=MID, anchor="mm")
        y += 26
    return img


def render_bear_with_us(hw: Hardware) -> Image.Image:
    """The 'please stand by' card shown while the player restarts between cycles (or after a hiccup)."""
    img = Image.new("RGBA", (hw.view_w, hw.view_h), (24, 38, 74, 255))
    d = ImageDraw.Draw(img)
    cx, cy = hw.bezel_center
    u = min(hw.view_w, hw.view_h) / 480.0              # drawn for a 480 px short side
    by = cy - 55 * u                                    # the bear's face centre
    brown, dark, tan = (150, 98, 54, 255), (92, 58, 30, 255), (226, 186, 132, 255)

    def ell(x, y, rx, ry, fill):
        d.ellipse((cx + (x - rx) * u, by + (y - ry) * u, cx + (x + rx) * u, by + (y + ry) * u), fill=fill)

    for sx in (-1, 1):                                  # ears
        ell(sx * 78, -78, 34, 34, brown)
        ell(sx * 78, -78, 18, 18, tan)
    ell(0, 0, 105, 95, brown)                           # head
    ell(0, 34, 52, 38, tan)                             # muzzle
    ell(0, 14, 17, 11, dark)                            # nose
    for sx in (-1, 1):
        ell(sx * 40, -22, 11, 14, (255, 255, 255, 255))     # eyes, looking up and to the side
        ell(sx * 40 + 3, -26, 5, 6, (20, 20, 20, 255))
    d.arc((cx - 24 * u, by + 36 * u, cx + 24 * u, by + 66 * u), 200, 340, fill=dark, width=max(2, int(4 * u)))   # worried mouth
    ell(88, -40, 7, 11, (120, 200, 255, 255))           # a bead of sweat
    d.text((cx, by + 140 * u), "BEAR WITH US", font=fonts.load(fonts.MONO_BOLD, max(16, int(40 * u))),
           fill=(255, 214, 90, 255), anchor="mm")
    d.text((cx, by + 182 * u), "Technical difficulties. Please stand by.", font=fonts.load(fonts.MONO, max(10, int(17 * u))),
           fill=(200, 210, 235, 255), anchor="mm")
    return img


def render_standby(hw: Hardware, path: str) -> Image.Image:
    """The 'technical difficulties' picture, shown whole on the screen (any spare edge takes the picture's border colour).
    Falls back to the bear if the file can't be read."""
    try:
        with Image.open(path) as src:
            pic = src.convert("RGBA")
    except Exception:
        return render_bear_with_us(hw)
    scale = min(hw.view_w / pic.width, hw.view_h / pic.height)
    size = (max(1, round(pic.width * scale)), max(1, round(pic.height * scale)))
    pic = pic.resize(size, getattr(Image, "Resampling", Image).LANCZOS)
    img = Image.new("RGBA", (hw.view_w, hw.view_h), pic.getpixel((0, 0)))
    img.paste(pic, ((hw.view_w - size[0]) // 2, (hw.view_h - size[1]) // 2))
    return img


def calibration_targets(hw: Hardware):
    """Where the wizard asks you to tap (landscape view coordinates): four corners, then the middle."""
    cx, cy = hw.bezel_center
    dx, dy = int(hw.view_w * 0.31), int(hw.view_h * 0.31)
    return [(cx - dx, cy - dy), (cx + dx, cy - dy), (cx + dx, cy + dy), (cx - dx, cy + dy), (cx, cy)]


def render_calibration(hw: Hardware, step: int, total: int, target: Tuple[float, float],
                       message: str = "", name: str = "") -> Image.Image:
    BG, BR, MID, FAINT, BORDER, ONBR, STRONG = _sc()
    img, d = _blank(hw)
    tx, ty = target
    for r, col in ((30, FAINT), (18, MID)):
        d.ellipse((tx - r, ty - r, tx + r, ty + r), outline=col, width=2)
    d.line([(tx - 40, ty), (tx + 40, ty)], fill=BR, width=2)
    d.line([(tx, ty - 40), (tx, ty + 40)], fill=BR, width=2)
    d.ellipse((tx - 3, ty - 3, tx + 3, ty + 3), fill=BR)
    cx, cy = hw.bezel_center
    title_y = cy - 12 if ty > cy + 30 or ty < cy - 30 else cy + 62
    d.text((cx, title_y - 16), "CALIBRATE TOUCH", font=fonts.load(fonts.MONO_BOLD, 22), fill=BR, anchor="mm")
    d.text((cx, title_y + 12), message or "Tap the crosshair", font=fonts.load(fonts.MONO, 17), fill=MID, anchor="mm")
    d.text((cx, title_y + 38), "%d of %d" % (step + 1, total), font=fonts.load(fonts.MONO, 15), fill=FAINT, anchor="mm")
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

# the two buttons on the empty screen, in the landscape view's coordinates
EMPTY_BUTTONS = {"upload": (84, 332, 322, 392), "recheck": (338, 332, 556, 392)}


def empty_button_at(vx: float, vy: float) -> str:
    """Which button a tap on the 'no videos' screen is on ('' if neither). A little generous, for fingers."""
    for name, (x0, y0, x1, y1) in EMPTY_BUTTONS.items():
        if x0 - 10 <= vx <= x1 + 10 and y0 - 12 <= vy <= y1 + 12:
            return name
    return ""


def render_empty(hw: Hardware, name: str, folder: str, notice: str = "") -> Image.Image:
    BG, BR, MID, FAINT, BORDER, ONBR, STRONG = _sc()
    img, d = _blank(hw)
    x0, y0, x1, y1 = hw.safe_rect
    left = x0 + 24
    d.rectangle(hw.safe_rect, outline=BORDER, width=_frame())
    d.text((left, y0 + 16), fit_text(d, name.upper(), fonts.load(fonts.MONO_BOLD, 22), 300), font=fonts.load(fonts.MONO_BOLD, 22), fill=FAINT)
    d.text((left, y0 + 52), "NO VIDEOS YET", font=fonts.load(fonts.MONO_BOLD, 38), fill=BR)
    lines = [
        ("This TV plays the videos in this folder,", MID),
        ("one sub-folder for each channel:", MID),
        ("    " + folder + "/<channel>/", BR),
        ("", MID),
        ("Copy them over the network (SFTP/scp),", MID),
        ("or add them from your phone or laptop.", MID),
    ]
    y = y0 + 112
    for text, colour in lines:
        d.text((left, y), text, font=fonts.load(fonts.MONO, 16), fill=colour)
        y += 24
    for key, label in (("upload", "UPLOAD PAGE"), ("recheck", "CHECK AGAIN")):
        bx0, by0, bx1, by1 = EMPTY_BUTTONS[key]
        solid = key == "upload"
        d.rectangle((bx0, by0, bx1, by1), fill=BR if solid else BG, outline=BR, width=2)
        d.text(((bx0 + bx1) // 2, (by0 + by1) // 2), label, font=fonts.load(fonts.MONO_BOLD, 20),
               fill=ONBR if solid else BR, anchor="mm")
    d.text((x0 + 24, y1 - 22), notice or "Tap a button.", font=fonts.load(fonts.MONO, 14), fill=FAINT)
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
    BG, BR, MID, FAINT, BORDER, ONBR, STRONG = _sc()
    img, d = _blank(hw)
    x0, y0, x1, y1 = hw.safe_rect
    left = x0 + 24
    d.rectangle(hw.safe_rect, outline=BORDER, width=_frame())
    d.text((left, y0 + 14), fit_text(d, name.upper(), fonts.load(fonts.MONO_BOLD, 18), 300), font=fonts.load(fonts.MONO_BOLD, 18), fill=FAINT)
    d.text((left, y0 + 40), "UPLOAD PAGE", font=fonts.load(fonts.MONO_BOLD, 32), fill=BR)

    top = y0 + 92
    size = 176
    if qr:
        side = _draw_qr(d, qr, left, top, size)
        text_x = left + side + 22
    else:
        text_x = left
    y = top
    d.text((text_x, y), "Scan, or open on your phone:" if qr else "Open on your phone or laptop:", font=fonts.load(fonts.MONO, 15), fill=MID)
    y += 24
    for url in urls:
        d.text((text_x, y), fit_text(d, url, fonts.load(fonts.MONO_BOLD, 16), x1 - 12 - text_x), font=fonts.load(fonts.MONO_BOLD, 16), fill=BR)
        y += 24
    y += 12
    d.text((text_x, y), "PIN", font=fonts.load(fonts.MONO, 15), fill=MID)
    d.text((text_x + 48, y - 14), pin, font=fonts.load(fonts.MONO_BOLD, 46), fill=STRONG)

    sy = max(top + (side if qr else 0) + 14, y + 52)
    for line in status[:3]:
        d.text((left, sy), fit_text(d, line, fonts.load(fonts.MONO, 16), x1 - 24 - left), font=fonts.load(fonts.MONO, 16), fill=MID)
        sy += 22
    if progress is not None:
        by0, by1 = sy + 4, sy + 18
        d.rectangle((left, by0, x1 - 24, by1), outline=FAINT, width=1)
        w = int((x1 - 24 - left - 4) * max(0.0, min(1.0, progress)))
        if w > 0:
            d.rectangle((left + 2, by0 + 2, left + 2 + w, by1 - 2), fill=BR)
    d.text((x0 + 24, y1 - 22), "HOLD the screen to finish and go back to the TV", font=fonts.load(fonts.MONO, 14), fill=FAINT)
    return img


def render_setup(hw: Hardware, name: str, ssid: str, password: str, qr: Optional[Sequence[Sequence[bool]]],
                 address: str, status: Sequence[str]) -> Image.Image:
    BG, BR, MID, FAINT, BORDER, ONBR, STRONG = _sc()
    """Wi-Fi setup: join the TV's hotspot, then pick your own network on the page that opens."""
    img, d = _blank(hw)
    x0, y0, x1, y1 = hw.safe_rect
    left = x0 + 24
    d.rectangle(hw.safe_rect, outline=BORDER, width=_frame())
    d.text((left, y0 + 14), fit_text(d, name.upper(), fonts.load(fonts.MONO_BOLD, 18), 300), font=fonts.load(fonts.MONO_BOLD, 18), fill=FAINT)
    d.text((left, y0 + 40), "WI-FI SETUP", font=fonts.load(fonts.MONO_BOLD, 32), fill=BR)
    top, size = y0 + 92, 168
    if qr:
        side = _draw_qr(d, qr, left, top, size)
        text_x = left + side + 22
    else:
        side, text_x = 0, left
    mono, bold = fonts.load(fonts.MONO, 15), fonts.load(fonts.MONO_BOLD, 17)
    y = top
    d.text((text_x, y), "1. Join this Wi-Fi" + (" (scan me)" if qr else ""), font=mono, fill=MID)
    y += 22
    d.text((text_x, y), fit_text(d, ssid, bold, x1 - 12 - text_x), font=bold, fill=BR)
    y += 24
    d.text((text_x, y), "password", font=mono, fill=FAINT)
    d.text((text_x + 84, y - 2), password, font=fonts.load(fonts.MONO_BOLD, 22), fill=STRONG)
    y += 38
    d.text((text_x, y), "2. Pick your network on the", font=mono, fill=MID)
    y += 20
    d.text((text_x, y), "page that opens, or visit", font=mono, fill=MID)
    y += 22
    d.text((text_x, y), address, font=bold, fill=BR)
    sy = max(top + side + 14, y + 34)
    for line in status[:3]:
        d.text((left, sy), fit_text(d, line, fonts.load(fonts.MONO, 16), x1 - 24 - left), font=fonts.load(fonts.MONO, 16), fill=MID)
        sy += 22
    d.text((x0 + 24, y1 - 22), "HOLD the screen to cancel", font=fonts.load(fonts.MONO, 14), fill=FAINT)
    return img
