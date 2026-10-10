"""
SimpTV menu renderer.

The panel is natively portrait (480x640) and mpv rotates the *video* with
video-rotate=270, but mpv's own OSD is not rotated. So instead of mpv OSD text
we draw the menu here as a normal landscape picture, rotate it to match the
video, and hand it to mpv as a bitmap overlay (overlay-add).

Run mpv with --fs so its OSD coordinate space is exactly the 480x640 panel.
"""
from PIL import Image, ImageDraw, ImageFont

FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Landscape canvas as the viewer sees it, and the panel's native size.
VIEW_W, VIEW_H     = 640, 480
PANEL_W, PANEL_H   = 480, 640
SS                 = 2            # supersampling factor for smooth edges

# The 3D-printed shell has a rounded, CRT-style opening that hides the panel's
# edges and corners, so the menu is drawn a little smaller and centred on the
# visible area (measured from a photo of the finished TV).
MENU_SCALE         = 0.85
MENU_CENTER        = (326, 237)   # in the 640x480 landscape view

# video-rotate=270 turns the picture 90 degrees counter-clockwise on the panel.

ROW_H, ROW_GAP     = 50, 4
VISIBLE_ROWS       = 6

YELLOW   = (254, 217, 15, 255)
PANEL_BG = (14, 18, 30, 232)
TEXT     = (240, 242, 248, 255)
DIM      = (150, 158, 178, 255)
DARK     = (24, 26, 36, 255)

_font_cache = {}

def _font(path, size):
    key = (path, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, size * SS)
    return _font_cache[key]

def _s(v):
    return int(round(v * SS))

def _rrect(d, box, radius, fill, outline=None, width=0):
    """Rounded rectangle for old Pillow (8.1). Coordinates are already scaled."""
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
#  Icons (vector, drawn at supersampled size)
# ─────────────────────────────────────────────
def _icon(d, kind, cx, cy, color):
    r = 11
    def P(*pts):
        d.polygon([(_s(cx + x), _s(cy + y)) for x, y in pts], fill=color)
    def R(x0, y0, x1, y1):
        d.rectangle((_s(cx + x0), _s(cy + y0), _s(cx + x1), _s(cy + y1)), fill=color)

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
        w = _s(3)
        d.line([(_s(cx - 11), _s(cy + 7)), (_s(cx - 3), _s(cy + 7)), (_s(cx + 3), _s(cy - 7)), (_s(cx + 7), _s(cy - 7))], fill=color, width=w)
        d.line([(_s(cx - 11), _s(cy - 7)), (_s(cx - 3), _s(cy - 7)), (_s(cx + 3), _s(cy + 7)), (_s(cx + 7), _s(cy + 7))], fill=color, width=w)
        P((7, -12), (12, -7), (7, -2))
        P((7, 2), (12, 7), (7, 12))
    elif kind == "sun":
        d.ellipse((_s(cx - 5), _s(cy - 5), _s(cx + 5), _s(cy + 5)), fill=color)
        import math
        for i in range(8):
            a = i * math.pi / 4
            d.line([(_s(cx + 8 * math.cos(a)), _s(cy + 8 * math.sin(a))),
                    (_s(cx + 12 * math.cos(a)), _s(cy + 12 * math.sin(a)))], fill=color, width=_s(2))
    else:  # "tv"
        d.rectangle((_s(cx - 11), _s(cy - 8), _s(cx + 11), _s(cy + 9)), outline=color, width=_s(2))
        d.line([(_s(cx - 5), _s(cy - 12)), (_s(cx), _s(cy - 8)), (_s(cx + 5), _s(cy - 12))], fill=color, width=_s(2))

# ─────────────────────────────────────────────
#  Menu drawing
# ─────────────────────────────────────────────
# Menu geometry in the (unscaled) drawing space; shared by the drawing code and hit_test().
PANEL_RECT = (22, 12, VIEW_W - 22, VIEW_H - 12)
ROWS_Y0    = 78
ROW_X0, ROW_X1 = 36, VIEW_W - 50

OUTSIDE = -1   # hit_test(): touch landed outside the menu panel

def hit_test(vx, vy, top, total):
    """
    Map a touch in landscape view coordinates to a menu row. 'top' is the
    index of the first visible row.
    Returns the item index, OUTSIDE (off the panel), or None (on the panel but
    not on a row, e.g. the header or footer).
    """
    # undo the shrink/centre applied in render_menu_landscape()
    dx = VIEW_W / 2 + (vx - MENU_CENTER[0]) / MENU_SCALE
    dy = VIEW_H / 2 + (vy - MENU_CENTER[1]) / MENU_SCALE
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

