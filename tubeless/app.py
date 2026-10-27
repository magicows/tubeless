"""
The application: ties touch, the menu, the player, the library and the settings together.

Threads:
  * the touch loop (main thread, or a thread in tests) reads touch samples and turns them into gestures
  * one worker thread performs every gesture, so menu/playback state is only touched from there
  * a monitor thread restarts the player if it exits (playlist finished) or stops answering
  * a tracker thread saves how far through an episode you are, and does the resume seek
"""
from __future__ import annotations

import json
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
from . import daynight, render, themes
from .config import Config, expand, set_config_value, user_config_path
from .gestures import GestureRecognizer
from . import health
from .hardware import Hardware, apply_affine, solve_affine
from . import jellyfin, live
from .ads import EVERY_OPTIONS, AdsManager
from . import jfchannel
from .jfchannel import JellyfinChannelFile
from .libindex import LibraryIndex
from .lookahead import Lookahead
from .library import STREAM_PREFIX, Library, Plan, Playlists
from .menu import AUTO_DIM_STEPS, SLEEP_TIMER_STEPS, MenuContext, MenuModel, next_step, time_left
from .pixels import image_to_565
from .skip import layout_target
from .state import Settings, Store
from .statics import StaticManager
from . import __version__
from .updater import UpdateError, Updater
from .setupweb import SetupServer
from .web import Session, UploadManager, WebServer, local_ip
from .youtube import DEFAULT_HEIGHT, HEIGHTS, YTDLP_RELEASES, YouTubeManager, YtDlp, default_fetch_json, find_ytdlp, ytdlp_newer
from .wifi import HOTSPOT_IP, Wifi, new_hotspot_credentials, wifi_qr_text

log = logging.getLogger(__name__)

