"""A pretend Pi: touch comes from a queue, the backlight is a number, power-off sets a flag."""
from __future__ import annotations

import logging
import queue
import threading
from typing import List, Optional

import numpy as np

from . import Backend, TouchSample

log = logging.getLogger(__name__)


class SimTouch:
    def __init__(self):
        self.q: "queue.Queue[TouchSample]" = queue.Queue()

    def feed(self, fingers: int, rx: float = 0.0, ry: float = 0.0) -> None:
        self.q.put(TouchSample(fingers, rx, ry))

    def read(self, timeout: Optional[float]) -> List[TouchSample]:
        try:
            out = [self.q.get(timeout=timeout if timeout is not None else 1.0)]
        except queue.Empty:
            return []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out


class SimBacklight:
    def __init__(self):
        self.percent = 0
        self.history: List[int] = []

    @property
    def ok(self) -> bool:
        return True

    def start(self, wait: float = 0.0) -> None:
        pass

    def set(self, percent: int) -> None:
        self.percent = max(0, min(100, int(percent)))
        self.history.append(self.percent)


class SimPower:
    def __init__(self):
        self.shutdown_requested = False
        self.reboot_requested = False

    def shutdown(self) -> None:
        log.info("[sim] shutdown requested")
        self.shutdown_requested = True

    def reboot(self) -> None:
        log.info("[sim] reboot requested")
        self.reboot_requested = True


class SimDisplay:
    """Holds the last frame drawn straight to the 'framebuffer' (the boot screen), as RGB565 landscape."""

    def __init__(self):
        self.frame565: Optional[np.ndarray] = None
        self.lock = threading.Lock()

    def show(self, px565_landscape) -> None:
        with self.lock:
            self.frame565 = np.array(px565_landscape, copy=True)


def make(cfg, hw) -> Backend:
    return Backend(touch=SimTouch(), backlight=SimBacklight(), power=SimPower(), display=SimDisplay())
