"""
Ads between videos (opt in, from the web page): every so many videos, a clip from the ads folder plays after the TV static.

The ads are ordinary video files in one folder (`~/ads`), put there by upload or by downloading a YouTube video: never streamed, so an
ad can't hold the picture up. Playback doesn't change shape: the ad goes in the *static slot* in front of a video, as one `edl://` entry
made of the static then the ad, so the playlist stays  static, video, static, video ...  (the static comes after the episode that ended,
and the ad cuts cleanly into the next one). With the static switched off the slot is the ad alone, and where no ad is due there is no
slot at all: the playlist is then  video, video, ad, video ...  (see Playlists.layout). `splice()` is the one place that decides how the two are joined: if mpv on the Pi can't play clips of different size and
codec in one entry cleanly, change it (a third kind of entry in the playlist is the other way).

An ad is never in the first slot of a playlist (that is a channel change, or the TV starting up: the static's job), and a swipe to skip
doesn't land on one (see App.skip_video). On a live channel the ads aren't part of the schedule; they just sit in the gap
between videos, and the schedule is worked out without them.
"""
from __future__ import annotations

import logging
import random
import threading
from pathlib import Path
from typing import Callable, List, Optional

from .library import VIDEO_EXTS

log = logging.getLogger(__name__)

EVERY_OPTIONS = (1, 2, 3, 4, 5, 10)
DEFAULT_EVERY = 3
MAX_ADS_SECONDS = 180                # a YouTube video longer than this isn't an ad (nobody means a feature film)


def edl_segment(path: str) -> str:
    """One whole file as an edl:// segment. `%n%` gives the path's length in bytes, so commas, spaces and semicolons in a file name
    can't be misread."""
    return f"%{len(path.encode('utf-8'))}%{path}"


def splice(static: str, ad: str) -> str:
    """The playlist entry for a static slot that has an ad: the static, then the ad. `static` is a file name or, for the TV's own static
    clip, already an edl:// entry (see statics.edl_entry)."""
    first = static[len("edl://"):] if static.startswith("edl://") else edl_segment(static)
    return "edl://" + first + ";" + edl_segment(ad)


def ad_slots(count: int, every: int) -> List[int]:
    """Which of `count` videos, counted from the first in the playlist, have an ad in front of them: every `every`-th, never the first."""
    return [i for i in range(1, count) if i % every == 0]


