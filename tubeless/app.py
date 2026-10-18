"""
The application: ties touch, the menu, the player, the library and the settings together.

Threads:
  * the touch loop (main thread, or a thread in tests) reads touch samples and turns them into gestures
  * one worker thread performs every gesture, so menu/playback state is only touched from there
  * a monitor thread restarts the player if it exits (playlist finished) or stops answering
  * a tracker thread saves how far through an episode you are, and does the resume seek
"""
from __future__ import annotations

import logging
import os
import queue
import random
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from . import qr as qrcodes
from . import render
from .config import Config, expand, set_config_value, user_config_path
from .gestures import GestureRecognizer
from .hardware import Hardware, apply_affine, solve_affine
from .libindex import LibraryIndex
from .library import Library, Plan, Playlists
from .menu import AUTO_DIM_STEPS, SLEEP_TIMER_STEPS, MenuContext, MenuModel, next_step
from .pixels import image_to_565
from .skip import skip_target
from .state import Settings, Store
from .updater import UpdateError, Updater
from .setupweb import SetupServer
from .web import Session, UploadManager, WebServer, local_ip
from .youtube import YouTubeManager, YtDlp, find_ytdlp
from .wifi import HOTSPOT_IP, Wifi, new_hotspot_credentials, wifi_qr_text

log = logging.getLogger(__name__)

HEARTBEAT_EVERY = 60          # seconds between "is the player still answering?" checks
EXIT_CHECK_EVERY = 2          # seconds between "has the player exited?" checks
MIN_RESTART_GAP = 5           # so a crash-looping player can't spin the CPU
PROGRESS_SAVE_EVERY = 30      # seconds between saving your position in an episode
RESUME_REWIND = 3             # seconds to rewind when resuming
CALIBRATION_TIMEOUT = 90      # seconds of inactivity before the wizard gives up
CALIBRATION_MAX_ERROR = 45    # px; a worse fit than this is rejected
DIM_LEVEL = 25                # percent


