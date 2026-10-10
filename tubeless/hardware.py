"""
Everything that is specific to the supported build lives here, in one place.

Tubeless targets exactly one build: a Raspberry Pi Zero 2 W with the Waveshare
2.8" DPI LCD (480x640 portrait panel, mounted sideways), a Goodix touch
controller, the backlight on GPIO 18 and a mono PWM speaker on GPIO 19.

Another screen = another profile: add a `Hardware(...)` below (or override
fields from config.json) and everything else follows.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

Affine = Tuple[Tuple[float, float, float], Tuple[float, float, float]]


@dataclass(frozen=True)
class Hardware:
    name: str = "waveshare-2.8-dpi"

    # The picture as the TV is *viewed* (landscape) and the panel's native framebuffer (portrait).
    view_w: int = 640
    view_h: int = 480
    panel_w: int = 480
    panel_h: int = 640
    # mpv --video-rotate that turns the portrait panel into a landscape picture, and the
    # matching number of numpy rot90 turns (counter-clockwise) for anything we draw ourselves.
    video_rotate: int = 270
    rot_k: int = 1

    # Touch controller.
    touch_name: str = "Goodix"
    touch_fallback: str = "/dev/input/event1"
    # Raw touch -> landscape view, as an affine map on *normalised* raw coordinates (rx, ry in 0..1):
    #   view_x = 640 * (1 - ry)      view_y = 480 * rx
    # (the controller's axes are swapped relative to how the TV is viewed; measured by tapping the
    # corners). The calibration wizard replaces this with a fitted map.
    touch_affine: Affine = ((0.0, -640.0, 640.0), (480.0, 0.0, 0.0))

    # GPIO.
    backlight_pin: int = 18
    backlight_pwm_hz: int = 800
    audio_pin: int = 19

    # The 3D-printed bezel hides the panel's edges and corners: draw the menu a little smaller and
    # centred on the visible opening (in landscape view coordinates).
    bezel_center: Tuple[int, int] = (326, 237)
    bezel_scale: float = 0.85
    # Where boot-screen text may go (left, top, right, bottom).
    safe_rect: Tuple[int, int, int, int] = (60, 38, 594, 440)

    brightness_levels: Tuple[int, ...] = (25, 50, 75, 100)


PROFILES: Dict[str, Hardware] = {"waveshare-2.8-dpi": Hardware()}


def _tuplify(value):
    if isinstance(value, list):
        return tuple(_tuplify(v) for v in value)
    return value


def load_hardware(name: str = "waveshare-2.8-dpi", overrides: Optional[dict] = None) -> Hardware:
    if name not in PROFILES:
        raise ValueError(f"unknown hardware profile {name!r} (have: {', '.join(sorted(PROFILES))})")
    base = PROFILES[name]
    if not overrides:
        return base
    valid = {f.name for f in fields(Hardware)}
    unknown = set(overrides) - valid
    if unknown:
        raise ValueError(f"unknown hardware setting(s): {', '.join(sorted(unknown))}")
    return replace(base, **{k: _tuplify(v) for k, v in overrides.items()})


# ─────────────────────────────────────────────
#  Touch calibration maths
# ─────────────────────────────────────────────
def apply_affine(affine: Affine, rx: float, ry: float) -> Tuple[float, float]:
    (a, b, c), (d, e, f) = affine
    return a * rx + b * ry + c, d * rx + e * ry + f


def invert_affine(affine: Affine) -> Affine:
    """The map from view back to normalised raw coordinates (used by the simulator)."""
    (a, b, c), (d, e, f) = affine
    det = a * e - b * d
    if abs(det) < 1e-9:
        raise ValueError("touch map is not invertible")
    ia, ib = e / det, -b / det
    id_, ie = -d / det, a / det
    return ((ia, ib, -(ia * c + ib * f)), (id_, ie, -(id_ * c + ie * f)))


def solve_affine(pairs: Sequence[Tuple[Tuple[float, float], Tuple[float, float]]]):
    """
    Fit view = A * [rx, ry, 1] to calibration taps.
    pairs: [((rx, ry), (view_x, view_y)), ...] with at least 3 points that are not in a line.
    Returns (affine, rms_error_in_view_pixels).
    """
    if len(pairs) < 3:
        raise ValueError("need at least 3 calibration points")
    design = np.array([[rx, ry, 1.0] for (rx, ry), _ in pairs], dtype=float)
    vx = np.array([v[0] for _, v in pairs], dtype=float)
    vy = np.array([v[1] for _, v in pairs], dtype=float)
    if np.linalg.matrix_rank(design) < 3:
        raise ValueError("calibration points are in a line")
    cx = np.linalg.lstsq(design, vx, rcond=None)[0]
    cy = np.linalg.lstsq(design, vy, rcond=None)[0]
    pred_x, pred_y = design @ cx, design @ cy
    rms = float(np.sqrt(np.mean((pred_x - vx) ** 2 + (pred_y - vy) ** 2)))
    affine = ((float(cx[0]), float(cx[1]), float(cx[2])), (float(cy[0]), float(cy[1]), float(cy[2])))
    return affine, rms
