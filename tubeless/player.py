"""
The video player.

The app only ever talks to a Player:

    start(playlist_path)      begin playing an .m3u
    stop()
    alive() -> bool
    command(*args)            an mpv IPC command, e.g. command("set_property", "pause", True)
    get(prop) -> value|None   an mpv property, e.g. get("playlist-pos")
    set_overlay(raw, w, h)    show a premultiplied-BGRA bitmap over the video (panel orientation)
    clear_overlay()

MpvPlayer drives the real mpv. FakePlayer behaves like it (a playlist that advances with
time, pause, seeking, static bursts) so the app can run in the simulator and in tests.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import Config, expand
from .hardware import Hardware

log = logging.getLogger(__name__)

OVERLAY_ID = 0


class MpvIPC:
    """
    One persistent connection to mpv's JSON IPC socket (instead of spawning `socat` for every command).
    Thread-safe; reconnects after an mpv restart.
    """

    def __init__(self, path: str):
        self.path = path
        self._sock = None
        self._file = None
        self._lock = threading.Lock()
        self._req = 0

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(self.path)
        self._sock = s
        self._file = s.makefile("rw", encoding="utf-8")

    def close(self):
        for obj in (self._file, self._sock):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self._file = self._sock = None

    def command(self, *args):
        """Send one command. Returns the reply data (None if mpv reported an error). Raises OSError if mpv can't be reached."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._req += 1
                    rid = self._req
                    self._file.write(json.dumps({"command": list(args), "request_id": rid}) + "\n")
                    self._file.flush()
                    while True:
                        line = self._file.readline()
                        if not line:
                            raise ConnectionError("mpv closed the IPC socket")
                        reply = json.loads(line)
                        if reply.get("request_id") == rid:
                            return reply.get("data") if reply.get("error") == "success" else None
                except (OSError, ValueError):
                    self.close()
                    if attempt == 2:
                        raise


