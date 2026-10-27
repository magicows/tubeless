"""
The TV static: which clip plays between videos, and how to choose your own.

Playback never changes: mpv plays one clip, as a normal file, before every video (and when you change channel). What changes is
*which file* that is:

  built-in   "TV static with sound" (the default), "TV static, silent", (either can be turned off, between videos and on a channel change, with the lengths below)
  your own   a clip uploaded from the phone page, or picked from the TV's home folder (never ~/videos)

A clip's sound is settled at the moment you choose it: keep its own, or (with ffmpeg, which YouTube support installs) give it the TV
static sound, make it silent, or give it a sound file you uploaded. The TV makes one new file from it, once, and plays that.

A choice is a small dict kept in settings.json:
    {"kind": "preset", "name": "sound" | "silent"}
    {"kind": "upload", "dir": "clip", "name": "x.mp4"}           a file in <data>/static/clip/
    {"kind": "home",   "path": "/home/me/clips/x.mp4"}           a file already on the TV, in the user's home folder
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import advice
from .config import ASSETS_DIR, Config

log = logging.getLogger(__name__)

CLIP_EXTS = (".mp4", ".mkv", ".m4v", ".mov", ".avi", ".webm", ".gif")
SOUND_EXTS = (".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".flac")
PRESETS = {"sound": "TV static with sound", "silent": "TV static, silent"}
SOUNDS = ("own", "static", "silent", "file")
MAX_UPLOAD = {"clip": 50 * 1024 * 1024, "sound": 20 * 1024 * 1024}
CAP_SECONDS = 5                     # a clip made by the TV is cut to this length: it plays before every single video
MAX_LISTED, MAX_DEPTH = 200, 3
SKIP_DIRS = {"tubeless", "node_modules", "site-packages", "__pycache__", "snap"}
DEFAULT = {"kind": "preset", "name": "sound"}
# How long your own clip plays: briefly between videos, longer when you change channel (the built-in static has a short and a
# long clip for this, as the original project did). One clip serves both: the TV plays its first part, or repeats it if it is shorter.
# 0 turns that static off: between videos the next one then follows at once (a clean cut), and on a channel change the new channel just starts.
BETWEEN_OPTIONS = (0.0, 0.6, 1.0, 2.0, 3.0)
CHANGE_OPTIONS = (0.0, 2.0, 3.0, 4.0, 5.0)
DEFAULT_BETWEEN, DEFAULT_CHANGE = 0.6, 3.0
BUILTIN_CLIP_SECONDS = 3.1          # the built-in long clip is a little longer than this, so asking for more repeats it


def exts(kind: str) -> tuple:
    return CLIP_EXTS if kind == "clip" else SOUND_EXTS


def check_kind(kind: str) -> str:
    if kind not in ("clip", "sound"):
        raise ValueError("That is neither a clip nor a sound.")
    return kind


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def list_home(home: Path, library_root: Path, kind: str, limit: int = MAX_LISTED) -> List[dict]:
    """Video clips (or sounds) in the user's home folder, not the videos library, a few folders deep. Newest first."""
    home, library_root = Path(home).resolve(), Path(library_root).resolve()
    found = []
    allowed = exts(check_kind(kind))
    for dirpath, dirnames, files in os.walk(home):
        here = Path(dirpath)
        depth = len(here.relative_to(home).parts)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS and depth < MAX_DEPTH
                       and (here / d).resolve() != library_root and not _inside((here / d).resolve(), library_root)]
        for f in files:
            if f.startswith(".") or not f.lower().endswith(allowed):
                continue
            p = here / f
            try:
                st = p.stat()
            except OSError:
                continue
            found.append({"path": str(p.relative_to(home)).replace(os.sep, "/"), "size": st.st_size, "mtime": st.st_mtime})
    found.sort(key=lambda d: -d["mtime"])
    return [{"path": d["path"], "size": d["size"]} for d in found[:limit]]


def check_home_pick(home: Path, library_root: Path, rel: str, kind: str) -> Path:
    """The file a viewer picked from list_home(), checked again: a real file of the right type in the home folder."""
    home, library_root = Path(home).resolve(), Path(library_root).resolve()
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise ValueError("That file isn't in the TV's home folder.")
    path = (home / rel).resolve()
    if not _inside(path, home) or _inside(path, library_root) or any(part.startswith(".") for part in path.relative_to(home).parts):
        raise ValueError("That file isn't somewhere the TV may use for this.")
    if not path.is_file():
        raise ValueError("That file isn't there any more.")
    if not path.name.lower().endswith(exts(kind)):
        raise ValueError("That isn't a " + ("video clip" if kind == "clip" else "sound") + " file type the TV can use here.")
    return path


