"""
Dim at night: full brightness by day, a lower level from sunset to sunrise (or between two set times), fading over half an hour at each
end like a Mac's day/night cycle. Opt in, from the web page.

The TV has no GPS, so "sunset" needs a latitude and longitude typed once on the page. Without one (or somewhere the sun doesn't set)
the set times are used. The clock is the TV's own (set from the network), and the day is the TV's local day.

Pure logic: no drawing, no hardware. app.py asks `Night.level(day_level, now)`.
"""
from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Tuple

RAMP = 30 * 60                     # seconds to fade at each end of the night
DEFAULT_NIGHT_LEVEL = 25           # percent
DEFAULT_FROM, DEFAULT_TO = "21:00", "07:00"
MODES = ("sun", "times")


# ─────────────────────────────────────────────
#  Sunrise and sunset (the almanac algorithm: good to a few minutes, which is plenty for a screen)
# ─────────────────────────────────────────────
def _sun_event(day: date, lat: float, lon: float, rising: bool) -> Optional[float]:
    """The UTC time (epoch seconds) of sunrise or sunset on the UTC date `day` at this place, or None (polar day or night)."""
    rad, deg = math.radians, math.degrees
    n = day.timetuple().tm_yday
    lng_hour = lon / 15.0
    t = n + (((6 if rising else 18) - lng_hour) / 24.0)
    m = 0.9856 * t - 3.289
    l = (m + 1.916 * math.sin(rad(m)) + 0.020 * math.sin(rad(2 * m)) + 282.634) % 360
    ra = deg(math.atan(0.91764 * math.tan(rad(l)))) % 360
    ra += (math.floor(l / 90) * 90) - (math.floor(ra / 90) * 90)
    ra /= 15.0
    sin_dec = 0.39782 * math.sin(rad(l))
    cos_dec = math.cos(math.asin(sin_dec))
    cos_h = (math.cos(rad(90.833)) - sin_dec * math.sin(rad(lat))) / (cos_dec * math.cos(rad(lat)))
    if cos_h > 1 or cos_h < -1:
        return None
    h = (360 - deg(math.acos(cos_h))) if rising else deg(math.acos(cos_h))
    h /= 15.0
    ut = (h + ra - 0.06571 * t - 6.622 - lng_hour) % 24
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return midnight + ut * 3600


def sun_events(now: float, lat: float, lon: float) -> List[Tuple[float, str]]:
    """Sunrises and sunsets around `now`, in time order: [(epoch, 'rise' | 'set')]. Looks at the day before, this one and the next
    (UTC dates), so a sunset that falls just after midnight UTC is still found."""
    today = datetime.fromtimestamp(now, timezone.utc).date()
    out = []
    for offset in (-1, 0, 1):
        day = today + timedelta(days=offset)
        for rising, kind in ((True, "rise"), (False, "set")):
            ts = _sun_event(day, lat, lon, rising)
            if ts is not None:
                out.append((ts, kind))
    return sorted(out)


def _night_around(now: float, events: List[Tuple[float, str]]) -> Optional[Tuple[float, float]]:
    """The night (sunset, sunrise) that `now` falls in, or None if it is day."""
    sets = [ts for ts, kind in events if kind == "set" and ts <= now]
    if not sets:
        return None
    start = sets[-1]
    rises = [ts for ts, kind in events if kind == "rise" and ts > start]
    return (start, rises[0]) if rises and now < rises[0] else None


# ─────────────────────────────────────────────
#  Parsing what the page sends
# ─────────────────────────────────────────────
def parse_location(text: str) -> Optional[Tuple[float, float]]:
    """'51.5072, -0.1276' -> (51.5072, -0.1276). Blank -> None. Raises ValueError with a message fit for the page."""
    text = str(text or "").strip()
    if not text:
        return None
    parts = [p for p in text.replace(";", ",").replace(" ", ",").split(",") if p]
    try:
        lat, lon = float(parts[0]), float(parts[1])
        if len(parts) != 2:
            raise ValueError
    except (ValueError, IndexError):
        raise ValueError("Enter the latitude then the longitude, like 51.5072, -0.1276 (negative means south or west).")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("That is off the map: latitude is -90 to 90 and longitude -180 to 180.")
    return (round(lat, 4), round(lon, 4))


def parse_clock(text: str) -> int:
    """'21:30' -> minutes since midnight. Raises ValueError with a message fit for the page."""
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except ValueError:
        pass
    raise ValueError("Enter a time like 21:30.")


