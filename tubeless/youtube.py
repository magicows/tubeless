"""
YouTube channels: a channel whose videos come from YouTube playlists (or single videos) you choose on the phone page.

* A YouTube channel is an ordinary channel folder plus a `channel.json` listing what you picked. Videos are downloaded
  at 480p or lower (H.264, which a Zero 2 W plays comfortably) into the folder, so after that the TV treats them like any
  other video: static between episodes, shuffle, resume, no network needed.
* Nothing here is bundled: it drives `yt-dlp` (and `ffmpeg` to join video and sound) if the user has installed them.
  yt-dlp needs frequent updates because YouTube keeps changing; see docs/ROADMAP.md.
* Only YouTube addresses are accepted (not "any site yt-dlp knows"), and the page can only name a video by its ID, so a
  phone on the Wi-Fi can't make the TV fetch arbitrary things or write outside the channel folder.
* Downloads run one at a time, only while the phone page is open (the TV is in "upload" mode and shows the progress).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

MANIFEST = "channel.json"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_URL = 300
MAX_ITEMS = 500                         # per channel: more than that is a library, not a channel
TITLE_MAX = 80
BYTES_PER_SECOND = 200_000              # about 1.6 Mbit/s: what a 480p H.264 download costs, for space checks
FALLBACK_BYTES = 150 * 1024 * 1024      # when the length isn't known

# 480p or less, 30 fps or less, H.264 + AAC. First choice joins separate video and audio (needs ffmpeg); the second is a
# ready-made single file, which YouTube offers less and less.
FORMAT_JOINED = "bv*[height<=480][fps<=30][vcodec^=avc1]+ba[acodec^=mp4a]"
FORMAT_SINGLE = "b[height<=480][fps<=30][vcodec^=avc1][acodec^=mp4a]"


class YouTubeError(Exception):
    """Something to tell the user, in words they can act on."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


