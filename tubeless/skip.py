"""Which playlist entry to jump to when you skip. Pure logic, no I/O."""
from __future__ import annotations

from typing import Optional, Sequence, Tuple


def layout_target(layout: Sequence[Optional[int]], pos: Optional[int], direction: int) -> Optional[Tuple]:
    """
    The same for a playlist whose layout is given (see Playlists.layout): for each position the episode's index, or None for a static or an
    ad. Each episode starts at the static or ad in front of it, or at the video itself where there is none (the static is off). Forward goes
    to the start of the next episode, back to the start of the previous one, wrapping to the last; the ends are as in `skip_target`.
    """
    if pos is None or not 0 <= pos < len(layout):
        return None
    videos = [j for j, k in enumerate(layout) if k is not None]
    starts = [v - 1 if v > 0 and layout[v - 1] is None else v for v in videos]
    here = layout[pos] if layout[pos] is not None else (layout[pos + 1] if pos + 1 < len(layout) else None)     # a static belongs to the video after it
    if here is None:
        return None
    if direction > 0:
        return ("goto", starts[here + 1]) if here + 1 < len(starts) else ("end",)
    return ("goto", starts[here - 1] if here > 0 else starts[-1])


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