class App:
    def __init__(self, cfg: Config, hw: Hardware, backend, player, library: Optional[Library] = None,
                 store: Optional[Store] = None, rng: Optional[random.Random] = None, tick_every: float = 5.0,
                 wifi: Optional[Wifi] = None, ytdlp=None):
        self.cfg, self.hw = cfg, hw
        self.touch, self.backlight, self.power = backend.touch, backend.backlight, backend.power
        self.audio = getattr(backend, "audio", None)
        self.display = getattr(backend, "display", None)
        self.player = player
        self.store = store or Store(cfg.data)
        self.settings = Settings(self.store, expand(cfg.legacy_state_file))
        self.library = library or Library(cfg.videos)
        self.playlists = Playlists(self.library, self.store, cfg.short_static, rng=rng)
        self.rec = GestureRecognizer(cfg.long_press, cfg.touch_move_px, cfg.swipe_min_px)
        self.name = cfg.name
        self.tick_every = tick_every
        self.switch_delay = 3.0               # how long the channel-change static plays
        self.exit_check_every = EXIT_CHECK_EVERY
        self.min_restart_gap = MIN_RESTART_GAP
        self.progress_save_every = PROGRESS_SAVE_EVERY
        self.tracker_every = 2.0

        # remembered settings
        self.shuffle = bool(self.settings.get("shuffle", True))
        b = self.settings.get("brightness", 100)
        self.brightness = b if b in hw.brightness_levels else hw.brightness_levels[-1]
        self.auto_dim_min = int(self.settings.get("auto_dim_min", 0))
        aff = self.settings.get("touch_affine")
        self.affine = tuple(tuple(r) for r in aff) if aff else hw.touch_affine
        self.channel_index = 0

        # runtime state (the gesture worker owns most of it)
        self.menu = MenuModel()
        self._menu_lock = threading.Lock()
        self._menu_open = False
        self.menu_deadline = 0.0
        self.mode = "play"                    # play | calibrate | standby | shutdown
        self.plan: Optional[Plan] = None
        self.sleep_timer_min = 0
        self.sleep_deadline: Optional[float] = None
        self.dimmed = False
        self.confirm_shutdown_until = 0.0
        self.last_activity = time.time()
        self.last_gesture: Optional[tuple] = None
        self._calib: Optional[dict] = None
        self._pending_resume = None           # (absolute path, seconds, relative path)
        self._cur_rel: Optional[str] = None
        self._last_save = 0.0
        self._last_pos = (None, None)
        self._standby_paused = False
        self._switch_timer: Optional[threading.Timer] = None
        self._waking = False
        self._overlay_pending = None          # an overlay the player wasn't ready to show yet (retried by the tracker)
        self.config_path = user_config_path()  # where the web page's rename is saved
        self.web: Optional[WebServer] = None
        self.uploads: Optional[UploadManager] = None
        self.web_info: Optional[dict] = None   # {"pin": ..., "urls": [...]} while upload mode is on
        self._stream: Optional[dict] = None    # the YouTube video that is loading / playing, if any
        self._stream_notice_until = 0.0
        self.stream_notice_after = 2.0         # seconds of loading before the 'loading from YouTube' message appears
        self.stream_notice_for = 7.0
        self._ytdlp = ytdlp                    # tests pass a FakeYtDlp; normally found when the upload page starts
        self.youtube: Optional[YouTubeManager] = None
        self.wifi = wifi                       # None, or something that can make a hotspot and join networks
        self.setup: Optional[dict] = None      # while Wi-Fi setup is on: ssid, password, address, status, ...
        self._setup_server: Optional[SetupServer] = None
        self._setup_lock = threading.Lock()
        self._setup_gen = 0                    # bumped on every start/stop so a stale background step can tell
        self.connect_delay = 1.5               # lets the page's answer reach the phone before the hotspot drops
        self.index = LibraryIndex(self.library, self.store)
        self.updater = Updater(Path(__file__).resolve().parent.parent)
        self._hold_player = False              # true while start_transfer/start_setup stop the player on purpose
        self.fast_death_seconds = 15.0         # a player that dies sooner than this after starting is failing, not finishing
        self.restart_backoff = 60.0            # after 3 such deaths in a row, wait this long between restarts
        self._fast_restarts = 0
        self._last_restart = 0.0
        self.saved_wifi_wait = 20.0            # how long Upload waits for a saved network that hasn't connected yet (just after boot)
        self.online_wait = 45.0                # how long after boot we wait for Wi-Fi before offering setup
        self._notice, self._notice_until = "", 0.0
        self._screen_dirty, self._last_screen = True, 0.0
        self._posted_deadline: Optional[float] = None
        self._next_tick = 0.0
        self.m3u_path = str(self.store.path("playlist.m3u"))

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self._threads = []

    # ─────────────────────────────────────────
    #  Small helpers
    # ─────────────────────────────────────────
    @property
    def menu_open(self) -> bool:
        with self._menu_lock:
            return self._menu_open

    def _set_menu_open(self, value: bool) -> None:
        with self._menu_lock:
            self._menu_open = value

    def cmd(self, *args):
        """Send the player a command; never raises (so worker/timer threads can't die on a flaky player)."""
        try:
            return self.player.command(*args)
        except Exception as e:
            log.warning("player command %s failed: %s", args[:1], e)
            return None

    def _set_overlay(self, image) -> None:
        raw, w, h = render.to_panel_bgra(image, self.hw)
        try:
            self.player.set_overlay(raw, w, h)
            self._overlay_pending = None
        except Exception as e:
            # Typically right after start-up: the player isn't accepting commands yet. Keep it and retry.
            self._overlay_pending = image
            log.info("overlay not shown yet (%s); will retry", e)

    def _retry_overlay(self) -> None:
        image = self._overlay_pending
        if image is not None:
            self._set_overlay(image)

    def _clear_overlay(self) -> None:
        self._overlay_pending = None
        try:
            self.player.clear_overlay()
        except Exception as e:
            log.warning("could not clear overlay: %s", e)

    def channels(self):
        return self.library.channels()

    def current_channel(self):
        channels = self.channels()
        if not channels:
            raise RuntimeError("No channels: put some folders of videos in " + str(self.library.root))
        self.channel_index = max(0, min(self.channel_index, len(channels) - 1))
        return channels[self.channel_index]

    # ─────────────────────────────────────────
    #  Start / stop
    # ─────────────────────────────────────────
    def startup(self) -> None:
        if self.audio is not None:
            self.audio.start()
        self.backlight.start()
        self.backlight.set(self.brightness)
        channels = self.channels()
        saved = self.settings.get("channel")
        names = [c.name for c in channels]
        if saved in names:
            self.channel_index = names.index(saved)
        idx = self.library.next_playable(self.channel_index, +1)
        if idx is None:
            log.warning("no videos found in %s: showing the 'no videos yet' screen", self.library.root)
            self.enter_empty()
            if self.settings.fresh and not self.settings.get("first_run_done"):
                self.begin_calibration(first_run=True)        # a brand new card has no videos yet, and still starts by calibrating
            return
        if idx != self.channel_index:
            log.warning("channel %s has no videos; starting on %s instead", names[self.channel_index], names[idx])
            self.channel_index = idx
        self.start_playback()
        if self.settings.fresh and not self.settings.get("first_run_done"):
            self.begin_calibration(first_run=True)            # a brand new install starts by calibrating touch

    def start_threads(self) -> None:
        for target in (self._worker_loop, self._monitor_loop, self._tracker_loop, self._watch_for_network):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def run(self) -> None:
        """Start everything and run the touch loop on this thread (what the service does)."""
        self.startup()
        self.start_threads()
        self.touch_loop()

    def start_background(self) -> None:
        """Start everything, with the touch loop on its own thread (tests and the simulator)."""
        self.startup()
        self.start_threads()
        t = threading.Thread(target=self.touch_loop, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self.stop_event.set()
        if self._switch_timer:
            self._switch_timer.cancel()
        try:
            self.player.stop()
        except Exception:
            pass

    # ─────────────────────────────────────────
    #  Playback
    # ─────────────────────────────────────────
    def start_playback(self) -> None:
        """(Re)build the current channel's playlist and start the player on it."""
        plan = self.playlists.plan(self.current_channel(), self.shuffle)
        self.plan = plan
        self._cur_rel = None
        path = self.playlists.write_m3u(plan, self.m3u_path)
        self.player.start(path)
        self._set_resume(plan)

    def _set_resume(self, plan: Plan) -> None:
        prog = self.playlists.progress(plan.channel)
        pos = float(prog.get("pos", 0)) if prog.get("current") == plan.current else 0.0
        if pos > RESUME_REWIND + 2:
            self._pending_resume = (self.library.abspath(plan.channel, plan.current), pos - RESUME_REWIND, plan.current)
        else:
            self._pending_resume = None

    def reload_channel(self) -> None:
        """Rebuild the current channel's playlist and load it into the running player."""
        self.save_progress_now()
        plan = self.playlists.plan(self.current_channel(), self.shuffle)
        self.plan = plan
        self._cur_rel = None
        path = self.playlists.write_m3u(plan, self.m3u_path)
        self.cmd("loadfile", path, "replace")
        self.cmd("set_property", "pause", False)
        self._set_resume(plan)

    def _show_standby(self) -> None:
        """Put the 'technical difficulties' picture on the display for the moment the player is down (it would show the boot screen)."""
        if self.display is None:
            return
        try:
            self.display.show(image_to_565(render.render_standby(self.hw, self.cfg.standby_image)))
        except Exception as e:
            log.warning("could not show the standby picture: %s", e)

    def _show_trouble(self) -> None:
        """What the screen says while the player keeps failing and the TV waits before trying again."""
        if self.display is None:
            return
        try:
            img = render.render_message(self.hw, "CAN'T KEEP PLAYING", ["Something in this channel won't play.", "Trying again in a minute.",
                                                                        "Swipe to change channel."], self.name)
            self.display.show(image_to_565(img))
        except Exception as e:
            log.warning("could not show the trouble screen: %s", e)

    def finish_cycle(self) -> None:
        """You have been through everything: reshuffle and start a new cycle."""
        plan = self.playlists.new_cycle(self.current_channel(), self.shuffle)
        self.plan = plan
        self._cur_rel = None
        self._pending_resume = None
        path = self.playlists.write_m3u(plan, self.m3u_path)
        self.player.start(path)

    def save_progress_now(self) -> None:
        if self.plan is None or not self.player.alive():
            return
        pos = self.player.get("playlist-pos")
        rel = self.playlists.episode_at(self.plan, pos)
        if rel is None or self._pending_resume:
            return
        t = self.player.get("time-pos") or 0.0
        self.playlists.set_progress(self.plan.channel, rel, t)
        self._cur_rel, self._last_save = rel, time.time()

    def toggle_pause(self) -> None:
        self.cmd("cycle", "pause")
        if self.player.get("pause"):
            self.save_progress_now()

    def skip_video(self, direction: int) -> None:
        target = skip_target(self.player.get("playlist-pos"), self.player.get("playlist-count"), direction)
        if target is None:
            return
        if target[0] == "end":
            self.finish_cycle()
            return
        self.cmd("set_property", "playlist-pos", target[1])
        self.cmd("set_property", "pause", False)

    def switch_channel(self, index: int, step: Optional[int] = None) -> None:
        """
        Change channel. With `step` (+1/-1, from a swipe) empty channels are skipped in that direction;
        without it (a menu tap) the exact channel is used, or nothing happens if it is empty.
        """
        channels = self.channels()
        if not channels:
            return
        if step is not None:
            target = self.library.next_playable(index, step)
        else:
            target = index % len(channels)
            if not self.library.has_videos(channels[target]):
                target = None
        if target is None:
            log.warning("channel change ignored: no videos in that channel")
            return
        self.save_progress_now()
        self.channel_index = target
        channel = channels[target]
        self.settings.set(channel=channel.name)
        log.info("switching to channel %s", channel.name)
        plan = self.playlists.plan(channel, self.shuffle)
        self.plan = plan
        self._cur_rel = None
        self._pending_resume = None
        self.playlists.write_m3u(plan, self.m3u_path)
        self.cmd("loadfile", self.cfg.long_static, "replace")
        self.cmd("set_property", "pause", False)
        # A newer channel change replaces any pending one, so rapid swipes don't pile up.
        if self._switch_timer is not None:
            self._switch_timer.cancel()
        self._switch_timer = threading.Timer(self.switch_delay, self._finish_switch, args=(plan,))
        self._switch_timer.daemon = True
        self._switch_timer.start()

    def _finish_switch(self, plan: Plan) -> None:
        if plan is not self.plan or self.stop_event.is_set():
            return
        self.cmd("loadfile", self.m3u_path, "replace")
        self.cmd("set_property", "pause", False)
        self._set_resume(plan)

    # ─────────────────────────────────────────
    #  Brightness, sleep, dim
    # ─────────────────────────────────────────
    def cycle_brightness(self) -> None:
        self.brightness = next_step(self.hw.brightness_levels, self.brightness)
        self.backlight.set(self.brightness)
        self.dimmed = False
        self.settings.set(brightness=self.brightness)

    def enter_standby(self) -> None:
        log.info("standby")
        self.save_progress_now()
        self._close_menu_quietly()
        self._standby_paused = not bool(self.player.get("pause"))
        if self._standby_paused:
            self.cmd("set_property", "pause", True)
        self.backlight.set(0)
        self._clear_overlay()
        self.mode = "standby"

    def _wake(self) -> None:
        if self.mode == "standby":
            log.info("waking")
            if self._standby_paused:
                self.cmd("set_property", "pause", False)
            self.mode = "play"
        self.dimmed = False
        self.backlight.set(self.brightness)
        self._waking = False

    def _tick(self) -> None:
        now = time.time()
        if self.mode == "calibrate" and self._calib and now > self._calib["deadline"]:
            self.end_calibration("Timed out")
            return
        if self.sleep_deadline and now >= self.sleep_deadline and self.mode == "play":
            self.sleep_deadline, self.sleep_timer_min = None, 0
            self.enter_standby()
            return
        if self.mode == "play" and not self.dimmed and self._should_dim(now):
            self.dimmed = True
            self.backlight.set(min(self.brightness, DIM_LEVEL))

    def _should_dim(self, now: float) -> bool:
        """Untouched for long enough: a paused picture dims soon (a still image burns the eye, and the battery);
        a playing one only dims if the user set Auto-dim."""
        idle = now - self.last_activity
        if self.auto_dim_min and idle >= self.auto_dim_min * 60:
            return True
        return bool(self.cfg.pause_dim_seconds) and idle >= self.cfg.pause_dim_seconds and bool(self.player.get("pause"))

    # ─────────────────────────────────────────
    #  The menu
    # ─────────────────────────────────────────
    def schedule_menu_hide(self) -> None:
        self.menu_deadline = time.time() + self.cfg.menu_timeout

    def open_menu(self) -> None:
        self._set_menu_open(True)
        self.menu.reset()
        self.confirm_shutdown_until = 0.0
        self.schedule_menu_hide()
        log.info("menu opened")
        self.render_menu()

    def _close_menu_quietly(self) -> None:
        self._set_menu_open(False)
        self.menu.reset()

    def close_menu(self) -> None:
        self._close_menu_quietly()
        self.confirm_shutdown_until = 0.0
        self._clear_overlay()
        log.info("menu closed")

    def _menu_ctx(self) -> MenuContext:
        channels = self.channels()
        return MenuContext(
            paused=bool(self.player.get("pause")), brightness=self.brightness, shuffle=self.shuffle,
            channels=[c.label for c in channels], current_channel=self.channel_index,
            sleep_timer_min=self.sleep_timer_min, auto_dim_min=self.auto_dim_min,
            confirm_shutdown=time.time() < self.confirm_shutdown_until, wifi_setup=self.wifi_available)

    def render_menu(self) -> None:
        if not self.menu_open:
            return
        items = self.menu.items(self._menu_ctx())
        self.menu.index = max(0, min(self.menu.index, len(items) - 1))
        self.menu.top = render.clamp_top(self.menu.top, len(items))
        channels = self.channels()
        subtitle = channels[self.channel_index].label if channels else ""
        image = render.render_menu_landscape(items, self.menu.index, self.hw, title=self.name,
                                             subtitle=subtitle, top=self.menu.top)
        self._set_overlay(image)

    def menu_select(self) -> None:
        items = self.menu.items(self._menu_ctx())
        if not 0 <= self.menu.index < len(items):
            return
        action = items[self.menu.index].action
        log.info("menu select: %s", action)
        kind = action[0]
        if kind == "page":
            self.menu.open_page(action[1])
            self.schedule_menu_hide()
            self.render_menu()
        elif kind == "playpause":
            self.toggle_pause()
            self.schedule_menu_hide()
            self.render_menu()
        elif kind in ("next", "prev"):
            self.close_menu()
            self.skip_video(+1 if kind == "next" else -1)
        elif kind == "brightness":
            self.cycle_brightness()
            self.schedule_menu_hide()
            self.render_menu()
        elif kind == "shuffle":
            self.shuffle = not self.shuffle
            self.settings.set(shuffle=self.shuffle)
            self.reload_channel()
            self.schedule_menu_hide()
            self.render_menu()
        elif kind == "reshuffle":
            if self.shuffle:
                self.finish_cycle_in_place()
            self.schedule_menu_hide()
            self.render_menu()
        elif kind == "sleep_timer":
            self.sleep_timer_min = next_step(SLEEP_TIMER_STEPS, self.sleep_timer_min)
            self.sleep_deadline = time.time() + self.sleep_timer_min * 60 if self.sleep_timer_min else None
            self.schedule_menu_hide()
            self.render_menu()
        elif kind == "auto_dim":
            self.auto_dim_min = next_step(AUTO_DIM_STEPS, self.auto_dim_min)
            self.settings.set(auto_dim_min=self.auto_dim_min)
            self.schedule_menu_hide()
            self.render_menu()
        elif kind == "calibrate":
            self.close_menu()
            self.begin_calibration()
        elif kind == "shutdown":
            self.request_shutdown()
        elif kind == "transfer":
            self.close_menu()
            self.start_transfer()
        elif kind == "wifi_setup":
            self.close_menu()
            self.start_setup()
        elif kind == "channel":
            self.close_menu()
            self.switch_channel(action[1])

    def finish_cycle_in_place(self) -> None:
        """Reshuffle the current channel now and load it into the running player."""
        self.save_progress_now()
        plan = self.playlists.new_cycle(self.current_channel(), True)
        self.plan = plan
        self._cur_rel = None
        self._pending_resume = None
        path = self.playlists.write_m3u(plan, self.m3u_path)
        self.cmd("loadfile", path, "replace")
        self.cmd("set_property", "pause", False)

    # ─────────────────────────────────────────
    #  Shutdown
    # ─────────────────────────────────────────
    def request_shutdown(self) -> None:
        now = time.time()
        if now < self.confirm_shutdown_until:
            self.do_shutdown()
        else:
            self.confirm_shutdown_until = now + 4.0       # the row now reads "Tap again to confirm"
            self.schedule_menu_hide()
            self.render_menu()

    def do_shutdown(self) -> None:
        log.info("shutting down")
        self.save_progress_now()
        self._close_menu_quietly()
        self.mode = "shutdown"
        halt = render.render_message(self.hw, "SYSTEM HALT", ["Safe to unplug in a few seconds"], self.name)
        self._set_overlay(halt)
        if self.display is not None:                    # also on the display itself: the overlay goes when mpv is stopped
            try:
                self.display.show(image_to_565(halt))
            except Exception as e:
                log.warning("could not draw the halt screen: %s", e)
        self.cmd("set_property", "pause", True)
        self.power.shutdown()

    # ─────────────────────────────────────────
    #  Touch calibration wizard
    # ─────────────────────────────────────────
    def begin_calibration(self, first_run: bool = False) -> None:
        log.info("calibration started")
        self._close_menu_quietly()
        back = "empty" if self.mode == "empty" else "play"           # on the 'no videos yet' screen there is no player to draw on
        self.mode = "calibrate"
        self._calib = {"step": 0, "targets": render.calibration_targets(self.hw), "pairs": [],
                       "deadline": time.time() + CALIBRATION_TIMEOUT, "first_run": first_run, "back": back}
        self._show_calibration()

    def _show_calibration_image(self, image) -> None:
        if self._calib and self._calib.get("back") == "empty" and self.display is not None:
            self.display.show(image_to_565(image))
        else:
            self._set_overlay(image)

    def _show_calibration(self, message: str = "") -> None:
        c = self._calib
        self._show_calibration_image(render.render_calibration(self.hw, c["step"], len(c["targets"]),
                                                               c["targets"][c["step"]], message, self.name))

    def end_calibration(self, message: str = "") -> None:
        if self._calib and self._calib.get("first_run"):
            self.settings.set(first_run_done=True)
        back = (self._calib or {}).get("back", "play")
        self._calib = None
        log.info("calibration ended: %s", message)
        if back == "empty":
            self.enter_empty()
            return
        self.mode = "play"
        self._clear_overlay()

    def _calibration_gesture(self, g: tuple) -> None:
        c = self._calib
        if c is None:
            return
        if g[0] == "long_press":
            self.end_calibration("Cancelled")
            return
        if g[0] != "tap" or len(g) < 4 or g[3] is None:
            return
        c["pairs"].append((g[3], c["targets"][c["step"]]))
        c["step"] += 1
        c["deadline"] = time.time() + CALIBRATION_TIMEOUT
        if c["step"] < len(c["targets"]):
            self._show_calibration()
            return
        try:
            affine, rms = solve_affine(c["pairs"])
        except ValueError:
            rms = float("inf")
        if rms > CALIBRATION_MAX_ERROR:
            log.warning("calibration rejected (error %.1f px)", rms)
            c["step"], c["pairs"] = 0, []
            self._show_calibration("Taps were too far off - try again")
            return
        self.affine = affine
        self.settings.set(touch_affine=[list(r) for r in affine], touch_calibrated=True)
        log.info("calibration applied (error %.1f px)", rms)
        self._show_calibration_image(render.render_message(self.hw, "TOUCH CALIBRATED", ["error %.1f px" % rms], self.name))
        c["done"] = True                                      # ignore taps while the result shows
        c["deadline"] = time.time() + 5
        threading.Timer(1.5, lambda: self.events.put(("calibration_done",))).start()

    # ─────────────────────────────────────────
    #  Gestures -> actions (worker thread only)
    #
    #    Menu closed                      Menu open
    #    tap          play / pause        tap row      activate it
    #    swipe left   next video          tap outside  close menu
    #    swipe right  previous video      swipe up     scroll down the list
    #    swipe up     next channel        swipe down   scroll up the list (a tap never scrolls)
    #    swipe down   previous channel
    #    hold         open menu           hold         close menu
    # ─────────────────────────────────────────
    def handle_gesture(self, g: tuple) -> None:
        kind = g[0]
        if kind != "tick":
            log.info("gesture %s", g[:2] if kind == "tap" else g)
            self.last_gesture = g
            if self._fast_restarts >= 3 and kind == "swipe":        # you changed channel while the TV was backing off: try at once
                self._fast_restarts, self._last_restart = 0, 0.0
        if kind == "tick":
            self._tick()
        elif kind == "wake":
            self._wake()
        elif kind == "setup_auto":
            if self.mode in ("play", "empty") and self.setup is None:
                self.start_setup()
        elif kind == "calibration_done":
            if self.mode == "calibrate":
                self.end_calibration("Calibrated")
        elif kind == "menu_timeout":
            if self.menu_open and time.time() >= self.menu_deadline:
                self.close_menu()
        elif self.mode == "calibrate":
            if not (self._calib and self._calib.get("done")):
                self._calibration_gesture(g)
        elif self.mode == "empty":
            self._empty_gesture(g)
        elif self.mode == "transfer":
            self._transfer_gesture(g)
        elif self.mode == "setup":
            self._setup_gesture(g)
        elif self.mode != "play":
            pass                                                # standby / shutting down: ignore
        elif self.menu_open:
            self._menu_gesture(g)
        else:
            self._playback_gesture(g)

    def _menu_gesture(self, g: tuple) -> None:
        kind = g[0]
        if kind == "long_press":
            self.close_menu()
        elif kind == "tap":
            total = len(self.menu.items(self._menu_ctx()))
            hit = render.hit_test(g[1], g[2], self.menu.top, total, self.hw)
            if hit == render.OUTSIDE:
                self.close_menu()
            elif hit is None:
                self.schedule_menu_hide()
            else:
                self.menu.index = hit
                self.menu_select()
        elif kind == "swipe":
            direction, dy = g[1], g[5]
            if direction in ("up", "down"):
                rows = max(1, int(round(abs(dy) / self.cfg.scroll_px_per_row)))
                step = rows if direction == "up" else -rows
                total = len(self.menu.items(self._menu_ctx()))
                self.menu.top = render.clamp_top(self.menu.top + step, total)
                top_row, bottom_row = self.menu.top, min(total, self.menu.top + render.VISIBLE_ROWS) - 1
                self.menu.index = max(top_row, min(bottom_row, self.menu.index))     # keep the highlight on screen
            self.schedule_menu_hide()
            self.render_menu()

    def _playback_gesture(self, g: tuple) -> None:
        kind = g[0]
        if kind == "long_press":
            self.open_menu()
        elif kind == "tap":
            self.toggle_pause()
        elif kind == "swipe":
            direction = g[1]
            if direction == "left":
                self.skip_video(+1)
            elif direction == "right":
                self.skip_video(-1)
            elif direction == "up":
                self.switch_channel(self.channel_index + 1, step=+1)
            elif direction == "down":
                self.switch_channel(self.channel_index - 1, step=-1)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                g = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.handle_gesture(g)
            except Exception:
                log.exception("error handling gesture %s", g[:1])

    # ─────────────────────────────────────────
    #  Touch loop (reads samples, makes gestures, posts timer events)
    # ─────────────────────────────────────────
    def _on_sample(self, sample, now: float) -> None:
        if sample.fingers > 0:
            self.last_activity = now
            if (self.dimmed or self.mode == "standby") and not self._waking:
                self._waking = True
                self.events.put(("wake",))                      # the touch that wakes the screen does nothing else
                self.rec.cancel()
            if self._waking:
                return
        if sample.fingers > 1:
            self.rec.cancel()
            return
        if sample.fingers == 1:
            vx, vy = apply_affine(self.affine, sample.rx, sample.ry)
            gestures = self.rec.update(True, vx, vy, now, raw=(sample.rx, sample.ry))
        else:
            gestures = self.rec.update(False, 0.0, 0.0, now)
        for g in gestures:
            self.events.put(g)

    def touch_loop(self) -> None:
        while not self.stop_event.is_set():
            now = time.time()
            timeout = self.rec.next_timeout(now)
            if self.menu_open and self._posted_deadline != self.menu_deadline:
                until = max(0.0, self.menu_deadline - now)
                timeout = until if timeout is None else min(timeout, until)
            until_tick = max(0.0, self._next_tick - now)
            timeout = until_tick if timeout is None else min(timeout, until_tick)
            timeout = min(timeout, 1.0)

            for sample in self.touch.read(timeout):
                self._on_sample(sample, time.time())

            now = time.time()
            for g in self.rec.poll(now):
                if not self._waking:
                    self.events.put(g)
            if self.menu_open and now >= self.menu_deadline and self._posted_deadline != self.menu_deadline:
                self._posted_deadline = self.menu_deadline
                self.events.put(("menu_timeout",))
            if now >= self._next_tick:
                self._next_tick = now + self.tick_every
                self.events.put(("tick",))

    # ─────────────────────────────────────────
    #  Monitor: keeps the player alive
    # ─────────────────────────────────────────
    def _monitor_loop(self) -> None:
        last_heartbeat = time.time()
        while not self.stop_event.wait(self.exit_check_every):
            try:
                if self._hold_player or self.mode in ("shutdown", "empty", "transfer", "setup"):
                    continue
                if not self.player.alive():
                    now = time.time()
                    gap = self.restart_backoff if self._fast_restarts >= 3 else self.min_restart_gap
                    if now - self._last_restart < gap:
                        continue
                    ran = now - self._last_restart
                    self._fast_restarts = self._fast_restarts + 1 if ran < self.fast_death_seconds else 0
                    pos, count = self._last_pos
                    # The player knows whether the playlist played through (exit status 0); fall back to where we last saw it.
                    # A player that died within seconds of starting did not play anything through, whatever it says.
                    finished = self._fast_restarts == 0 and (bool(getattr(self.player, "finished_normally", lambda: False)()) or (
                        pos is not None and count is not None and pos >= count - 2))
                    log.info("player is not running (%s) - restarting", "playlist finished" if finished else "crashed")
                    if self._fast_restarts >= 3:
                        log.warning("the player keeps stopping straight after it starts; waiting %.0f s between restarts "
                                    "(swipe to change channel)", self.restart_backoff)
                        self._show_trouble()
                    else:
                        self._show_standby()
                    if finished:
                        self.finish_cycle()
                    else:
                        self.start_playback()
                    self._last_restart = last_heartbeat = time.time()
                    continue
                if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
                    last_heartbeat = time.time()
                    try:
                        self.player.command("get_property", "pause")
                    except Exception as e:
                        log.warning("player heartbeat failed (%s) - restarting", e)
                        self.start_playback()
                        self._last_restart = time.time()
            except Exception:
                log.exception("monitor error")

    # ─────────────────────────────────────────
    #  Tracker: where are we in the episode?
    # ─────────────────────────────────────────
    def _tracker_loop(self) -> None:
        while not self.stop_event.wait(min(self.tracker_every, 1.0) if (self._pending_resume or self._overlay_pending
                                                                        or self._stream or self.mode in ("transfer", "setup"))
                                       else self.tracker_every):
            try:
                self._retry_overlay()
                self._refresh_screen()
                self._track()
            except Exception:
                log.exception("tracker error")

    # ─────────────────────────────────────────
    #  Videos played straight from YouTube: say when they are loading, and when one can't be played
    # ─────────────────────────────────────────
    def _watch_stream(self, rel: Optional[str]) -> None:
        now = time.time()
        if self._stream_notice_until and now >= self._stream_notice_until:
            self._stream_notice_until = 0.0
            if not self.menu_open:
                self._clear_overlay()
        cur = self._stream
        streaming = self.library.is_stream(rel)
        if cur and (not streaming or cur["rel"] != rel):               # moved on from a YouTube video
            if cur["shown"] and not self.menu_open:
                self._clear_overlay()
            if not cur["loaded"] and self.last_activity < cur["since"]:
                self._stream_failed(cur)                               # nobody skipped it: it genuinely wouldn't play
            self._stream = cur = None
        if not streaming or self.plan is None:
            return
        if cur is None:
            self._stream = cur = {"rel": rel, "since": now, "loaded": False, "shown": False}
        if self.player.get("time-pos") is not None:
            if not cur["loaded"]:
                cur["loaded"] = True
                if cur["shown"] and not self.menu_open:
                    self._clear_overlay()
        elif not cur["loaded"] and not cur["shown"] and now - cur["since"] > self.stream_notice_after and not self.menu_open and self.mode == "play":
            cur["shown"] = True
            title = self.library.title_of(self.plan.channel, rel)
            self._set_overlay(render.render_message(self.hw, "LOADING FROM YOUTUBE", [title[:44], "This can take about 10 seconds"], self.name))

    def _stream_failed(self, cur: dict) -> None:
        title = self.library.title_of(self.plan.channel, cur["rel"]) if self.plan else ""
        if not find_ytdlp(self.cfg.ytdlp_path):
            why = "yt-dlp isn't installed on this TV"
        elif self.wifi_available and not self.online():
            why = "No Wi-Fi: this channel needs the internet"
        else:
            why = "YouTube wouldn't play it, so it was skipped"
        log.warning("could not play %r from YouTube: %s", title, why)
        if self.menu_open or self.mode != "play":
            return
        self._set_overlay(render.render_message(self.hw, "COULDN'T PLAY", [title[:44], why, "Swipe to change channel"], self.name))
        self._stream_notice_until = time.time() + self.stream_notice_for

    def _track(self) -> None:
        if self.plan is None or self.mode == "shutdown" or not self.player.alive():
            return
        pos, count = self.player.get("playlist-pos"), self.player.get("playlist-count")
        if pos is None:
            return
        self._last_pos = (pos, count)
        rel = self.playlists.episode_at(self.plan, pos)
        self._watch_stream(rel)
        if rel is None:
            return                                              # on a static
        if self._pending_resume:
            path, seconds, want = self._pending_resume
            if rel != want:
                self._pending_resume = None                     # you skipped away before it resumed
            elif self.player.get("path") == path and self.player.get("time-pos") is not None:
                self.cmd("seek", seconds, "absolute")
                self._pending_resume = None
            return
        t = self.player.get("time-pos") or 0.0
        now = time.time()
        if rel != self._cur_rel:
            self._cur_rel, self._last_save = rel, now
            log.info("now playing [%s] %s (cycle %d)", self.plan.channel.name, rel, self.plan.cycle)
            self.playlists.set_progress(self.plan.channel, rel, t)
        elif now - self._last_save >= self.progress_save_every:
            self._last_save = now
            self.playlists.set_progress(self.plan.channel, rel, t)

    # ─────────────────────────────────────────
    #  Full-screen modes drawn straight to the display (no player needed): "no videos yet" and the upload page
    # ─────────────────────────────────────────
    def enter_empty(self) -> None:
        self.mode = "empty"
        self._screen_dirty = True
        self._draw_screen()

    def _wait_for_saved_network(self) -> bool:
        """Just after boot a saved network may not have connected yet; give it a moment rather than offering hotspot setup."""
        self._notice, self._notice_until = "Connecting to Wi-Fi...", time.time() + self.saved_wifi_wait + 1
        self._screen_dirty = True
        self._draw_screen()
        deadline = time.time() + self.saved_wifi_wait
        while True:
            if self.wifi.connected_ip() is not None:
                self._notice = ""
                return True
            left = deadline - time.time()
            if left <= 0 or self.stop_event.is_set():
                self._notice = ""
                return False
            self.stop_event.wait(min(0.5, left))

    def start_transfer(self) -> None:
        """Stop the player, start the upload page, and show its address, QR code and PIN on the TV."""
        if self.mode == "transfer":
            return
        if self.wifi_available and not self.online():
            if not (self.wifi.has_saved() and self._wait_for_saved_network()):
                self.start_setup(then_upload=True)          # an upload page is no use without a network
                return
        self.save_progress_now()
        self._close_menu_quietly()
        self._clear_overlay()
        self._hold_player = True        # the monitor must not take the player stopping for a crash and restart it
        try:
            self.player.stop()
        except Exception:
            pass
        self.uploads = UploadManager(self.library.root)
        session = Session()
        self.youtube = YouTubeManager(self.library.root, self._ytdlp or YtDlp(find_ytdlp(self.cfg.ytdlp_path)),
                                      on_change=self._mark_screen_dirty)
        server = WebServer(self.uploads, session, _WebHooks(self), host="0.0.0.0", port=self.cfg.web_port,
                           youtube=self.youtube)
        try:
            port = server.start()
        except OSError as e:
            log.error("could not start the upload page: %s", e)
            self._notice, self._notice_until = f"Couldn't start the upload page ({e.strerror or e}).", time.time() + 6
            self._hold_player = False
            self._back_from_transfer()
            return
        self.web = server
        host = socket.gethostname().split(".")[0]
        ip = local_ip()
        self.web_info = {"pin": session.pin, "port": port, "urls": [f"http://{ip}:{port}", f"http://{host}.local:{port}"]}
        log.info("upload page on %s (PIN shown on the TV)", self.web_info["urls"])
        self.mode = "transfer"
        self._hold_player = False
        self.index.start_scan()                         # the player is stopped, so reading durations costs playback nothing
        self._screen_dirty = True
        self._draw_screen()

    def _mark_screen_dirty(self) -> None:
        self._screen_dirty = True

    def stop_transfer(self) -> None:
        if self.youtube is not None:
            self.youtube.shutdown()                             # downloads pause; the list of what you picked stays
            self.youtube = None
        if self.web is not None:
            self.web.stop()
        self.web, self.web_info = None, None
        log.info("upload page stopped")
        self._back_from_transfer()

    # ─────────────────────────────────────────
    #  Wi-Fi setup: the TV makes a hotspot, you join it and tell the TV which network to use
    # ─────────────────────────────────────────
    @property
    def wifi_available(self) -> bool:
        return self.wifi is not None and bool(getattr(self.wifi, "available", False))

    def online(self) -> bool:
        return not self.wifi_available or self.wifi.connected_ip() is not None

    def _watch_for_network(self) -> None:
        """First boot with no Wi-Fi saved at all: after a grace period, start setup by itself. A TV that has a network
        saved but can't reach it just keeps playing; it works fine offline, and Settings has Wi-Fi setup for later."""
        if not (self.wifi_available and self.cfg.setup_auto):
            return
        deadline = time.time() + self.online_wait
        while not self.stop_event.is_set():
            if self.wifi.connected_ip() is not None:
                return
            if time.time() >= deadline:
                break
            self.stop_event.wait(1.0)
        else:
            return
        if not self.wifi.has_saved():
            log.info("no Wi-Fi saved and not connected: starting Wi-Fi setup")
            self.events.put(("setup_auto",))

    def start_setup(self, then_upload: bool = False) -> None:
        if not self.wifi_available or self.mode == "setup":
            return
        if self.mode in ("calibrate", "shutdown", "standby"):
            return
        self.save_progress_now()
        self._close_menu_quietly()
        self._clear_overlay()
        self._hold_player = True        # so the monitor doesn't restart the player we are stopping
        try:
            self.player.stop()
        except Exception:
            pass
        ssid, password = new_hotspot_credentials()
        with self._setup_lock:
            self._setup_gen += 1
            gen = self._setup_gen
        self.setup = {"ssid": ssid, "password": password, "address": HOTSPOT_IP, "status": "Starting Wi-Fi setup...",
                      "busy": False, "then_upload": then_upload, "networks": []}
        self.mode = "setup"
        self._hold_player = False
        self._screen_dirty = True
        self._draw_screen()
        threading.Thread(target=self._setup_bringup, args=(gen,), daemon=True).start()

    def _setup_current(self, gen: int) -> bool:
        return self.mode == "setup" and self.setup is not None and gen == self._setup_gen

    def _setup_bringup(self, gen: int) -> None:
        try:
            nets = self.wifi.scan()                                 # before the hotspot: scanning while hosting is unreliable
            if not self._setup_current(gen):
                return
            self.setup["networks"] = nets
            ok, why = self.wifi.hotspot_up(self.setup["ssid"], self.setup["password"])
            if not self._setup_current(gen):
                self.wifi.hotspot_down()
                return
            if not ok:
                raise RuntimeError(why or "the hotspot didn't start")
            server = SetupServer(_SetupHooks(self), host="0.0.0.0", port=self.cfg.setup_port)
            try:
                server.start()
            except OSError:
                server = SetupServer(_SetupHooks(self), host="0.0.0.0", port=8080)       # not root, or 80 is taken
                server.start()
            self._setup_server = server
            if server.port != 80:
                self.setup["address"] = f"{HOTSPOT_IP}:{server.port}"
            self.setup["status"] = "Waiting for you to connect..."
        except Exception as e:
            log.exception("could not start Wi-Fi setup")
            if self._setup_current(gen):
                self._leave_setup(f"Couldn't start Wi-Fi setup ({e}).")
            return
        self._screen_dirty = True

    def _leave_setup(self, notice: str = "") -> None:
        """Tear everything down and go back to the TV (or on to the upload page if that is what you asked for)."""
        with self._setup_lock:
            self._setup_gen += 1
        server, self._setup_server = self._setup_server, None
        if server is not None:
            server.stop()
        then_upload = bool(self.setup and self.setup.get("then_upload"))
        try:
            self.wifi.hotspot_down()
        except Exception:
            pass
        self.setup = None
        if notice:
            self._notice, self._notice_until = notice, time.time() + 6
        self._screen_dirty = True
        self.mode = "play"
        if then_upload and self.online():
            self.start_transfer()
        else:
            self._back_from_transfer()

    def _setup_gesture(self, g: tuple) -> None:
        if g[0] == "long_press":
            log.info("Wi-Fi setup cancelled from the TV")
            self._leave_setup()

    # -- what the setup page asks of the TV
    def setup_networks(self, rescan: bool):
        if rescan and self.setup is not None and not self.setup["busy"]:
            found = self.wifi.scan()
            if found:
                self.setup["networks"] = found
        return list(self.setup["networks"]) if self.setup else []

    def setup_connect(self, ssid: str, password: str) -> None:
        with self._setup_lock:
            if self.setup is None or self.mode != "setup":
                raise RuntimeError("Setup isn't running.")
            if self.setup["busy"]:
                raise RuntimeError("The TV is already joining a network.")
            self.setup["busy"] = True
            gen = self._setup_gen
        self.setup["status"] = f"Joining {ssid}..."
        self._screen_dirty = True
        threading.Thread(target=self._setup_join, args=(gen, ssid, password), daemon=True).start()

    def _setup_join(self, gen: int, ssid: str, password: str) -> None:
        time.sleep(self.connect_delay)
        if not self._setup_current(gen):
            return
        creds = (self.setup["ssid"], self.setup["password"])
        ok, why = self.wifi.connect(ssid, password)                 # the hotspot goes down as part of this
        if not self._setup_current(gen):
            return
        if ok:
            log.info("joined Wi-Fi %r", ssid)
            self.setup["status"] = f"Connected to {ssid}"
            self.setup["connected"] = True
            self._screen_dirty = True
            self._draw_screen()
            time.sleep(self.connect_delay + 2.0)
            if self._setup_current(gen):
                self._leave_setup(f"Connected to {ssid}")
            return
        log.warning("could not join Wi-Fi %r: %s", ssid, why)
        reason = "wrong password?" if "secret" in why.lower() or "password" in why.lower() else "not reachable?"
        self.setup["status"] = f"Couldn't join {ssid} ({reason}). Setup Wi-Fi is back."
        self._screen_dirty = True
        ok, _ = self.wifi.hotspot_up(*creds)
        if self._setup_current(gen):
            self.setup["busy"] = False
            if not ok:
                self._leave_setup("Wi-Fi setup stopped (couldn't restart the hotspot).")

    def _back_from_transfer(self) -> None:
        if self.library.next_playable(self.channel_index, +1) is None:
            self.enter_empty()
            return
        self.mode = "play"
        idx = self.library.next_playable(self.channel_index, +1)
        self.channel_index = idx
        self.start_playback()

    def _empty_gesture(self, g: tuple) -> None:
        if g[0] != "tap":
            return
        button = render.empty_button_at(g[1], g[2])
        if button == "upload":
            self.start_transfer()
        elif button == "recheck":
            if self.library.next_playable(self.channel_index, +1) is None:
                self._notice, self._notice_until = "Still no videos found.", time.time() + 4
                self._screen_dirty = True
                self._draw_screen()
            else:
                self._back_from_transfer()

    def _transfer_gesture(self, g: tuple) -> None:
        if g[0] == "long_press":
            self.stop_transfer()

    def _draw_screen(self) -> None:
        if self.display is None:
            return
        now = time.time()
        notice = self._notice if now < self._notice_until else ""
        if self.mode == "empty":
            img = render.render_empty(self.hw, self.name, self.cfg.videos_dir, notice)
        elif self.mode == "transfer" and self.web_info:
            snap = self.uploads.snapshot() if self.uploads else {"active": [], "completed": []}
            status, progress = [], None
            if snap["active"]:
                a = snap["active"][0]
                status.append(f"Uploading  {a['name']}")
                progress = a["received"] / a["total"] if a["total"] else None
            yt = self.youtube.progress() if self.youtube else None
            if yt and yt["active"]:
                a = yt["active"]
                status.append(f"YouTube: {a['title']}" + (f"  (+{yt['queued']} waiting)" if yt["queued"] else ""))
                if progress is None:
                    progress = a["progress"]
            elif yt and yt["failed"] and self.youtube.busy() is False and not snap["active"]:
                status.append(f"{len(yt['failed'])} YouTube video{'s' if len(yt['failed']) != 1 else ''} couldn't be fetched (see the page)")
            done = len(snap["completed"])
            status.append(f"{done} file{'s' if done != 1 else ''} received" if done else
                          ("" if snap["active"] else "Waiting for you to connect..."))
            if notice:
                status.append(notice)
            matrix = qrcodes.matrix(self.web_info["urls"][0] + "/")
            img = render.render_transfer(self.hw, self.name, self.web_info["urls"], self.web_info["pin"], matrix,
                                         [t for t in status if t], progress)
        elif self.mode == "setup" and self.setup:
            s = self.setup
            matrix = qrcodes.matrix(wifi_qr_text(s["ssid"], s["password"]))
            status = [s["status"]] if s.get("status") else []
            if notice:
                status.append(notice)
            img = render.render_setup(self.hw, self.name, s["ssid"], s["password"], matrix, s["address"], status)
        else:
            return
        self.display.show(image_to_565(img))
        self._screen_dirty, self._last_screen = False, now

    def _refresh_screen(self) -> None:
        if self.mode not in ("empty", "transfer", "setup"):
            return
        now = time.time()
        if self._notice and now >= self._notice_until:
            self._notice = ""
            self._screen_dirty = True
        if self._screen_dirty or (self.mode in ("transfer", "setup") and now - self._last_screen >= 1.0):
            self._draw_screen()

    # --- what the web page asks of the TV -----------------------------------------------------------------------
    def rename(self, name: str) -> str:
        value = set_config_value(self.config_path, "name", name)        # raises ValueError with a message for the page
        self.name = value
        self._screen_dirty = True
        log.info("renamed to %r from the upload page", value)
        return value

    def set_standby(self, data: bytes) -> None:
        """Keep a picture sent from the web page as the standby picture. Raises ValueError with a message for the page."""
        import io
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as src:
                pic = src.convert("RGB")
        except Exception:
            raise ValueError("That doesn't look like a picture the TV can read (try a JPEG or PNG).")
        pic.thumbnail((1280, 1280))
        dest = self.cfg.standby_custom
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        pic.save(tmp, format="JPEG", quality=90)
        tmp.replace(dest)
        log.info("standby picture changed from the web page")

    def reset_standby(self) -> None:
        try:
            self.cfg.standby_custom.unlink()
        except FileNotFoundError:
            pass
        log.info("standby picture reset to the built-in one")

    def web_state(self) -> dict:
        channels = []
        for c in self.library.channels():
            channels.append({"name": c.name, "label": c.label, "count": len(self.library.videos(c)),
                             "library": self.index.summary(c)})
        return {"name": self.name, "channels": channels, "standby_custom": self.cfg.standby_custom.is_file(),
                "scan": self.index.status(), "settings": self.web_settings()}

    # --- settings, status and logs for the web page ------------------------------------------------------------
    def web_settings(self) -> dict:
        return {"values": {"brightness": self.brightness, "shuffle": self.shuffle, "auto_dim_min": self.auto_dim_min,
                           "sleep_timer_min": self.sleep_timer_min},
                "options": {"brightness": list(self.hw.brightness_levels), "auto_dim_min": list(AUTO_DIM_STEPS),
                            "sleep_timer_min": list(SLEEP_TIMER_STEPS)}}

    def set_web_setting(self, key: str, value) -> None:
        """Change one of the settings the menu has, from the web page (the TV is in upload mode then, so the player is stopped)."""
        options = self.web_settings()["options"]
        if key == "shuffle":
            if not isinstance(value, bool):
                raise ValueError("Shuffle is on or off.")
            self.shuffle = value
            self.settings.set(shuffle=value)
        elif key in options:
            if isinstance(value, bool) or value not in options[key]:
                raise ValueError(f"{value!r} isn't one of the choices for that setting.")
            if key == "brightness":
                self.brightness, self.dimmed = value, False
                self.backlight.set(value)
                self.settings.set(brightness=value)
            elif key == "auto_dim_min":
                self.auto_dim_min = value
                self.settings.set(auto_dim_min=value)
            else:
                self.sleep_timer_min = value
                self.sleep_deadline = time.time() + value * 60 if value else None
        else:
            raise ValueError("That isn't a setting you can change here.")
        log.info("setting %s = %r from the web page", key, value)

    def system_status(self) -> dict:
        import platform
        out = {"python": platform.python_version(), "load": None, "uptime_s": None, "cpu_temp_c": None, "mem_free_mb": None,
               "os": None}
        try:
            out["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
            out["load"] = [round(x, 2) for x in os.getloadavg()]
        except (OSError, ValueError, AttributeError):
            pass
        try:
            out["cpu_temp_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000.0, 1)
        except (OSError, ValueError):
            pass
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    out["mem_free_mb"] = int(line.split()[1]) // 1024
        except (OSError, ValueError):
            pass
        try:
            for line in open("/etc/os-release"):
                if line.startswith("PRETTY_NAME="):
                    out["os"] = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return out

    def recent_log(self, lines: int = 200) -> str:
        """The last lines of the app's log file (at most 200 lines / 64 KB)."""
        lines = max(1, min(int(lines), 200))
        try:
            with open(expand(self.cfg.log_file), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def rescan_library(self, force: bool = False) -> dict:
        self.index.start_scan(force)
        return self.index.status()

    def on_uploaded(self, info: dict) -> None:
        log.info("uploaded %s/%s", info.get("channel"), info.get("file"))
        self.index.start_scan()
        self._screen_dirty = True


    # ─────────────────────────────────────────
    #  For the simulator / tests
    # ─────────────────────────────────────────
    def debug_state(self) -> dict:
        channels = self.channels()
        return {
            "name": self.name, "mode": self.mode,
            "menu_open": self.menu_open, "menu_page": self.menu.page,
            "paused": bool(self.player.get("pause")),
            "playlist_pos": self.player.get("playlist-pos"), "playlist_count": self.player.get("playlist-count"),
            "now_playing": (self.player.get("path") or "").split("/")[-1],
            "time_pos": self.player.get("time-pos"),
            "channel": channels[self.channel_index].name if channels else None,
            "brightness": self.brightness, "shuffle": self.shuffle, "dimmed": self.dimmed,
            "auto_dim_min": self.auto_dim_min, "sleep_timer_min": self.sleep_timer_min,
            "backlight": getattr(self.backlight, "percent", None),
            "last_gesture": list(self.last_gesture[:2]) if self.last_gesture else None,
            "shutdown_requested": getattr(self.power, "shutdown_requested", False),
            "upload_page": self.web_info,
            "wifi_setup": {k: v for k, v in self.setup.items() if k != "password"} if self.setup else None,
        }


class _WebHooks:
    """What the upload page may ask of the app (kept as a narrow interface)."""

    def __init__(self, app: "App"):
        self.app = app

    def state(self) -> dict:
        return self.app.web_state()

    def set_name(self, name: str) -> str:
        return self.app.rename(name)

    def standby_path(self) -> str:
        return self.app.cfg.standby_image

    # updates: UpdateError becomes ValueError so the web layer needs to know nothing about the updater
    def rescan(self, force: bool = False) -> dict:
        return self.app.rescan_library(force)

    def set_setting(self, key: str, value) -> None:
        self.app.set_web_setting(key, value)

    def status(self) -> dict:
        return self.app.system_status()

    def logs(self, lines: int) -> str:
        return self.app.recent_log(lines)

    def update_status(self) -> dict:
        return self.app.updater.status()

    def update_check(self) -> dict:
        return self.app.updater.check()

    def update_apply(self) -> dict:
        updater = self.app.updater
        why = updater.can_update()
        if why:
            raise ValueError(why)
        if updater.status().get("state") == "working":
            raise ValueError("An update is already running.")
        threading.Thread(target=updater.apply, daemon=True).start()
        return {"state": "working"}

    def update_rollback(self) -> dict:
        try:
            return self.app.updater.rollback()
        except UpdateError as e:
            raise ValueError(str(e))

    def set_standby(self, data: bytes) -> None:
        self.app.set_standby(data)

    def reset_standby(self) -> None:
        self.app.reset_standby()

    def uploaded(self, info: dict) -> None:
        self.app.on_uploaded(info)


class _SetupHooks:
    """What the Wi-Fi setup page may ask of the app."""

    def __init__(self, app: "App"):
        self.app = app

    def name(self) -> str:
        return self.app.name

    def networks(self, rescan: bool):
        return self.app.setup_networks(rescan)

    def connect(self, ssid: str, password: str) -> None:
        self.app.setup_connect(ssid, password)
