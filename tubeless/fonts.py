"""Font loading. The fonts are bundled so the simulator looks the same as the TV."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from .config import ASSETS_DIR

FONT_DIR = ASSETS_DIR / "fonts"
SYSTEM_DIR = Path("/usr/share/fonts/truetype/dejavu")

SANS = "DejaVuSans.ttf"
SANS_BOLD = "DejaVuSans-Bold.ttf"
MONO = "DejaVuSansMono.ttf"
MONO_BOLD = "DejaVuSansMono-Bold.ttf"
KANA = "KanaSubset.ttf"


@lru_cache(maxsize=None)
def load(name: str, size: int):
    for base in (FONT_DIR, SYSTEM_DIR):
        candidate = base / name
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    raise FileNotFoundError(f"font {name} not found in {FONT_DIR}")
