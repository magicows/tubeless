"""
The menu as data: what rows exist on each page, and what selecting one means.
Pure logic; drawing is in render.py and doing is in app.py.

    (Every page also shows icon buttons in the header: the guide, shut down and upload mode, the last two needing two taps; see app.py.)
    main:      Pause/Play, Next video, Previous video, Brightness, Settings >, then the channels
    settings:  < Back, Shuffle, Reshuffle channel, Sleep timer, Auto-dim, Calibrate touch, Upload videos, Wi-Fi setup, Shut down
    guide:     every channel: its number and name, and what is on it now (or next) with the time left; tap one to tune to it
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

SLEEP_TIMER_STEPS = (0, 15, 30, 60, 90)       # minutes, 0 = off
AUTO_DIM_STEPS = (0, 2, 5, 10)                # minutes, 0 = off


@dataclass
class Item:
    icon: str
    label: str
    action: Tuple = ()
    value: Optional[str] = None
    current: bool = False
    number: Optional[int] = None      # drawn in place of the icon (the guide's channel numbers)
    detail: str = ""                  # a smaller line under the label (the guide's "what is on")


@dataclass
class MenuContext:
    paused: bool = False
    brightness: int = 100
    shuffle: bool = True
    channels: Sequence[str] = ()              # labels
    current_channel: int = 0
    sleep_timer_min: int = 0
    auto_dim_min: int = 0
    confirm_shutdown: bool = False
    confirm_transfer: bool = False            # the header's upload button has had its first tap
    wifi_setup: bool = False                  # offered only where the OS can do it (NetworkManager)
    guide: Sequence[str] = ()                 # what is on each channel, as the guide shows it (same order as `channels`)


def _minutes(m: int) -> str:
    return "Off" if not m else f"{m} min"


class MenuModel:
    def __init__(self):
        self.page = "main"
        self.index = 0        # highlighted row
        self.top = 0          # first visible row (only changes when the user scrolls)

    def reset(self) -> None:
        self.page, self.index, self.top = "main", 0, 0

    def open_page(self, page: str) -> None:
        self.page, self.index, self.top = page, 0, 0

    def items(self, ctx: MenuContext) -> List[Item]:
        if self.page == "settings":
            rows = [
                Item("back", "Back", ("page", "main")),
                Item("shuffle", "Shuffle", ("shuffle",), "ON" if ctx.shuffle else "OFF"),
                Item("reshuffle", "Reshuffle channel", ("reshuffle",)),
                Item("moon", "Sleep timer", ("sleep_timer",), _minutes(ctx.sleep_timer_min)),
                Item("sun", "Auto-dim", ("auto_dim",), _minutes(ctx.auto_dim_min)),
                Item("target", "Calibrate touch", ("calibrate",)),
                Item("upload", "Upload videos", ("transfer",)),
            ]
            if ctx.wifi_setup:
                rows.append(Item("wifi", "Wi-Fi setup", ("wifi_setup",)))
            rows.append(Item("power", "Tap again to confirm" if ctx.confirm_shutdown else "Shut down", ("shutdown",)))
            return rows
        if self.page == "guide":
            return [Item("tv", label, ("channel", i), current=(i == ctx.current_channel), number=i + 1,
                         detail=ctx.guide[i] if i < len(ctx.guide) else "")
                    for i, label in enumerate(ctx.channels)]
        rows = [
            Item("play" if ctx.paused else "pause", "Play" if ctx.paused else "Pause", ("playpause",)),
            Item("next", "Next video", ("next",)),
            Item("prev", "Previous video", ("prev",)),
            Item("sun", "Brightness", ("brightness",), f"{ctx.brightness}%"),
            Item("gear", "Settings", ("page", "settings")),
        ]
        for i, label in enumerate(ctx.channels):
            rows.append(Item("tv", label, ("channel", i), current=(i == ctx.current_channel)))
        return rows


def time_left(seconds: Optional[float]) -> str:
    """'12 min left', as the guide says it ('' when the length is not known)."""
    if seconds is None or seconds <= 0:
        return ""
    return "under a minute left" if seconds < 60 else f"{int(round(seconds / 60.0))} min left"


def next_step(steps: Sequence[int], value: int) -> int:
    """The step after `value` in a cycling list (wraps to the first)."""
    bigger = [s for s in steps if s > value]
    return bigger[0] if bigger else steps[0]
