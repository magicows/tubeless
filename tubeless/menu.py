"""
The menu as data: what rows exist on each page, and what selecting one means.
Pure logic; drawing is in render.py and doing is in app.py.

    main:      Pause/Play, Next video, Previous video, Brightness, Settings >, then the channels
    settings:  < Back, Shuffle, Reshuffle channel, Sleep timer, Auto-dim, Calibrate touch, Shut down
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
            return [
                Item("back", "Back", ("page", "main")),
                Item("shuffle", "Shuffle", ("shuffle",), "ON" if ctx.shuffle else "OFF"),
                Item("reshuffle", "Reshuffle channel", ("reshuffle",)),
                Item("moon", "Sleep timer", ("sleep_timer",), _minutes(ctx.sleep_timer_min)),
                Item("sun", "Auto-dim", ("auto_dim",), _minutes(ctx.auto_dim_min)),
                Item("target", "Calibrate touch", ("calibrate",)),
                Item("power", "Tap again to confirm" if ctx.confirm_shutdown else "Shut down", ("shutdown",)),
            ]
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


def next_step(steps: Sequence[int], value: int) -> int:
    """The step after `value` in a cycling list (wraps to the first)."""
    bigger = [s for s in steps if s > value]
    return bigger[0] if bigger else steps[0]
