"""
Gesture recognition. Pure logic, no I/O: fed (finger down?, x, y, time) samples in
screen coordinates, it returns gestures. One finger only; a second finger cancels.

    ("tap", x, y, raw)                    raw = the untouched (rx, ry), used by calibration
    ("swipe", direction, x0, y0, dx, dy)  direction is left / right / up / down
    ("long_press",)                       fires while the finger is still down
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple


class GestureRecognizer:
    def __init__(self, long_press: float = 1.0, move_px: float = 25.0, swipe_min_px: float = 70.0):
        self.long_press = long_press
        self.move_px = move_px
        self.swipe_min_px = swipe_min_px
        self.tracking = False
        self.suppressed = False        # set when a 2nd finger appears (or on request), until all lift
        self.moved = False
        self.long_fired = False
        self.t0 = self.x0 = self.y0 = self.x = self.y = 0.0
        self.raw0: Optional[Tuple[float, float]] = None

    def cancel(self) -> None:
        self.tracking = False
        self.suppressed = True

    def update(self, active: bool, x: float, y: float, now: float, raw: Optional[Tuple[float, float]] = None) -> List[tuple]:
        out: List[tuple] = []
        if not active:
            self.suppressed = False
            if self.tracking:
                self.tracking = False
                dx, dy = self.x - self.x0, self.y - self.y0
                if self.long_fired:
                    pass
                elif self.moved:
                    if max(abs(dx), abs(dy)) >= self.swipe_min_px:
                        if abs(dx) >= abs(dy):
                            direction = "right" if dx > 0 else "left"
                        else:
                            direction = "down" if dy > 0 else "up"
                        out.append(("swipe", direction, self.x0, self.y0, dx, dy))
                else:
                    out.append(("tap", self.x0, self.y0, self.raw0))
            return out

        if self.suppressed:
            return out
        if not self.tracking:
            self.tracking = True
            self.moved = False
            self.long_fired = False
            self.t0, self.x0, self.y0 = now, x, y
            self.raw0 = raw
        self.x, self.y = x, y
        if not self.moved and math.hypot(x - self.x0, y - self.y0) > self.move_px:
            self.moved = True
        return out

    def poll(self, now: float) -> List[tuple]:
        """Time-based gestures: a long press fires while the finger is still down."""
        if self.tracking and not self.moved and not self.long_fired and now - self.t0 >= self.long_press:
            self.long_fired = True
            return [("long_press",)]
        return []

    def next_timeout(self, now: float) -> Optional[float]:
        """Seconds until poll() could produce something, or None."""
        if self.tracking and not self.moved and not self.long_fired:
            return max(0.0, self.t0 + self.long_press - now)
        return None
