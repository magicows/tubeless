"""
The boot screen: green digital rain with a live status panel, drawn straight onto the
framebuffer while the TV starts. Every line is a real check (display, touch, audio,
backlight, video library, network, player). It exits as soon as the player has put its
first frame on screen, and lights the backlight (at the saved brightness) once its first
frame is drawn, so the text console is never seen.

    python -m tubeless boot               run for real (what tubeless-boot.service does)
    python -m tubeless boot --demo 30     a scripted 30 s run that doesn't wait for the player
    python -m tubeless boot --png out.png render one preview frame to a file (no screen needed)
"""
from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

from . import fonts
from .config import Config, expand
from .hardware import Hardware
from .library import Library
from .pixels import pack565, unpack565          # noqa: F401  (re-exported)
from .render import fit_text

log = logging.getLogger(__name__)

FPS = 7
TIMEOUT = 120                    # give up (and leave the screen alone) after this long

GREEN      = (0, 255, 70)
GREEN_MID  = (0, 190, 55)
GREEN_DIM  = (0, 120, 38)
GREEN_DARK = (0, 55, 20)
WHITE_G    = (200, 255, 215)

STEPS = ["DISPLAY", "TOUCH", "AUDIO", "BACKLIGHT", "LIBRARY", "NETWORK", "PLAYER"]
PANEL_ALPHA = 196


