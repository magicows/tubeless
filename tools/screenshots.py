#!/usr/bin/env python3
"""
Regenerate the pictures in docs/images (used by the README): what the TV shows, and the phone page.

    pip install playwright              (only for the phone pictures; it drives your installed Google Chrome)
    python3 tools/screenshots.py [--tv-only]

Everything is made up: a fake player, fake videos, a pretend yt-dlp, a pretend Wi-Fi. No network, no real TV.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tubeless.app import App                                     # noqa: E402
from tubeless.config import Config                               # noqa: E402
from tubeless.hal import make_backend                            # noqa: E402
from tubeless.hardware import load_hardware                      # noqa: E402
from tubeless.player import FakePlayer                           # noqa: E402
from tubeless.simserver import Finger, Screen                    # noqa: E402
from tubeless.web import Session, UploadManager, WebServer       # noqa: E402
from tubeless.wifi import FakeWifi                               # noqa: E402
from tubeless.youtube import Entry, FakeYtDlp, Listing, YouTubeManager    # noqa: E402
from tubeless.__main__ import make_demo_library                  # noqa: E402

OUT = ROOT / "docs" / "images"


def wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ─────────────────────────────────────────────
#  The TV
# ─────────────────────────────────────────────
def _still(player, hw, now=0.0):
    """A pleasant made-up video frame (sky, sun, hills) instead of the simulator's test pattern."""
    import numpy as np
    h, w = hw.view_h, hw.view_w
    y = np.linspace(0, 1, h)[:, None, None]
    sky = (1 - y) * np.array([255, 170, 90]) + y * np.array([60, 110, 200])
    img = np.broadcast_to(sky, (h, w, 3)).copy()
    xx, yy = np.meshgrid(np.arange(w), np.arange(h))
    img[(xx - 470) ** 2 + (yy - 150) ** 2 < 55 ** 2] = (255, 238, 170)
    for base, amp, col in ((330, 40, (70, 60, 110)), (380, 30, (40, 40, 80))):
        hill = base + amp * np.sin(xx / 90.0 + base)
        img[yy > hill] = col
    return img.astype(np.uint8)


class Tv:
    def __init__(self, work: Path, videos: bool = True, wifi=None, name: str = "Tubeless"):
        self.cfg = Config(name=name, videos_dir=str(work / "videos"), data_dir=str(work / "data"),
                          static_short="/sim/s.mp4", static_long="/sim/l.mp4", legacy_state_file=str(work / "none.json"),
                          web_port=0, setup_port=0, long_press=0.25, menu_timeout=60, setup_auto=False)
        self.hw = load_hardware(self.cfg.hardware, self.cfg.hardware_overrides)
        if videos:
            make_demo_library(self.cfg.videos)
        else:
            self.cfg.videos.mkdir(parents=True, exist_ok=True)
        self.backend = make_backend(self.cfg, self.hw, sim=True)
        self.player = FakePlayer(self.cfg.short_static, self.cfg.long_static, speed=1.0)
        self.app = App(self.cfg, self.hw, self.backend, self.player, wifi=wifi, ytdlp=FakeYtDlp())
        self.app.config_path = work / "config.json"
        from tubeless.state import Store
        Store(self.cfg.data).save("settings.json", {"first_run_done": True, "touch_calibrated": True})
        self.app.settings = type(self.app.settings)(Store(self.cfg.data), self.cfg.legacy_state_file)
        self.app.start_background()
        self.finger = Finger(self.backend.touch, self.hw)
        self.screen = Screen(self.hw, self.player, self.backend.backlight, self.backend.display, mode_fn=lambda: self.app.mode)

    def shot(self, path: Path) -> None:
        if self.app.mode in ("empty", "transfer", "setup"):
            self.app._draw_screen()
        self.screen.compose(bezel=True).save(path)
        print("wrote", path.relative_to(ROOT))

    def stop(self):
        self.app.stop()


