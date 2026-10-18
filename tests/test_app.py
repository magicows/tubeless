import json
import time

import pytest

from rig import HW, Rig, wait_for
from tubeless.hardware import apply_affine, invert_affine


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.stop()


def test_starts_playing_the_first_channel_with_a_static_up_front(rig):
    s = rig.state
    assert s["channel"] == "1.cartoons" and s["mode"] == "play"
    assert s["playlist_pos"] in (0, 1) and s["playlist_count"] == 10      # 5 videos, each behind a static
    assert rig.player.playlist[0] == "/static_short.mp4"
    assert rig.backlight.percent == 100


def test_tap_pauses_and_resumes(rig):
    rig.tap()
    assert wait_for(lambda: rig.state["paused"] is True)
    rig.tap()
    assert wait_for(lambda: rig.state["paused"] is False)


def test_swipe_left_goes_to_the_next_video_through_a_static(rig):
    rig.player.command("set_property", "playlist-pos", 1)
    rig.swipe_left()
    assert wait_for(lambda: rig.state["playlist_pos"] == 2)
    assert rig.player.playlist[2] == "/static_short.mp4"


def test_swipe_right_from_the_first_video_wraps_to_the_last_one(rig):
    rig.player.command("set_property", "playlist-pos", 1)
    rig.swipe_right()
    assert wait_for(lambda: rig.state["playlist_pos"] == 8)               # the static in front of the last video


def test_swipe_up_and_down_change_channel_and_remember_it(rig):
    rig.swipe_up()
    assert wait_for(lambda: rig.state["channel"] == "2.nights")
    assert wait_for(lambda: any(c[0] == "loadfile" and str(c[1]).endswith("playlist.m3u") for c in rig.player.commands))
    assert ("loadfile", "/static_long.mp4", "replace") in rig.player.commands
    assert rig.app.settings.get("channel") == "2.nights"
    assert wait_for(lambda: len(rig.player.playlist) == 6)                 # the nights playlist: 3 videos
    rig.swipe_down()
    assert wait_for(lambda: rig.state["channel"] == "1.cartoons")


def test_long_press_opens_the_menu_and_it_hides_itself(rig):
    rig.open_menu()
    assert wait_for(lambda: rig.player.overlay is not None)                # drawing takes a moment on a slow machine
    assert wait_for(lambda: not rig.app.menu_open, timeout=8)             # auto-hide
    assert wait_for(lambda: rig.player.overlay is None)


def test_menu_rows_by_touch(rig):
    rig.open_menu()
    rig.tap_row(3)                                                         # Brightness
    assert wait_for(lambda: rig.state["brightness"] == 25)
    assert rig.backlight.percent == 25
    assert json.load(open(rig.cfg.data / "settings.json"))["brightness"] == 25
    rig.tap_row(3)
    assert wait_for(lambda: rig.state["brightness"] == 50)
    rig.tap_row(4)                                                         # Settings >
    assert wait_for(lambda: rig.state["menu_page"] == "settings")
    rig.tap_row(0)                                                         # < Back
    assert wait_for(lambda: rig.state["menu_page"] == "main")
    rig.tap(5, 5)                                                          # outside the panel
    assert wait_for(lambda: not rig.app.menu_open)


def test_menu_play_pause_and_next_rows(rig):
    rig.open_menu()
    rig.tap_row(0)
    assert wait_for(lambda: rig.state["paused"])
    rig.tap_row(0)
    assert wait_for(lambda: not rig.state["paused"])
    rig.player.command("set_property", "playlist-pos", 1)
    rig.tap_row(1)                                                         # Next video closes the menu and skips
    assert wait_for(lambda: not rig.app.menu_open and rig.state["playlist_pos"] == 2)


def test_menu_channel_row_switches_channel(rig):
    rig.open_menu()
    rig.swipe(320, 380, 320, 260, duration=0.15)                           # scroll so the channel rows are in view
    assert wait_for(lambda: rig.app.menu.top == 1)                         # 7 rows, 6 visible: scrolled to the bottom
    rig.tap_row(5)                                                         # the second channel (row 6 of the list)
    assert wait_for(lambda: rig.state["channel"] == "2.nights" and not rig.app.menu_open)


