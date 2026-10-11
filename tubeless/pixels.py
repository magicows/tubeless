"""RGB565 pixel packing, shared by the boot screen and the full-screen mode screens."""
from __future__ import annotations

import numpy as np


def pack565(r, g, b) -> np.ndarray:
    r, g, b = (np.asarray(v).astype(np.uint16) for v in (r, g, b))
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def unpack565(px: np.ndarray) -> np.ndarray:
    r, g, b = ((px >> 11) & 31) << 3, ((px >> 5) & 63) << 2, (px & 31) << 3
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def image_to_565(img) -> np.ndarray:
    """A PIL image as a (height, width) uint16 RGB565 array, ready for a framebuffer."""
    rgb = np.asarray(img.convert("RGB"))
    return pack565(rgb[..., 0], rgb[..., 1], rgb[..., 2])