# ─────────────────────────────────────────────
#  Addresses and IDs
# ─────────────────────────────────────────────
def clean_url(text) -> Optional[str]:
    """A YouTube video or playlist address in a canonical form, or None."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > MAX_URL or any(ord(c) < 32 for c in text):
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        u = urlparse(text)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in YOUTUBE_HOSTS or u.username or u.password:
        return None
    host = (u.hostname or "").lower()
    q = parse_qs(u.query)
    if host.endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
        if not _ID.match(vid):
            return None
        return f"https://www.youtube.com/watch?v={vid}" + (f"&list={q['list'][0]}" if q.get("list") else "")
    playlist = (q.get("list") or [""])[0]
    video = (q.get("v") or [""])[0]
    if u.path.rstrip("/") == "/playlist" and re.match(r"^[A-Za-z0-9_-]{10,60}$", playlist):
        return f"https://www.youtube.com/playlist?list={playlist}"
    if u.path.rstrip("/") == "/watch" and _ID.match(video):
        if re.match(r"^[A-Za-z0-9_-]{10,60}$", playlist or ""):
            return f"https://www.youtube.com/playlist?list={playlist}"        # a video inside a playlist: take the playlist
        return f"https://www.youtube.com/watch?v={video}"
    m = re.match(r"^/(shorts|embed|live)/([A-Za-z0-9_-]{11})$", u.path.rstrip("/"))
    if m:
        return f"https://www.youtube.com/watch?v={m.group(2)}"
    return None


def valid_id(video_id) -> bool:
    return isinstance(video_id, str) and bool(_ID.match(video_id))


def safe_stem(title: str, video_id: str) -> str:
    """The file name (without extension) for a downloaded video: readable, but safe and unique."""
    bad = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")
    t = bad.sub("_", (title or "").strip()).strip(". ")
    t = re.sub(r"\s+", " ", t)[:TITLE_MAX].strip(". ") or "video"
    return f"{t} [{video_id}]"


def friendly_error(stderr: str) -> str:
    low = (stderr or "").lower()
    if "private video" in low:
        return "This video is private."
    if "video unavailable" in low or "has been removed" in low or "no longer available" in low or "unavailable" in low and "video" in low:
        return "This video isn't available any more."
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube is asking this network to prove it isn't a robot. Try again later."
    if "age" in low and ("restricted" in low or "confirm your age" in low):
        return "This video is age-restricted, which needs a signed-in YouTube account."
    if "429" in low or "too many requests" in low:
        return "YouTube is limiting requests from this network. Try again later."
    if "requested format is not available" in low:
        return "YouTube has no small-enough version of this video."
    if "members-only" in low or "join this channel" in low:
        return "This video is for channel members only."
    if "geo" in low and "block" in low or "not available in your country" in low:
        return "This video isn't available in your country."
    if any(s in low for s in ("temporary failure", "name or service not known", "network is unreachable", "timed out",
                              "connection reset", "unable to download webpage", "urlopen error")):
        return "The TV couldn't reach YouTube. Is the Wi-Fi working?"
    lines = [l for l in (stderr or "").splitlines() if l.strip()]
    last = lines[-1] if lines else "yt-dlp failed"
    return "YouTube download failed: " + re.sub(r"^ERROR:\s*(\[[^\]]*\]\s*)?", "", last)[:160]


# ─────────────────────────────────────────────
#  What yt-dlp tells us
# ─────────────────────────────────────────────
@dataclass
class Entry:
    id: str
    title: str
    duration: Optional[float] = None


@dataclass
class Listing:
    title: str
    kind: str                           # "playlist" or "video"
    entries: List[Entry] = field(default_factory=list)
    skipped: int = 0                    # private/deleted entries that can't be used


LIVE_STATES = ("is_live", "is_upcoming", "post_live")        # not a finished video: it can't be downloaded, nor played from the start
LIVE_MESSAGE = "That's a live stream, and live streams can't be played on the TV. Choose a recorded video or playlist instead."


def is_live(info) -> bool:
    return isinstance(info, dict) and (info.get("is_live") is True or info.get("live_status") in LIVE_STATES)


def parse_listing(data: dict, url: str = "") -> Listing:
    """From `yt-dlp --flat-playlist -J` output (a playlist, or one video's info)."""
    if not isinstance(data, dict):
        raise YouTubeError("YouTube didn't return anything usable for that address.", 502)
    if data.get("_type") == "playlist" or "entries" in data:
        entries, skipped, seen = [], 0, set()
        for e in data.get("entries") or []:
            vid = e.get("id") if isinstance(e, dict) else None
            title = (e.get("title") if isinstance(e, dict) else "") or ""
            if not valid_id(vid) or title.startswith("[Private") or title.startswith("[Deleted") or vid in seen or is_live(e):
                skipped += 1
                continue
            seen.add(vid)
            dur = e.get("duration")
            entries.append(Entry(vid, title.strip() or vid, float(dur) if isinstance(dur, (int, float)) else None))
        if not entries:
            raise YouTubeError("No videos found there. Is the playlist public (or unlisted)? (Live streams are left out.)")
        return Listing(str(data.get("title") or "YouTube playlist")[:100], "playlist", entries, skipped)
    vid = data.get("id")
    if not valid_id(vid):
        raise YouTubeError("That doesn't look like a YouTube video or playlist.")
    if is_live(data):
        raise YouTubeError(LIVE_MESSAGE, 422)
    dur = data.get("duration")
    return Listing(str(data.get("title") or vid)[:100], "video",
                   [Entry(vid, str(data.get("title") or vid).strip(), float(dur) if isinstance(dur, (int, float)) else None)])


# ─────────────────────────────────────────────
#  yt-dlp itself
# ─────────────────────────────────────────────
def find_ytdlp(configured: str = "") -> Optional[str]:
    candidates = [configured] if configured else []
    candidates += [str(Path.home() / ".local/share/tubeless/ytdlp/bin/yt-dlp"), shutil.which("yt-dlp") or ""]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


_PROGRESS = re.compile(r"^tubeless-progress:(\S+) (\S+) (\S+)\s*$")


class YtDlp:
    """The real thing: runs yt-dlp as a subprocess. (Tests use FakeYtDlp, which has the same methods.)"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or find_ytdlp()
        self.has_ffmpeg = shutil.which("ffmpeg") is not None

    def available(self) -> bool:
        return bool(self.path)

    def version(self) -> Optional[str]:
        if not self.path:
            return None
        try:
            return subprocess.run([self.path, "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    def _base(self) -> List[str]:
        return [self.path, "--no-update", "--no-warnings", "--no-config", "--ignore-config", "--no-cache-dir"]

    def listing(self, url: str, timeout: int = 180) -> Listing:
        if not self.path:
            raise YouTubeError("yt-dlp isn't installed on this TV.", 501)
        try:
            res = subprocess.run(self._base() + ["--flat-playlist", "--playlist-items", f"1:{MAX_ITEMS}", "-J", "--", url],
                                 capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise YouTubeError("YouTube took too long to answer. Try again.", 504)
        except OSError as e:
            raise YouTubeError(f"Couldn't run yt-dlp ({e.strerror or e}).", 500)
        if res.returncode != 0:
            raise YouTubeError(friendly_error(res.stderr), 502)
        try:
            return parse_listing(json.loads(res.stdout), url)
        except ValueError:
            raise YouTubeError("YouTube's answer couldn't be read.", 502)

    def download(self, video_id: str, dest_dir: Path, stem: str, on_progress: Callable[[float, str], None],
                 cancel: threading.Event) -> str:
        """Download one video as `<stem>.mp4` into dest_dir. Returns the file name. Raises YouTubeError."""
        if not self.path:
            raise YouTubeError("yt-dlp isn't installed on this TV.", 501)
        fmt = f"{FORMAT_JOINED}/{FORMAT_SINGLE}" if self.has_ffmpeg else FORMAT_SINGLE
        tmp = dest_dir / ".downloading"
        tmp.mkdir(exist_ok=True)
        cmd = self._base() + ["--newline", "--no-playlist", "--continue",
                              "-f", fmt, "--merge-output-format", "mp4",
                              "-P", f"home:{dest_dir}", "-P", f"temp:{tmp}", "-o", stem + ".%(ext)s",
                              "--progress-template",
                              "tubeless-progress:%(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
                              "--", f"https://www.youtube.com/watch?v={video_id}"]
        proc = subprocess.Popen(["nice", "-n", "10"] + cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        errors: List[str] = []
        threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
        try:
            for line in proc.stdout:
                if cancel.is_set():
                    proc.terminate()
                    break
                m = _PROGRESS.match(line)
                if m:
                    done, total, est = m.groups()
                    size = next((float(v) for v in (total, est) if v not in ("NA", "None") and v.replace(".", "").isdigit()), 0.0)
                    if size and done.replace(".", "").isdigit():
                        on_progress(min(0.99, float(done) / size), "downloading")
                elif line.startswith("[Merger]") or line.startswith("[VideoRemuxer]"):
                    on_progress(0.99, "joining")
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
        if cancel.is_set():
            raise YouTubeError("Stopped.", 499)
        if proc.returncode != 0:
            raise YouTubeError(friendly_error("".join(errors)), 502)
        final = dest_dir / (stem + ".mp4")
        if not final.exists():
            raise YouTubeError("The download finished but the file wasn't found.", 500)
        return final.name


# ─────────────────────────────────────────────
#  The per-channel list of what you picked
# ─────────────────────────────────────────────
class ChannelFile:
    """channel.json inside a YouTube channel's folder."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.mode = "download"          # "download" (keep offline) or "stream" (play from YouTube; needs Wi-Fi)
        self.sources: List[dict] = []   # {"url", "title", "kind"}
        self.items: List[dict] = []     # {"id", "title", "duration", "selected", "file", "error"}

    @property
    def path(self) -> Path:
        return self.folder / MANIFEST

    @classmethod
    def exists(cls, folder: Path) -> bool:
        return (Path(folder) / MANIFEST).is_file()

    @classmethod
    def load(cls, folder: Path) -> "ChannelFile":
        cf = cls(folder)
        try:
            data = json.loads(cf.path.read_text())
        except (OSError, ValueError):
            return cf
        if isinstance(data, dict) and data.get("source") == "youtube":
            cf.mode = data.get("mode") if data.get("mode") in ("download", "stream") else "download"
            cf.sources = [s for s in data.get("sources", []) if isinstance(s, dict)]
            cf.items = [i for i in data.get("items", []) if isinstance(i, dict) and valid_id(i.get("id"))]
        return cf

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "source": "youtube", "mode": self.mode, "sources": self.sources,
                                   "items": self.items}, indent=1))
        os.replace(tmp, self.path)

    def find(self, video_id: str) -> Optional[dict]:
        return next((i for i in self.items if i["id"] == video_id), None)

    def merge(self, url: str, listing: Listing, select: Optional[set] = None) -> Tuple[int, int]:
        """Add what a playlist/video offers; videos already here keep their state. Returns (added, already_there).
        New videos are selected unless `select` names a subset."""
        added = there = 0
        for e in listing.entries:
            if self.find(e.id):
                there += 1
                if select is not None and e.id in select:
                    self.find(e.id)["selected"] = True
                continue
            if len(self.items) >= MAX_ITEMS:
                raise YouTubeError(f"A channel can hold up to {MAX_ITEMS} videos.")
            self.items.append({"id": e.id, "title": e.title, "duration": e.duration,
                               "selected": True if select is None else e.id in select, "file": None, "error": None})
            added += 1
        if not any(s.get("url") == url for s in self.sources):
            self.sources.append({"url": url, "title": listing.title, "kind": listing.kind})
        return added, there

    def reconcile(self) -> None:
        """Forget files that have vanished from disk (so they show as waiting to download again)."""
        for i in self.items:
            if i.get("file") and not (self.folder / i["file"]).is_file():
                i["file"] = None

    def status(self, item: dict) -> str:
        if not item.get("selected"):
            return "off"
        if item.get("file") and (self.folder / item["file"]).is_file():
            return "done"
        if item.get("error"):
            return "error"
        return "waiting"

    def summary(self) -> dict:
        counts = {"done": 0, "waiting": 0, "error": 0, "off": 0}
        for i in self.items:
            counts[self.status(i)] += 1
        return {"mode": self.mode, "total": len(self.items), "selected": len(self.items) - counts["off"], **counts}

    def as_dict(self) -> dict:
        return {"mode": self.mode, "sources": self.sources,
                "items": [{"id": i["id"], "title": i["title"], "duration": i.get("duration"), "selected": bool(i.get("selected")),
                           "status": self.status(i), "error": i.get("error")} for i in self.items]}