def test_swiping_scrolls_the_menu_and_taps_never_do(tmp_path):
    chans = {f"{i}.chan{i}": ["a.mp4"] for i in range(1, 12)}
    r = Rig(tmp_path, channels=chans)
    try:
        r.open_menu()
        assert r.app.menu.top == 0
        r.swipe(320, 380, 320, 200, duration=0.15)                         # swipe up: scroll down
        assert wait_for(lambda: r.app.menu.top > 0)
        top = r.app.menu.top
        r.tap_row(0)                                                       # a tap activates a row (Brightness) but must not scroll
        assert wait_for(lambda: r.state["brightness"] == 25)
        assert r.app.menu.top == top and r.app.menu_open
    finally:
        r.stop()


def test_settings_survive_a_restart(tmp_path):
    r = Rig(tmp_path, menu_timeout=30)                                     # a slow machine must not see the menu hide mid-test
    r.open_menu()
    r.tap_row(3)                                                           # brightness 100 -> 25
    assert wait_for(lambda: r.state["brightness"] == 25)
    r.tap_row(4)                                                           # settings
    assert wait_for(lambda: r.app.menu.page == "settings")
    r.tap_row(1)                                                           # shuffle off
    assert wait_for(lambda: r.state["shuffle"] is False)
    r.tap_row(4)                                                           # auto-dim
    assert wait_for(lambda: r.state["auto_dim_min"] == 2)
    saved = lambda: json.loads((tmp_path / "data" / "settings.json").read_text())      # the app saves just after it changes
    assert wait_for(lambda: saved().get("auto_dim_min") == 2 and saved().get("shuffle") is False and saved().get("brightness") == 25)
    r.stop()
    again = Rig(tmp_path, start=False)
    assert again.app.brightness == 25 and again.app.shuffle is False and again.app.auto_dim_min == 2


def test_resumes_the_episode_and_position_after_a_restart(tmp_path):
    r = Rig(tmp_path)
    ch = r.app.current_channel()
    r.app.shuffle = False
    r.app.settings.set(shuffle=False)
    r.app.reload_channel()
    r.player.command("set_property", "playlist-pos", 5)                    # third video
    r.player.command("seek", 120.0, "absolute")
    r.app.save_progress_now()
    r.stop()
    prog = r.app.playlists.progress(ch)
    assert prog["current"] == "c2.mp4" and prog["pos"] == 120.0

    again = Rig(tmp_path, start=False)
    again.app.startup()
    again.app.start_threads()
    try:
        assert again.player.playlist[1].endswith("c2.mp4")                 # rotated to start at the episode you were on
        assert wait_for(lambda: ("seek", 117.0, "absolute") in again.player.commands, timeout=5)   # rewound 3 s
    finally:
        again.stop()


def test_progress_is_recorded_as_episodes_change(tmp_path):
    r = Rig(tmp_path, settings={"shuffle": False})
    try:
        r.player.command("set_property", "playlist-pos", 3)
        assert wait_for(lambda: r.app.playlists.progress(r.app.current_channel()).get("current") == "c1.mp4", timeout=4)
        r.player.command("set_property", "playlist-pos", 5)
        assert wait_for(lambda: r.app.playlists.progress(r.app.current_channel()).get("current") == "c2.mp4", timeout=4)
    finally:
        r.stop()


def test_a_finished_playlist_starts_a_new_cycle(tmp_path):
    r = Rig(tmp_path, channels={"1.short": ["a.mp4", "b.mp4"]}, duration=lambda p: 0.3)
    try:
        cycle0 = r.app.plan.cycle
        assert wait_for(lambda: r.app.plan.cycle > cycle0, timeout=8)       # played through, reshuffled ...
        assert wait_for(lambda: r.player.alive(), timeout=3)                # ... and restarted (the plan is updated just before the player)
    finally:
        r.stop()


def test_skipping_forward_past_the_end_starts_a_new_cycle(rig):
    rig.player.command("set_property", "playlist-pos", 9)
    cycle0 = rig.app.plan.cycle
    rig.swipe_left()
    assert wait_for(lambda: rig.app.plan.cycle == cycle0 + 1)


