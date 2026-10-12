"""
The desktop simulator: the real app running against a pretend Pi and a pretend mpv,
shown in a browser. Mouse = finger. Nothing here is used on the TV itself.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import zlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw

from . import fonts, render
from .bootscreen import unpack565
from .config import ASSETS_DIR
from .hardware import Hardware, apply_affine, invert_affine

log = logging.getLogger(__name__)


class RingLog(logging.Handler):
    """Keeps the last few log lines for the page."""

    def __init__(self, size: int = 80):
        super().__init__()
        self.lines = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.lines.append(self.format(record))


# ─────────────────────────────────────────────
#  What the screen shows
# ─────────────────────────────────────────────
def fake_video_frame(player, hw: Hardware, now: float) -> np.ndarray:
    """A stand-in for mpv's picture: static noise between episodes, a coloured card with the title otherwise."""
    w, h = hw.view_w, hw.view_h
    if not player.alive() and player.get("playlist-pos") is None:
        return np.zeros((h, w, 3), np.uint8)
    path = player.get("path") or ""
    if path in (player.short_static, player.long_static):
        noise = np.random.randint(0, 256, (h, w), dtype=np.uint8)
        return np.repeat(noise[..., None], 3, axis=2)
    seed = zlib.crc32(path.encode())
    base = np.array([60 + seed % 120, 70 + (seed >> 8) % 110, 90 + (seed >> 16) % 100], dtype=np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    stripes = 0.5 + 0.5 * np.sin((xx + yy) / 38.0 + now * 1.3)
    img = (base[None, None, :] * (0.55 + 0.45 * stripes[..., None])).clip(0, 255).astype(np.uint8)
    pil = Image.fromarray(img)
    d = ImageDraw.Draw(pil)
    title = player.current_title().rsplit(".", 1)[0]
    d.text((w / 2, h / 2 - 24), title[:44], font=fonts.load(fonts.SANS_BOLD, 34), fill=(255, 255, 255), anchor="mm")
    t, dur = player.get("time-pos") or 0.0, player.get("duration") or 0.0
    d.text((w / 2, h / 2 + 22), "%d:%02d / %d:%02d" % (t // 60, t % 60, dur // 60, dur % 60),
           font=fonts.load(fonts.MONO, 22), fill=(235, 235, 235), anchor="mm")
    if player.get("pause"):
        d.text((w / 2, h / 2 + 70), "PAUSED", font=fonts.load(fonts.SANS_BOLD, 24), fill=(255, 255, 255), anchor="mm")
    return np.asarray(pil)


def _bezel_mask(hw: Hardware) -> np.ndarray:
    """The 3D-printed shell's rounded opening (measured from a photo of the finished TV)."""
    mask = Image.new("L", (hw.view_w, hw.view_h), 0)
    render._rrect(ImageDraw.Draw(mask), (22, 8, 632, 465), 64, 255)
    return np.asarray(mask, dtype=np.float32)[..., None] / 255.0


class Screen:
    """Composes what you would see on the TV."""

    def __init__(self, hw: Hardware, player=None, backlight=None, display=None, mode_fn=None):
        self.hw, self.player, self.backlight, self.display = hw, player, backlight, display
        self.mode_fn = mode_fn                      # full-screen modes draw to the framebuffer, not over the video
        self._bezel = _bezel_mask(hw)

    def compose(self, bezel: bool = False) -> Image.Image:
        hw = self.hw
        framebuffer_mode = self.mode_fn is not None and self.mode_fn() in ("empty", "transfer", "setup")
        if self.display is not None and (self.player is None or framebuffer_mode):
            with self.display.lock:
                px = self.display.frame565
            base = unpack565(px).astype(np.float32) if px is not None else np.zeros((hw.view_h, hw.view_w, 3), np.float32)
        else:
            base = fake_video_frame(self.player, hw, time.time()).astype(np.float32)
            overlay = self.player.overlay
            if overlay:
                raw, w, h = overlay
                bgra = render.from_panel_bgra(raw, w, h, hw).astype(np.float32)
                alpha = bgra[..., 3:4] / 255.0
                base = base * (1.0 - alpha) + bgra[..., 2::-1]                 # premultiplied BGR -> RGB
        pct = self.backlight.percent if self.backlight is not None else 100
        base = base * (pct / 100.0 * 0.85 + 0.15 if pct else 0.0)
        if bezel:
            base = base * self._bezel
        return Image.fromarray(base.clip(0, 255).astype(np.uint8))


# ─────────────────────────────────────────────
#  Gestures made of touch samples (what the buttons do)
# ─────────────────────────────────────────────
class Finger:
    def __init__(self, touch, hw: Hardware):
        self.touch = touch
        self._to_raw = invert_affine(hw.touch_affine)       # the pretend panel reports what the real Goodix does

    def _feed(self, vx: float, vy: float) -> None:
        rx, ry = apply_affine(self._to_raw, vx, vy)
        self.touch.feed(1, min(1.0, max(0.0, rx)), min(1.0, max(0.0, ry)))

    def down(self, x, y): self._feed(x, y)
    def move(self, x, y): self._feed(x, y)
    def up(self): self.touch.feed(0)

    def tap(self, x=320, y=240):
        self.down(x, y); time.sleep(0.06); self.up()

    def swipe(self, direction: str, duration: float = 0.25, steps: int = 10):
        x0, y0, x1, y1 = {"left": (520, 240, 160, 240), "right": (160, 240, 520, 240),
                          "up": (320, 400, 320, 110), "down": (320, 110, 320, 400)}[direction]
        self.down(x0, y0)
        for i in range(1, steps + 1):
            time.sleep(duration / steps)
            self.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        self.up()

    def hold(self, secs: float, x=320, y=240):
        self.down(x, y); time.sleep(secs); self.up()


# ─────────────────────────────────────────────
#  The web server
# ─────────────────────────────────────────────
class SimServer:
    def __init__(self, hw: Hardware, screen: Screen, touch, state_fn: Callable[[], dict], ring: RingLog,
                 long_press: float, port: int = 8765):
        self.hw, self.screen, self.touch, self.state_fn, self.ring = hw, screen, touch, state_fn, ring
        self.finger = Finger(touch, hw) if touch is not None else None
        self.long_press = long_press
        self.port = port
        self._cache = (0.0, None, None)
        self._lock = threading.Lock()
        self.httpd: Optional[ThreadingHTTPServer] = None

    def frame_png(self, bezel: bool) -> bytes:
        now = time.time()
        with self._lock:
            ts, key, data = self._cache
            if data is not None and key == bezel and now - ts < 0.06:
                return data
            buf = io.BytesIO()
            self.screen.compose(bezel).save(buf, "PNG", compress_level=1)
            self._cache = (now, bezel, buf.getvalue())
            return self._cache[2]

    def serve(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    self._send(200, (ASSETS_DIR / "sim.html").read_bytes(), "text/html; charset=utf-8")
                elif path == "/frame.png":
                    bezel = "bezel=1" in self.path
                    self._send(200, server.frame_png(bezel), "image/png")
                elif path == "/api/state":
                    body = json.dumps({"app": server.state_fn(), "log": list(server.ring.lines)[-60:]})
                    self._send(200, body.encode(), "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    data = {}
                if self.path == "/api/touch" and server.finger:
                    phase = data.get("phase")
                    if phase == "up":
                        server.finger.up()
                    elif phase in ("down", "move"):
                        server.finger._feed(float(data.get("x", 0)), float(data.get("y", 0)))
                elif self.path == "/api/gesture" and server.finger:
                    kind = data.get("kind")
                    if kind == "tap":
                        threading.Thread(target=server.finger.tap, daemon=True).start()
                    elif kind == "swipe":
                        threading.Thread(target=server.finger.swipe, args=(data.get("dir", "left"),), daemon=True).start()
                    elif kind == "hold":
                        threading.Thread(target=server.finger.hold, args=(server.long_press + 0.35,), daemon=True).start()
                self._send(200, b"{}", "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        log.info("simulator at http://127.0.0.1:%d/", self.port)
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