def format_clock(minutes: int) -> str:
    return "%02d:%02d" % (minutes // 60 % 24, minutes % 60)


# ─────────────────────────────────────────────
#  The schedule
# ─────────────────────────────────────────────
@dataclass
class Night:
    enabled: bool = False
    level: int = DEFAULT_NIGHT_LEVEL                  # percent at night
    mode: str = "sun"                                 # 'sun' (sunset to sunrise at `location`) or 'times'
    location: Optional[Tuple[float, float]] = None
    start: int = parse_clock(DEFAULT_FROM)            # minutes since midnight, for 'times' (and when the sun can't be worked out)
    end: int = parse_clock(DEFAULT_TO)

    @classmethod
    def from_settings(cls, get: Callable, levels: Sequence[int]) -> "Night":
        """From settings.json values, tolerating anything damaged (it falls back to the defaults, never raises)."""
        n = cls()
        n.enabled = get("night_mode", False) is True
        level = get("night_brightness", DEFAULT_NIGHT_LEVEL)
        n.level = level if level in levels else min(levels, key=lambda v: abs(v - DEFAULT_NIGHT_LEVEL))
        mode = get("night_schedule", "sun")
        n.mode = mode if mode in MODES else "sun"
        try:
            loc = get("night_location", None)
            n.location = parse_location("%s,%s" % (loc[0], loc[1])) if isinstance(loc, (list, tuple)) and len(loc) == 2 else None
        except ValueError:
            n.location = None
        for attr, key, default in (("start", "night_from", DEFAULT_FROM), ("end", "night_to", DEFAULT_TO)):
            try:
                setattr(n, attr, parse_clock(get(key, default)))
            except ValueError:
                setattr(n, attr, parse_clock(default))
        return n

    @property
    def uses_sun(self) -> bool:
        return self.mode == "sun" and self.location is not None

    def _window(self, now: float) -> Optional[Tuple[float, float]]:
        """The night (start, end) in epoch seconds that `now` is inside, or None."""
        if self.uses_sun:
            events = sun_events(now, *self.location)
            if events:                                                # the sun does rise and set here
                return _night_around(now, events)
        # set times, in the TV's local time (also the fallback where the sun doesn't rise or set)
        local = datetime.fromtimestamp(now)
        for day_offset in (-1, 0):
            day = (local + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
            begin = (day + timedelta(minutes=self.start)).timestamp()
            finish = (day + timedelta(minutes=self.end)).timestamp()
            if finish <= begin:
                finish = (day + timedelta(days=1, minutes=self.end)).timestamp()
            if begin <= now < finish:
                return (begin, finish)
        return None

    def fraction(self, now: float) -> float:
        """0.0 in full daylight, 1.0 in the depth of the night, fading over half an hour at each end."""
        if not self.enabled:
            return 0.0
        window = self._window(now)
        if window is None:
            return 0.0
        begin, finish = window
        return max(0.0, min(1.0, (now - begin) / RAMP, (finish - now) / RAMP))

    def level_at(self, day_level: int, now: float) -> int:
        """The brightness to show now: the day level, fading to the night level (never brighter than the day level)."""
        f = self.fraction(now)
        night = min(self.level, day_level)
        return int(round(day_level + (night - day_level) * f))

    def is_night(self, now: float) -> bool:
        return self.fraction(now) >= 0.5

    def describe(self, now: float) -> dict:
        """For the web page: what it is working from, and today's sunrise and sunset if known."""
        info = {"using": "sun" if self.uses_sun else "times", "night": self.is_night(now), "sunrise": "", "sunset": ""}
        if self.uses_sun:
            events = sun_events(now, *self.location)
            today = datetime.fromtimestamp(now).date()
            for ts, kind in events:
                if datetime.fromtimestamp(ts).date() == today:
                    info["sunrise" if kind == "rise" else "sunset"] = datetime.fromtimestamp(ts).strftime("%H:%M")
        return info


def clock_synced() -> bool:
    """Has the TV's clock been set from the network this time round? The Pi has no battery-backed clock, so until it has, the time
    of day can't be trusted and dimming at night waits. Where there is no `timedatectl` to ask (a desktop), the clock is trusted."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "yes"


# ─────────────────────────────────────────────
#  A rough location from the TV's own time zone (no browser, no internet, nothing sent anywhere)
# ─────────────────────────────────────────────
ZONEINFO = "/usr/share/zoneinfo"


def _iso6709(text: str) -> Tuple[float, float]:
    """'+513030-0000731' -> (51.5083, -0.1253): the coordinates as written in the time zone tables."""
    i = max(text.rfind("+"), text.rfind("-"))
    lat_s, lon_s = text[:i], text[i:]

    def part(s: str, degree_digits: int) -> float:
        sign = -1.0 if s[0] == "-" else 1.0
        digits = s[1:]
        deg = int(digits[:degree_digits])
        minutes = int(digits[degree_digits:degree_digits + 2] or 0)
        seconds = int(digits[degree_digits + 2:degree_digits + 4] or 0)
        return sign * (deg + minutes / 60.0 + seconds / 3600.0)

    return (round(part(lat_s, 2), 4), round(part(lon_s, 3), 4))


def system_timezone(root: str = "/etc/localtime") -> str:
    """The TV's time zone name, e.g. 'Europe/London' ('' if it can't be told)."""
    try:
        target = os.path.realpath(root)
    except OSError:
        return ""
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else ""


def timezone_location(zone: str = "", zoneinfo: str = ZONEINFO) -> Optional[Tuple[str, Tuple[float, float]]]:
    """(zone name, (latitude, longitude)) of the TV's time zone's main city, from the system's time zone tables; None if unknown.
    Good to a few hundred kilometres, which moves sunset by a quarter of an hour or so: fine for a screen, and a place to start from."""
    zone = zone or system_timezone()
    if not zone:
        return None
    for table in ("zone.tab", "zone1970.tab"):
        try:
            with open(os.path.join(zoneinfo, table), encoding="utf-8") as f:
                for line in f:
                    cols = line.rstrip("\n").split("\t")
                    if not line.startswith("#") and len(cols) >= 3 and cols[2] == zone:
                        return zone, _iso6709(cols[1])
        except (OSError, ValueError):
            continue
    return None


def level_from_settings(data: dict, levels: Sequence[int], now: Optional[float] = None, synced: bool = True) -> int:
    """The brightness to start at (the boot screen uses this). With the clock not yet synced it is the daytime level."""
    now = time.time() if now is None else now
    day = data.get("brightness", 100)
    day = day if day in levels else 100
    return Night.from_settings(data.get, levels).level_at(day, now) if synced else day
