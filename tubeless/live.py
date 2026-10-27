"""
Live TV mode: channels behave like broadcast TV. Each channel has a fixed running order that plays round and round on the clock, whether
or not you are watching, so tuning in puts you in the middle of whatever is "on now", and coming back an hour later finds the
channel has moved on. Opt in, from the web page.

The order is the same every time (not shuffled afresh each cycle): each video's place in it is fixed by its name, so adding a video
slots it in without reshuffling the rest. The position is `now` modulo the channel's total running time, so it needs every video's
length: a channel with any length unknown plays as it always did until they are read (while the TV is in upload mode).

Pure logic; app.py asks `slot()` when a channel is tuned.
"""
from __future__ import annotations

import zlib
from typing import Callable, List, Optional, Sequence, Tuple

DEFAULT_GAP = 0.6                   # seconds of static between videos, counted in the running time so the clock stays true


def broadcast_order(name: str, rels: Sequence[str], in_order: Optional[Callable[[List[str]], List[str]]] = None, seed: int = 0) -> List[str]:
    """The channel's fixed running order: the channel's own order if it plays in order, else a stable shuffle keyed by file name.
    A different `seed` gives a different (equally stable) shuffle: that is how a channel is reshuffled."""
    rels = list(rels)
    if in_order is not None:
        return in_order(rels)
    salt = f"{seed}:" if seed else ""
    return sorted(rels, key=lambda rel: (zlib.crc32(f"{salt}{name}/{rel}".encode()), rel))


def total_seconds(durations: Sequence[float], gap: float = DEFAULT_GAP) -> float:
    return sum(d + gap for d in durations)


def slot(order: Sequence[str], durations: Sequence[Optional[float]], now: float, gap: float = DEFAULT_GAP) -> Optional[Tuple[str, float]]:
    """(video, seconds into it) that is on air at `now`, or None if any length is unknown (or there is nothing to play)."""
    if not order or len(order) != len(durations) or any(d is None or d <= 0 for d in durations):
        return None
    t = now % total_seconds(durations, gap)
    for i, (rel, d) in enumerate(zip(order, durations)):
        if t < d:
            return rel, t
        t -= d
        if t < gap:                                 # in the static between this video and the next: the next starts from its beginning
            return order[(i + 1) % len(order)], 0.0
        t -= gap
    return order[0], 0.0                            # (rounding at the very end of the loop)


def minutes(seconds: float) -> int:
    return int(round(seconds / 60.0))
