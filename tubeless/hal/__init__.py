"""
Hardware abstraction. Everything that touches the real world lives behind these:

    touch       read(timeout) -> [TouchSample, ...]
    backlight   start(), set(percent)
    power       shutdown(), reboot()

`pi` talks to the real hardware; `sim` is a pretend one for the desktop simulator and tests.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TouchSample:
    """One snapshot of the touchscreen. rx/ry are the raw position normalised to 0..1."""
    fingers: int
    rx: float = 0.0
    ry: float = 0.0


@dataclass
class Backend:
    touch: object
    backlight: object
    power: object
    display: object = None
    audio: object = None


def make_backend(cfg, hw, sim: bool = False) -> Backend:
    if sim:
        from . import sim as impl
        return impl.make(cfg, hw)
    from . import pi as impl
    return impl.make(cfg, hw)