# ─────────────────────────────────────────────
#  Digital rain
# ─────────────────────────────────────────────
class Rain:
    COL_W, ROW_H, LEVELS = 14, 18, 6

    def __init__(self, w: int, h: int, seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self.w, self.h = w, h
        self.ncols = w // self.COL_W
        self.nrows = h // self.ROW_H + 1
        kana = fonts.load(fonts.KANA, 16)            # half-width katakana (a tiny bundled subset)
        ascii_font = fonts.load(fonts.MONO, 15)

        def glyph(ch):
            im = Image.new("L", (self.COL_W, self.ROW_H), 0)
            f = ascii_font if ord(ch) < 128 else kana
            ImageDraw.Draw(im).text((self.COL_W // 2, self.ROW_H // 2), ch, font=f, fill=255, anchor="mm")
            return im

        missing = glyph("￿").tobytes()          # what the font draws for a character it doesn't have
        chars = [ch for ch in [chr(c) for c in range(0xFF66, 0xFF9E)] + list("0123456789")
                 if glyph(ch).tobytes() != missing]
        self.masks = []                              # masks[char][level] -> uint8 (ROW_H, COL_W)
        for ch in chars:
            base = np.asarray(glyph(ch), dtype=np.uint16)
            self.masks.append([(base * (255 * (lv + 1) // self.LEVELS) >> 8).astype(np.uint8)
                               for lv in range(self.LEVELS)])
        self.head = [-self.rng.uniform(0, self.nrows * 1.5) for _ in range(self.ncols)]
        self.speed = [self.rng.uniform(0.35, 1.0) for _ in range(self.ncols)]
        self.length = [self.rng.randint(6, 14) for _ in range(self.ncols)]
        self.cells = [[self.rng.randrange(len(chars)) for _ in range(self.nrows)] for _ in range(self.ncols)]
        self.nchars = len(chars)

    def frame(self) -> np.ndarray:
        """The rain as a (h, w) uint8 intensity map; advances it one step."""
        intensity = np.zeros((self.h + self.ROW_H, self.w + self.COL_W), np.uint8)
        for c in range(self.ncols):
            head, length = self.head[c], self.length[c]
            x0 = c * self.COL_W
            for i in range(length):
                r = int(head) - i
                if 0 <= r < self.nrows:
                    lvl = self.LEVELS - 1 if i == 0 else 4 - min(4, int(4 * i / length))
                    y0 = r * self.ROW_H
                    region = intensity[y0:y0 + self.ROW_H, x0:x0 + self.COL_W]
                    np.maximum(region, self.masks[self.cells[c][r]][lvl], out=region)
            self.head[c] += self.speed[c]
            if int(self.head[c]) - length > self.nrows:
                self.head[c] = -self.rng.uniform(2, 14)
                self.speed[c] = self.rng.uniform(0.35, 1.0)
                self.length[c] = self.rng.randint(6, 14)
            if self.rng.random() < 0.25:             # flicker one glyph in this column
                self.cells[c][self.rng.randrange(self.nrows)] = self.rng.randrange(self.nchars)
        return intensity[:self.h, :self.w]


# ─────────────────────────────────────────────
#  The text layer, and compositing it over the rain
# ─────────────────────────────────────────────
def draw_foreground(hw: Hardware, name: str, greeting: str, t: float, lines: List[tuple],
                    progress: float, status: str) -> Image.Image:
    """
    lines: [(label, detail, state)] with state 'done' | 'pending' | 'warn'.
    Returns an RGB image: text and lines on black. The dark panel behind them is applied by Compositor.
    """
    img = Image.new("RGB", (hw.view_w, hw.view_h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    px0, py0, px1, py1 = hw.safe_rect
    x0, x1 = px0 + 24, px1 - 24
    d.rectangle(hw.safe_rect, outline=GREEN_DARK, width=1)
    for cx, cy, sx, sy in ((px0, py0, 1, 1), (px1, py0, -1, 1), (px0, py1, 1, -1), (px1, py1, -1, -1)):
        d.line([(cx, cy), (cx + 22 * sx, cy)], fill=GREEN, width=2)           # bright corner brackets
        d.line([(cx, cy), (cx, cy + 22 * sy)], fill=GREEN, width=2)

    hf = fonts.load(fonts.MONO_BOLD, 36)
    title = fit_text(d, name.upper(), hf, (x1 - x0) - 190)       # leave room for "// BOOT SEQUENCE"
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.text((x0 + dx, py0 + 18 + dy), title, font=hf, fill=GREEN_DIM)       # a little glow
    d.text((x0, py0 + 18), title, font=hf, fill=GREEN)
    d.text((x1, py0 + 36), "// BOOT SEQUENCE", font=fonts.load(fonts.MONO, 14), fill=GREEN_DIM, anchor="rs")
    d.line([(x0, py0 + 68), (x1, py0 + 68)], fill=GREEN_DARK, width=1)

    typed = greeting[:int(t * 12)]
    cursor = "_" if int(t * 2) % 2 == 0 else " "
    d.text((x0, py0 + 76), "> " + typed + cursor, font=fonts.load(fonts.MONO, 16), fill=WHITE_G)

    lf, df = fonts.load(fonts.MONO_BOLD, 17), fonts.load(fonts.MONO, 15)
    y = py0 + 108
    spinner = "|/-\\"[int(t * 8) % 4]
    for label, detail, state in lines:
        if state == "pending":
            d.text((x0, y), spinner + " " + label, font=lf, fill=GREEN_MID)
            d.text((x1, y), "...", font=lf, fill=GREEN_DIM, anchor="ra")
        else:
            d.text((x0, y), "> " + label, font=lf, fill=GREEN)
            if detail:
                d.text((260, y + 2), detail, font=df, fill=GREEN_MID)
            word, col = ("OK", WHITE_G) if state == "done" else ("--", GREEN_DIM)
            d.text((x1, y), word, font=lf, fill=col, anchor="ra")
        y += 28

    by0 = py1 - 68
    by1 = by0 + 16
    d.rectangle((x0, by0, x1, by1), outline=GREEN_DIM, width=1)
    fill_w = int((x1 - x0 - 4) * max(0.0, min(1.0, progress)))
    if fill_w > 0:
        d.rectangle((x0 + 2, by0 + 2, x0 + 2 + fill_w, by1 - 2), fill=GREEN)
    d.text((x1, by1 + 6), "%3d%%" % int(progress * 100), font=fonts.load(fonts.MONO, 14), fill=GREEN_MID, anchor="ra")
    d.text((x0, by1 + 6), "> " + status + ("_" if int(t * 2) % 2 == 0 else " "), font=fonts.load(fonts.MONO, 14), fill=GREEN_MID)
    return img


class Compositor:
    """
    Rain intensity map + text layer -> RGB565 pixels with almost no arithmetic: the rain's colour
    comes from lookup tables, and each pixel is one of three kinds (outside the panel / panel
    background / text). Cheap enough to run while the player is starting on a Pi Zero 2 W.
    """

    def __init__(self, hw: Hardware):
        i = np.arange(256, dtype=np.int32)
        tint = (np.maximum(i, 200) - 200) * 2                  # only a streak's leading glyph gets this
        g = (i >> 1) + (i >> 2) + (i >> 3)
        self.lut_out = pack565(tint, g, tint)                  # rain on the bare screen
        k = (256 - PANEL_ALPHA) / 256.0                        # rain seen through the dark panel
        self.lut_panel = pack565(tint * k, g * k + 4, tint * k + 1)
        x0, y0, x1, y1 = hw.safe_rect
        self.box = (slice(y0, y1 + 1), slice(x0, x1 + 1))
        self.text565 = np.zeros((hw.view_h, hw.view_w), np.uint16)
        self.is_text = np.zeros((hw.view_h, hw.view_w), bool)
        self.ready = False

    def set_text(self, fg_rgb: Image.Image) -> None:
        region = np.asarray(fg_rgb)[self.box]                  # only the panel area can hold text
        self.text565[self.box] = pack565(region[..., 0], region[..., 1], region[..., 2])
        self.is_text[self.box] = region.max(axis=2) > 6
        self.ready = True

    def frame565(self, intensity: np.ndarray) -> np.ndarray:
        px = self.lut_out[intensity]
        px[self.box] = self.lut_panel[intensity[self.box]]
        np.copyto(px, self.text565, where=self.is_text)
        return px


# ─────────────────────────────────────────────
#  Checks (real, and scripted for demos)
# ─────────────────────────────────────────────
def mpv_query(sock_path: str, prop: str):
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(0.6)
        s.connect(sock_path)
        s.sendall((json.dumps({"command": ["get_property", prop], "request_id": 1}) + "\n").encode())
        buf = b""
        while b'"request_id"' not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        for line in buf.splitlines():
            msg = json.loads(line)
            if msg.get("request_id") == 1:
                return msg.get("data")
    except Exception:
        pass
    return None


class RealChecks:
    def __init__(self, cfg: Config, hw: Hardware, backlight, fb_size: Tuple[int, int]):
        self.cfg, self.hw, self.bl, self.fb_size = cfg, hw, backlight, fb_size
        self.library = None

    def _library(self):
        if self.library is None:
            lib = Library(self.cfg.videos)
            videos = channels = 0
            for ch in lib.channels():
                n = len(lib.videos(ch))
                videos += n
                channels += 1 if n else 0
            self.library = (videos, channels)
        return self.library

    def poll(self, t: float) -> Dict[str, Tuple[bool, str]]:
        import psutil
        res = {"DISPLAY": (True, "vc4drm %dx%d" % self.fb_size)}
        try:
            touch = self.hw.touch_name in open("/proc/bus/input/devices").read()
        except OSError:
            touch = False
        res["TOUCH"] = (touch, self.hw.touch_name + " capacitive")
        res["AUDIO"] = (os.path.exists("/proc/asound/card0"), "pwm gpio%d" % self.hw.audio_pin)
        if not self.bl.ok:
            self.bl.start(wait=0)
        res["BACKLIGHT"] = (self.bl.ok, "pwm gpio%d" % self.hw.backlight_pin)
        videos, channels = self._library()
        res["LIBRARY"] = (videos > 0, "%d videos / %d channels" % (videos, channels))
        ip = None
        for addr in psutil.net_if_addrs().get("wlan0", []):
            if addr.family == socket.AF_INET:
                ip = addr.address
        res["NETWORK"] = (ip is not None, ip or "waiting")
        res["PLAYER"] = (False, "player loading" if os.path.exists(self.cfg.mpv_socket) else "player starting")
        return res

    def player_ready(self, t: float) -> bool:
        return (mpv_query(self.cfg.mpv_socket, "time-pos") is not None
                and mpv_query(self.cfg.mpv_socket, "core-idle") is False)


class DemoChecks:
    """Scripted timings so the screen can be judged without rebooting."""
    SCHEDULE = [("DISPLAY", 0.0, "vc4drm 480x640"), ("TOUCH", 1.2, "Goodix capacitive"),
                ("AUDIO", 2.0, "pwm gpio19"), ("BACKLIGHT", 2.8, "pwm gpio18"),
                ("LIBRARY", 3.6, "271 videos / 8 channels"), ("NETWORK", 7.5, "192.168.1.124")]

    def __init__(self, length: float):
        self.length = length

    def poll(self, t: float) -> Dict[str, Tuple[bool, str]]:
        res = {key: (t >= at, detail) for key, at, detail in self.SCHEDULE}
        res["PLAYER"] = (False, "player loading" if t > 8 else "player starting")
        return res

    def player_ready(self, t: float) -> bool:
        return t >= self.length


class Screen:
    """Turns check results into lines, a progress value and a status message."""

    def __init__(self):
        self.revealed, self.last_reveal, self.progress, self.player_since = 0, 0.0, 0.0, None

    def update(self, t: float, res: dict, ready: bool):
        n = len(STEPS)
        if self.revealed < n and t - self.last_reveal > 0.30:
            key = STEPS[self.revealed]
            done = res[key][0]
            if key == "PLAYER":
                if self.player_since is None:
                    self.player_since = t
                done = ready
            if done or (key == "NETWORK" and ready):        # never hold the splash up for the network
                self.revealed += 1
                self.last_reveal = t
        if ready:
            self.revealed = n
        lines = []
        for i, key in enumerate(STEPS):
            ok, detail = res[key]
            if i < self.revealed:
                state = "done" if (ok or key == "PLAYER") else "warn"
                if key == "PLAYER":
                    detail = "player online"
                elif state == "warn":
                    detail = "not connected"
                lines.append((key, detail, state))
            elif i == self.revealed:
                lines.append((key, "", "pending"))
        target = self.revealed / n
        if self.revealed == n - 1 and self.player_since is not None:       # creep while the player loads
            target += min(0.9, (t - self.player_since) / 10.0) / n
        if ready:
            target = 1.0
        self.progress += (target - self.progress) * 0.3
        if ready:
            self.progress = 1.0
        if ready:
            status = "SYSTEM ONLINE"
        elif self.revealed < n:
            status = "checking " + STEPS[self.revealed].lower()
        else:
            status = "starting player"
        return lines, self.progress, status


# ─────────────────────────────────────────────
#  Running it
# ─────────────────────────────────────────────
def read_brightness(cfg: Config, hw: Hardware) -> int:
    """Read-only: the boot screen must not create settings (that would hide the first-run wizard)."""
    for path in (cfg.data / "settings.json", expand(cfg.legacy_state_file)):
        try:
            with open(path) as f:
                value = json.load(f).get("brightness", 100)
            return value if value in hw.brightness_levels else 100
        except (OSError, ValueError):
            continue
    return 100


def preview_png(path: str, cfg: Config, hw: Hardware, at: float = 9.0) -> None:
    rain, comp = Rain(hw.view_w, hw.view_h, seed=7), Compositor(hw)
    screen, checks = Screen(), DemoChecks(1e9)
    t = 0.0
    frame565 = None
    for _ in range(int(at * FPS)):
        t += 1.0 / FPS
        lines, prog, status = screen.update(t, checks.poll(t), False)
        comp.set_text(draw_foreground(hw, cfg.name, cfg.boot_greeting, t, lines, prog, status))
        frame565 = comp.frame565(rain.frame())
    Image.fromarray(unpack565(frame565)).save(path)


def run(cfg: Config, hw: Hardware, display, backlight, checks, brightness: int,
        stop: Optional[Callable[[], bool]] = None, fps: int = FPS, timeout: float = TIMEOUT,
        always_poll_player: bool = False) -> str:
    """Draw frames until the player is up. `display.show(px565_landscape)` puts a frame on screen."""
    rain, comp, screen = Rain(hw.view_w, hw.view_h), Compositor(hw), Screen()
    t0 = time.time()
    lit = False
    period = 1.0 / fps
    frame_no, ready, last_poll = 0, False, -1.0
    while not (stop and stop()):
        started = time.time()
        t = started - t0
        res = checks.poll(t)
        if (screen.revealed >= len(STEPS) - 1 or always_poll_player) and t - last_poll >= 0.5:
            last_poll = t
            ready = checks.player_ready(t)
        lines, prog, status = screen.update(t, res, ready)
        if not comp.ready or frame_no % 3 == 0 or ready:        # text/progress change slowly: a few Hz is plenty
            comp.set_text(draw_foreground(hw, cfg.name, cfg.boot_greeting, t, lines, prog, status))
        display.show(comp.frame565(rain.frame()))
        frame_no += 1
        if not lit:
            backlight.set(brightness)                           # only light the panel once there is something to see
            lit = True
            log.info("first frame drawn; backlight %d%%", brightness)
        if ready or t > timeout:
            return "player online" if ready else "timeout"
        time.sleep(max(0.0, period - (time.time() - started)))
    return "stopped"


def run_real(cfg: Config, hw: Hardware, demo: float = 0.0) -> None:
    """What `python -m tubeless boot` does on the Pi."""
    from .hal import pi
    os.nice(10)                                                 # never get in the player's way
    stop_flag: List[int] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop_flag.append(1))
    deadline = time.time() + 60
    while not os.path.exists(pi.FB_DEV) or not os.path.exists(pi.FB_SYS + "/virtual_size"):
        if time.time() > deadline or stop_flag:
            print("no framebuffer; nothing to show")
            return
        time.sleep(0.3)
    fb = pi.Framebuffer(hw)
    print("framebuffer %dx%d %dbpp stride %d" % (fb.w, fb.h, fb.bpp, fb.stride), flush=True)
    backlight = pi.PigpioBacklight(hw)
    backlight.start(wait=0)
    checks = DemoChecks(demo) if demo else RealChecks(cfg, hw, backlight, (fb.w, fb.h))
    reason = run(cfg, hw, fb, backlight, checks, read_brightness(cfg, hw), stop=lambda: bool(stop_flag),
                 always_poll_player=bool(demo))
    print("done:", reason, flush=True)
