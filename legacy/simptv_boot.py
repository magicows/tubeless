#!/usr/bin/env python3
"""
SimpTV boot screen: a green-on-black "Matrix" rundown drawn straight onto the
framebuffer while the TV starts up. Every line is a real check (display, touch,
audio, backlight, video library, network, player). It exits as soon as mpv has
put its first frame on screen, and turns the backlight on at the saved
brightness once the first frame is drawn.

    simptv_boot.py                run for real (what simptv-boot.service does)
    simptv_boot.py --demo 30      30 s scripted run, doesn't wait for mpv
    simptv_boot.py --png out.png  render one preview frame to a file (no screen)
"""
import argparse
import json
import mmap
import os
import random
import signal
import socket
import subprocess
import sys
import time

import numpy as np
import psutil
from PIL import Image, ImageDraw, ImageFont

FB_DEV     = "/dev/fb0"
FB_SYS     = "/sys/class/graphics/fb0"
STATE_FILE = os.path.expanduser("~/.simptv_state.json")
MPV_SOCKET = "/tmp/mpvsocket"
VIDEOS_DIR = os.path.expanduser("~/videos")

VIEW_W, VIEW_H = 640, 480        # landscape, as the TV is viewed
ROT_K          = 1               # np.rot90 turns: landscape -> the panel's native portrait (same as the menu overlay)
FPS            = 7
TIMEOUT        = 120             # give up (and leave the screen alone) after this long
BACKLIGHT_PIN  = 18
PWM_HZ         = 800

FONT_MONO   = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
FONT_RAIN   = "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"

GREEN      = (0, 255, 70)
GREEN_MID  = (0, 190, 55)
GREEN_DIM  = (0, 120, 38)
GREEN_DARK = (0, 55, 20)
WHITE_G    = (200, 255, 215)

STEPS = ["DISPLAY", "TOUCH", "AUDIO", "BACKLIGHT", "LIBRARY", "NETWORK", "PLAYER"]
GREETING = "Wake up, Homer..."

