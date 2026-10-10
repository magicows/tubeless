"""Which playlist entry to jump to when you skip. Pure logic, no I/O."""
from __future__ import annotations

from typing import Optional, Tuple


def skip_target(pos: Optional[int], count: Optional[int], direction: int) -> Optional[Tuple]:
    """
    Playlist layout is  static, video, static, video, ...  (even = static, odd = video).
    Each static belongs to the video that follows it, and skipping always lands on the
    static in front of the target video, so the channel-flick effect plays every time.

        on a video  (odd):   forward -> pos+1    back -> pos-3
        on a static (even):  forward -> pos+2    back -> pos-2

    Going back past the start wraps round to the last video, so "previous" always
    changes the episode. Returns:
        ("goto", index)   jump to that playlist entry
        ("end",)          ran off the end: the channel needs a new cycle
        None              nothing to skip (e.g. only the channel-change static is loaded)
    """
    if pos is None or count is None or count < 2:
        return None
    on_video = pos % 2 == 1
    if direction > 0:
        target = pos + 1 if on_video else pos + 2
    else:
        target = pos - 3 if on_video else pos - 2
    if target < 0:
        target = count - 2
    if target >= count:
        return ("end",)
    return ("goto", target)
