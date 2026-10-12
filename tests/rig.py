"""A whole Tubeless running against the pretend Pi and pretend mpv, driven with synthetic touches."""
import random
import time
from pathlib import Path

from tubeless import render
from tubeless.app import App
from tubeless.config import Config
from tubeless.hal import sim
from tubeless.hardware import Hardware, apply_affine, invert_affine
from tubeless.player import FakePlayer
from tubeless.state import Store

HW = Hardware()


def wait_for(cond, timeout=4.0, step=0.01):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


class Rig:
    def __init__(self, tmp_path: Path, channels=None, fresh=False, settings=None, panel_affine=None,
                 duration=lambda p: 1000.0, start=True, wifi=None, **cfg_over):
        videos = tmp_path / "videos"
        for name, files in (channels or {"1.cartoons": [f"c{i}.mp4" for i in range(5)],
                                          "2.nights": [f"n{i}.mp4" for i in range(3)]}).items():
            for f in files:
                p = videos / name / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"")
        kwargs = dict(videos_dir=str(videos), data_dir=str(tmp_path / "data"),
                      static_short="/static_short.mp4", static_long="/static_long.mp4",
                      legacy_state_file=str(tmp_path / "no-legacy.json"),
                      menu_timeout=2.0, long_press=0.25, web_port=0, setup_port=0)
        kwargs.update(cfg_over)
        self.cfg = Config(**kwargs)
        if not fresh:                                      # an existing install (no first-run wizard)
            if settings is not None or not (self.cfg.data / "settings.json").exists():
                Store(self.cfg.data).save("settings.json", dict(settings or {}))
        elif settings:
            raise ValueError("settings need fresh=False")
        self.backend = sim.make(self.cfg, HW)
        self.player = FakePlayer(self.cfg.short_static, self.cfg.long_static, duration_of=duration)
        self.app = App(self.cfg, HW, self.backend, self.player, rng=random.Random(7), tick_every=0.05, wifi=wifi)
        self.app.switch_delay = 0.15
        self.app.connect_delay = 0.05
        self.app.online_wait = 0.3
        self.app.exit_check_every = 0.05
        self.app.min_restart_gap = 0.0
        self.app.progress_save_every = 0.1
        self.app.tracker_every = 0.05
        self.app.config_path = tmp_path / "config.json"
        self.display = self.backend.display
        # what the touch panel *really* reports: by default what the app already assumes
        self.panel_affine = panel_affine or HW.touch_affine
        self._to_raw = invert_affine(self.panel_affine)
        self.power = self.backend.power
        self.backlight = self.backend.backlight
        if start:
            self.app.start_background()

    def stop(self):
        self.app.stop()

    # --- fingers --------------------------------------------------------------------
    def _raw(self, vx, vy):
        return apply_affine(self._to_raw, vx, vy)

    def down(self, vx, vy):
        rx, ry = self._raw(vx, vy)
        self.backend.touch.feed(1, rx, ry)

    def up(self):
        self.backend.touch.feed(0)

    def tap(self, vx=320, vy=240, settle=0.12):
        self.down(vx, vy)
        time.sleep(0.04)
        self.up()
        time.sleep(settle)

    def swipe(self, x0, y0, x1, y1, duration=0.2, steps=8, settle=0.15):
        self.down(x0, y0)
        for i in range(1, steps + 1):
            time.sleep(duration / steps)
            rx, ry = self._raw(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
            self.backend.touch.feed(1, rx, ry)
        self.up()
        time.sleep(settle)

    def swipe_left(self, **kw):  self.swipe(520, 240, 160, 240, **kw)
    def swipe_right(self, **kw): self.swipe(160, 240, 520, 240, **kw)
    def swipe_up(self, **kw):    self.swipe(320, 400, 320, 100, **kw)
    def swipe_down(self, **kw):  self.swipe(320, 100, 320, 400, **kw)

    def hold(self, vx=320, vy=240, secs=0.5, settle=0.25):
        self.down(vx, vy)
        time.sleep(secs)
        self.up()
        time.sleep(settle)

    # --- the menu ---------------------------------------------------------------------
    def row_xy(self, n):
        """Where on the view the n-th visible menu row is."""
        cx, cy = HW.bezel_center
        dy = render.ROWS_Y0 + n * (render.ROW_H + render.ROW_GAP) + render.ROW_H / 2
        return cx + (300 - 320) * HW.bezel_scale, cy + (dy - 240) * HW.bezel_scale

    def tap_row(self, n):
        self.tap(*self.row_xy(n))

    def open_menu(self):
        self.hold()
        assert wait_for(lambda: self.app.menu_open), "menu did not open"

    # --- state -------------------------------------------------------------------------
    @property
    def state(self):
        return self.app.debug_state()


# --- talking to the upload page like a phone would ---------------------------------------------------------
import json as _json
import urllib.error as _urlerror
import urllib.request as _urlrequest


def http(base, method, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (_json.dumps(body).encode() if body is not None else None)
    req = _urlrequest.Request(base + path, data=data, method=method)
    if token:
        req.add_header("X-Token", token)
    try:
        with _urlrequest.urlopen(req, timeout=10) as r:
            ctype = r.headers.get("Content-Type", "")
            payload = r.read()
            return r.status, (_json.loads(payload) if ctype.startswith("application/json") else payload)
    except _urlerror.HTTPError as e:
        return e.code, _json.loads(e.read() or b"{}")