# --- calibration --------------------------------------------------------------------
def test_first_run_starts_with_the_calibration_wizard(tmp_path):
    r = Rig(tmp_path, fresh=True)
    try:
        assert r.state["mode"] == "calibrate"
        assert r.player.overlay is not None
    finally:
        r.stop()


def test_first_run_with_no_videos_calibrates_on_the_display_then_shows_the_empty_screen(tmp_path):
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, fresh=True)
    try:
        assert wait_for(lambda: r.state["mode"] == "calibrate")
        assert wait_for(lambda: r.display.frame565 is not None)                 # drawn on the display itself, not an mpv overlay
        calibrate(r, lambda vx, vy: (vx, vy))
        assert wait_for(lambda: r.app.settings.get("touch_calibrated") is True, timeout=3)
        assert wait_for(lambda: r.state["mode"] == "empty", timeout=4)          # and back to 'No videos yet'
        assert r.app.settings.get("first_run_done") is True
    finally:
        r.stop()


def calibrate(r, panel_affine_view_to_raw_taps):
    from tubeless import render
    for vx, vy in render.calibration_targets(HW):
        r.tap(*panel_affine_view_to_raw_taps(vx, vy), settle=0.2)


def test_calibration_learns_a_different_touch_panel(tmp_path):
    # A panel whose axes disagree with the default map: rotated the other way, slightly scaled.
    weird = ((0.0, 650.0, -5.0), (-470.0, 0.0, 475.0))
    r = Rig(tmp_path, settings={}, panel_affine=weird)
    try:
        r.app.begin_calibration()
        assert wait_for(lambda: r.state["mode"] == "calibrate")
        calibrate(r, lambda vx, vy: (vx, vy))                    # the rig converts the *view* point into this panel's raw
        assert wait_for(lambda: r.app.settings.get("touch_calibrated") is True, timeout=3)
        for vx, vy in [(100, 100), (500, 300), (320, 240)]:
            rx, ry = apply_affine(invert_affine(weird), vx, vy)
            got = apply_affine(r.app.affine, rx, ry)
            assert got == pytest.approx((vx, vy), abs=1.5)
        assert wait_for(lambda: r.state["mode"] == "play", timeout=4)        # wizard finishes by itself
        assert r.app.settings.get("touch_affine") is not None
    finally:
        r.stop()


def test_calibration_rejects_inconsistent_taps(tmp_path):
    r = Rig(tmp_path, settings={})
    try:
        r.app.begin_calibration()
        wait_for(lambda: r.state["mode"] == "calibrate")
        for vx, vy in [(320, 240), (100, 100), (500, 400), (100, 400), (500, 100)]:     # nowhere near the targets
            r.tap(vx, vy, settle=0.15)
        time.sleep(0.4)
        assert r.app.settings.get("touch_calibrated") is None
        assert r.state["mode"] == "calibrate" and r.app._calib["step"] == 0           # asks again from the top
    finally:
        r.stop()


def test_long_press_cancels_calibration(tmp_path):
    r = Rig(tmp_path, settings={})
    try:
        r.app.begin_calibration()
        wait_for(lambda: r.state["mode"] == "calibrate")
        r.hold(secs=0.5)
        assert wait_for(lambda: r.state["mode"] == "play")
        assert r.app.settings.get("touch_calibrated") is None
    finally:
        r.stop()


def test_menu_calibrate_row_starts_the_wizard(rig):
    rig.open_menu()
    rig.tap_row(4)
    rig.tap_row(5)                                                         # Calibrate touch
    assert wait_for(lambda: rig.state["mode"] == "calibrate")


# --- sleep / dim / shutdown ---------------------------------------------------------
def test_sleep_timer_puts_the_tv_to_sleep_and_a_touch_wakes_it(rig):
    rig.app.sleep_deadline = time.time() + 0.2
    assert wait_for(lambda: rig.state["mode"] == "standby", timeout=3)
    assert rig.backlight.percent == 0 and rig.state["paused"]
    paused_before = rig.player.commands.count(("cycle", "pause"))
    rig.tap()                                                              # wakes it ...
    assert wait_for(lambda: rig.state["mode"] == "play" and not rig.state["paused"])
    assert rig.backlight.percent == 100
    assert rig.player.commands.count(("cycle", "pause")) == paused_before   # ... and that tap did nothing else