# ─────────────────────────────────────────────
#  The manager: what the phone page drives
# ─────────────────────────────────────────────
class YouTubeManager:
    def __init__(self, root, ytdlp, reserve: int = 300 * 1024 * 1024, disk_free: Optional[Callable[[Path], int]] = None,
                 on_change: Optional[Callable[[], None]] = None, cache_seconds: float = 600.0):
        self.root = Path(root)
        self.yt = ytdlp
        self.reserve = reserve
        self._disk_free = disk_free or (lambda p: shutil.disk_usage(str(p)).free)
        self.on_change = on_change or (lambda: None)
        self.cache_seconds = cache_seconds
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, Listing]] = {}
        self._queue: List[Tuple[str, str]] = []                 # (channel folder name, video id)
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self.current: Optional[dict] = None                     # what is downloading right now
        self.finished = 0
        self.failed: List[dict] = []

    # -- helpers
    def _folder(self, channel: str) -> Path:
        from .web import sanitize_channel
        clean = sanitize_channel(channel)
        if clean is None:
            raise YouTubeError("That channel name isn't allowed.")
        folder = self.root / clean
        if folder.resolve().parent != self.root.resolve() or not folder.is_dir():
            raise YouTubeError("No such channel.", 404)
        return folder

    def _channel(self, channel: str) -> ChannelFile:
        folder = self._folder(channel)
        if not ChannelFile.exists(folder):
            raise YouTubeError("That isn't a YouTube channel.", 404)
        cf = ChannelFile.load(folder)
        cf.reconcile()
        return cf

    def status(self) -> dict:
        return {"available": self.yt.available(), "version": self.yt.version() if self.yt.available() else None,
                "ffmpeg": bool(getattr(self.yt, "has_ffmpeg", True))}

    def is_youtube_channel(self, folder_name: str) -> bool:
        return ChannelFile.exists(self.root / folder_name)

    # -- channels
    def create(self, name: str) -> str:
        from .web import sanitize_channel
        clean = sanitize_channel(name)
        if clean is None:
            raise YouTubeError("That channel name isn't allowed.")
        folder = self.root / clean
        if folder.resolve().parent != self.root.resolve():
            raise YouTubeError("That channel name isn't allowed.")
        if folder.exists() and any(p for p in folder.iterdir() if not p.name.startswith(".")):
            raise YouTubeError("A channel with that name already has videos in it. Pick a new name.", 409)
        folder.mkdir(parents=True, exist_ok=True)
        ChannelFile(folder).save()
        return clean

    def delete(self, channel: str) -> None:
        folder = self._folder(channel)
        if not ChannelFile.exists(folder):
            raise YouTubeError("Only YouTube channels can be removed here.", 403)
        with self._lock:
            self._queue = [q for q in self._queue if q[0] != folder.name]
            if self.current and self.current["channel"] == folder.name:
                self._cancel.set()
        shutil.rmtree(folder, ignore_errors=True)
        self.on_change()

    def info(self, channel: str) -> dict:
        data = self._channel(channel).as_dict()
        with self._lock:
            data["queued"] = sum(1 for q in self._queue if q[0] == self._folder(channel).name)
        return data

    def summaries(self) -> Dict[str, dict]:
        out = {}
        try:
            names = [p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            return out
        for n in names:
            if ChannelFile.exists(self.root / n):
                cf = ChannelFile.load(self.root / n)
                cf.reconcile()
                out[n] = cf.summary()
        return out

    # -- looking things up
    def resolve(self, url: str) -> Listing:
        clean = clean_url(url)
        if clean is None:
            raise YouTubeError("That doesn't look like a YouTube video or playlist link.")
        now = time.time()
        with self._lock:
            hit = self._cache.get(clean)
            if hit and now - hit[0] < self.cache_seconds:
                return hit[1]
        listing = self.yt.listing(clean)
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.cache_seconds}
            self._cache[clean] = (now, listing)
        return listing

    def add(self, channel: str, url: str, selected: Optional[List[str]] = None) -> dict:
        """Merge a playlist/video into the channel. Uses the listing the page just saw (never client-supplied titles)."""
        clean = clean_url(url)
        if clean is None:
            raise YouTubeError("That doesn't look like a YouTube video or playlist link.")
        listing = self.resolve(clean)
        cf = self._channel(channel)
        pick = None if selected is None else {s for s in selected if valid_id(s)}
        added, there = cf.merge(clean, listing, pick)
        if pick is not None:                                    # an explicit choice: everything else from this list is off
            for e in listing.entries:
                if e.id not in pick and cf.find(e.id) and not cf.find(e.id).get("file"):
                    cf.find(e.id)["selected"] = False
        cf.save()
        self.on_change()
        return {"added": added, "already_there": there, "title": listing.title, "skipped": listing.skipped}

    def set_selection(self, channel: str, selected: List[str]) -> dict:
        """Tick/untick videos. Unticking one that was downloaded removes the file from the TV."""
        cf = self._channel(channel)
        pick = {s for s in selected if valid_id(s)}
        removed = 0
        for item in cf.items:
            on = item["id"] in pick
            if not on and item.get("file"):
                try:
                    (cf.folder / item["file"]).unlink()
                    removed += 1
                except OSError:
                    pass
                item["file"] = None
            if on and not item.get("selected"):
                item["error"] = None
            item["selected"] = on
        cf.save()
        with self._lock:
            self._queue = [q for q in self._queue if not (q[0] == cf.folder.name and q[1] not in pick)]
        self.on_change()
        return {"removed": removed}

    def set_mode(self, channel: str, mode: str) -> None:
        if mode not in ("download", "stream"):
            raise YouTubeError("Unknown mode.")
        cf = self._channel(channel)
        cf.mode = mode
        cf.save()
        self.on_change()

    # -- downloading
    def sync(self, channel: Optional[str] = None, retry_errors: bool = True) -> int:
        """Queue everything selected that isn't on the TV yet. Returns how many videos were queued."""
        if not self.yt.available():
            raise YouTubeError("yt-dlp isn't installed on this TV, so videos can't be downloaded.", 501)
        folders = [self._folder(channel)] if channel else [self.root / n for n in sorted(self.summaries())]
        queued = 0
        with self._lock:
            have = set(self._queue)
            for folder in folders:
                cf = ChannelFile.load(folder)
                cf.reconcile()
                if cf.mode != "download":
                    continue
                for item in cf.items:
                    if cf.status(item) in ("waiting", "error") and (retry_errors or not item.get("error")):
                        key = (folder.name, item["id"])
                        if key not in have and not (self.current and (self.current["channel"], self.current["id"]) == key):
                            self._queue.append(key)
                            queued += 1
            if queued:
                self.failed = [f for f in self.failed if (f["channel"], f["id"]) not in set(self._queue)]
                self._start_worker()
        self.on_change()
        return queued

    def cancel(self) -> None:
        with self._lock:
            self._queue.clear()
        self._cancel.set()
        self.on_change()

    def _start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._cancel.clear()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue or self._cancel.is_set():
                    self.current = None
                    self._queue.clear()
                    break
                channel, vid = self._queue.pop(0)
            try:
                self._download_one(channel, vid)
            except Exception:
                log.exception("download crashed")
        self.on_change()

    def _download_one(self, channel: str, vid: str) -> None:
        folder = self.root / channel
        if not ChannelFile.exists(folder):
            return
        cf = ChannelFile.load(folder)
        item = cf.find(vid)
        if not item or not item.get("selected") or cf.mode != "download":
            return
        need = int((item.get("duration") or 0) * BYTES_PER_SECOND) or FALLBACK_BYTES
        free = self._disk_free(self.root) - self.reserve
        with self._lock:
            self.current = {"channel": channel, "id": vid, "title": item["title"], "progress": 0.0, "stage": "starting",
                            "queued": len(self._queue)}
        self.on_change()
        if free < need:
            self._record_error(channel, vid, item["title"], "Not enough space left on the TV for this video.")
            return

        def progress(fraction: float, stage: str) -> None:
            with self._lock:
                if self.current:
                    self.current.update(progress=fraction, stage=stage)

        try:
            name = self.yt.download(vid, folder, safe_stem(item["title"], vid), progress, self._cancel)
        except YouTubeError as e:
            if e.status == 499:
                return
            self._record_error(channel, vid, item["title"], e.message)
            return
        cf = ChannelFile.load(folder)                         # re-read: the page may have changed things meanwhile
        again = cf.find(vid)
        if again is not None:
            again["file"], again["error"] = name, None
            cf.save()
        with self._lock:
            self.finished += 1
        log.info("downloaded %s into %s", name, channel)
        self.on_change()

    def _record_error(self, channel: str, vid: str, title: str, message: str) -> None:
        cf = ChannelFile.load(self.root / channel)
        item = cf.find(vid)
        if item is not None:
            item["error"] = message
            cf.save()
        with self._lock:
            self.failed.append({"channel": channel, "id": vid, "title": title, "error": message})
        log.warning("could not download %s: %s", vid, message)
        self.on_change()

    # -- what the TV and the page show
    def progress(self) -> dict:
        with self._lock:
            return {"active": dict(self.current) if self.current else None, "queued": len(self._queue),
                    "finished": self.finished, "failed": list(self.failed[-20:]),
                    "working": bool(self.current or self._queue)}

    def busy(self) -> bool:
        with self._lock:
            return bool(self.current or self._queue or (self._worker and self._worker.is_alive()))

    def shutdown(self) -> None:
        """Called when the phone page closes: stop downloading (what was queued stays on the channel's list)."""
        self.cancel()
        w = self._worker
        if w and w.is_alive():
            w.join(timeout=15)


