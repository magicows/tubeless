"""
A one-line health note for the log, once a minute, so that when something odd happens (a stutter, a Wi-Fi drop) the log shows what
the power, heat, load and signal looked like at the time. It only reads; it never changes anything.

    health: power ok | temp 51.2C | load 1.31 | free 214 MB | wifi -58 dBm

The power flags come from the Pi's own `vcgencmd get_throttled`. They only trip when the 5 V supply drops under about 4.63 V, so
"power ok" does not rule out a short dip on the Wi-Fi chip's 3.3 V (a battery that sags under load can do that); a flag that says
"under-voltage has occurred" proves the supply is marginal.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

NOW_BITS = {0: "UNDER-VOLTAGE NOW", 1: "CPU speed capped now", 2: "THROTTLED NOW", 3: "temperature limit now"}
EVER_BITS = {16: "under-voltage has occurred since boot", 17: "CPU speed was capped since boot",
             18: "throttling has occurred since boot", 19: "temperature limit was reached since boot"}


def decode_throttled(value: int) -> Dict[str, List[str]]:
    """The Pi's throttled flags as words: what is wrong right now, and what has gone wrong at some point since boot."""
    return {"now": [text for bit, text in NOW_BITS.items() if value & (1 << bit)],
            "ever": [text for bit, text in EVER_BITS.items() if value & (1 << bit)]}


def parse_throttled(text: str) -> Optional[int]:
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", text or "")
    return int(m.group(1), 16) if m else None


def parse_temp(text: str) -> Optional[float]:
    m = re.search(r"temp=([0-9.]+)", text or "")
    return float(m.group(1)) if m else None


def parse_wireless(text: str, interface: str = "wlan0") -> Optional[int]:
    """Signal strength in dBm from /proc/net/wireless."""
    for line in (text or "").splitlines():
        if line.strip().startswith(interface + ":"):
            fields = line.split()
            try:
                return int(float(fields[3].rstrip(".")))
            except (IndexError, ValueError):
                return None
    return None


def _vcgencmd(*args: str) -> str:
    exe = shutil.which("vcgencmd")
    if not exe:
        return ""
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def snapshot(interface: str = "wlan0") -> Dict[str, object]:
    """Everything we can read cheaply right now (anything unavailable is None)."""
    mem = re.search(r"MemAvailable:\s+(\d+)", _read("/proc/meminfo"))
    try:
        load = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load = None
    return {"throttled": parse_throttled(_vcgencmd("get_throttled")), "temp": parse_temp(_vcgencmd("measure_temp")), "load": load,
            "free_mb": int(mem.group(1)) // 1024 if mem else None, "wifi_dbm": parse_wireless(_read("/proc/net/wireless"), interface)}


def has_vcgencmd() -> bool:
    return shutil.which("vcgencmd") is not None


def format_line(snap: Dict[str, object]) -> str:
    parts = []
    t = snap.get("throttled")
    if t is None:
        parts.append("power unknown")
    else:
        flags = decode_throttled(t)
        if flags["now"]:
            parts.append("POWER: " + ", ".join(flags["now"]) + f" (0x{t:x})")
        elif flags["ever"]:
            parts.append("power ok now, but " + ", ".join(flags["ever"]) + f" (0x{t:x})")
        else:
            parts.append("power ok")
    if snap.get("temp") is not None:
        parts.append(f"temp {snap['temp']:.1f}C")
    if snap.get("load") is not None:
        parts.append(f"load {snap['load']:.2f}")
    if snap.get("free_mb") is not None:
        parts.append(f"free {snap['free_mb']} MB")
    parts.append(f"wifi {snap['wifi_dbm']} dBm" if snap.get("wifi_dbm") is not None else "wifi no signal reading")
    return "health: " + " | ".join(parts)


def is_worrying(snap: Dict[str, object]) -> bool:
    t = snap.get("throttled")
    return bool(t is not None and t & 0xF)           # a power or heat problem right now