def test_auto_dim_dims_then_a_touch_restores_without_acting(rig):
    rig.app.auto_dim_min = 1
    rig.app.last_activity = time.time() - 61
    assert wait_for(lambda: rig.state["dimmed"], timeout=3)
    assert rig.backlight.percent == 25
    rig.tap()
    assert wait_for(lambda: not rig.state["dimmed"] and rig.backlight.percent == 100)
    assert rig.state["paused"] is False


def test_a_paused_tv_dims_by_itself_but_a_playing_one_does_not(rig):
    rig.app.last_activity = time.time() - 61                               # a minute untouched, and the default pause-dim is a minute
    time.sleep(0.4)
    assert not rig.state["dimmed"]                                         # playing, auto-dim off: stays bright
    rig.tap()                                                              # pause
    assert wait_for(lambda: rig.state["paused"])
    rig.app.last_activity = time.time() - 61
    assert wait_for(lambda: rig.state["dimmed"], timeout=3)
    assert rig.backlight.percent == 25
    rig.tap()                                                              # wakes without un-pausing
    assert wait_for(lambda: not rig.state["dimmed"] and rig.backlight.percent == 100)
    assert rig.state["paused"] is True


def test_the_pause_dim_can_be_switched_off(tmp_path):
    r = Rig(tmp_path, pause_dim_seconds=0)
    try:
        r.tap()
        assert wait_for(lambda: r.state["paused"])
        r.app.last_activity = time.time() - 3600
        time.sleep(0.5)
        assert not r.state["dimmed"]
    finally:
        r.stop()


def test_shutdown_needs_a_second_tap_to_confirm(rig):
    rig.open_menu()
    rig.tap_row(4)                                                         # Settings
    rig.swipe(320, 380, 320, 200, duration=0.15)                           # scroll down to reach "Shut down"
    assert wait_for(lambda: rig.app.menu.top > 0)
    rig.tap_row(5)
    assert wait_for(lambda: rig.app.confirm_shutdown_until > time.time())  # first tap only asks ...
    assert not rig.power.shutdown_requested
    rig.tap_row(5)
    assert wait_for(lambda: rig.power.shutdown_requested)
    assert rig.state["mode"] == "shutdown"
    assert wait_for(lambda: rig.display.frame565 is not None)             # SYSTEM HALT is on the display, not only an mpv overlay


def test_gestures_are_ignored_while_shutting_down(rig):
    rig.app.do_shutdown()
    before = list(rig.player.commands)
    rig.tap()
    rig.swipe_left()
    time.sleep(0.3)
    assert rig.player.commands == before


# --- resilience ------------------------------------------------------------------------
def test_empty_channels_are_skipped_when_swiping(tmp_path):
    r = Rig(tmp_path, channels={"1.a": ["x.mp4"], "2.empty": ["readme.txt"], "3.c": ["y.mp4"]})
    try:
        r.swipe_up()
        assert wait_for(lambda: r.state["channel"] == "3.c")
        r.swipe_down()
        assert wait_for(lambda: r.state["channel"] == "1.a")
    finally:
        r.stop()


def test_starts_on_the_first_playable_channel_if_the_saved_one_is_empty(tmp_path):
    r = Rig(tmp_path, channels={"1.a": ["x.txt"], "2.b": ["y.mp4"]}, settings={"channel": "1.a"})
    try:
        assert r.state["channel"] == "2.b"
    finally:
        r.stop()


def test_a_second_finger_cancels_the_gesture(rig):
    rig.backend.touch.feed(1, *rig._raw(320, 240))
    rig.backend.touch.feed(2)
    rig.up()
    time.sleep(0.3)
    assert rig.state["paused"] is False and rig.state["last_gesture"] is None


def test_imports_an_older_installs_settings(tmp_path):
    (tmp_path / "old.json").write_text(json.dumps({"brightness": 50, "shuffle": False, "channel": "2.nights"}))
    r = Rig(tmp_path, fresh=True, start=False, legacy_state_file=str(tmp_path / "old.json"))
    assert r.app.brightness == 50 and r.app.shuffle is False
    r.app.startup()
    try:
        assert r.state["channel"] == "2.nights" and r.state["mode"] == "play"        # an existing TV doesn't get the wizard
    finally:
        r.stop()