class AdsManager:
    """The ads switch, how often, and the clips in the folder."""

    def __init__(self, folder, settings, rng: Optional[random.Random] = None, on_change: Callable[[], None] = lambda: None):
        self.folder = Path(folder)
        self.settings = settings
        self.rng = rng or random.Random()
        self.on_change = on_change
        self._last: Optional[str] = None
        self._lock = threading.Lock()
        self.download: Optional[dict] = None            # a YouTube download in progress, or how the last one ended
        self._cancel = threading.Event()

    # --- the settings ----------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("ads_on", False))

    @property
    def every(self) -> int:
        value = self.settings.get("ads_every")
        return value if value in EVERY_OPTIONS else DEFAULT_EVERY

    def set_enabled(self, value) -> None:
        if not isinstance(value, bool):
            raise ValueError("That is on or off.")
        self.settings.set(ads_on=value)
        log.info("ads between videos %s", "on" if value else "off")
        self.on_change()

    def set_every(self, value) -> None:
        if isinstance(value, bool) or value not in EVERY_OPTIONS:
            raise ValueError(f"{value!r} isn't one of the choices for that setting.")
        self.settings.set(ads_every=value)
        self.on_change()

    # --- the folder ------------------------------------------------------------------------------
    def clips(self) -> List[str]:
        """File names of the ads, sorted."""
        try:
            return sorted(f.name for f in self.folder.iterdir()
                          if f.is_file() and f.name.lower().endswith(VIDEO_EXTS) and not f.name.startswith("."))
        except OSError:
            return []

    def active(self) -> bool:
        """Ads are on and there is something to play."""
        return self.enabled and bool(self.clips())

    def pick(self) -> Optional[str]:
        """A random ad's path (not the one just used, if there is another), or None if there are none."""
        clips = self.clips()
        if not clips:
            return None
        if len(clips) > 1 and self._last in clips:
            clips.remove(self._last)
        self._last = self.rng.choice(clips)
        return str(self.folder / self._last)

    def slot_entry(self, static: Optional[str], index: int) -> Optional[str]:
        """What goes in the static slot in front of the video at `index` (0 is the first in the playlist): the static, with an ad after it
        if one is due. With no static (None) the slot is the ad alone, or nothing (None) where no ad is due: a clean cut into the video."""
        if index <= 0 or index % self.every != 0 or not self.enabled:
            return static
        ad = self.pick()
        if not ad:
            return static
        return splice(static, ad) if static else ad

    def delete(self, name: str) -> None:
        clean = Path(name).name
        if not clean or clean != name or clean.startswith("."):
            raise ValueError("That isn't one of the ads.")
        (self.folder / clean).unlink(missing_ok=True)
        self.on_change()

    # --- what the page shows ---------------------------------------------------------------------
    def state(self) -> dict:
        clips = self.clips()
        warning = None
        if self.enabled and not clips:
            warning = "Ads are switched on, but the ads folder is empty, so nothing will play until you add some."
        with self._lock:
            download = dict(self.download) if self.download else None
        return {"enabled": self.enabled, "every": self.every, "options": list(EVERY_OPTIONS), "folder": str(self.folder),
                "clips": clips, "warning": warning, "download": download}

    # --- downloading one from YouTube ------------------------------------------------------------
    def start_download(self, ytdlp, url: str) -> None:
        """Download the YouTube video at `url` into the folder, in the background (the page polls state()). Raises ValueError("why not")."""
        from .youtube import YouTubeError, clean_url, safe_stem
        clean = clean_url(url)
        if clean is None or "list=" in clean:
            raise ValueError("Paste the link to one YouTube video.")
        if not ytdlp.available():
            raise ValueError("yt-dlp isn't installed on this TV, so videos can't be downloaded.")
        with self._lock:
            if self.download and self.download.get("stage") in ("looking", "downloading", "joining"):
                raise ValueError("An ad is already downloading.")
            self.download = {"stage": "looking", "progress": 0.0, "title": "", "error": None}
        self._cancel.clear()

        def progress(fraction: float, stage: str) -> None:
            with self._lock:
                if self.download:
                    self.download.update(progress=fraction, stage=stage)

        def work() -> None:
            try:
                listing = ytdlp.listing(clean)
                if listing.kind != "video" or len(listing.entries) != 1:
                    raise YouTubeError("Paste the link to one YouTube video.")
                entry = listing.entries[0]
                if entry.duration and entry.duration > MAX_ADS_SECONDS:
                    raise YouTubeError(f"That video is {int(entry.duration // 60)} minutes long: too long for an ad "
                                       f"(the limit is {MAX_ADS_SECONDS // 60} minutes).")
                with self._lock:
                    self.download.update(title=entry.title, stage="downloading")
                self.folder.mkdir(parents=True, exist_ok=True)
                name = ytdlp.download(entry.id, self.folder, safe_stem(entry.title, entry.id), progress, self._cancel)
                with self._lock:
                    self.download.update(stage="done", progress=1.0, file=name)
                log.info("downloaded the ad %s", name)
            except YouTubeError as e:
                with self._lock:
                    self.download.update(stage="error", error=e.message)
                log.warning("ad download failed: %s", e.message)
            except Exception:
                log.exception("ad download failed")
                with self._lock:
                    self.download.update(stage="error", error="The download failed.")
            self.on_change()

        threading.Thread(target=work, daemon=True, name="ad-download").start()

    def cancel_download(self) -> None:
        self._cancel.set()
