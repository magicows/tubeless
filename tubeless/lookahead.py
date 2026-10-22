"""
Look-ahead for YouTube channels that stream.

Asking YouTube for a video's real stream addresses takes yt-dlp 6 to 12 seconds on a Pi Zero 2 W, and mpv only does that when the
video's turn comes, so every changeover costs about 12 seconds (measured: 12.2 to 13.1 s on four changeovers, with or without a
static clip in between; mpv's own look-ahead does not help because its YouTube hook only runs when an entry starts).

So the app does it ahead of time: while one video plays it resolves the NEXT video's addresses, one video at a time at the lowest
priority, and swaps the ready-made address into the playlist. mpv then opens it directly (about 2 or 3 seconds) when its turn comes.

The address is built the way mpv's own YouTube hook builds it: yt-dlp prints the video stream's address and the audio stream's
address, and they are joined into an `edl://` entry (`%n%` gives each address's length in bytes, so no character in it can be
misread). One address (a ready-made single file) is used as it is. Addresses expire after several hours; a cached one is dropped
after a few.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .youtube import DEFAULT_HEIGHT, formats

log = logging.getLogger(__name__)

TTL = 4 * 3600                 # YouTube's addresses last about 6 hours; use them for at most 4
MAX_ADDRESS = 8000


def build_entry(urls: List[str]) -> Optional[str]:
    """A playlist entry from yt-dlp's addresses: the address itself, or video + audio joined as an edl:// entry."""
    urls = [u for u in urls if u]
    if not urls or len(urls) > 2 or any(not u.startswith("https://") or len(u) > MAX_ADDRESS for u in urls):
        return None
    if len(urls) == 1:
        return urls[0]
    return "edl://" + ";".join("!new_stream;!no_clip;!no_chapters;%%%d%%%s" % (len(u.encode("utf-8")), u) for u in urls)


class Lookahead:
    def __init__(self, ytdlp_path: Callable[[], Optional[str]], height: Callable[[], int] = lambda: DEFAULT_HEIGHT,
                 runner: Callable = subprocess.run, clock: Callable[[], float] = time.time, timeout: float = 75.0):
        self.ytdlp_path, self.height, self.runner, self.clock, self.timeout = ytdlp_path, height, runner, clock, timeout
        self.resolver: Callable[[str], Optional[str]] = self.resolve         # tests swap this for a stand-in
        self._lock = threading.Lock()
        self._ready: Dict[str, Tuple[float, str, float]] = {}                # video id -> (when, entry, seconds it took)
        self._busy = False

    def resolve(self, video_id: str) -> Optional[str]:
        """Ask yt-dlp for the addresses of one video (blocking, about 6 to 12 s on the Pi). None if anything goes wrong."""
        path = self.ytdlp_path()
        if not path:
            return None
        joined, single = formats(self.height())
        cmd = ["nice", "-n", "19", path, "--no-update", "--no-warnings", "--no-config", "--ignore-config", "--no-cache-dir",
               "--no-playlist", "-f", f"{joined}/{single}", "-g", "--", "https://www.youtube.com/watch?v=" + video_id]
        try:
            res = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        return build_entry(res.stdout.split())

    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, video_id: str) -> bool:
        """Resolve in the background, if nothing else is being resolved. Returns whether it started."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def work():
            began = self.clock()
            entry = None
            try:
                entry = self.resolver(video_id)
            except Exception:
                log.exception("look-ahead could not resolve %s", video_id)
            with self._lock:
                if entry:
                    self._ready[video_id] = (self.clock(), entry, self.clock() - began)
                self._busy = False
            if not entry:
                log.info("look-ahead: no ready address for the next video (it will load the usual way)")

        threading.Thread(target=work, daemon=True).start()
        return True

    def ready(self, video_id: str) -> Optional[Tuple[str, float]]:
        """(entry, seconds it took to resolve) if one is ready and fresh."""
        with self._lock:
            hit = self._ready.get(video_id)
            if not hit:
                return None
            if self.clock() - hit[0] > TTL:
                del self._ready[video_id]
                return None
            return hit[1], hit[2]

    def forget(self, video_id: str) -> None:
        with self._lock:
            self._ready.pop(video_id, None)