class FakeYtDlp:
    """The same interface as YtDlp, with scripted answers (tests and the simulator)."""

    has_ffmpeg = True

    def __init__(self, listings: Optional[Dict[str, Listing]] = None, fail: Optional[Dict[str, str]] = None, delay: float = 0.0,
                 is_available: bool = True):
        self.listings = listings or {}
        self.fail = fail or {}
        self.delay = delay
        self.is_available = is_available
        self.calls: List[tuple] = []

    def available(self): return self.is_available
    def version(self): return "fake-2026.01.01"

    def listing(self, url, timeout=180):
        self.calls.append(("listing", url))
        for key, value in self.listings.items():
            if key in url:
                return value
        raise YouTubeError("YouTube didn't return anything usable for that address.", 502)

    def download(self, video_id, dest_dir, stem, on_progress, cancel):
        self.calls.append(("download", video_id))
        end = time.time() + self.delay
        while time.time() < end:
            if cancel.is_set():
                raise YouTubeError("Stopped.", 499)
            on_progress(0.5, "downloading")
            time.sleep(0.01)
        if video_id in self.fail:
            raise YouTubeError(self.fail[video_id], 502)
        (dest_dir / (stem + ".mp4")).write_bytes(b"fake video " + video_id.encode())
        return stem + ".mp4"