# ─────────────────────────────────────────────
#  Digital rain
# ─────────────────────────────────────────────
class Rain:
    COL_W, ROW_H, LEVELS = 14, 18, 6

    def __init__(self, w, h, seed=None):
        self.rng = random.Random(seed)
        self.w, self.h = w, h
        self.ncols = w // self.COL_W
        self.nrows = h // self.ROW_H + 1
        font = ImageFont.truetype(FONT_RAIN, 16)          # half-width katakana (it has no digits)
        font_ascii = ImageFont.truetype(FONT_MONO, 15)

        def glyph(ch):
            im = Image.new("L", (self.COL_W, self.ROW_H), 0)
            f = font_ascii if ord(ch) < 128 else font
            ImageDraw.Draw(im).text((self.COL_W // 2, self.ROW_H // 2), ch, font=f, fill=255, anchor="mm")
            return im
        missing = glyph("\uFFFF").tobytes()          # what the font draws for a character it doesn't have
        chars = [ch for ch in [chr(c) for c in range(0xFF66, 0xFF9E)] + list("0123456789")
                 if glyph(ch).tobytes() != missing]
        self.masks = []                       # masks[char][level] -> uint8 (ROW_H, COL_W)
        for ch in chars:
            base = np.asarray(glyph(ch), dtype=np.uint16)
            self.masks.append([(base * (255 * (lv + 1) // self.LEVELS) >> 8).astype(np.uint8)
                               for lv in range(self.LEVELS)])
        self.head   = [-self.rng.uniform(0, self.nrows * 1.5) for _ in range(self.ncols)]
        self.speed  = [self.rng.uniform(0.35, 1.0) for _ in range(self.ncols)]
        self.length = [self.rng.randint(6, 14) for _ in range(self.ncols)]
        self.cells  = [[self.rng.randrange(len(chars)) for _ in range(self.nrows)] for _ in range(self.ncols)]
        self.nchars = len(chars)

    def frame(self):
        """Return the rain as a (h, w) uint8 intensity map and advance it one step."""
        I = np.zeros((self.h + self.ROW_H, self.w + self.COL_W), np.uint8)
        for c in range(self.ncols):
            head, length = self.head[c], self.length[c]
            x0 = c * self.COL_W
            for i in range(length):
                r = int(head) - i
                if 0 <= r < self.nrows:
                    lvl = self.LEVELS - 1 if i == 0 else 4 - min(4, int(4 * i / length))
                    y0 = r * self.ROW_H
                    region = I[y0:y0 + self.ROW_H, x0:x0 + self.COL_W]
                    np.maximum(region, self.masks[self.cells[c][r]][lvl], out=region)
            self.head[c] += self.speed[c]
            if int(self.head[c]) - length > self.nrows:
                self.head[c]   = -self.rng.uniform(2, 14)
                self.speed[c]  = self.rng.uniform(0.35, 1.0)
                self.length[c] = self.rng.randint(6, 14)
            if self.rng.random() < 0.25:      # flicker one glyph in this column
                self.cells[c][self.rng.randrange(self.nrows)] = self.rng.randrange(self.nchars)
        return I[:self.h, :self.w]                      # (h, w) uint8 intensity; colour comes from a lookup table

# ─────────────────────────────────────────────
#  Foreground (panel + text), blended over the rain
# ─────────────────────────────────────────────
_fonts = {}
def font(path, size):
    if (path, size) not in _fonts:
        _fonts[(path, size)] = ImageFont.truetype(path, size)
    return _fonts[(path, size)]

PANEL = (60, 38, 594, 440)       # centred on the bezel's visible area (see simptv_ui.MENU_CENTER)
X0, X1 = 84, 570                 # text columns

def draw_foreground(t, lines, progress, status):
    """
    lines: list of (label, detail, state) with state in 'done'|'pending'|'warn'.
    Returns an RGB PIL image the size of the view: text and lines on black. The dark panel
    behind it is applied by Compositor, not drawn here.
    """
    img = Image.new("RGB", (VIEW_W, VIEW_H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle(PANEL, outline=GREEN_DARK, width=1)
    x0, y0, x1, y1 = PANEL                                  # bright corner brackets
    for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        d.line([(cx, cy), (cx + 22 * sx, cy)], fill=GREEN, width=2)
        d.line([(cx, cy), (cx, cy + 22 * sy)], fill=GREEN, width=2)

    # header with a little glow
    hf = font(FONT_MONO_B, 36)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.text((X0 + dx, 56 + dy), "SIMPTV", font=hf, fill=GREEN_DIM)
    d.text((X0, 56), "SIMPTV", font=hf, fill=GREEN)
    d.text((X1, 74), "// BOOT SEQUENCE", font=font(FONT_MONO, 14), fill=GREEN_DIM, anchor="rs")
    d.line([(X0, 106), (X1, 106)], fill=GREEN_DARK, width=1)

    # typed greeting with a blinking cursor
    typed = GREETING[:int(t * 12)]
    cursor = "_" if int(t * 2) % 2 == 0 else " "
    d.text((X0, 114), "> " + typed + cursor, font=font(FONT_MONO, 16), fill=WHITE_G)

    # check lines
    lf, df = font(FONT_MONO_B, 17), font(FONT_MONO, 15)
    y = 146
    spinner = "|/-\\"[int(t * 8) % 4]
    for label, detail, state in lines:
        if state == "pending":
            d.text((X0, y), spinner + " " + label, font=lf, fill=GREEN_MID)
            d.text((X1, y), "...", font=lf, fill=GREEN_DIM, anchor="ra")
        else:
            d.text((X0, y), "> " + label, font=lf, fill=GREEN)
            if detail:
                d.text((260, y + 2), detail, font=df, fill=GREEN_MID)
            word, col = ("OK", WHITE_G) if state == "done" else ("--", GREEN_DIM)
            d.text((X1, y), word, font=lf, fill=col, anchor="ra")
        y += 28

    # progress bar
    by0, by1 = 372, 388
    d.rectangle((X0, by0, X1, by1), outline=GREEN_DIM, width=1)
    fill_w = int((X1 - X0 - 4) * max(0.0, min(1.0, progress)))
    if fill_w > 0:
        d.rectangle((X0 + 2, by0 + 2, X0 + 2 + fill_w, by1 - 2), fill=GREEN)
    d.text((X1, by1 + 6), "%3d%%" % int(progress * 100), font=font(FONT_MONO, 14), fill=GREEN_MID, anchor="ra")
    d.text((X0, by1 + 6), "> " + status + ("_" if int(t * 2) % 2 == 0 else " "), font=font(FONT_MONO, 14),
           fill=GREEN_MID)
    return img

def pack565(r, g, b):
    r, g, b = (np.asarray(v).astype(np.uint16) for v in (r, g, b))
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

PANEL_ALPHA = 196

class Compositor:
    """
    Turns a rain intensity map + the text layer into RGB565 pixels with almost no
    arithmetic: rain colour comes from lookup tables, and each pixel is one of
    three kinds (outside the panel / panel background / text).
    """
    def __init__(self):
        i = np.arange(256, dtype=np.int32)
        tint = (np.maximum(i, 200) - 200) * 2                  # only a streak's leading glyph gets this
        g = (i >> 1) + (i >> 2) + (i >> 3)
        self.lut_out = pack565(tint, g, tint)                  # rain on the bare screen
        k = (256 - PANEL_ALPHA) / 256.0                        # rain seen through the dark panel
        self.lut_panel = pack565(tint * k, g * k + 4, tint * k + 1)
        x0, y0, x1, y1 = PANEL
        self.box = (slice(y0, y1 + 1), slice(x0, x1 + 1))
        self.text565 = np.zeros((VIEW_H, VIEW_W), np.uint16)
        self.is_text = np.zeros((VIEW_H, VIEW_W), bool)
        self.ready = False

    def set_text(self, fg_rgb):
        region = np.asarray(fg_rgb)[self.box]                  # only the panel area can hold text
        self.text565[self.box] = pack565(region[..., 0], region[..., 1], region[..., 2])
        self.is_text[self.box] = region.max(axis=2) > 6
        self.ready = True

    def frame565(self, intensity):
        px = self.lut_out[intensity]
        px[self.box] = self.lut_panel[intensity[self.box]]
        np.copyto(px, self.text565, where=self.is_text)
        return px

def unpack565(px):
    r, g, b = ((px >> 11) & 31) << 3, ((px >> 5) & 63) << 2, (px & 31) << 3
    return np.stack([r, g, b], axis=-1).astype(np.uint8)

# ─────────────────────────────────────────────
#  Framebuffer + backlight
# ─────────────────────────────────────────────
class Framebuffer:
    def __init__(self):
        w, h = (int(v) for v in open(FB_SYS + "/virtual_size").read().strip().split(","))
        self.w, self.h = w, h
        self.bpp = int(open(FB_SYS + "/bits_per_pixel").read())
        self.stride = int(open(FB_SYS + "/stride").read())
        self.f = open(FB_DEV, "r+b")
        self.mm = mmap.mmap(self.f.fileno(), self.stride * h)

    def show(self, px565_landscape):
        """px565_landscape: (480, 640) uint16 RGB565 in the landscape orientation the TV is viewed in."""
        if self.bpp != 16:
            raise RuntimeError(f"unsupported framebuffer depth {self.bpp} (expected 16)")
        px = np.ascontiguousarray(np.rot90(px565_landscape, ROT_K)).astype("<u2")
        rows = px.view(np.uint8).reshape(self.h, -1)
        assert rows.shape[0] == self.h
        if rows.shape[1] != self.stride:
            padded = np.zeros((self.h, self.stride), np.uint8)
            padded[:, :rows.shape[1]] = rows
            rows = padded
        self.mm[:self.stride * self.h] = rows.tobytes()

class Backlight:
    """Same pigpio software-PWM as the app (both hardware PWM channels belong to the audio)."""
    def __init__(self):
        self.pi = None
        self.connect()

    def connect(self):
        try:
            import pigpio
            pi = pigpio.pi()
            if pi.connected:
                pi.set_mode(BACKLIGHT_PIN, pigpio.OUTPUT)
                pi.set_PWM_frequency(BACKLIGHT_PIN, PWM_HZ)
                self.pi = pi
        except Exception:
            pass

    @property
    def ok(self):
        return self.pi is not None

    def set(self, percent):
        percent = max(0, min(100, percent))
        if self.pi is None:
            self.connect()
        if self.pi is not None:
            self.pi.set_PWM_dutycycle(BACKLIGHT_PIN, round(percent * 255 / 100))
        else:
            subprocess.run(["raspi-gpio", "set", str(BACKLIGHT_PIN), "op", "dh" if percent else "dl"], check=False)

def saved_brightness():
    try:
        value = json.load(open(STATE_FILE)).get("brightness", 100)
        return value if value in (25, 50, 75, 100) else 100
    except Exception:
        return 100

# ─────────────────────────────────────────────
#  Checks
# ─────────────────────────────────────────────
def mpv_query(prop):
    try:
        s = socket.socket(socket.AF_UNIX); s.settimeout(0.6); s.connect(MPV_SOCKET)
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
    def __init__(self, fb, backlight):
        self.fb, self.bl = fb, backlight
        self.library = None

    def _library(self):
        if self.library is None:
            videos, channels = 0, 0
            try:
                for entry in os.scandir(VIDEOS_DIR):
                    if entry.is_dir():
                        n = sum(1 for _, _, files in os.walk(entry.path) for f in files if f.lower().endswith(".mp4"))
                        videos += n
                        channels += 1 if n else 0
            except OSError:
                pass
            self.library = (videos, channels)
        return self.library

    def poll(self, t):
        res = {}
        res["DISPLAY"]   = (True, "vc4drm %dx%d" % (self.fb.w, self.fb.h))
        try:
            touch = "Goodix" in open("/proc/bus/input/devices").read()
        except OSError:
            touch = False
        res["TOUCH"]     = (touch, "Goodix capacitive")
        res["AUDIO"]     = (os.path.exists("/proc/asound/card0"), "pwm gpio19")
        if not self.bl.ok:
            self.bl.connect()
        res["BACKLIGHT"] = (self.bl.ok, "pwm gpio18")
        videos, channels = self._library()
        res["LIBRARY"]   = (videos > 0, "%d videos / %d channels" % (videos, channels))
        ip = None
        for addr in psutil.net_if_addrs().get("wlan0", []):
            if addr.family == socket.AF_INET:
                ip = addr.address
        res["NETWORK"]   = (ip is not None, ip or "waiting")
        started = os.path.exists(MPV_SOCKET)
        res["PLAYER"]    = (False, "mpv loading" if started else "mpv starting")
        return res

    def player_ready(self, t):
        return mpv_query("time-pos") is not None and mpv_query("core-idle") is False

class DemoChecks:
    """Scripted timings so the screen can be judged without rebooting."""
    SCHEDULE = {"DISPLAY": 0.0, "TOUCH": 1.2, "AUDIO": 2.0, "BACKLIGHT": 2.8, "LIBRARY": 3.6, "NETWORK": 7.5}
    def __init__(self, length): self.length = length
    def poll(self, t):
        res = {k: (t >= at, d) for (k, at), d in zip(self.SCHEDULE.items(), [
            "vc4drm 480x640", "Goodix capacitive", "pwm gpio19", "pwm gpio18", "271 videos / 8 channels", "192.168.1.124"])}
        res["PLAYER"] = (False, "mpv loading" if t > 8 else "mpv starting")
        return res
    def player_ready(self, t): return t >= self.length

# ─────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────
class Screen:
    """Turns check results into lines, a progress value and a status message."""
    def __init__(self):
        self.revealed, self.last_reveal, self.progress, self.player_since = 0, 0.0, 0.0, None

    def update(self, t, res, ready):
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
                    detail = "mpv online"
                elif state == "warn":
                    detail = "not connected"
                lines.append((key, detail, state))
            elif i == self.revealed:
                lines.append((key, "", "pending"))
        target = self.revealed / n
        if self.revealed == n - 1 and self.player_since is not None:       # creep while mpv loads
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

def run(args):
    if args.png:
        rain, comp = Rain(VIEW_W, VIEW_H, seed=7), Compositor()
        screen, checks = Screen(), DemoChecks(1e9)
        t = 0.0
        for _ in range(int(args.at * FPS)):
            t += 1.0 / FPS
            lines, prog, status = screen.update(t, checks.poll(t), False)
            comp.set_text(draw_foreground(t, lines, prog, status))
            frame565 = comp.frame565(rain.frame())
        Image.fromarray(unpack565(frame565)).save(args.png)
        print("preview written:", args.png)
        return

    os.nice(10)                                   # never get in mpv's way
    stop = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.append(1))

    deadline = time.time() + 60
    while not os.path.exists(FB_DEV) or not os.path.exists(FB_SYS + "/virtual_size"):
        if time.time() > deadline or stop:
            print("no framebuffer; nothing to show")
            return
        time.sleep(0.3)
    fb = Framebuffer()
    print("framebuffer %dx%d %dbpp stride %d" % (fb.w, fb.h, fb.bpp, fb.stride))
    backlight = Backlight()
    brightness = saved_brightness()

    rain, comp = Rain(VIEW_W, VIEW_H), Compositor()
    checks = DemoChecks(args.demo) if args.demo else RealChecks(fb, backlight)
    screen = Screen()
    t0 = time.time()
    lit = False
    period = 1.0 / FPS
    frame_no, ready, last_poll = 0, False, -1.0
    while not stop:
        started = time.time()
        t = started - t0
        res = checks.poll(t)
        if (screen.revealed >= len(STEPS) - 1 or args.demo) and t - last_poll >= 0.5:
            last_poll = t
            ready = checks.player_ready(t)
        lines, prog, status = screen.update(t, res, ready)
        if not comp.ready or frame_no % 3 == 0 or ready:    # text/progress change slowly: a few Hz is plenty
            comp.set_text(draw_foreground(t, lines, prog, status))
        fb.show(comp.frame565(rain.frame()))
        frame_no += 1
        if not lit:
            backlight.set(brightness)             # only light the panel once there is something to see
            lit = True
            print("first frame drawn; backlight %d%%" % brightness)
        if ready or t > TIMEOUT:
            print("done after %.1fs (%s)" % (t, "player online" if ready else "timeout"))
            return
        time.sleep(max(0.0, period - (time.time() - started)))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", type=float, default=0, help="scripted run of this many seconds")
    ap.add_argument("--png", help="render one preview frame to this file instead of the screen")
    ap.add_argument("--at", type=float, default=9.0, help="with --png: how many seconds into the sequence")
    run(ap.parse_args())
