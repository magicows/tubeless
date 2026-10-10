"""
    python -m tubeless run                 run on the TV (what the service does)
    python -m tubeless sim                 the desktop simulator, in your browser
    python -m tubeless boot [--demo N]     the boot screen (on the TV), or --png out.png for a preview
    python -m tubeless version
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import Config, expand
from .hardware import load_hardware


def setup_logging(cfg: Config, to_file: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    stderr = logging.StreamHandler()
    stderr.setFormatter(fmt)
    root.addHandler(stderr)
    if to_file:
        try:
            path = expand(cfg.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(path, maxBytes=3 * 1024 * 1024, backupCount=3)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            root.warning("no log file (%s)", e)


# ─────────────────────────────────────────────
def cmd_run(args) -> int:
    from .app import App
    from .hal import make_backend
    from .player import MpvPlayer

    cfg = Config.load(args.config)
    setup_logging(cfg)
    hw = load_hardware(cfg.hardware, cfg.hardware_overrides)
    log = logging.getLogger("tubeless")
    log.info("Tubeless %s starting as %r", __version__, cfg.name)
    app = App(cfg, hw, make_backend(cfg, hw), MpvPlayer(cfg, hw))
    signal.signal(signal.SIGTERM, lambda *_: (app.stop(), sys.exit(0)))
    try:
        app.run()
    except Exception:
        log.critical("critical error", exc_info=True)
        return 1                                    # non-zero so systemd restarts everything
    return 0


# ─────────────────────────────────────────────
DEMO_LIBRARY = {
    "1.cartoons": ["S01E01 The Pilot", "S01E02 Rainy Day", "S01E03 The Big Game", "S02E01 Road Trip",
                   "S02E02 The One With The Cat", "S02E03 Halloween Special", "S03E01 Snow Day", "S03E02 Finale"],
    "2.movies": ["Space Cows (1987)", "The Long Weekend (1994)", "Robot Picnic (2002)", "Midnight Diner (1999)"],
    "3.nights": ["Late Show 001", "Late Show 002", "Late Show 003", "Late Show 004", "Late Show 005"],
    "4.documentaries": ["How Toasters Work", "The Secret Life Of Moss", "Trains Of The World"],
}


def make_demo_library(root: Path) -> None:
    for channel, titles in DEMO_LIBRARY.items():
        for title in titles:
            f = root / channel / f"{title}.mp4"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"")


def cmd_sim(args) -> int:
    from .app import App
    from .hal import make_backend
    from .player import FakePlayer
    from .simserver import RingLog, Screen, SimServer
    from . import bootscreen

    work = Path(args.dir).resolve()
    if args.fresh and work.exists():
        shutil.rmtree(work)
    cfg = Config(name=args.name, videos_dir=str(work / "videos"), data_dir=str(work / "data"),
                 static_short="/sim/static-short.mp4", static_long="/sim/static-long.mp4",
                 legacy_state_file=str(work / "no-legacy.json"), menu_timeout=args.menu_timeout,
                 boot_greeting=args.greeting)
    hw = load_hardware(cfg.hardware, cfg.hardware_overrides)
    if not cfg.videos.exists():
        make_demo_library(cfg.videos)
    ring = RingLog()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger().addHandler(ring)
    backend = make_backend(cfg, hw, sim=True)
    port = args.port

    if args.screen == "boot":
        screen = Screen(hw, player=None, backlight=backend.backlight, display=backend.display)

        def loop():
            while True:
                bootscreen.run(cfg, hw, backend.display, backend.backlight, bootscreen.DemoChecks(args.boot_seconds),
                               brightness=100, always_poll_player=True)
                time.sleep(2.0)
        threading.Thread(target=loop, daemon=True).start()
        server = SimServer(hw, screen, None, lambda: {"name": cfg.name, "screen": "boot (looping demo)"}, ring,
                           cfg.long_press, port)
    else:
        player = FakePlayer(cfg.short_static, cfg.long_static, speed=args.speed)
        app = App(cfg, hw, backend, player)
        app.switch_delay = 3.0 / max(args.speed, 1)
        app.start_background()
        screen = Screen(hw, player, backend.backlight)
        server = SimServer(hw, screen, backend.touch, app.debug_state, ring, cfg.long_press, port)
    print(f"Tubeless simulator: http://127.0.0.1:{port}/   (Ctrl-C to stop)")
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    return 0


# ─────────────────────────────────────────────
def cmd_boot(args) -> int:
    from . import bootscreen
    cfg = Config.load(args.config)
    hw = load_hardware(cfg.hardware, cfg.hardware_overrides)
    if args.png:
        bootscreen.preview_png(args.png, cfg, hw, args.at)
        print("preview written:", args.png)
        return 0
    logging.basicConfig(level=logging.INFO)
    bootscreen.run_real(cfg, hw, demo=args.demo)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tubeless", description="A tiny touchscreen TV for the Raspberry Pi Zero 2 W.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run on the TV")
    p.add_argument("--config")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("sim", help="desktop simulator in your browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--dir", default="sim_data", help="where the simulator keeps its fake library and settings")
    p.add_argument("--fresh", action="store_true", help="start from scratch (shows the first-run calibration)")
    p.add_argument("--name", default="Tubeless")
    p.add_argument("--screen", choices=["tv", "boot"], default="tv")
    p.add_argument("--speed", type=float, default=1.0, help="make fake time run faster")
    p.add_argument("--menu-timeout", type=float, default=5.0)
    p.add_argument("--greeting", default="Wake up, couch potato...")
    p.add_argument("--boot-seconds", type=float, default=25.0)
    p.set_defaults(func=cmd_sim)

    p = sub.add_parser("boot", help="the boot screen")
    p.add_argument("--config")
    p.add_argument("--demo", type=float, default=0.0, help="a scripted run of this many seconds")
    p.add_argument("--png", help="render a preview frame to this file instead of the screen")
    p.add_argument("--at", type=float, default=9.0)
    p.set_defaults(func=cmd_boot)

    p = sub.add_parser("version")
    p.set_defaults(func=lambda a: print(__version__) or 0)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