def probe_seconds(path) -> Optional[float]:
    """How long a clip is: mediainfo if it is there, else ffprobe, else unknown."""
    info = advice.probe(str(path), timeout=15)
    if info and info.get("duration"):
        return float(info["duration"])
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
            return float(out) if out else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def ffmpeg_command(ffmpeg: str, src: str, out: str, sound: str, audio: Optional[str] = None, cap: int = CAP_SECONDS) -> List[str]:
    """Make a Pi-friendly static clip from a source: H.264 at most 480 lines tall, at most `cap` seconds, with the chosen sound."""
    video = ["-vf", "scale=-2:'min(480,ih)',setsar=1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p"]
    cmd = [ffmpeg, "-y", "-v", "error", "-i", src]
    if sound in ("static", "file"):
        if not audio:
            raise ValueError("There is no sound to add.")
        # the added sound is looped to be as long as it needs; the picture decides when the clip ends
        cmd += ["-stream_loop", "-1", "-i", audio, "-map", "0:v:0", "-map", "1:a:0"] + video
        cmd += ["-c:a", "aac", "-b:a", "64k", "-shortest", "-t", str(cap), out]
    elif sound == "silent":
        cmd += ["-map", "0:v:0", "-an"] + video + ["-t", str(cap), out]
    else:
        raise ValueError("Nothing to convert.")
    return cmd