def visible_window(selected, total, rows=VISIBLE_ROWS):
    """Return (start, end) of the rows to show, keeping the selection near the middle."""
    if total <= rows:
        return 0, total
    start = max(0, selected - rows // 2)
    start = min(start, total - rows)
    return start, start + rows

def render_menu_landscape(items, selected, title="SimpTV", subtitle="", top=None):
    """
    items: list of dicts with keys: icon, label, value (optional), current (optional).
    top: index of the first visible row (default: scroll so 'selected' stays in view).
    Returns an RGBA PIL image, VIEW_W x VIEW_H, in landscape orientation.
    """
    img = Image.new("RGBA", (_s(VIEW_W), _s(VIEW_H)), YELLOW[:3] + (0,))   # transparent, but yellow-tinted so edges don't go dark when shrunk
    d = ImageDraw.Draw(img)

    # Panel
    _rrect(d, (_s(22), _s(12), _s(VIEW_W - 22), _s(VIEW_H - 12)),
           _s(20), PANEL_BG, outline=YELLOW, width=_s(2))

    # Header
    d.text((_s(46), _s(24)), title, font=_font(FONT_BOLD, 28), fill=YELLOW)
    if subtitle:
        d.text((_s(VIEW_W - 46), _s(30)), subtitle, font=_font(FONT_REG, 20), fill=DIM, anchor="ra")
    d.line([(_s(40), _s(68)), (_s(VIEW_W - 40), _s(68))], fill=(60, 68, 92, 255), width=_s(1))

    # Rows
    total = len(items)
    if top is None:
        start, end = visible_window(selected, total)
    else:
        start = max(0, min(top, max(0, total - VISIBLE_ROWS)))
        end = min(total, start + VISIBLE_ROWS)
    y = 78
    label_font = _font(FONT_BOLD, 24)
    value_font = _font(FONT_BOLD, 22)
    for i in range(start, end):
        item = items[i]
        sel = (i == selected)
        x0, x1 = 36, VIEW_W - 50
        if sel:
            _rrect(d, (_s(x0), _s(y), _s(x1), _s(y + ROW_H)), _s(12), YELLOW)
        fg = DARK if sel else TEXT
        _icon(d, item.get("icon", "tv"), x0 + 32, y + ROW_H / 2, fg)
        d.text((_s(x0 + 62), _s(y + ROW_H / 2)), item["label"], font=label_font, fill=fg, anchor="lm")
        value = item.get("value")
        if value:
            d.text((_s(x1 - 18), _s(y + ROW_H / 2)), value, font=value_font,
                   fill=(DARK if sel else YELLOW), anchor="rm")
        elif item.get("current"):
            cx, cy = x1 - 26, y + ROW_H / 2
            d.ellipse((_s(cx - 7), _s(cy - 7), _s(cx + 7), _s(cy + 7)), fill=(DARK if sel else YELLOW))
        y += ROW_H + ROW_GAP

    # Scroll bar
    if total > VISIBLE_ROWS:
        track_top, track_bot = 78, 78 + VISIBLE_ROWS * (ROW_H + ROW_GAP) - ROW_GAP
        th = (track_bot - track_top) * VISIBLE_ROWS / total
        ty = track_top + (track_bot - track_top - th) * start / (total - VISIBLE_ROWS)
        sx = VIEW_W - 38
        _rrect(d, (_s(sx), _s(track_top), _s(sx + 5), _s(track_bot)), _s(3), (40, 46, 66, 255))
        _rrect(d, (_s(sx), _s(ty), _s(sx + 5), _s(ty + th)), _s(3), YELLOW)

    # Footer hint
    d.text((_s(VIEW_W / 2), _s(VIEW_H - 32)), "TAP  choose        SWIPE  scroll        HOLD  close",
           font=_font(FONT_REG, 15), fill=DIM, anchor="mm")

    # Shrink (from the supersampled drawing, so text stays crisp) and centre on the visible area.
    w, h = int(round(VIEW_W * MENU_SCALE)), int(round(VIEW_H * MENU_SCALE))
    small = img.resize((w, h), Image.BOX)
    out = Image.new("RGBA", (VIEW_W, VIEW_H), YELLOW[:3] + (0,))
    out.paste(small, (int(round(MENU_CENTER[0] - w / 2)), int(round(MENU_CENTER[1] - h / 2))))
    return out

def to_panel_bgra(landscape_img):
    """Rotate to the panel's native orientation and return (premultiplied BGRA bytes, w, h)."""
    rot = landscape_img.transpose(Image.ROTATE_90)   # 90 degrees counter-clockwise, exact
    assert rot.size == (PANEL_W, PANEL_H), rot.size
    r, g, b, a = rot.convert("RGBa").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes(), PANEL_W, PANEL_H

if __name__ == "__main__":
    # Preview: python3 simptv_ui.py out.png
    import sys
    demo = [
        {"icon": "pause", "label": "Pause"},
        {"icon": "next", "label": "Next video"},
        {"icon": "prev", "label": "Previous video"},
        {"icon": "shuffle", "label": "Shuffle", "value": "ON"},
        {"icon": "sun", "label": "Brightness", "value": "75%"},
        {"icon": "tv", "label": "simpsons", "current": True},
        {"icon": "tv", "label": "nights"},
        {"icon": "tv", "label": "peep"},
        {"icon": "tv", "label": "kaything"},
    ]
    sel = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    render_menu_landscape(demo, sel, subtitle="simpsons").save(sys.argv[1] if len(sys.argv) > 1 else "menu.png")
