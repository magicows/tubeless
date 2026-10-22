"""
Themes: what the menu, the full-screen info screens (upload, Wi-Fi setup, calibration ...) and the web page look like.
The boot screen keeps its Matrix rain whatever theme is chosen.

A theme is a handful of colours plus a few shape options. Palettes pasted from a colour-sharing site (hex codes, or a link such as
coolors.co/palette/264653-2a9d8f-e9c46a) are turned into a theme by `from_palette`, which decides which colour is the background,
which is the text and which is the accent, and keeps the text readable.

Pure logic with no drawing; render.py and web.py read the active theme.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]

DEFAULT_THEME = "classic"
MAX_PALETTE = 10
MAX_CUSTOM = 12


# ─────────────────────────────────────────────
#  Colour helpers
# ─────────────────────────────────────────────
def _lin(c: int) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(c: RGB) -> float:
    return 0.2126 * _lin(c[0]) + 0.7152 * _lin(c[1]) + 0.0722 * _lin(c[2])


def contrast(a: RGB, b: RGB) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: RGB, b: RGB, t: float) -> RGB:
    """`t` of the way from a to b."""
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))   # type: ignore[return-value]


def saturation(c: RGB) -> float:
    hi, lo = max(c), min(c)
    return 0.0 if hi == 0 else (hi - lo) / hi


def to_hex(c: RGB) -> str:
    return "#%02x%02x%02x" % tuple(c)


BLACK: RGB = (0, 0, 0)
WHITE: RGB = (255, 255, 255)


def readable_on(c: RGB, bg: RGB) -> RGB:
    """Black or white, whichever reads better on `bg`."""
    return BLACK if contrast(BLACK, bg) >= contrast(WHITE, bg) else WHITE


def _toward(c: RGB, bg: RGB, target: RGB, ratio: float) -> RGB:
    """Move `c` toward `target` (black or white) until it has `ratio` contrast against `bg`."""
    out = c
    for step in range(1, 21):
        if contrast(out, bg) >= ratio:
            break
        out = mix(c, target, step / 20.0)
    return out


# ─────────────────────────────────────────────
#  The theme
# ─────────────────────────────────────────────
@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    bg: RGB            # page background / the screen behind the menu panel
    panel: RGB         # menu panel and web cards
    text: RGB
    dim: RGB           # quieter text
    accent: RGB        # selection, buttons, titles
    on_accent: RGB     # text drawn on the accent
    line: RGB          # hairlines
    ink: RGB           # outlines
    light: bool = False
    style: str = "soft"          # "soft" (rounded, thin lines) or "brutal" (square, thick black outlines, hard shadows)
    screen: Optional[Tuple[RGB, RGB, RGB, RGB, RGB, RGB, RGB]] = None    # (bg, bright, mid, faint, border, on_bright, strong) if the info screens differ
    web: bool = True             # False: the web page keeps its own look
    panel_alpha: int = 232

    # --- shape ----------------------------------------------------------------------------------------------
    @property
    def brutal(self) -> bool:
        return self.style == "brutal"

    @property
    def radius(self) -> int:
        return 0 if self.brutal else 12

    @property
    def panel_radius(self) -> int:
        return 0 if self.brutal else 20

    @property
    def outline(self) -> int:
        return 4 if self.brutal else 2

    @property
    def row_outline(self) -> int:
        return 3 if self.brutal else 0

    @property
    def shadow(self) -> int:
        return 7 if self.brutal else 0

    # --- what to draw with ----------------------------------------------------------------------------------
    @property
    def accent_text(self) -> RGB:
        """The accent where it reads on the panel (titles, values); otherwise the text colour."""
        return self.accent if contrast(self.accent, self.panel) >= 3.0 else self.text

    @property
    def outline_colour(self) -> RGB:
        return self.ink if self.brutal else self.accent

    def screen_colours(self) -> Tuple[RGB, RGB, RGB, RGB, RGB, RGB, RGB]:
        """(background, bright, mid, faint, border, text-on-bright, strong) for the full-screen info screens."""
        if self.screen is not None:
            return self.screen
        bright = self.ink if self.brutal else self.accent_text
        return (self.bg, bright, self.text, self.dim, self.ink if self.brutal else self.line, readable_on(bright, bright), self.text)

    def css(self) -> str:
        """The web page's colours as CSS (empty for a theme that keeps the page's own look)."""
        if not self.web:
            return ""
        field = WHITE if (self.brutal and self.light) else mix(self.panel, self.text, 0.05) if self.light else mix(self.panel, BLACK, 0.35)
        return (":root{color-scheme:%s;--bg:%s;--panel:%s;--line:%s;--text:%s;--dim:%s;--accent:%s;--on-accent:%s;--atext:%s;--ink:%s;--field:%s;--field2:%s;}"
                % ("light" if self.light else "dark", to_hex(self.bg), to_hex(self.panel), to_hex(self.line), to_hex(self.text),
                   to_hex(self.dim), to_hex(self.accent), to_hex(self.on_accent), to_hex(self.accent_text), to_hex(self.ink), to_hex(field),
                   to_hex(mix(field, self.panel, 0.5))))

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "style": self.style, "light": self.light,
                "swatches": [to_hex(self.bg), to_hex(self.panel), to_hex(self.accent), to_hex(self.text)]}


# ─────────────────────────────────────────────
#  Built in
# ─────────────────────────────────────────────
CLASSIC = Theme(
    "classic", "Classic (yellow and green)",
    bg=(14, 18, 30), panel=(14, 18, 30), text=(240, 242, 248), dim=(150, 158, 178), accent=(254, 217, 15),
    on_accent=(24, 26, 36), line=(60, 68, 92), ink=(254, 217, 15),
    screen=((0, 0, 0), (0, 255, 70), (0, 190, 55), (0, 120, 38), (0, 55, 20), (0, 20, 6), (220, 255, 230)), web=False)

NEOBRUTALIST = Theme(
    "neobrutalist", "Neobrutalist",
    bg=(255, 244, 214), panel=(255, 253, 245), text=(0, 0, 0), dim=(84, 78, 62), accent=(255, 201, 0),
    on_accent=(0, 0, 0), line=(0, 0, 0), ink=(0, 0, 0), light=True, style="brutal", panel_alpha=255,
    screen=((255, 244, 214), (0, 0, 0), (30, 28, 24), (110, 100, 78), (0, 0, 0), (255, 201, 0), (0, 0, 0)))


def _hexes(*codes: str) -> List[RGB]:
    return [tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) for h in codes]       # type: ignore[misc]


def from_palette(theme_id: str, label: str, colours: Sequence[RGB], light: bool = False, brutal: bool = False) -> Theme:
    """Turn a few colours into a theme: darkest (or lightest) for the background, the best contrast for the text, and the most
    colourful of the rest for the accent. Text always gets at least 7:1 against the background, the accent 3:1."""
    uniq: List[RGB] = []
    for c in colours:
        if c not in uniq:
            uniq.append(c)
    if len(uniq) < 2:
        raise ValueError("A palette needs at least two different colours.")
    by_lum = sorted(uniq, key=luminance)
    if light:
        bg = by_lum[-1]
        if luminance(bg) < 0.55:
            bg = mix(bg, WHITE, 0.75)
        text_src = by_lum[0]
        text = _toward(text_src, bg, BLACK, 7.0)
        if contrast(text, bg) < 7.0:
            text = BLACK
    else:
        bg = by_lum[0]
        if luminance(bg) > 0.12:
            bg = mix(bg, BLACK, 0.8)
        text_src = by_lum[-1]
        text = _toward(text_src, bg, WHITE, 7.0)
        if contrast(text, bg) < 7.0:
            text = WHITE
    bg_src = by_lum[-1] if light else by_lum[0]
    pool = [c for c in uniq if c not in (bg_src, text_src)] or [text]
    accent = max(pool, key=lambda c: (saturation(c) * 0.7 + min(1.0, contrast(c, bg) / 7.0) * 0.3))
    accent = _toward(accent, bg, BLACK if light else WHITE, 3.0)
    panel = mix(bg, BLACK, 0.05) if light else mix(bg, WHITE, 0.07)
    if brutal and light:
        panel = mix(bg, WHITE, 0.6)
    dim = _toward(mix(text, bg, 0.4), bg, BLACK if light else WHITE, 4.5)
    line = mix(text, bg, 0.82)
    ink = BLACK if (light or brutal and luminance(bg) > 0.3) else text
    return Theme(theme_id, label, bg=bg, panel=panel, text=text, dim=dim, accent=accent, on_accent=readable_on(accent, accent),
                 line=line if not brutal else ink, ink=ink, light=light, style="brutal" if brutal else "soft",
                 panel_alpha=255 if brutal else 232)


BUILT_IN_PALETTES = (
    ("nord", "Nord", _hexes("2e3440", "3b4252", "88c0d0", "81a1c1", "eceff4", "bf616a"), False),
    ("dracula", "Dracula", _hexes("282a36", "44475a", "bd93f9", "ff79c6", "f8f8f2", "50fa7b"), False),
    ("gruvbox", "Gruvbox", _hexes("282828", "3c3836", "fabd2f", "fe8019", "ebdbb2", "b8bb26"), False),
    ("solarized-light", "Solarized light", _hexes("fdf6e3", "eee8d5", "268bd2", "cb4b16", "073642", "859900"), True),
)


def built_in() -> Dict[str, Theme]:
    themes = {CLASSIC.id: CLASSIC, NEOBRUTALIST.id: NEOBRUTALIST}
    for tid, label, colours, light in BUILT_IN_PALETTES:
        themes[tid] = from_palette(tid, label, colours, light=light)
    return themes


# ─────────────────────────────────────────────
#  Pasted palettes
# ─────────────────────────────────────────────
_HEX = re.compile(r"(?<![0-9a-fA-F])(?:#)?([0-9a-fA-F]{6})(?![0-9a-fA-F])")


def parse_palette(text: str) -> List[RGB]:
    """Colours from pasted text: hex codes separated by anything, or a link like coolors.co/palette/264653-2a9d8f-e9c46a."""
    text = str(text or "")
    found = []
    for token in re.split(r"[\s,;/\-]+", text.replace("%23", "#")):
        m = re.fullmatch(r"#?([0-9a-fA-F]{6})", token.strip())
        if m:
            found.append(_hexes(m.group(1).lower())[0])
    if not found:
        found = [_hexes(h.lower())[0] for h in _HEX.findall(text)]
    if len(found) > MAX_PALETTE:
        raise ValueError(f"That is {len(found)} colours; up to {MAX_PALETTE} please.")
    return found


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:24] or "palette"


def make_custom(name: str, text: str, light: bool, brutal: bool, taken: Sequence[str] = ()) -> dict:
    """A custom palette as it is stored (settings.json). Raises ValueError with a message fit for the page."""
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("Give the palette a name.")
    if len(name) > 24:
        raise ValueError("That name is too long (24 characters at most).")
    colours = parse_palette(text)
    if len(colours) < 2:
        raise ValueError("I couldn't find at least two colours. Paste hex codes like 264653 2a9d8f e9c46a, or a coolors.co link.")
    base = "c-" + slug(name)
    tid, n = base, 2
    while tid in taken:
        tid, n = f"{base}-{n}", n + 1
    from_palette(tid, name, colours, light, brutal)               # make sure it works before it is stored
    return {"id": tid, "name": name, "colours": [to_hex(c)[1:] for c in colours], "light": bool(light), "brutal": bool(brutal)}


def build_custom(entry: dict) -> Optional[Theme]:
    try:
        return from_palette(str(entry["id"]), str(entry["name"]), _hexes(*[str(c) for c in entry["colours"]]),
                            bool(entry.get("light")), bool(entry.get("brutal")))
    except Exception:
        return None


def all_themes(customs: Sequence[dict] = ()) -> Dict[str, Theme]:
    themes = built_in()
    for entry in customs:
        t = build_custom(entry) if isinstance(entry, dict) else None
        if t is not None and t.id not in themes:
            themes[t.id] = t
    return themes


# ─────────────────────────────────────────────
#  The one in use (render.py reads it each time it draws)
# ─────────────────────────────────────────────
_active: Theme = CLASSIC


def active() -> Theme:
    return _active


def activate(theme: Theme) -> None:
    global _active
    _active = theme