@functools.lru_cache(maxsize=None)
def mpv_has_option(name: str) -> bool:
    """Does the installed mpv know this option? (An option it doesn't know stops mpv from starting at all.) Asked once."""
    try:
        out = subprocess.run(["mpv", "--list-options"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[:1] == ["--" + name] for line in out.splitlines())


class MpvPlayer:
    def __init__(self, cfg: Config, hw: Hardware):
        self.cfg, self.hw = cfg, hw
        self.proc: Optional[subprocess.Popen] = None
        self.youtube_height = 360                # the quality streamed from YouTube (set by the app from its settings)
        self.started_at = 0.0                    # when mpv was last started: its control socket takes a moment to appear
        self.ipc = MpvIPC(cfg.mpv_socket)
        shm = Path("/dev/shm")
        self.overlay_file = str((shm if shm.is_dir() else Path(tempfile.gettempdir())) / "tubeless-overlay.raw")

    def start(self, playlist_path: str) -> None:
        self.stop()
        try:
            os.remove(self.cfg.mpv_socket)
        except OSError:
            pass
        self.ipc.close()
        out = self._open_log()
        try:
            # --no-config: nothing outside this command line changes how it behaves.
            # --fs makes mpv's OSD space exactly the panel, which the overlay bitmaps are drawn for.
            self.proc = subprocess.Popen([
                "mpv", "--no-config", "--hwdec=no", "--fs", f"--video-rotate={self.hw.video_rotate}",
                "--playlist=" + str(playlist_path),
                "--input-ipc-server=" + self.cfg.mpv_socket,
                "--loop-playlist=no",
                "--msg-level=all=warn",
                # A Pi Zero 2 W has about 400 MB free in all. mpv's defaults buffer up to 150 MB of a network stream (and as much
                # again behind the playhead), which is enough to starve the whole TV. A few seconds of buffer is plenty here.
                "--demuxer-max-bytes=24MiB", "--demuxer-max-back-bytes=4MiB", "--cache-secs=20",
            ] + self._prefetch_options() + self._youtube_options(), stdout=out, stderr=subprocess.STDOUT)
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        self.started_at = time.time()
        log.info("started mpv PID %s", self.proc.pid)

    def _prefetch_options(self) -> list:
        """Look ahead: open the next playlist entry while the current one plays. On a YouTube channel (no static clip between
        videos) that is the next video, resolved and opened in the background."""
        return ["--prefetch-playlist=yes"] if mpv_has_option("prefetch-playlist") else []

    def _youtube_options(self) -> list:
        """So a YouTube link in a playlist is played (as 480p H.264) by the yt-dlp the user installed."""
        from .youtube import DEFAULT_HEIGHT, find_ytdlp, formats
        path = find_ytdlp(getattr(self.cfg, "ytdlp_path", ""))
        if not path:
            return ["--ytdl=no"]
        joined, single = formats(getattr(self, "youtube_height", DEFAULT_HEIGHT))
        return ["--ytdl=yes", f"--ytdl-format={joined}/{single}", f"--script-opts=ytdl_hook-ytdl_path={path}"]

    def _open_log(self):
        """Append to mpv.log, starting over once it passes 512 KB."""
        path = expand(self.cfg.mpv_log)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            big = path.exists() and path.stat().st_size > 512 * 1024
            f = open(path, "w" if big else "a")
            f.write(f"\n--- mpv started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            f.flush()
            return f
        except OSError:
            return subprocess.DEVNULL

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.ipc.close()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def finished_normally(self) -> bool:
        """mpv exits with status 0 when the playlist plays through; a crash or a kill gives something else."""
        return self.proc is not None and self.proc.poll() == 0

    def command(self, *args):
        return self.ipc.command(*args)

    def get(self, prop: str):
        try:
            return self.ipc.command("get_property", prop)
        except Exception:
            return None

    def set_overlay(self, raw: bytes, w: int, h: int) -> None:
        with open(self.overlay_file, "wb") as f:
            f.write(raw)
        self.command("overlay-add", OVERLAY_ID, 0, 0, self.overlay_file, 0, "bgra", w, h, w * 4)

    def clear_overlay(self) -> None:
        self.command("overlay-remove", OVERLAY_ID)


# ─────────────────────────────────────────────
#  A pretend mpv
# ─────────────────────────────────────────────
def default_duration(path: str) -> float:
    """A stable made-up length for a file (the real one comes from the library index later)."""
    return 40.0 + (zlib.crc32(path.encode()) % 50)


class FakePlayer:
    """Just enough of mpv: a playlist that advances with time, pause, seek, overlay."""

    def __init__(self, short_static: str, long_static: str,
                 duration_of: Callable[[str], float] = default_duration,
                 clock: Callable[[], float] = time.monotonic, speed: float = 1.0, url_delay: float = 0.0,
                 url_fail: Tuple[str, ...] = ()):
        self.short_static, self.long_static = short_static, long_static
        self.youtube_height = 360                                      # like MpvPlayer: the app sets it from its settings
        self.url_delay, self.url_fail = url_delay, tuple(url_fail)     # network entries load slowly; some never do
        self._stall_until, self._will_fail = 0.0, False
        self.duration_of = duration_of
        self.clock = clock
        self.speed = speed
        self._lock = threading.RLock()
        self.playlist: List[str] = []
        self.pos = 0
        self.time_pos = 0.0
        self.paused = False
        self._last = clock()
        self._alive = False
        self._ended = False                           # the playlist played through (as opposed to being stopped)
        self.overlay: Optional[tuple] = None          # (raw bgra bytes, w, h)
        self.commands: List[tuple] = []               # everything the app asked for (tests look at this)

    # --- time ------------------------------------------------------------------------
    def _dur(self, path: str) -> float:
        if path == self.short_static:
            return 0.64
        if path == self.long_static:
            return 3.2
        if path.startswith("edl://"):                                  # a made-up file: the lengths of its segments, added up
            import re
            return sum(float(x) for x in re.findall(r",0,([0-9.]+)(?:;|$)", path)) or 1.0
        return self.duration_of(path)

    def is_static(self, path: str) -> bool:
        return path in (self.short_static, self.long_static) or path.startswith("edl://")

    def _arm(self) -> None:
        """Entering a playlist entry: a network address has to load first, like mpv resolving a YouTube link."""
        cur = self.playlist[self.pos] if self.playlist else ""
        if "://" in cur and not cur.startswith("edl://"):
            self._stall_until, self._will_fail = self.clock() + self.url_delay, any(k in cur for k in self.url_fail)
        else:
            self._stall_until, self._will_fail = 0.0, False

    def _tick(self) -> None:
        now = self.clock()
        if self._alive and self.playlist and not self.paused:
            dt = (now - self._last) * self.speed
            if self._stall_until:
                if now < self._stall_until:
                    dt = 0.0                                       # still loading
                else:
                    self._stall_until = 0.0
                    if self._will_fail:                            # couldn't be opened: mpv moves on to the next entry
                        if self.pos + 1 >= len(self.playlist):
                            self._alive, self._ended = False, True
                        else:
                            self.pos, self.time_pos = self.pos + 1, 0.0
                            self._arm()
                        dt = 0.0
            self.time_pos += dt
            while self._alive and self.time_pos >= self._dur(self.playlist[self.pos]):
                self.time_pos -= self._dur(self.playlist[self.pos])
                if self.pos + 1 >= len(self.playlist):
                    self._alive = False                 # like mpv: it quits when the playlist ends
                    self._ended = True
                    self.time_pos = self._dur(self.playlist[self.pos])
                    break
                self.pos += 1
                self._arm()
        self._last = now

    # --- the Player interface ------------------------------------------------------
    def start(self, playlist_path: str) -> None:
        with self._lock:
            self._load(playlist_path)
            self._alive = True
            self._ended = False
            self.paused = False
            self._last = self.clock()
            self._arm()

    def _load(self, path: str) -> None:
        if str(path).endswith(".m3u"):
            with open(path) as f:
                self.playlist = [l.strip() for l in f if l.strip()]
        else:
            self.playlist = [str(path)]
        self.pos, self.time_pos = 0, 0.0
        self._arm()

    def stop(self) -> None:
        with self._lock:
            self._alive = False
            self._ended = False

    def alive(self) -> bool:
        with self._lock:
            self._tick()
            return self._alive

    def finished_normally(self) -> bool:
        with self._lock:
            self._tick()
            return self._ended and not self._alive

    def get(self, prop: str):
        with self._lock:
            self._tick()
            if not self._alive and prop not in ("pause",):
                return None
            if prop == "pause":
                return self.paused
            if prop == "playlist-pos":
                return self.pos
            if prop == "playlist-count":
                return len(self.playlist)
            if prop == "path":
                return self.playlist[self.pos] if self.playlist else None
            if prop == "time-pos":
                return None if self._stall_until else self.time_pos
            if prop == "duration":
                return self._dur(self.playlist[self.pos]) if self.playlist else None
            if prop == "core-idle":
                return self.paused
            return None

    def command(self, *args):
        with self._lock:
            self._tick()
            if not self._alive:
                raise ConnectionError("fake mpv is not running")
            self.commands.append(args)
            name = args[0]
            if name == "set_property":
                prop, value = args[1], args[2]
                if prop == "pause":
                    self.paused = bool(value)
                elif prop == "playlist-pos":
                    self.pos, self.time_pos = max(0, min(int(value), len(self.playlist) - 1)), 0.0
                    self._arm()
                elif prop == "time-pos":
                    self.time_pos = float(value)
            elif name == "cycle" and args[1] == "pause":
                self.paused = not self.paused
            elif name == "loadfile":
                self._load(args[1])
            elif name == "seek":
                self.time_pos = float(args[1]) if (len(args) < 3 or args[2] == "absolute") else self.time_pos + float(args[1])
            elif name == "playlist-next":
                self.pos, self.time_pos = min(self.pos + 1, len(self.playlist) - 1), 0.0
                self._arm()
            return None

    def set_overlay(self, raw: bytes, w: int, h: int) -> None:
        self.overlay = (raw, w, h)

    def clear_overlay(self) -> None:
        self.overlay = None

    def current_title(self) -> str:
        with self._lock:
            self._tick()
            if not self.playlist:
                return ""
            return os.path.basename(self.playlist[self.pos])