def edl_entry(path: str, seconds: float, clip_seconds: Optional[float] = None) -> str:
    """A playlist entry that mpv plays as a file `seconds` long made from the clip: its first part, or the clip over again if it
    is shorter than that (static is noise: nobody sees the join). `%n%` gives the path's length in bytes, so commas, spaces and
    semicolons in a file name can't be misread."""
    n = len(path.encode("utf-8"))

    def seg(length: float) -> str:
        return f"%{n}%{path},0,{length:g}"

    if not clip_seconds or clip_seconds >= seconds - 0.02:
        segments = [seg(seconds)]
    else:
        whole = int(seconds // clip_seconds)
        rest = seconds - whole * clip_seconds
        segments = [seg(clip_seconds)] * whole + ([seg(rest)] if rest > 0.05 else [])
    return "edl://" + ";".join(segments)


def _stem(name: str) -> str:
    return Path(name).stem[:60] or "clip"


class StaticManager:
    """The choice of TV static clip, the files that go with it, and the page's operations on them."""

    def __init__(self, cfg: Config, settings, library_root, on_change: Callable[[], None] = lambda: None,
                 ffmpeg: Optional[str] = None, home: Optional[Path] = None):
        self.cfg, self.settings, self.root = cfg, settings, Path(library_root)
        self.on_change = on_change
        self.ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self.home = Path(home) if home else Path.home()
        raw = self.settings.get("static_choice")
        if isinstance(raw, dict) and raw.get("kind") == "preset" and raw.get("name") == "none":
            # the old "No static" choice (a quick blank): now it is the default static with none between videos
            self.settings.set(static_choice=dict(DEFAULT), static_between=0.0)

    # --- where things are ------------------------------------------------------------------------
    @property
    def folder(self) -> Path:
        return self.cfg.data / "static"

    def _dir(self, kind: str) -> Path:
        return self.folder / check_kind(kind)

    def _spec(self) -> dict:
        spec = self.settings.get("static_choice")
        return spec if isinstance(spec, dict) and spec.get("kind") in ("preset", "upload", "home") else dict(DEFAULT)

    def _resolve(self, spec: dict) -> Optional[Path]:
        if spec.get("kind") == "upload":
            name = str(spec.get("name", ""))
            if not name or "/" in name or name.startswith("."):
                return None
            p = self._dir("clip") / name
        elif spec.get("kind") == "home":
            p = Path(str(spec.get("path", "")))
            if not p.is_absolute():
                return None
        else:
            return None
        return p if p.is_file() else None

    def _builtin_files(self, name) -> Tuple[str, str]:
        """(short clip, long clip) of a built-in static."""
        if name == "silent":
            return str(ASSETS_DIR / "static_short_silent.mp4"), str(ASSETS_DIR / "static_long_silent.mp4")
        return self.cfg.short_static, self.cfg.long_static

    def _preset_paths(self, name) -> Tuple[str, str]:
        """The built-in clips as they play: the files themselves at the default lengths, otherwise the start of the long clip for as
        long as is chosen (repeated if need be)."""
        short, long_ = self._builtin_files(name)
        between, change = self.lengths()
        return (short if between in (0.0, DEFAULT_BETWEEN) else edl_entry(long_, between, BUILTIN_CLIP_SECONDS),
                long_ if change in (0.0, DEFAULT_CHANGE) else edl_entry(long_, change, BUILTIN_CLIP_SECONDS))

    def paths(self) -> Tuple[str, str]:
        """(what plays before each video, what plays when you change channel): file names for the built-in clips, and for your
        own clip a playlist entry that plays just the first part of it (see edl_entry). A choice whose file has gone falls back
        to the default."""
        spec = self._spec()
        if spec["kind"] == "preset":
            return self._preset_paths(spec.get("name"))
        p = self._resolve(spec)
        if not p:
            return self.cfg.short_static, self.cfg.long_static
        clip = self.settings.get("static_seconds")
        clip = float(clip) if isinstance(clip, (int, float)) and clip > 0 else None
        between, change = self.lengths()
        return edl_entry(str(p), between or DEFAULT_BETWEEN, clip), edl_entry(str(p), change or DEFAULT_CHANGE, clip)

    def lengths(self) -> Tuple[float, float]:
        """(seconds of static between videos, seconds when you change channel); 0 is off."""
        between, change = self.settings.get("static_between"), self.settings.get("static_change")
        return (between if between in BETWEEN_OPTIONS else DEFAULT_BETWEEN, change if change in CHANGE_OPTIONS else DEFAULT_CHANGE)

    def between_on(self) -> bool:
        """Is there static between videos? Off puts none there: videos follow each other (a clean cut)."""
        return self.lengths()[0] > 0

    def change_on(self) -> bool:
        """Is there static when you change channel? Off: the new channel just starts."""
        return self.lengths()[1] > 0

    def gap(self) -> float:
        """Seconds between one video and the next that are not video: the static's length, or none without static."""
        return self.lengths()[0]

    def delay(self, normal: float) -> float:
        """How long the channel-change static holds the screen before the new channel loads (0 with it off)."""
        change = self.lengths()[1]
        spec = self._spec()
        if spec["kind"] == "preset":
            return normal if change == DEFAULT_CHANGE else change
        return change if self._resolve(spec) else normal

    def set_lengths(self, between=None, change=None) -> None:
        if between is not None:
            if between not in BETWEEN_OPTIONS:
                raise ValueError("Choose one of the lengths offered for between videos.")
        if change is not None:
            if change not in CHANGE_OPTIONS:
                raise ValueError("Choose one of the lengths offered for changing channel.")
        updates = {}
        if between is not None:
            updates["static_between"] = float(between)
        if change is not None:
            updates["static_change"] = float(change)
        if updates:
            self.settings.set(**updates)
            self.on_change()

    # --- what the page shows ---------------------------------------------------------------------
    def state(self) -> dict:
        spec = self._spec()
        if spec["kind"] == "preset":
            choice = {"kind": "preset", "name": spec.get("name"), "label": PRESETS.get(spec.get("name"), PRESETS["sound"])}
        else:
            p = self._resolve(spec)
            choice = ({"kind": spec["kind"], "label": p.name, "name": None} if p
                      else {"kind": "preset", "name": "sound", "label": PRESETS["sound"], "missing": True})
        listing = {}
        for kind in ("clip", "sound"):
            try:
                listing[kind + "s"] = sorted(f.name for f in self._dir(kind).iterdir() if f.is_file() and not f.name.startswith("."))
            except OSError:
                listing[kind + "s"] = []
        between, change = self.lengths()
        return {"choice": choice, "presets": [{"name": k, "label": v} for k, v in PRESETS.items()],
                "ffmpeg": bool(self.ffmpeg), "uploads": listing,
                "lengths": {"between": between, "change": change, "between_options": list(BETWEEN_OPTIONS),
                            "change_options": list(CHANGE_OPTIONS), "applies": True}}

    def current_file(self) -> Path:
        """The actual file of the chosen clip (for the page's preview), not the playlist entry made from it."""
        spec = self._spec()
        p = self._resolve(spec) if spec["kind"] != "preset" else None
        return p if p else Path(self._builtin_files(spec.get("name"))[1])

    def home_files(self, kind: str) -> List[dict]:
        return list_home(self.home, self.root, kind)

    # --- choosing --------------------------------------------------------------------------------
    def _select(self, spec: dict, seconds: Optional[float]) -> None:
        self.settings.set(static_choice=spec, static_seconds=round(seconds, 2) if seconds else None)
        self.on_change()

    def choose_preset(self, name: str) -> None:
        if name not in PRESETS:
            raise ValueError("That isn't one of the built-in choices.")
        self._select({"kind": "preset", "name": name}, None)
        log.info("TV static: %s", PRESETS[name])

    def use(self, kind: str, ref: str, sound: str = "own", sound_name: str = "") -> dict:
        """Use a clip that is on the TV: one uploaded to it (`ref` is its name) or one in the home folder (`ref` is its path
        under home). `sound` says what its sound should be; anything but 'own' makes a new file with ffmpeg."""
        if kind == "upload":
            name = Path(ref).name
            src = self._dir("clip") / name
            if not name or name != ref or not src.is_file():
                raise ValueError("That upload isn't there any more.")
        elif kind == "home":
            src = check_home_pick(self.home, self.root, ref, "clip")
        else:
            raise ValueError("Choose an uploaded clip or one from the home folder.")
        if sound not in SOUNDS:
            raise ValueError("Choose what the clip's sound should be.")
        info = advice.probe(str(src), timeout=20)
        if info is not None and not info.get("video"):
            raise ValueError("That file has no picture in it, so it can't be used as TV static.")
        if sound == "own":
            spec = {"kind": "upload", "dir": "clip", "name": src.name} if kind == "upload" else {"kind": "home", "path": str(src)}
            self._select(spec, probe_seconds(src))
            log.info("TV static: %s (its own sound)", src.name)
            return self.state()
        if not self.ffmpeg:
            raise ValueError("Changing a clip's sound needs ffmpeg, which isn't installed on this TV "
                             "(it comes with YouTube support). You can still use the clip with its own sound.")
        audio = None
        if sound == "static":
            audio = self.cfg.builtin_noise
        elif sound == "file":
            audio_path = self._dir("sound") / Path(sound_name).name
            if not sound_name or not audio_path.is_file():
                raise ValueError("Upload a sound file first, then choose it.")
            audio = str(audio_path)
        tag = {"static": "static-sound", "silent": "silent", "file": "own-sound"}[sound]
        out = self._dir("clip") / f"{_stem(src.name)}-{tag}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name("." + out.name + ".part.mp4")
        try:
            res = subprocess.run(ffmpeg_command(self.ffmpeg, str(src), str(tmp), sound, audio), capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            raise ValueError("Converting that clip took too long. Try a shorter one.")
        if res.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            tail = [l for l in (res.stderr or "").splitlines() if l.strip()][-1:] or ["ffmpeg failed"]
            raise ValueError("The TV couldn't convert that clip (" + tail[0][:120] + ").")
        os.replace(tmp, out)
        self._select({"kind": "upload", "dir": "clip", "name": out.name}, probe_seconds(out))
        log.info("TV static: made %s from %s (%s)", out.name, src.name, sound)
        return self.state()

    # --- files -----------------------------------------------------------------------------------
    def save_upload(self, kind: str, name: str, rfile, length: int) -> str:
        from .web import sanitize_filename
        check_kind(kind)
        clean = sanitize_filename(name)
        if not clean or not clean.lower().endswith(exts(kind)):
            raise ValueError("That isn't a " + ("video clip" if kind == "clip" else "sound") + " file type the TV can use here.")
        if length <= 0:
            raise ValueError("That file is empty.")
        if length > MAX_UPLOAD[kind]:
            raise ValueError(f"That file is too big (the limit is {MAX_UPLOAD[kind] // (1024 * 1024)} MB).")
        folder = self._dir(kind)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / ("." + clean + ".part")
        left = length
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = rfile.read(min(65536, left))
                    if not chunk:
                        raise ValueError("The upload was cut short.")
                    f.write(chunk)
                    left -= len(chunk)
            info = advice.probe(str(tmp), timeout=20)
            if info is not None and not info.get("video" if kind == "clip" else "audio"):
                raise ValueError("That file has no " + ("picture" if kind == "clip" else "sound") + " in it.")
            os.replace(tmp, folder / clean)
        finally:
            tmp.unlink(missing_ok=True)
        return clean

    def delete(self, kind: str, name: str) -> None:
        folder = self._dir(kind)
        clean = Path(name).name
        if not clean or clean != name or clean.startswith("."):
            raise ValueError("That isn't a file the TV is holding.")
        (folder / clean).unlink(missing_ok=True)
        spec = self._spec()
        if kind == "clip" and spec.get("kind") == "upload" and spec.get("name") == clean:
            self.choose_preset("sound")                                  # the clip in use is gone: back to the default
