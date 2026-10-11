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
import queue
import random
import threading
import time
from typing import Optional

from . import render
from .config import Config, expand
from .gestures import GestureRecognizer
from .hardware import Hardware, apply_affine, solve_affine
from .library import Library, Plan, Playlists
from .menu import AUTO_DIM_STEPS, SLEEP_TIMER_STEPS, MenuContext, MenuModel, next_step
from .skip import skip_target
from .state import Settings, Store

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
                 store: Optional[Store] = None, rng: Optional[random.Random] = None, tick_every: float = 5.0):
        self.cfg, self.hw = cfg, hw
        self.touch, self.backlight, self.power = backend.touch, backend.backlight, backend.power
        self.audio = getattr(backend, "audio", None)
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
            raise RuntimeError("No channel has any videos")
        if idx != self.channel_index:
            log.warning("channel %s has no videos; starting on %s instead", names[self.channel_index], names[idx])
            self.channel_index = idx
        self.start_playback()
        if self.settings.fresh and not self.settings.get("first_run_done"):
            self.begin_calibration(first_run=True)            # a brand new install starts by calibrating touch

    def start_threads(self) -> None:
        for target in (self._worker_loop, self._monitor_loop, self._tracker_loop):
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
        if (self.mode == "play" and self.auto_dim_min and not self.dimmed
                and now - self.last_activity >= self.auto_dim_min * 60):
            self.dimmed = True
            self.backlight.set(min(self.brightness, DIM_LEVEL))

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
            confirm_shutdown=time.time() < self.confirm_shutdown_until)

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
        self._set_overlay(render.render_message(self.hw, "SYSTEM HALT", ["Safe to unplug in a few seconds"], self.name))
        self.cmd("set_property", "pause", True)
        self.power.shutdown()

    # ─────────────────────────────────────────
    #  Touch calibration wizard
    # ─────────────────────────────────────────
    def begin_calibration(self, first_run: bool = False) -> None:
        log.info("calibration started")
        self._close_menu_quietly()
        self.mode = "calibrate"
        self._calib = {"step": 0, "targets": render.calibration_targets(self.hw), "pairs": [],
                       "deadline": time.time() + CALIBRATION_TIMEOUT, "first_run": first_run}
        self._show_calibration()

    def _show_calibration(self, message: str = "") -> None:
        c = self._calib
        self._set_overlay(render.render_calibration(self.hw, c["step"], len(c["targets"]),
                                                    c["targets"][c["step"]], message, self.name))

    def end_calibration(self, message: str = "") -> None:
        if self._calib and self._calib.get("first_run"):
            self.settings.set(first_run_done=True)
        self._calib = None
        self.mode = "play"
        self._clear_overlay()
        log.info("calibration ended: %s", message)

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
        self._set_overlay(render.render_message(self.hw, "TOUCH CALIBRATED", ["error %.1f px" % rms], self.name))
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
        if kind == "tick":
            self._tick()
        elif kind == "wake":
            self._wake()
        elif kind == "calibration_done":
            if self.mode == "calibrate":
                self.end_calibration("Calibrated")
        elif kind == "menu_timeout":
            if self.menu_open and time.time() >= self.menu_deadline:
                self.close_menu()
        elif self.mode == "calibrate":
            if not (self._calib and self._calib.get("done")):
                self._calibration_gesture(g)
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
        last_restart = 0.0
        while not self.stop_event.wait(self.exit_check_every):
            try:
                if self.mode == "shutdown":
                    continue
                if not self.player.alive():
                    if time.time() - last_restart < self.min_restart_gap:
                        continue
                    pos, count = self._last_pos
                    finished = pos is not None and count is not None and pos >= count - 2
                    log.info("player is not running (%s) - restarting", "playlist finished" if finished else "crashed")
                    if finished:
                        self.finish_cycle()
                    else:
                        self.start_playback()
                    last_restart = last_heartbeat = time.time()
                    continue
                if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
                    last_heartbeat = time.time()
                    try:
                        self.player.command("get_property", "pause")
                    except Exception as e:
                        log.warning("player heartbeat failed (%s) - restarting", e)
                        self.start_playback()
                        last_restart = time.time()
            except Exception:
                log.exception("monitor error")

    # ─────────────────────────────────────────
    #  Tracker: where are we in the episode?
    # ─────────────────────────────────────────
    def _tracker_loop(self) -> None:
        while not self.stop_event.wait(min(self.tracker_every, 1.0) if (self._pending_resume or self._overlay_pending)
                                       else self.tracker_every):
            try:
                self._retry_overlay()
                self._track()
            except Exception:
                log.exception("tracker error")

    def _track(self) -> None:
        if self.plan is None or self.mode == "shutdown" or not self.player.alive():
            return
        pos, count = self.player.get("playlist-pos"), self.player.get("playlist-count")
        if pos is None:
            return
        self._last_pos = (pos, count)
        rel = self.playlists.episode_at(self.plan, pos)
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
        }
