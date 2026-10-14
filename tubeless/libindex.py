"""
A small index of the library: how long each video is.

mediainfo is run once per file (a few tenths of a second on the Pi), and the answer is remembered in index.json together with the
file's size and modification time, so only new or changed files are read again. It runs while the TV is in upload mode (the
player is stopped then, so it costs playback nothing) and after each upload, never during playback. The web page shows the
totals; a file that mediainfo can't read is counted as "unread" so you can see which ones to look at.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from typing import Callable, Dict, List, Optional

from . import advice
from .library import Channel, Library
from .state import Store

log = logging.getLogger(__name__)

SAVE_EVERY = 20           # probes between saves, so a power cut loses little


class LibraryIndex:
    NAME = "index.json"

    def __init__(self, library: Library, store: Store, probe: Callable[[str], Optional[dict]] = advice.probe,
                 available: Callable[[], bool] = lambda: shutil.which("mediainfo") is not None):
        self.library, self.store, self.probe, self.available = library, store, probe, available
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = store.load(self.NAME, {}).get("files", {})
        self._scanned_at: Optional[float] = store.load(self.NAME, {}).get("scanned_at")
        self._running = False
        self._again = False
        self._done = self._total = 0
        self._thread: Optional[threading.Thread] = None

    # --- reading ---------------------------------------------------------------------------------
    @staticmethod
    def _key(channel: Channel, rel: str) -> str:
        return f"{channel.name}/{rel}"

    def duration(self, channel: Channel, rel: str) -> Optional[float]:
        with self._lock:
            e = self._entries.get(self._key(channel, rel))
        return e.get("duration") if e else None

    def summary(self, channel: Channel) -> dict:
        """Totals for a channel: how long it is, how many files are known, and how many couldn't be read or aren't scanned yet."""
        total, known, unread, files = 0.0, 0, 0, 0
        with self._lock:
            entries = dict(self._entries)
        for rel in self.library.videos(channel):
            if self.library.is_stream(rel):
                continue
            files += 1
            e = entries.get(self._key(channel, rel))
            if e and e.get("duration"):
                total += e["duration"]
                known += 1
            else:
                unread += 1
        return {"files": files, "known": known, "unread": unread, "seconds": round(total)}

    def status(self) -> dict:
        with self._lock:
            return {"running": self._running, "done": self._done, "total": self._total,
                    "mediainfo": self.available(), "scanned_at": self._scanned_at}

    # --- scanning --------------------------------------------------------------------------------
    def start_scan(self, force: bool = False) -> bool:
        """Scan in the background. If a scan is already running it will go round once more when it finishes."""
        with self._lock:
            if self._running:
                self._again = True
                return False
            self._running = True
        self._thread = threading.Thread(target=self._run, args=(force,), daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float = 30.0) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _run(self, force: bool) -> None:
        try:
            while True:
                self.scan(force)
                force = False
                with self._lock:
                    if not self._again:
                        self._running = False
                        return
                    self._again = False
        except Exception:
            log.exception("library scan failed")
            with self._lock:
                self._running = False

    def scan(self, force: bool = False) -> dict:
        """Read every new or changed video's duration (all of them with force). Returns what it found."""
        work: List[tuple] = []
        seen = set()
        for channel in self.library.channels():
            for rel in self.library.videos(channel):
                if self.library.is_stream(rel):
                    continue
                key = self._key(channel, rel)
                seen.add(key)
                path = self.library.abspath(channel, rel)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                with self._lock:
                    old = self._entries.get(key)
                if force or not old or old.get("size") != st.st_size or old.get("mtime") != int(st.st_mtime):
                    work.append((key, path, st.st_size, int(st.st_mtime)))
        with self._lock:
            for key in [k for k in self._entries if k not in seen]:          # files that are gone
                del self._entries[key]
            self._done, self._total = 0, len(work)
        if work and not self.available():
            log.info("mediainfo is not installed: durations can't be read")
            return {"scanned": 0, "pending": len(work)}
        for i, (key, path, size, mtime) in enumerate(work, 1):
            info = self.probe(path)
            duration = info.get("duration") if info else None
            with self._lock:
                self._entries[key] = {"size": size, "mtime": mtime, "duration": round(duration, 1) if duration else None}
                self._done = i
            if i % SAVE_EVERY == 0:
                self._save()
        with self._lock:
            self._scanned_at = time.time()
        self._save()
        return {"scanned": len(work), "pending": 0}

    def _save(self) -> None:
        with self._lock:
            data = {"files": dict(self._entries), "scanned_at": self._scanned_at}
        self.store.save(self.NAME, data)