# --- first run: the player may not be ready for the wizard's overlay yet -------------------
def test_the_wizard_still_appears_if_the_player_is_not_ready_at_first(tmp_path):
    from tubeless.player import FakePlayer

    class SlowStarter(FakePlayer):
        """Refuses overlays for the first second, like mpv before its IPC socket exists."""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.ready_at = time.time() + 1.0

        def set_overlay(self, raw, w, h):
            if time.time() < self.ready_at:
                raise FileNotFoundError(2, "No such file or directory")
            super().set_overlay(raw, w, h)

    r = Rig(tmp_path, fresh=True, start=False)
    r.player = SlowStarter(r.cfg.short_static, r.cfg.long_static, duration_of=lambda p: 1000.0)
    r.app.player = r.player
    r.app.tracker_every = 0.05
    r.app.start_background()
    try:
        assert r.state["mode"] == "calibrate"
        assert r.player.overlay is None                                     # not shown at first ...
        assert wait_for(lambda: r.player.overlay is not None, timeout=5)    # ... but it does appear once the player is ready
    finally:
        r.stop()


def test_a_picture_from_the_web_page_becomes_the_standby_picture_until_reset(rig):
    import io
    from PIL import Image
    assert rig.cfg.standby_image.endswith("standby.jpg") and not rig.app.web_state()["standby_custom"]
    buf = io.BytesIO()
    Image.new("RGB", (50, 30), (200, 10, 10)).save(buf, "PNG")
    rig.app.set_standby(buf.getvalue())
    assert rig.app.web_state()["standby_custom"] and rig.cfg.standby_image == str(rig.cfg.standby_custom)
    with pytest.raises(ValueError):
        rig.app.set_standby(b"nope")
    rig.app.reset_standby()
    assert not rig.app.web_state()["standby_custom"]


def test_settings_changed_from_the_web_page_apply_and_stick(rig):
    app = rig.app
    st = app.web_settings()
    assert st["options"]["brightness"] == [25, 50, 75, 100] and st["values"]["brightness"] == 100
    app.set_web_setting("brightness", 50)
    app.set_web_setting("shuffle", False)
    app.set_web_setting("auto_dim_min", 5)
    app.set_web_setting("sleep_timer_min", 30)
    assert rig.backlight.percent == 50 and app.sleep_deadline
    assert json.loads((rig.cfg.data / "settings.json").read_text())["auto_dim_min"] == 5
    for key, bad in (("brightness", 33), ("auto_dim_min", True), ("shuffle", "yes"), ("password", 1)):
        with pytest.raises(ValueError):
            app.set_web_setting(key, bad)


def test_status_and_recent_log_for_the_web_page(rig):
    assert "python" in rig.app.system_status()
    log_file = rig.cfg.data / "tubeless.log"
    rig.cfg.log_file = str(log_file)
    log_file.write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
    out = rig.app.recent_log(3)
    assert out == "line 497\nline 498\nline 499"
    assert len(rig.app.recent_log(10_000).splitlines()) == 200


def test_a_player_that_keeps_dying_at_once_is_not_restarted_in_a_tight_loop(tmp_path):
    r = Rig(tmp_path)
    try:
        starts = []
        r.player.alive = lambda: False                              # mpv exits straight after every start (a stream that won't open)
        r.player.finished_normally = lambda: True                   # ... and calls it a clean exit, which is what mpv does
        real_start = r.player.start
        r.player.start = lambda path: (starts.append(1), real_start(path))[1]
        r.app.fast_death_seconds, r.app.restart_backoff = 60.0, 30.0
        time.sleep(1.5)
        assert 3 <= len(starts) <= 5                                # 3 quick retries, then it waits (it was dozens a second before)
        assert r.app._fast_restarts >= 3 and r.app.plan.cycle <= 1     # and it did not burn through new shuffle cycles (the first death looks like a real finish)
        assert r.display.frame565 is not None                       # the 'can't keep playing' screen is up
        n = len(starts)
        r.swipe_up()                                                # change channel: try again at once
        assert wait_for(lambda: len(starts) > n, timeout=2)
    finally:
        r.stop()