def tv_pictures(work: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    from tubeless import app as app_module, simserver
    simserver.fake_video_frame = _still
    app_module.local_ip = lambda: "192.168.1.42"                    # not this computer's real address or name
    app_module.socket.gethostname = lambda: "livingroom"
    tv = Tv(work / "play", name="Living Room")
    time.sleep(1.0)
    tv.finger.hold(0.6)
    wait(lambda: tv.app.menu_open)
    time.sleep(0.4)
    tv.shot(OUT / "tv-menu.png")
    tv.stop()

    tv = Tv(work / "empty", videos=False, wifi=FakeWifi(ip="192.168.1.50", saved=True), name="Living Room")
    wait(lambda: tv.app.mode == "empty")
    time.sleep(0.3)
    tv.shot(OUT / "tv-no-videos.png")
    tv.app.start_transfer()
    wait(lambda: tv.app.mode == "transfer")
    time.sleep(1.2)
    tv.shot(OUT / "tv-upload.png")
    tv.stop()

    tv = Tv(work / "wifi", videos=False, wifi=FakeWifi(ip=None, saved=False), name="Living Room")
    tv.app.start_setup()
    wait(lambda: tv.app.setup and tv.app.setup.get("status") == "Waiting for you to connect...")
    time.sleep(1.2)
    tv.shot(OUT / "tv-wifi-setup.png")
    tv.stop()

    subprocess.run([sys.executable, "-m", "tubeless", "boot", "--demo", "30", "--png", str(OUT / "tv-boot.png"), "--at", "9"],
                   cwd=ROOT, check=True, capture_output=True)
    print("wrote docs/images/tv-boot.png")


# ─────────────────────────────────────────────
#  The phone page
# ─────────────────────────────────────────────
TITLES = ["The Pilot", "Rainy Day", "The Big Game", "Road Trip", "The One With The Cat", "Halloween Special", "Snow Day",
          "The Finale (extended cut)"]


def phone_pictures(work: Path) -> None:
    from playwright.sync_api import sync_playwright
    root = work / "phone" / "videos"
    for ch, n in (("1.cartoons", 6), ("2.movies", 4)):
        (root / ch).mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (root / ch / f"video{i}.mp4").write_bytes(b"x")
    fake = FakeYtDlp({"PLdemodemodemo1": Listing("Saturday morning cartoons", "playlist",
                                                 [Entry("demo%07d" % i, t, 300.0 + 40 * i) for i, t in enumerate(TITLES)])},
                     fail={"demo0000003": "This video isn't available any more."})
    yt = YouTubeManager(root, fake, reserve=0)
    yt.create("3.youtube")
    yt.add("3.youtube", "https://www.youtube.com/playlist?list=PLdemodemodemo1", selected=["demo%07d" % i for i in (0, 1, 2, 3, 5)])
    yt.sync("3.youtube")
    wait(lambda: not yt.busy())
    yt.create("4.weekend")                                              # empty: for the "add a playlist" picture

    class Hooks:
        def state(self):
            return {"name": "Living Room", "channels": [
                {"name": p.name, "label": p.name.split(".", 1)[-1].replace("_", " "),
                 "count": len(list(p.glob("*.mp4")))} for p in sorted(root.iterdir()) if p.is_dir()]}
        def set_name(self, n): return n
        def uploaded(self, i): pass

    web = WebServer(UploadManager(root, reserve=0), Session("4821"), Hooks(), host="127.0.0.1", port=0, youtube=yt)
    port = web.start()
    fake.delay = 0.0

    from tubeless.setupweb import SetupServer
    from tubeless.wifi import Network

    class SetupHooks:
        def name(self): return "Living Room"
        def networks(self, rescan): return [Network("HomeWiFi", 88), Network("Neighbours-5G", 64), Network("CafeGuest", 41, False),
                                            Network("BT-Hub-7421", 33)]
        def connect(self, ssid, pw): pass
    setup = SetupServer(SetupHooks(), host="127.0.0.1", port=0)
    sport = setup.start()

    def save(page, name, full=False):
        path = OUT / name
        page.screenshot(path=str(path), full_page=full)
        print("wrote", path.relative_to(ROOT))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        phone = p.devices["iPhone 13"]
        ctx = browser.new_context(**phone, color_scheme="dark")
        page = ctx.new_page()

        page.goto(f"http://127.0.0.1:{port}/")
        page.fill("#pin", "4821")
        page.wait_for_selector("#channels .chan")
        save(page, "phone-channels.png")

        page.locator("#channels .nm", has_text="youtube").first.click()
        page.wait_for_selector("#ytitems .item")
        time.sleep(0.4)
        page.evaluate("document.getElementById('ytitems').scrollIntoView({block:'start'})")
        save(page, "phone-youtube-channel.png")

        page.click("#ytback")
        page.locator("#channels .nm", has_text="weekend").first.click()
        page.wait_for_selector("#yturl")
        page.fill("#yturl", "https://www.youtube.com/playlist?list=PLdemodemodemo1")
        page.click("#ytfetch")
        page.wait_for_selector("#found:not(.hide)")
        page.evaluate("document.getElementById('found').scrollIntoView({block:'start'})")
        time.sleep(0.3)
        save(page, "phone-youtube-add.png")
        ctx.close()

        ctx = browser.new_context(**phone, color_scheme="dark")
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{sport}/")
        page.wait_for_selector("#nets li")
        save(page, "phone-wifi-setup.png")
        browser.close()
    web.stop()
    setup.stop()


def optimise() -> None:
    from PIL import Image
    for path in OUT.glob("*.png"):
        im = Image.open(path).convert("RGB")
        im.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(path, optimize=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv-only", action="store_true")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="tubeless-shots-"))
    try:
        tv_pictures(work)
        if not args.tv_only:
            phone_pictures(work)
        optimise()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
