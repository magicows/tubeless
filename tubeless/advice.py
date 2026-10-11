"""
Advice about a video file, not conversion. Tubeless never re-encodes anything: after an upload it tells you what it
found and what would make playback smoother or the file smaller, and you decide.

The numbers come from this build: a 640x480 picture on a Pi Zero 2 W, which decodes video in software.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Advice:
    level: str      # "ok" | "info" | "warn"
    text: str

    def as_dict(self) -> dict:
        return {"level": self.level, "text": self.text}


def _num(value: Any) -> Optional[float]:
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def parse_mediainfo(text: str) -> Optional[Dict[str, Any]]:
    """The facts we care about from `mediainfo --Output=JSON`, or None if it isn't usable."""
    try:
        tracks = json.loads(text)["media"]["track"]
    except (ValueError, KeyError, TypeError):
        return None
    info: Dict[str, Any] = {"container": None, "duration": None, "size": None, "video": None, "audio": None}
    for t in tracks:
        kind = t.get("@type")
        if kind == "General":
            info["container"] = t.get("Format")
            info["duration"] = _num(t.get("Duration"))
            info["size"] = _num(t.get("FileSize"))
            info["overall_bitrate"] = _num(t.get("OverallBitRate"))
        elif kind == "Video" and info["video"] is None:
            info["video"] = {
                "codec": t.get("Format"), "profile": t.get("Format_Profile"),
                "width": _num(t.get("Width")), "height": _num(t.get("Height")),
                "fps": _num(t.get("FrameRate")), "bitrate": _num(t.get("BitRate")),
                "bit_depth": _num(t.get("BitDepth")),
            }
        elif kind == "Audio" and info["audio"] is None:
            info["audio"] = {"codec": t.get("Format"), "channels": _num(t.get("Channels")), "bitrate": _num(t.get("BitRate"))}
    return info


def probe(path: str, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """Run mediainfo on a file. None if it isn't installed or can't read the file."""
    exe = shutil.which("mediainfo")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--Output=JSON", path], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_mediainfo(out.stdout) if out.returncode == 0 else None


HARD_CODECS = {"HEVC": "H.265/HEVC", "AV1": "AV1", "VP9": "VP9"}


def advise(info: Optional[Dict[str, Any]]) -> List[Advice]:
    """What to tell the user about a probed file. An empty 'ok' list means nothing worth saying."""
    if not info or not info.get("video"):
        return [Advice("info", "Couldn't read this file's details, so it is untested: it will be added anyway.")]
    v = info["video"]
    out: List[Advice] = []
    height, width = v.get("height"), v.get("width")
    codec = (v.get("codec") or "").strip()

    if codec in HARD_CODECS:
        out.append(Advice("warn", f"{HARD_CODECS[codec]} video is slow to decode on this Pi and may stutter. "
                                  "H.264 is the safe choice."))
    if v.get("bit_depth") and v["bit_depth"] > 8:
        out.append(Advice("warn", f"{int(v['bit_depth'])}-bit video is much slower to decode than 8-bit; it may stutter."))
    if height and height > 720:
        out.append(Advice("warn", f"{int(width or 0)}x{int(height)} is far bigger than the screen can show and too heavy for "
                                  "this Pi to decode smoothly. 480p (or even 360p) looks the same here and plays properly."))
    elif height and height > 480:
        out.append(Advice("info", f"{int(width or 0)}x{int(height)} is bigger than the 640x480 screen needs. 480p would be "
                                  "smaller and smoother with no visible difference."))
    bitrate = v.get("bitrate") or info.get("overall_bitrate")
    if bitrate and bitrate > 6_000_000:
        out.append(Advice("warn", f"At {bitrate / 1e6:.1f} Mbit/s this file is unnecessarily large and may stutter. "
                                  "About 1-1.5 Mbit/s is plenty for this screen."))
    elif bitrate and bitrate > 2_500_000:
        out.append(Advice("info", f"At {bitrate / 1e6:.1f} Mbit/s this file is larger than it needs to be "
                                  "(about 1-1.5 Mbit/s is plenty for this screen)."))
    if v.get("fps") and v["fps"] > 30.5:
        out.append(Advice("info", f"{v['fps']:.0f} frames per second: 24-30 is smoother on this Pi."))
    a = info.get("audio")
    if a and a.get("channels") and a["channels"] > 2:
        out.append(Advice("info", f"{int(a['channels'])}-channel audio is mixed down to the single speaker."))
    if not out:
        out.append(Advice("ok", "Looks good for this screen."))
    return out