STARTUP_GRACE = 10.0               # seconds after mpv starts before its control socket is expected to answer
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
        self._offline = False                  # is the network down? (kept by the Wi-Fi guard; the library then counts only downloaded videos)
        self.library.offline = lambda: self._offline
        self.playlists = Playlists(self.library, self.store, cfg.short_static, rng=rng)
        self.statics = StaticManager(cfg, self.settings, self.library.root, on_change=self._static_changed)
        self.playlists.static_path = self.statics.paths()[0]
        self.playlists.static_on = self.statics.between_on()
        self.ads = AdsManager(cfg.ads, self.settings, rng=rng)             # opt in (see ads.py)
        self.playlists.static_slot = self.ads.slot_entry
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
        h = self.settings.get("youtube_height", DEFAULT_HEIGHT)
        self.youtube_height = h if h in HEIGHTS else DEFAULT_HEIGHT
        if hasattr(player, "youtube_height"):
            player.youtube_height = self.youtube_height
        customs = self.settings.get("custom_themes", [])
        listed_live = self.settings.get("live_channels")                  # channels that run on the clock like broadcast TV (see live.py); opt in, one by one
        if isinstance(listed_live, list):
            self.live_channels: set = {n for n in listed_live if isinstance(n, str)}
        elif self.settings.get("live_tv") is True:                        # the old all-channels switch: every channel it had is Live, as it was
            self.live_channels = {c.name for c in self.library.channels()}
            self.settings.set(live_channels=sorted(self.live_channels))
        else:
            self.live_channels = set()
        self.playlists.live = self._live_slot
        self.guide_view = bool(self.settings.get("menu_guide", False))    # the menu opens on the TV guide (the header's guide button toggles it)
        listed = self.settings.get("channel_list", [])
        self.library.order = [n for n in listed if isinstance(n, str)] if isinstance(listed, list) else []
        seeds = self.settings.get("live_seeds", {})
        self.live_seeds: dict = {k: v for k, v in (seeds if isinstance(seeds, dict) else {}).items() if isinstance(v, int)}   # a channel's reshuffles
        self.jf_credentials = jellyfin.Credentials(self.cfg.data)       # the Jellyfin token, if signed in (never the password)
        self.library.jf_url = self._jf_stream_url
        self._jf_quick: Optional[dict] = None                           # a Quick Connect waiting for approval: {server, secret, code}
        self.custom_themes: list = [c for c in customs if isinstance(c, dict)] if isinstance(customs, list) else []
        self.theme_id = str(self.settings.get("theme", themes.DEFAULT_THEME))
        self._apply_theme()
        self.lookahead_enabled = bool(self.settings.get("youtube_lookahead", True))       # see lookahead.py
        self.lookahead = Lookahead(lambda: find_ytdlp(cfg.ytdlp_path), lambda: self.youtube_height)
        self.lookahead_after = 15.0            # seconds into a video before the next one is looked up (the current one settles first)
        self.lookahead_min_free_mb = 80        # not when memory is this short
        self._lookahead_tried: set = set()     # videos already tried this time round, so a failure is not retried every few seconds
        order = self.settings.get("channel_order", {})
        self.channel_order = {k: v for k, v in (order if isinstance(order, dict) else {}).items() if v in ("shuffle", "order")}
        b = self.settings.get("brightness", 100)
        self.brightness = b if b in hw.brightness_levels else hw.brightness_levels[-1]       # the daytime level
        self.night = daynight.Night.from_settings(self.settings.get, hw.brightness_levels)      # dimming at night (opt in; see daynight.py)
        self._night_checked = 0.0
        self.clock_synced = daynight.clock_synced          # asked now and then; swapped in tests
        self._clock_ok_until, self._clock_ok = 0.0, False
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
        self.confirm_transfer_until = 0.0     # likewise for the header's upload button
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
        self.ad_uploads: Optional[UploadManager] = None
        self.web_info: Optional[dict] = None   # {"pin": ..., "urls": [...]} while upload mode is on
        self._stream: Optional[dict] = None    # the YouTube video that is loading / playing, if any
        self._stream_notice_until = 0.0
        self.stream_notice_after = 2.0         # seconds of loading before the 'loading from YouTube' message appears
        self.stream_notice_for = 7.0
        self.net_wait_max = 60.0               # after a start, a channel that streams waits this long for the network before trying anyway
        self.net_settle = 3.0                  # ... and a moment more once it is up, for name lookups to work
        self._net_waited = False
        self.stream_give_up = 45.0             # a YouTube video that has not started after this long is skipped
        self.stream_restart = 120.0            # ... and if mpv is still stuck on it after this, the player is restarted past it
        self.ytdlp_auto_every = 24 * 3600.0     # yt-dlp is looked at (and updated if it is behind) this often, at most, when the TV is idle
        self.ytdlp_fetch_json = default_fetch_json
        self._ytdlp_auto_running = False
        self._ytdlp = ytdlp                    # tests pass a FakeYtDlp; normally found when the upload page starts
        self.youtube: Optional[YouTubeManager] = None
        self.wifi = wifi                       # None, or something that can make a hotspot and join networks
        self.setup: Optional[dict] = None      # while Wi-Fi setup is on: ssid, password, address, status, ...
        self._setup_server: Optional[SetupServer] = None
        self._setup_lock = threading.Lock()
        self._setup_gen = 0                    # bumped on every start/stop so a stale background step can tell
        self.connect_delay = 1.5               # lets the page's answer reach the phone before the hotspot drops
        self.index = LibraryIndex(self.library, self.store)
        self._updating = False                 # the 'update installed, restarting' card is on the display: leave it alone
        self.updater = Updater(Path(__file__).resolve().parent.parent, notify=self._announce_update,
                               marker=self.cfg.data / "updated.json")
        self._hold_player = False              # true while start_transfer/start_setup stop the player on purpose
        self.update_notice_seconds = 4.0       # how long 'update complete' stays over the video
        self.health_every = 60.0               # a health note goes in the log this often (only on a Pi, where there is something to read)
        self.net_check_every = 20.0            # how often the Wi-Fi guard looks
        self.net_down_grace = 120.0            # the network must be gone this long before the Wi-Fi chip is reloaded
        self.net_recover_every = 600.0         # ... and it is not tried more often than this
        self.net_reboot_after = 480.0          # if reloading the chip did not help, restart the TV this long after trying
        self.net_min_uptime = 600.0            # ... but never in the first ten minutes after the app starts
        self._started_at = time.time()
        self._net_down_since: Optional[float] = None
        self._net_last_recover = 0.0
        self._net_tried_this_outage = False
        self._net_seen_up = False              # the network has worked at some point since boot (a TV that never connected won't restart itself)
        self._net_rebooted = False             # at most once per boot: no restart loops
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
        self.backlight.set(self.current_brightness())
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
        self._confirm_update()
        if self.settings.fresh and not self.settings.get("first_run_done"):
            self.begin_calibration(first_run=True)            # a brand new install starts by calibrating touch

    def start_threads(self) -> None:
        for target in (self._worker_loop, self._monitor_loop, self._tracker_loop, self._watch_for_network, self._wifi_guard_loop, self._health_loop):
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
    def _static_changed(self) -> None:
        """A different TV static clip was chosen on the web page: the next playlist uses it (the player restarts on leaving upload mode)."""
        self.playlists.static_path = self.statics.paths()[0]
        self.playlists.static_on = self.statics.between_on()

    def shuffle_for(self, channel) -> bool:
        """Does this channel play shuffled? A channel can be set to 'shuffle' or 'order' on the web page; otherwise the TV's Shuffle setting."""
        mode = self.channel_order.get(getattr(channel, "name", channel))
        return self.shuffle if mode is None else mode == "shuffle"

    def start_playback(self) -> None:
        """(Re)build the current channel's playlist and start the player on it."""
        plan = self.playlists.plan(self.current_channel(), self.shuffle_for(self.current_channel()))
        self.plan = plan
        self._cur_rel = None
        self._lookahead_tried.clear()
        if not self._net_waited:
            self._net_waited = True
            self._wait_for_network(plan)
            self._refresh_offline()
        if self._offline and not self.library.has_videos(plan.channel):
            other = self.library.next_playable(self.channel_index, +1)         # still no network: a channel with downloaded videos instead
            if other is not None:
                self.channel_index = other
                self.settings.set(channel=self.channels()[other].name)
                log.warning("no network, and %s only streams; starting on %s instead", plan.channel.name, self.channels()[other].name)
                plan = self.playlists.plan(self.current_channel(), self.shuffle_for(self.current_channel()))
                self.plan = plan
        path = self.playlists.write_m3u(plan, self.m3u_path)
        self.player.start(path)
        self._set_resume(plan)

    def _network_ready(self) -> bool:
        return self.online() and self.wifi.reachable() is not False

    def _set_offline(self, offline: bool) -> None:
        if offline != self._offline:
            log.info("the network is down: channels that only stream are left out until it is back" if offline
                     else "the network is up: streamed channels are back")
        self._offline = offline

    def _refresh_offline(self) -> None:
        """Ask the network now (it runs commands, so not from the touch handler); the Wi-Fi guard does this every so often."""
        self._set_offline(self.wifi_available and not self._network_ready())

    def _wait_for_network(self, plan: Plan) -> None:
        """Just after the TV starts its Wi-Fi is still joining. A channel that streams from YouTube would run through its videos
        ('No Wi-Fi', next, 'No Wi-Fi', next ...) before it was up, so wait for the network first (up to net_wait_max), once per start."""
        if not self.wifi_available or not any(self.library.is_stream(r) for r in plan.order) or self._network_ready():
            return
        started = time.time()
        log.info("waiting for the network before starting a channel that streams")
        self._show_busy("WAITING FOR WI-FI", ["This channel plays from YouTube", "It starts when the network is up"])
        while not self.stop_event.is_set():
            waited = time.time() - started
            if self._network_ready():
                self.stop_event.wait(self.net_settle)
                log.info("the network came up after %.0f s: starting", waited)
                return
            if waited >= self.net_wait_max:
                log.warning("still no network after %.0f s: starting anyway", waited)
                return
            self.stop_event.wait(1.0 if self.net_wait_max > 5 else 0.05)

    def _set_resume(self, plan: Plan) -> None:
        prog = self.playlists.progress(plan.channel)
        pos = float(prog.get("pos", 0)) if prog.get("current") == plan.current else 0.0
        if self.library.is_jellyfin(plan.current):
            pos = 0.0                                          # a transcoded stream can't be sought into: a Jellyfin video starts from the beginning
        if pos > RESUME_REWIND + 2:
            self._pending_resume = (self.library.abspath(plan.channel, plan.current), pos - RESUME_REWIND, plan.current)
        else:
            self._pending_resume = None

    def reload_channel(self) -> None:
        """Rebuild the current channel's playlist and load it into the running player."""
        self.save_progress_now()
        plan = self.playlists.plan(self.current_channel(), self.shuffle_for(self.current_channel()))
        self.plan = plan
        self._cur_rel = None
        path = self.playlists.write_m3u(plan, self.m3u_path)
        self.cmd("loadfile", path, "replace")
        self.cmd("set_property", "pause", False)
        self._set_resume(plan)

    def _show_busy(self, title: str, lines) -> None:
        """A message straight onto the display, for the moment between mpv stopping and the next screen being ready."""
        if self.display is None or self._updating:
            return
        try:
            self.display.show(image_to_565(render.render_message(self.hw, title, lines, self.name)))
        except Exception as e:
            log.warning("could not draw %r: %s", title, e)

    def _announce_update(self, kind: str, version: str) -> None:
        """The card on the TV just before it restarts into the new (or previous) version."""
        self._updating = True
        threading.Timer(20.0, self._update_card_expired).start()          # still running 20 s on = the restart didn't happen
        if self.display is None:
            return
        title = "UPDATE INSTALLED" if kind == "update" else "BACK TO THE PREVIOUS VERSION"
        img = render.render_message(self.hw, title, [f"Version {version}", "Restarting now.", "Playback resumes in a moment."], self.name)
        self.display.show(image_to_565(img))

    def _update_card_expired(self) -> None:
        if self._updating and not self.stop_event.is_set():
            log.warning("the update card has been up for 20 s and the TV has not restarted; going back to normal")
            self._updating, self._screen_dirty = False, True

    def _confirm_update(self) -> None:
        """After a restart that an update (or rollback) caused: say so over the video for a few seconds."""
        marker = self.cfg.data / "updated.json"
        try:
            note = json.loads(marker.read_text())
            marker.unlink()
        except (OSError, ValueError):
            return
        if not isinstance(note, dict) or note.get("to") != __version__:
            return                                                        # the restart didn't bring the version we expected: say nothing
        log.info("now running %s (%s from %s)", __version__, note.get("kind"), note.get("from"))
        title = "UPDATE COMPLETE" if note.get("kind") == "update" else "ROLLED BACK"
        self._set_overlay(render.render_message(self.hw, title, [f"Now running version {__version__}", "Resuming playback..."], self.name))

        def clear_later():
            end = time.time() + 30
            while self._overlay_pending is not None and time.time() < end and not self.stop_event.is_set():
                time.sleep(0.2)                                           # not on screen until mpv is up
            self.stop_event.wait(self.update_notice_seconds)
            self.events.put(("clear_update_notice",))
        threading.Thread(target=clear_later, daemon=True).start()

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
        plan = self.playlists.new_cycle(self.current_channel(), self.shuffle_for(self.current_channel()))
        self.plan = plan
        self._cur_rel = None
        self._pending_resume = None
        path = self.playlists.write_m3u(plan, self.m3u_path)
        self.player.start(path)

    def _episode_at(self, pos) -> Optional[str]:
        """The episode at a playlist position, or None on a static or an ad, and while a channel change's static is still showing (the new
        channel's playlist is not loaded yet, so what the player reports is not a position in it)."""
        if self._switch_timer is not None and self._switch_timer.is_alive():
            return None
        return self.playlists.episode_at(self.plan, pos)

    def save_progress_now(self) -> None:
        if self.plan is None or not self.player.alive():
            return
        pos = self.player.get("playlist-pos")
        rel = self._episode_at(pos)
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
        if self.plan is None or (self._switch_timer is not None and self._switch_timer.is_alive()):
            return                                                  # nothing to skip: only the channel-change static is loaded
        target = layout_target(self.playlists.layout(self.plan), self.player.get("playlist-pos"), direction)
        if target is None:
            return
        if target[0] == "end":
            self.finish_cycle()
            return
        where = target[1] + 1 if target[1] in self.playlists.ad_positions else target[1]       # a skip never plays an ad: straight to the video
        self.cmd("set_property", "playlist-pos", where)
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
        plan = self.playlists.plan(channel, self.shuffle_for(channel))
        self.plan = plan
        self._cur_rel = None
        self._pending_resume = None
        self.playlists.write_m3u(plan, self.m3u_path)
        # A newer channel change replaces any pending one, so rapid swipes don't pile up.
        if self._switch_timer is not None:
            self._switch_timer.cancel()
            self._switch_timer = None
        if not self.statics.change_on():
            self._finish_switch(plan)                                # no static on a channel change: the new channel just starts
            return
        self.cmd("loadfile", self.statics.paths()[1], "replace")
        self.cmd("set_property", "pause", False)
        self._switch_timer = threading.Timer(self.statics.delay(self.switch_delay), self._finish_switch, args=(plan,))
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
    def _clock_trusted(self, now: float) -> bool:
        """Is the time of day to be believed? Only once the clock has been set from the network this time round (the Pi has no
        battery-backed clock). Once it has been, it stays trusted for the rest of the boot, even if the Wi-Fi drops."""
        if self._clock_ok:
            return True
        if now >= self._clock_ok_until:
            self._clock_ok = bool(self.clock_synced())
            self._clock_ok_until = now + 30.0
        return self._clock_ok

    def current_brightness(self, now: Optional[float] = None) -> int:
        """What the backlight should be at: the daytime level, or the night level (fading at each end) if dimming at night is on
        and the clock can be trusted."""
        now = time.time() if now is None else now
        if not self.night.enabled or not self._clock_trusted(now):
            return self.brightness
        return self.night.level_at(self.brightness, now)

    def cycle_brightness(self) -> None:
        """The menu's brightness row steps the level for the time of day it is: the night level at night, the daytime one otherwise."""
        if self.night.enabled and self._clock_trusted(time.time()) and self.night.is_night(time.time()):
            self.night.level = next_step(self.hw.brightness_levels, self.night.level)
            self.settings.set(night_brightness=self.night.level)
        else:
            self.brightness = next_step(self.hw.brightness_levels, self.brightness)
            self.settings.set(brightness=self.brightness)
        self.dimmed = False
        self.backlight.set(self.current_brightness())

    def _night_step(self, now: float) -> None:
        """Once a half minute: follow the day and night fade."""
        if now - self._night_checked < 30.0:
            return
        self._night_checked = now
        if self.mode != "standby" and not self.dimmed:
            level = self.current_brightness(now)
            if level != getattr(self.backlight, "percent", None):
                self.backlight.set(level)

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
        self._auto_update_ytdlp()                       # the screen is off and the player paused: a good moment

    def _wake(self) -> None:
        if self.mode == "standby":
            log.info("waking")
            if self._standby_paused:
                self.cmd("set_property", "pause", False)
            self.mode = "play"
        self.dimmed = False
        self.backlight.set(self.current_brightness())
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
            self.backlight.set(min(self.current_brightness(now), DIM_LEVEL))
        self._night_step(now)

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
        if self.guide_view:
            self._show_guide()
        self.confirm_shutdown_until = self.confirm_transfer_until = 0.0
        self.schedule_menu_hide()
        log.info("menu opened")
        self.render_menu()

    def _close_menu_quietly(self) -> None:
        self._set_menu_open(False)
        self.menu.reset()

    def close_menu(self) -> None:
        self._close_menu_quietly()
        self.confirm_shutdown_until = self.confirm_transfer_until = 0.0
        self._clear_overlay()
        log.info("menu closed")

    def _show_guide(self) -> None:
        """Open the guide page with the channel you are on in view."""
        self.menu.open_page("guide")
        self.menu.index = max(0, self.channel_index)
        self.menu.top = render.clamp_top(self.menu.index - render.VISIBLE_ROWS // 2, len(self.channels()))

    def toggle_guide(self) -> None:
        """The header's guide button: the guide page, or back to the usual menu; the choice is remembered for next time."""
        if self.menu.page == "guide":
            self.menu.open_page("main")
        else:
            self._show_guide()
        self.guide_view = self.menu.page == "guide"
        self.settings.set(menu_guide=self.guide_view)
        self.confirm_shutdown_until = self.confirm_transfer_until = 0.0
        self.schedule_menu_hide()
        self.render_menu()

    def _now_on(self, channel) -> str:
        """The guide's line for a channel: what is on it now (a live channel) or would play next, and how long is left of it."""
        rels = self.library.videos(channel)
        if not rels:
            return "No videos"
        partial = False
        if self._offline:                                          # what would play without the network: the downloaded videos
            local = [r for r in rels if not self.library.is_stream(r)]
            if not local:
                return "Needs Wi-Fi"
            partial, rels = len(local) < len(rels), local
        streams = self.library.stream_seconds(channel) if any(self.library.is_stream(r) for r in rels) else {}
        found = self._live_channel(channel, rels) if self.is_live(channel) and not partial else None     # (a schedule with videos left out is not the schedule)
        if found is not None:
            order, durations = found
            rel, seconds_in = live.slot(order, durations, time.time(), self.statics.gap())
            prefix = "On now"
        else:
            saved = self.playlists.progress(channel)
            rel, seconds_in = (saved.get("current"), float(saved.get("pos") or 0)) if saved.get("current") in rels else (None, 0.0)
            if rel is None:
                return f"{len(rels)} video{'' if len(rels) == 1 else 's'}"
            prefix = "Next"
        length = self._seconds_of(channel, rel, streams)
        left = time_left(length - seconds_in if length else None)
        return f"{prefix}: {self.library.title_of(channel, rel)}" + (f"  ·  {left}" if left else "")

    def _menu_ctx(self) -> MenuContext:
        channels = self.channels()
        shuffling = self.shuffle_for(channels[self.channel_index]) if 0 <= self.channel_index < len(channels) else self.shuffle
        offline = [not self.library.has_videos(c) for c in channels] if self._offline else ()      # channels that only stream, with the network down
        return MenuContext(guide=[self._now_on(c) for c in channels] if self.menu.page == "guide" else (), needs_wifi=offline,

            paused=bool(self.player.get("pause")), brightness=self.current_brightness(), shuffle=shuffling,
            channels=[c.label for c in channels], current_channel=self.channel_index,
            sleep_timer_min=self.sleep_timer_min, auto_dim_min=self.auto_dim_min,
            confirm_shutdown=time.time() < self.confirm_shutdown_until,
            confirm_transfer=time.time() < self.confirm_transfer_until, wifi_setup=self.wifi_available)

    def render_menu(self) -> None:
        if not self.menu_open:
            return
        items = self.menu.items(self._menu_ctx())
        self.menu.index = max(0, min(self.menu.index, len(items) - 1))
        self.menu.top = render.clamp_top(self.menu.top, len(items))
        channels = self.channels()
        subtitle = channels[self.channel_index].label if channels else ""
        now = time.time()
        armed = tuple(n for n, until in (("power", self.confirm_shutdown_until), ("upload", self.confirm_transfer_until)) if now < until)
        footer = {"power": "TAP THE POWER BUTTON AGAIN TO SHUT DOWN", "upload": "TAP THE UPLOAD BUTTON AGAIN FOR UPLOAD AND SETTINGS"}[armed[-1]] if armed else None
        if not footer and self.menu.page == "guide":
            footer = "TAP  tune in        SWIPE  scroll        HOLD  close"
        image = render.render_menu_landscape(items, self.menu.index, self.hw, title=self.name, subtitle=subtitle,
                                             top=self.menu.top, armed=armed, lit=("guide",) if self.menu.page == "guide" else (),
                                             **({"footer": footer} if footer else {}))
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
            now = self.shuffle_for(self.current_channel())                  # what this channel is doing, whoever decided it
            self.shuffle = not now
            if self.channel_order.pop(self.current_channel().name, None) is not None:
                self.settings.set(channel_order=dict(self.channel_order))   # the toggle speaks for the channel you are on
            self.settings.set(shuffle=self.shuffle)
            self.reload_channel()
            self.schedule_menu_hide()
            self.render_menu()
        elif kind == "reshuffle":
            if self.shuffle_for(self.current_channel()):
                if self.is_live(self.current_channel()) and self._live_channel(self.current_channel()) is not None:
                    self.reshuffle_live(self.current_channel().name)          # a new running order; what is on air in it plays now
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
            self.confirm_transfer_until = 0.0             # one thing at a time waits for its second tap
            self.schedule_menu_hide()
            self.render_menu()

    def request_transfer(self) -> None:
        """The header's upload button: the first tap arms it, a second one within a few seconds starts upload mode."""
        now = time.time()
        if now < self.confirm_transfer_until:
            self.confirm_transfer_until = 0.0
            self.close_menu()
            self.start_transfer()
        else:
            self.confirm_transfer_until = now + 4.0
            self.confirm_shutdown_until = 0.0
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
        elif kind == "clear_update_notice":
            if self.mode == "play" and not self.menu_open:
                self._clear_overlay()
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
            button = render.header_hit(g[1], g[2], self.hw)
            if button == "guide":
                self.toggle_guide()
                return
            if button == "power":
                self.request_shutdown()
                return
            if button == "upload":
                self.request_transfer()
                return
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
                        pos is not None and count is not None and self.plan is not None
                        and pos >= count - (2 if self.playlists.layout(self.plan)[-2:-1] == [None] else 1)))     # the last video, or the static before it
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
                if time.time() - last_heartbeat >= HEARTBEAT_EVERY and time.time() - getattr(self.player, "started_at", 0.0) > STARTUP_GRACE:
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
    #  Look-ahead: have the NEXT YouTube video's real addresses ready before its turn (see lookahead.py)
    # ─────────────────────────────────────────
    def _lookahead_step(self, pos: int) -> None:
        """Called from the tracker while a video is playing at playlist position `pos`."""
        if (self.mode != "play" or self.plan is None or self.menu_open
                or not getattr(self.player, "supports_lookahead", False)):
            return
        idx = self.playlists.next_video_pos(self.plan, pos)     # the next video (a static clip or an ad may sit between)
        nxt = self.playlists.episode_at(self.plan, idx) if idx is not None else None
        if nxt is None or not self.library.is_youtube(nxt):          # looking ahead is for YouTube (a Jellyfin video starts in seconds anyway)
            return
        vid = nxt[len(STREAM_PREFIX):]
        hit = self.lookahead.ready(vid)
        if hit:
            self._swap_in(idx, nxt, vid, hit[0], hit[1])
            return
        if vid in self._lookahead_tried or self.lookahead.busy():
            return
        t = self.player.get("time-pos")
        if t is None or t < self.lookahead_after:
            return
        free = health.available_mb()
        if free is not None and free < self.lookahead_min_free_mb:
            log.info("look-ahead skipped: only %d MB of memory free", free)
            return
        self._lookahead_tried.add(vid)
        self.lookahead.start(vid)

    def _swap_in(self, idx: int, rel: str, vid: str, entry: str, took: float) -> None:
        """Put the ready address in the playlist where the YouTube link for that video is. It is added first and the old link
        removed second, and the playlist is checked afterwards: a hole or an extra entry would throw out which entry is which."""
        original = self.library.abspath(self.plan.channel, rel)
        pos, count = self.player.get("playlist-pos"), self.player.get("playlist-count")
        self.lookahead.forget(vid)
        if pos is None or count is None or pos >= idx or idx >= count:
            return                                              # too late: it is playing already, or the playlist changed
        if self.player.get(f"playlist/{idx}/filename") != original:
            return                                              # that entry is not the link we expected (already swapped, or changed)
        self.cmd("loadfile", entry, "insert-at", idx)
        self.cmd("playlist-remove", idx + 1)
        if (self.player.get("playlist-count") != count or self.player.get(f"playlist/{idx}/filename") != entry
                or self.player.get(f"playlist/{idx + 1}/filename") == original):
            log.warning("look-ahead left the playlist in an unexpected state: switching it off and rebuilding the playlist")
            self.lookahead_enabled = False
            self.reload_channel()
            return
        log.info("look-ahead: the next video's address was ready (it took %.1f s to get); it is in the playlist at %d", took, idx)

    # ─────────────────────────────────────────
    #  Videos played straight from YouTube: say when they are loading, and when one can't be played
    # ─────────────────────────────────────────
    def _watch_stream(self, rel: Optional[str]) -> bool:
        """Returns True if it restarted the player (so the caller's picture of what is playing is out of date)."""
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
                log.info("stream started playing %.1f s after the TV reached it", now - cur["since"])
                if cur["shown"] and not self.menu_open:
                    self._clear_overlay()
        elif not cur["loaded"] and not cur["shown"] and now - cur["since"] > self.stream_notice_after and not self.menu_open and self.mode == "play":
            cur["shown"] = True
            title = self.library.title_of(self.plan.channel, rel)
            where, wait = ("JELLYFIN", "This can take a few seconds") if self.library.is_jellyfin(rel) else ("YOUTUBE", "This can take about 10 seconds")
            self._set_overlay(render.render_message(self.hw, f"LOADING FROM {where}", [title[:44], wait], self.name))
        if not cur["loaded"] and self.mode == "play":
            waited = now - cur["since"]
            if waited > self.stream_restart and not cur.get("restarted"):
                cur["restarted"] = True
                log.warning("mpv has been stuck loading %s for %.0f s: restarting the player past it", rel, waited)
                self._restart_past(rel)
                return True
            elif waited > self.stream_give_up and not cur.get("skipped"):
                cur["skipped"] = True
                log.warning("%s has not started after %.0f s: skipping it", rel, waited)
                self.cmd("playlist-next")                              # on to the static, then the next video

    def _restart_past(self, rel: str) -> None:
        """mpv is not answering its way out of a stuck load: start it again on the next episode instead of the stuck one."""
        plan = self.plan
        if plan is not None and rel in plan.order and len(plan.order) > 1:
            nxt = plan.order[(plan.order.index(rel) + 1) % len(plan.order)]
            self.playlists.set_progress(plan.channel, nxt, 0.0)
        self._stream = None
        self.start_playback()

    def _stream_failed(self, cur: dict) -> None:
        title = self.library.title_of(self.plan.channel, cur["rel"]) if self.plan else ""
        jelly = self.library.is_jellyfin(cur["rel"])
        if jelly and not self.jf_credentials.signed_in:
            why = "Jellyfin isn't connected: sign in on the upload page"
        elif not jelly and not find_ytdlp(self.cfg.ytdlp_path):
            why = "yt-dlp isn't installed on this TV"
        elif self.wifi_available and not self.online():
            why = "No Wi-Fi: this channel needs the internet"
        else:
            why = ("Your Jellyfin server wouldn't play it" if jelly else "YouTube wouldn't play it") + ", so it was skipped"
        log.warning("could not play %r from %s: %s", title, "Jellyfin" if jelly else "YouTube", why)
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
        rel = self._episode_at(pos)
        if self._watch_stream(rel):
            return                                              # the player was restarted: what we just read is out of date
        if rel is not None and self.lookahead_enabled:
            self._lookahead_step(pos)
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
        self._show_busy("UPLOAD MODE", ["Starting the upload page..."])      # stopping mpv uncovers whatever was drawn last
        self.uploads = UploadManager(self.library.root)
        self.ad_uploads = UploadManager(self.cfg.ads.parent)             # the ads folder is a "channel" of its own parent
        session = Session()
        ytdlp = self._ytdlp or YtDlp(find_ytdlp(self.cfg.ytdlp_path))
        if hasattr(ytdlp, "height"):
            ytdlp.height = self.youtube_height                  # downloads at the quality chosen on the page
        self.youtube = YouTubeManager(self.library.root, ytdlp,
                                      on_change=self._mark_screen_dirty)
        server = WebServer(self.uploads, session, _WebHooks(self), host="0.0.0.0", port=self.cfg.web_port,
                           youtube=self.youtube, ad_uploads=self.ad_uploads)
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
        try:
            if self.youtube.yt.available():
                self.youtube.start_refresh()            # look for new videos in your playlists (a listing only: nothing is downloaded)
        except Exception:
            log.exception("could not start checking playlists")
        self.index.start_scan()                         # the player is stopped, so reading durations costs playback nothing
        self._auto_update_ytdlp()
        self._screen_dirty = True
        self._draw_screen()

    def _auto_update_ytdlp(self) -> None:
        """On by default (a checkbox on the page turns it off): at most once a day, when the TV is in upload mode or in standby (never while
        it is playing, and never while videos are downloading), look for a newer yt-dlp and install it. YouTube changes often, and an old
        yt-dlp is the usual reason a YouTube video stops playing."""
        if not self.settings.get("ytdlp_auto", True) or self._ytdlp_auto_running:
            return
        now = time.time()
        if now - float(self.settings.get("ytdlp_auto_at", 0) or 0) < self.ytdlp_auto_every:
            return
        manager = self.youtube
        yt = manager.yt if manager is not None else (self._ytdlp or YtDlp(find_ytdlp(self.cfg.ytdlp_path)))
        if not yt.available() or not yt.can_update() or not self.online() or (manager is not None and manager.busy()):
            return
        self._ytdlp_auto_running = True
        self.settings.set(ytdlp_auto_at=now)                # tried today, whatever happens: a failure is not retried every few minutes

        def work():
            try:
                if manager is not None:
                    newer = manager.check_ytdlp()["update_available"]
                else:
                    latest = str(self.ytdlp_fetch_json(YTDLP_RELEASES)["tag_name"]).lstrip("v")
                    newer = ytdlp_newer(latest, yt.version() or "")
                if not newer:
                    log.info("yt-dlp checked: already the newest")
                elif manager is not None:
                    manager.update_ytdlp()                    # the page shows its progress like a manual update
                    log.info("yt-dlp: updating automatically")
                else:
                    before = yt.version()
                    log.info("yt-dlp updated automatically: %s -> %s", before, yt.update())
            except Exception as e:
                log.warning("could not update yt-dlp automatically: %s", e)
            finally:
                self._ytdlp_auto_running = False

        threading.Thread(target=work, daemon=True).start()

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

    def _health_loop(self) -> None:
        if not health.has_vcgencmd():
            return                                               # not a Pi (a laptop running the simulator, the tests): nothing to read
        last_ever = 0
        while True:
            try:
                snap = health.snapshot()
                line = health.format_line(snap)
                ever = (snap.get("throttled") or 0) & 0xF0000
                if health.is_worrying(snap) or ever != last_ever:       # a new problem is a warning; the routine note is just info
                    log.warning(line)
                else:
                    log.info(line)
                last_ever = ever
            except Exception:
                log.exception("health note failed")
            if self.stop_event.wait(self.health_every):
                return

    def _wifi_guard_loop(self) -> None:
        while not self.stop_event.wait(self.net_check_every):
            try:
                self._wifi_guard()
            except Exception:
                log.exception("Wi-Fi guard error")

    def _wifi_guard(self) -> None:
        """The Pi Zero 2 W's Wi-Fi chip can fail under load and then stays dead until the Pi restarts. It dies in two ways: the
        address disappears, or (as the log of a real failure showed) the chip stops answering while the interface KEEPS its address
        and NetworkManager notices nothing. So 'down' means no address, or an address but the router can't be reached.
        After a couple of minutes down: reload the chip's driver and re-seat its bus (root helper). If that doesn't bring it back,
        restart the TV (once per boot, and only if the network had been working), because a restart always does."""
        if not self.wifi_available or self.mode not in ("play", "empty", "transfer"):
            self._net_down_since = None                          # not while the TV is hosting its own setup hotspot, etc.
            return
        ip = self.wifi.connected_ip()
        reach = self.wifi.reachable() if ip is not None else None
        now = time.time()
        self._set_offline(ip is None or reach is False)
        if ip is not None and reach is not False:                # reach None = can't tell: assume fine
            if self._net_down_since is not None:
                log.info("the network is back")
            self._net_down_since, self._net_tried_this_outage = None, False
            self._net_seen_up = True
            return
        why = "no address" if ip is None else "it has an address but the router does not answer"
        if self._net_down_since is None:
            self._net_down_since = now
            return
        gone = now - self._net_down_since
        if self._net_tried_this_outage:
            if (now - self._net_last_recover >= self.net_reboot_after and self._net_seen_up and not self._net_rebooted
                    and self.mode in ("play", "empty") and now - self._started_at >= self.net_min_uptime):
                self._net_rebooted = True
                log.warning("the network is still gone %.0f s after reloading the chip: restarting the TV", now - self._net_last_recover)
                self.save_progress_now()
                self._show_busy("WI-FI LOST", ["Restarting the TV to bring it back.", "It will carry on where you were."])
                self.power.reboot()
                return
            if now - self._net_last_recover < self.net_recover_every:
                return
            self._net_tried_this_outage = False                  # it has been a while: try the reload again (a TV out of range for hours)
        if gone < self.net_down_grace or now - self._net_last_recover < self.net_recover_every or not self.wifi.has_saved():
            return
        self._net_last_recover, self._net_tried_this_outage = now, True
        log.warning("the network has been gone for %.0f s (%s): reloading the Wi-Fi chip's driver", gone, why)
        ok, why_not = self.wifi.recover()
        log.info("the Wi-Fi chip is back" if ok else f"could not bring the Wi-Fi chip back: {why_not}")

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
        if self.display is None or self._updating:
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

    def set_greeting(self, text: str) -> str:
        """The line typed out on the boot screen (read from config.json at boot). Blank puts the placeholder back."""
        value = set_config_value(self.config_path, "boot_greeting", text)       # raises ValueError with a message for the page
        self.cfg.boot_greeting = value
        log.info("boot greeting changed from the upload page")
        return value

    # --- Live TV mode (see live.py) ------------------------------------------------------------------------------------
    def _seconds_of(self, channel, rel: str, streams: dict) -> Optional[float]:
        return streams.get(rel) if self.library.is_stream(rel) else self.index.duration(channel, rel)

    def _live_channel(self, channel, rels: Optional[list] = None) -> Optional[tuple]:
        """(running order, durations) for a channel if Live TV mode can schedule it (every length known), else None."""
        rels = rels if rels is not None else self.library.videos(channel)
        if not rels:
            return None
        streams = self.library.stream_seconds(channel) if any(self.library.is_stream(r) for r in rels) else {}
        in_order = None if self.shuffle_for(channel) else (lambda r, c=channel: self.library.in_order(c, r))
        order = live.broadcast_order(channel.name, rels, in_order, self.live_seeds.get(channel.name, 0))
        durations = [self._seconds_of(channel, r, streams) for r in order]
        return (order, durations) if all(d and d > 0 for d in durations) else None

    def is_live(self, channel) -> bool:
        """Does this channel run on the clock (Live)? Set per channel on the web page."""
        return getattr(channel, "name", channel) in self.live_channels

    def set_channel_live(self, channel: str, live) -> None:
        """Make a channel Live (it runs on the clock, see live.py) or not, from the web page."""
        if channel not in [c.name for c in self.library.channels()]:
            raise ValueError("No such channel.")
        if not isinstance(live, bool):
            raise ValueError("A channel is live or not.")
        (self.live_channels.add if live else self.live_channels.discard)(channel)
        self.settings.set(live_channels=sorted(self.live_channels))
        log.info("channel %s is %s", channel, "live" if live else "not live")

    def _live_slot(self, channel) -> Optional[tuple]:
        """What is on air on this channel now: (running order, video, seconds in), or None (the channel is not Live, or a length unknown)."""
        if not self.is_live(channel):
            return None
        if self._offline and any(self.library.is_stream(r) for r in self.library.videos(channel)):
            return None                                           # the network is down and the schedule includes streamed videos: play what is downloaded
        found = self._live_channel(channel)
        if found is None:
            return None
        order, durations = found
        on_air = live.slot(order, durations, time.time(), self.statics.gap())
        return (order, on_air[0], on_air[1]) if on_air else None

    def reshuffle_live(self, channel_name: str) -> None:
        """A new running order for a Live channel (it plays round and round, so it never reshuffles by itself)."""
        channel = next((c for c in self.library.channels() if c.name == channel_name), None)
        if channel is None:
            raise ValueError("No such channel.")
        if not self.is_live(channel):
            raise ValueError("That channel is not live.")
        if not self.shuffle_for(channel):
            raise ValueError("That channel plays in order, so there is nothing to reshuffle. Set it to shuffle first.")
        rels = self.library.videos(channel)
        current = live.broadcast_order(channel.name, rels, None, self.live_seeds.get(channel.name, 0))
        seed = self.live_seeds.get(channel.name, 0)
        for _ in range(50):                                          # a different order, not by chance the same one again
            seed += 1
            if len(rels) < 2 or live.broadcast_order(channel.name, rels, None, seed) != current:
                break
        self.live_seeds[channel.name] = seed
        self.settings.set(live_seeds=dict(self.live_seeds))
        log.info("Live TV running order of %s reshuffled", channel.name)

    def live_status(self) -> dict:
        """For the page: the Live channels, which of them can be scheduled, and for the others how many lengths are missing."""
        channels = []
        for c in self.library.channels():
            rels = self.library.videos(c)
            streams = self.library.stream_seconds(c) if any(self.library.is_stream(r) for r in rels) else {}
            missing = sum(1 for r in rels if not (self._seconds_of(c, r, streams) or 0) > 0)
            found = None if missing or not rels else self._live_channel(c, rels)
            channels.append({"name": c.name, "label": c.label, "live": self.is_live(c), "ready": found is not None, "missing": missing,
                             "minutes": live.minutes(live.total_seconds(found[1], self.statics.gap())) if found else None})
        return {"channels": channels}

    # --- Jellyfin (see jellyfin.py; the page's Jellyfin card) ---------------------------------------------------------
    def _jf_stream_url(self, item_id: str) -> str:
        """The address a Jellyfin video plays from (at the quality set for YouTube too). Raises JellyfinError if not signed in."""
        return self._jf_client().stream_url(item_id, self.youtube_height)

    def _jf_channel_folder(self, name: str) -> Path:
        return self.library.root / name

    def _jf_add(self, p: dict) -> dict:
        """Add ticked movies, series, collections or playlists to a new or an existing Jellyfin channel."""
        from .web import sanitize_channel
        client = self._jf_client()
        existing, new = str(p.get("channel", "") or ""), str(p.get("new", "") or "")
        ids = p.get("ids") or []
        if not isinstance(ids, list):
            raise jellyfin.JellyfinError("Tick something to add first.")
        if existing:
            folder = self._jf_channel_folder(existing)
            if existing not in [c.name for c in self.library.channels()] or not JellyfinChannelFile.exists(folder):
                raise jellyfin.JellyfinError("That isn't one of your Jellyfin channels.")
            name = existing
        else:
            name = sanitize_channel(new)
            if not name:
                raise jellyfin.JellyfinError("Give the new channel a name.")
            if (self.library.root / name).exists():
                raise jellyfin.JellyfinError("There is already a channel with that name.")
            folder = self._jf_channel_folder(name)
        videos = client.resolve([str(i) for i in ids])                  # may raise: nothing is created in that case
        folder.mkdir(parents=True, exist_ok=True)
        cf = JellyfinChannelFile.load(folder)
        added, skipped = cf.add(videos)
        cf.save()
        log.info("Jellyfin channel %s: added %d video(s), %d already there", name, added, skipped)
        return {"channel": name, "added": added, "skipped": skipped, "total": len(cf.items)}

    def _jf_channel(self, name: str) -> "JellyfinChannelFile":
        folder = self._jf_channel_folder(str(name))
        if str(name) not in [c.name for c in self.library.channels()] or not JellyfinChannelFile.exists(folder):
            raise jellyfin.JellyfinError("That isn't one of your Jellyfin channels.")
        return JellyfinChannelFile.load(folder)

    def _jf_delete(self, name: str) -> dict:
        """Remove a Jellyfin channel from the TV. It holds only a list of what is on the server, so nothing of yours is deleted (the server
        is not touched); a folder that also has other files in it is left alone, and the page is told why."""
        self._jf_channel(name)                                         # it must be one of the Jellyfin channels
        folder = self._jf_channel_folder(name)
        extra = sorted(e.name for e in folder.iterdir() if e.name != jfchannel.FILE)
        if extra:
            raise jellyfin.JellyfinError("That channel's folder also holds " + ", ".join(extra[:3]) + (" and more" if len(extra) > 3 else "") +
                                         ", so the TV won't remove it. Take those out first.")
        channel = next(c for c in self.library.channels() if c.name == name)
        channels = self.channels()
        keep = self.current_channel().name if channels else None
        old_index = self.channel_index
        (folder / jfchannel.FILE).unlink()
        folder.rmdir()
        self.playlists.forget(channel)
        if self.plan is not None and self.plan.channel.name == name:
            self.plan = None
        self.channel_order.pop(name, None)
        self.live_channels.discard(name)
        self.live_seeds.pop(name, None)
        self.library.order = [n for n in self.library.order if n != name]
        self.settings.set(channel_order=dict(self.channel_order), live_channels=sorted(self.live_channels), live_seeds=dict(self.live_seeds),
                          channel_list=list(self.library.order))
        names = [c.name for c in self.channels()]
        if keep in names:
            self.channel_index = names.index(keep)                     # the TV stays on the channel it was on, whatever its number now
        else:
            self.channel_index = max(0, min(old_index, len(names) - 1))      # it was on this one: the one that took its place
            if names:
                self.settings.set(channel=names[self.channel_index])
        log.info("Jellyfin channel %s removed", name)
        return {"removed": name}

    def _jf_client(self) -> "jellyfin.Client":
        cred = self.jf_credentials
        if not cred.signed_in:
            raise jellyfin.JellyfinError("Connect to your Jellyfin server first.")
        return jellyfin.Client(cred.server, cred.token, cred.data.get("user_id", ""), cred.data.get("device_id", ""))

    def jf_status(self) -> dict:
        cred = self.jf_credentials
        return {"connected": cred.signed_in, "server": cred.server, "user": cred.data.get("user_name", ""),
                "version": cred.data.get("version", ""), "quick_code": self._jf_quick["code"] if self._jf_quick else ""}

    def _jf_save(self, client: "jellyfin.Client", who: dict, info: dict) -> dict:
        self.jf_credentials.save(server=client.server, token=who["token"], user_id=who["user_id"], user_name=who["user_name"],
                                 version=info.get("version", ""), server_name=info.get("name", ""))
        self._jf_quick = None
        log.info("signed in to Jellyfin at %s as %s", client.server, who["user_name"])
        return self.jf_status()

    def jellyfin(self, action: str, p: dict):
        """Everything the page's Jellyfin card asks for. Raises jellyfin.JellyfinError (a ValueError) with a message for the page."""
        if action == "status":
            return self.jf_status()
        if action == "connect":                                       # the password is used once, here, and not kept
            client = jellyfin.Client(p.get("server", ""))
            info = client.public_info()
            return self._jf_save(client, client.login(p.get("username", ""), p.get("password", "")), info)
        if action == "quick":
            client = jellyfin.Client(p.get("server", ""))
            client.public_info()
            start = client.quick_start()
            self._jf_quick = {"server": client.server, "secret": start["secret"], "code": start["code"]}
            return {"code": start["code"]}
        if action == "quick_poll":
            pending = self._jf_quick
            if not pending:
                raise jellyfin.JellyfinError("No Quick Connect is waiting. Start it again.")
            client = jellyfin.Client(pending["server"])
            if not client.quick_check(pending["secret"]):
                return {"state": "waiting", "code": pending["code"]}
            info = client.public_info()
            return dict(self._jf_save(client, client.quick_finish(pending["secret"]), info), state="signed_in")
        if action == "channels":
            return {"channels": [{"name": c.name, "label": c.label, "count": len(JellyfinChannelFile.load(Path(c.path)).items)}
                                 for c in self.library.channels() if JellyfinChannelFile.exists(Path(c.path))]}
        if action == "channel":
            return self._jf_channel(p.get("channel", "")).as_dict()
        if action == "remove":
            cf = self._jf_channel(p.get("channel", ""))
            ids = p.get("ids") or []
            gone = cf.remove(ids if isinstance(ids, list) else [])
            cf.save()
            return {"removed": gone, "left": len(cf.items)}
        if action == "add":
            return self._jf_add(p)
        if action == "delete":
            return self._jf_delete(p.get("channel", ""))
        if action == "disconnect":
            self._jf_quick = None
            self.jf_credentials.forget()
            return self.jf_status()
        client = self._jf_client()
        if action == "libraries":
            return {"libraries": client.libraries()}
        if action == "items":
            return client.items(p.get("parent", ""), p.get("q", ""), start=int(p.get("start") or 0))
        if action == "episodes":
            return {"episodes": client.episodes(p.get("id", ""))}
        if action == "test":
            return client.check_stream(p.get("id", ""), self.youtube_height)
        raise jellyfin.JellyfinError("The TV doesn't know that.")

    # --- themes (see themes.py) ----------------------------------------------------------------------------------
    def _apply_theme(self) -> None:
        available = themes.all_themes(self.custom_themes)
        if self.theme_id not in available:
            self.theme_id = themes.DEFAULT_THEME             # a palette that was deleted, or a settings file from elsewhere
        themes.activate(available[self.theme_id])

    def _theme_changed(self) -> None:
        self._apply_theme()
        self._screen_dirty = True                           # an info screen on show redraws in the new colours
        if self.menu_open:
            self.render_menu()

    def theme_list(self) -> list:
        customs = {c.get("id") for c in self.custom_themes}
        return [dict(t.to_dict(), custom=t.id in customs) for t in themes.all_themes(self.custom_themes).values()]

    def theme_css(self) -> str:
        return themes.active().css()

    def theme_style(self) -> str:
        return themes.active().style

    def add_palette(self, name: str, text: str, light: bool, brutal: bool) -> dict:
        """Save a pasted palette and switch to it. Raises ValueError with a message fit for the page."""
        if len(self.custom_themes) >= themes.MAX_CUSTOM:
            raise ValueError(f"That is {themes.MAX_CUSTOM} palettes already; remove one first.")
        taken = list(themes.all_themes(self.custom_themes))
        entry = themes.make_custom(name, text, light, brutal, taken)
        self.custom_themes.append(entry)
        self.theme_id = entry["id"]
        self.settings.set(custom_themes=list(self.custom_themes), theme=self.theme_id)
        self._theme_changed()
        log.info("palette %r added from the web page and chosen", entry["name"])
        return entry

    def delete_palette(self, theme_id: str) -> None:
        if theme_id not in {c.get("id") for c in self.custom_themes}:
            raise ValueError("That is not one of your palettes.")
        self.custom_themes = [c for c in self.custom_themes if c.get("id") != theme_id]
        self.settings.set(custom_themes=list(self.custom_themes))
        if self.theme_id == theme_id:
            self.theme_id = themes.DEFAULT_THEME
            self.settings.set(theme=self.theme_id)
        self._theme_changed()

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
            entry = {"name": c.name, "label": c.label, "count": len(self.library.videos(c)),
                     "library": self.index.summary(c), "order": self.channel_order.get(c.name), "live": self.is_live(c)}
            if JellyfinChannelFile.exists(Path(c.path)):
                entry["kind"] = "jellyfin"
                entry["jellyfin"] = JellyfinChannelFile.load(Path(c.path)).as_dict()
                entry["jellyfin"].pop("items", None)
            channels.append(entry)
        return {"name": self.name, "greeting": self.cfg.boot_greeting, "channels": channels, "standby_custom": self.cfg.standby_custom.is_file(),
                "scan": self.index.status(), "settings": self.web_settings(), "static": self.statics.state(),
                "ads": dict(self.ads.state(), channel=self.cfg.ads.name),
                "themes": self.theme_list(), "theme": self.theme_id,
                "theme_css": self.theme_css(), "theme_style": self.theme_style()}

    # --- settings, status and logs for the web page ------------------------------------------------------------
    def web_settings(self) -> dict:
        return {"values": {"brightness": self.brightness, "shuffle": self.shuffle, "auto_dim_min": self.auto_dim_min,
                           "sleep_timer_min": self.sleep_timer_min, "youtube_height": self.youtube_height,
                           "youtube_lookahead": self.lookahead_enabled, "theme": self.theme_id,
                           "ads": self.ads.enabled, "ads_every": self.ads.every, "ytdlp_auto": bool(self.settings.get("ytdlp_auto", True)),
                           "night_mode": self.night.enabled, "night_brightness": self.night.level, "night_schedule": self.night.mode,
                           "night_location": ("%s, %s" % self.night.location) if self.night.location else "",
                           "night_from": daynight.format_clock(self.night.start), "night_to": daynight.format_clock(self.night.end)},
                "live": self.live_status() if self.live_channels else None,
                "night": dict(self.night.describe(time.time()), clock=self._clock_trusted(time.time())),
                "options": {"brightness": list(self.hw.brightness_levels), "auto_dim_min": list(AUTO_DIM_STEPS),
                            "sleep_timer_min": list(SLEEP_TIMER_STEPS), "youtube_height": list(HEIGHTS),
                            "theme": list(themes.all_themes(self.custom_themes)), "night_brightness": list(self.hw.brightness_levels),
                            "night_schedule": list(daynight.MODES), "ads_every": list(EVERY_OPTIONS)}}

    def set_channel_list(self, names: list) -> None:
        """The channels' numbers, set by dragging them into order on the web page: `names` is every channel, first is channel 1."""
        known = [c.name for c in self.library.channels()]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("That isn't a list of channels.")
        wanted = [n for n in dict.fromkeys(names) if n in known]
        if not wanted:
            raise ValueError("None of those are channels on this TV.")
        order = wanted + [n for n in known if n not in wanted]                 # any channel left out keeps its place at the end
        current = self.current_channel().name if known else None
        self.library.order = order
        self.settings.set(channel_list=order)
        if current in order:
            self.channel_index = order.index(current)                           # the TV stays on the channel it is on, whatever its number
        log.info("channel order set from the web page: %s", ", ".join(order))

    def set_channel_order(self, channel: str, order: str) -> None:
        """'shuffle', 'order', or 'default' (follow the TV's Shuffle setting) for one channel, from the web page."""
        if channel not in [c.name for c in self.library.channels()]:
            raise ValueError("No such channel.")
        if order not in ("default", "shuffle", "order"):
            raise ValueError("Play order is 'shuffle', 'order' or 'default'.")
        if order == "default":
            self.channel_order.pop(channel, None)
        else:
            self.channel_order[channel] = order
        self.settings.set(channel_order=dict(self.channel_order))
        log.info("channel %s plays %s", channel, {"default": "as the TV's Shuffle setting says", "shuffle": "shuffled", "order": "in order"}[order])

    def set_web_setting(self, key: str, value) -> None:
        """Change one of the settings the menu has, from the web page (the TV is in upload mode then, so the player is stopped)."""
        options = self.web_settings()["options"]
        if key == "youtube_lookahead":
            if not isinstance(value, bool):
                raise ValueError("That is on or off.")
            self.lookahead_enabled = value
            self.settings.set(youtube_lookahead=value)
        elif key == "ytdlp_auto":
            if not isinstance(value, bool):
                raise ValueError("That is on or off.")
            self.settings.set(ytdlp_auto=value)
        elif key == "ads":
            self.ads.set_enabled(value)                                       # the playlist built next has them (or not)
        elif key == "ads_every":
            self.ads.set_every(value)
        elif key in ("night_mode", "night_location", "night_from", "night_to"):
            if key == "night_mode":
                if not isinstance(value, bool):
                    raise ValueError("That is on or off.")
                self.night.enabled = value
                self.settings.set(night_mode=value)
            elif key == "night_location":
                loc = daynight.parse_location(value)                      # raises ValueError with a message for the page
                self.night.location = loc
                self.settings.set(night_location=list(loc) if loc else None)
            else:
                minutes = daynight.parse_clock(value)
                setattr(self.night, "start" if key == "night_from" else "end", minutes)
                self.settings.set(**{key: daynight.format_clock(minutes)})
            self._night_checked = 0.0
            self._night_step(time.time())
        elif key == "shuffle":
            if not isinstance(value, bool):
                raise ValueError("Shuffle is on or off.")
            self.shuffle = value
            self.settings.set(shuffle=value)
        elif key in options:
            if isinstance(value, bool) or value not in options[key]:
                raise ValueError(f"{value!r} isn't one of the choices for that setting.")
            if key == "brightness":
                self.brightness, self.dimmed = value, False
                self.backlight.set(self.current_brightness())
                self.settings.set(brightness=value)
            elif key == "night_brightness":
                self.night.level, self.dimmed = value, False
                self.backlight.set(self.current_brightness())
                self.settings.set(night_brightness=value)
            elif key == "night_schedule":
                self.night.mode = value
                self.settings.set(night_schedule=value)
                self._night_checked = 0.0
                self._night_step(time.time())
            elif key == "youtube_height":
                self.youtube_height = value
                self.settings.set(youtube_height=value)
                if hasattr(self.player, "youtube_height"):
                    self.player.youtube_height = value                      # streaming picks it up when playback restarts
                if self._ytdlp is not None and hasattr(self._ytdlp, "height"):
                    self._ytdlp.height = value
                if self.youtube is not None and hasattr(self.youtube.yt, "height"):
                    self.youtube.yt.height = value                          # and new downloads use it straight away
            elif key == "theme":
                self.theme_id = value
                self.settings.set(theme=value)
                self._theme_changed()
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

    def ads_download(self, url: str) -> None:
        """Download a YouTube video into the ads folder (from the web page): raises ValueError("why not")."""
        ytdlp = self._ytdlp or YtDlp(find_ytdlp(self.cfg.ytdlp_path))
        if hasattr(ytdlp, "height"):
            ytdlp.height = self.youtube_height
        self.ads.start_download(ytdlp, url)

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

    def set_greeting(self, text: str) -> str:
        return self.app.set_greeting(text)

    def standby_path(self) -> str:
        return self.app.cfg.standby_image

    # updates: UpdateError becomes ValueError so the web layer needs to know nothing about the updater
    def rescan(self, force: bool = False) -> dict:
        return self.app.rescan_library(force)

    def set_setting(self, key: str, value) -> None:
        self.app.set_web_setting(key, value)

    def set_channel_order(self, channel: str, order: str) -> None:
        self.app.set_channel_order(channel, order)

    # the ads between videos (see ads.py)
    @property
    def ads(self):
        return self.app.ads

    def ads_download(self, url: str) -> None:
        self.app.ads_download(url)

    def set_channel_list(self, names: list) -> None:
        self.app.set_channel_list(names)

    def theme_css(self) -> str:
        return self.app.theme_css()

    def theme_style(self) -> str:
        return self.app.theme_style()

    def jellyfin(self, action: str, params: dict):
        return self.app.jellyfin(action, params)

    def reshuffle_live(self, channel: str) -> None:
        self.app.reshuffle_live(channel)

    def set_channel_live(self, channel: str, live) -> None:
        self.app.set_channel_live(channel, live)

    def timezone_location(self) -> Optional[dict]:
        found = daynight.timezone_location()
        return {"zone": found[0], "location": "%s, %s" % found[1]} if found else None

    def add_palette(self, name: str, text: str, light: bool, brutal: bool) -> dict:
        return self.app.add_palette(name, text, light, brutal)

    def delete_palette(self, theme_id: str) -> None:
        self.app.delete_palette(theme_id)

    # the TV static (see statics.py)
    @property
    def statics(self):
        return self.app.statics

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

    def theme_css(self) -> str:
        return self.app.theme_css()

    def theme_style(self) -> str:
        return self.app.theme_style()

    def networks(self, rescan: bool):
        return self.app.setup_networks(rescan)

    def connect(self, ssid: str, password: str) -> None:
        self.app.setup_connect(ssid, password)
