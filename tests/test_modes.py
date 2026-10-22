import hashlib
import json
import time

import pytest

from rig import HW, Rig, http, wait_for
from tubeless import render


def button_centre(name):
    x0, y0, x1, y1 = render.EMPTY_BUTTONS[name]
    return (x0 + x1) / 2, (y0 + y1) / 2


def frame_hash(rig):
    return hashlib.md5(rig.display.frame565.tobytes()).hexdigest()


@pytest.fixture
def empty(tmp_path):
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]})
    yield r
    r.stop()


def page(rig):
    """(base url, token) for the upload page, using the PIN the TV is showing."""
    info = rig.app.web_info
    base = f"http://127.0.0.1:{info['port']}"
    status, body = http(base, "POST", "/api/login", {"pin": info["pin"]})
    assert status == 200
    return base, body["token"]


# ---- "no videos yet" -------------------------------------------------------------------------------------------
def test_no_videos_shows_the_empty_screen_instead_of_crashing(empty):
    assert empty.state["mode"] == "empty"
    assert not empty.player.alive()
    assert empty.display.frame565 is not None and empty.display.frame565.any()
    assert empty.backlight.percent == 100


def test_tapping_elsewhere_on_the_empty_screen_does_nothing(empty):
    empty.tap(60, 60)
    empty.tap(320, 250)
    time.sleep(0.3)
    assert empty.state["mode"] == "empty"


def test_check_again_with_still_no_videos_says_so(empty):
    before = frame_hash(empty)
    empty.tap(*button_centre("recheck"))
    assert wait_for(lambda: frame_hash(empty) != before)                     # the notice was drawn
    assert empty.state["mode"] == "empty"


def test_check_again_after_copying_videos_in_starts_the_tv(empty, tmp_path):
    (tmp_path / "videos" / "1.nothing" / "copied.mp4").write_bytes(b"x")
    empty.tap(*button_centre("recheck"))
    assert wait_for(lambda: empty.state["mode"] == "play" and empty.player.alive())


# ---- the upload page ---------------------------------------------------------------------------------------------
def test_the_upload_button_starts_the_page_and_shows_address_and_pin(empty):
    assert empty.app.web is None
    empty.tap(*button_centre("upload"))
    assert wait_for(lambda: empty.state["mode"] == "transfer")
    info = empty.state["upload_page"]
    assert len(info["pin"]) == 4 and info["pin"].isdigit()
    assert info["urls"][0].startswith("http://") and info["urls"][1].endswith(".local:%d" % info["port"])
    assert http(f"http://127.0.0.1:{info['port']}", "GET", "/")[0] == 200


def test_phone_flow_rename_channel_upload_then_hold_to_watch(empty, tmp_path):
    empty.tap(*button_centre("upload"))
    assert wait_for(lambda: empty.state["mode"] == "transfer")
    base, token = page(empty)

    assert http(base, "GET", "/api/state")[0] == 401                          # nothing without the PIN
    status, state = http(base, "GET", "/api/state", token=token)
    assert state["name"] == "Tubeless" and state["free_bytes"] > 0

    assert http(base, "POST", "/api/name", {"name": "Den"}, token)[1] == {"name": "Den"}
    assert empty.app.name == "Den"
    assert json.loads((tmp_path / "config.json").read_text())["name"] == "Den"
    assert http(base, "POST", "/api/name", {"name": "x" * 40}, token)[0] == 400
    assert empty.app.name == "Den"

    assert http(base, "POST", "/api/channel", {"name": "1.cartoons"}, token)[1] == {"channel": "1.cartoons"}
    q = "/api/upload?channel=1.cartoons&name=Pilot.mp4&total=3000"
    assert http(base, "PUT", q + "&offset=0", token=token, raw=b"a" * 1500)[1]["done"] is False
    status, done = http(base, "PUT", q + "&offset=1500", token=token, raw=b"b" * 1500)
    assert status == 200 and done["done"] and done["advice"]
    assert (tmp_path / "videos" / "1.cartoons" / "Pilot.mp4").stat().st_size == 3000

    empty.hold()                                                               # hold the TV's screen to finish
    assert wait_for(lambda: empty.state["mode"] == "play" and empty.player.alive(), timeout=6)
    assert empty.state["channel"] == "1.cartoons"
    assert empty.app.web is None and empty.state["upload_page"] is None
    with pytest.raises(Exception):                                             # the page is gone again
        http(base, "GET", "/", )


def test_the_tv_screen_shows_upload_progress(empty):
    empty.tap(*button_centre("upload"))
    assert wait_for(lambda: empty.state["mode"] == "transfer")
    base, token = page(empty)
    waiting = frame_hash(empty)
    q = "/api/upload?channel=big&name=Big.mp4&total=100000"
    http(base, "PUT", q + "&offset=0", token=token, raw=b"x" * 40000)
    assert wait_for(lambda: empty.app.uploads.snapshot()["active"])
    assert wait_for(lambda: frame_hash(empty) != waiting, timeout=4)           # the TV redraws with the progress


def test_while_uploading_only_a_long_press_leaves_the_mode(empty):
    empty.tap(*button_centre("upload"))
    assert wait_for(lambda: empty.state["mode"] == "transfer")
    empty.tap()
    empty.swipe_left()
    empty.swipe_up()
    time.sleep(0.3)
    assert empty.state["mode"] == "transfer"
    empty.hold()
    assert wait_for(lambda: empty.state["mode"] == "empty")                    # nothing uploaded: back to the empty screen
    assert empty.app.web is None


# ---- upload mode launched from the menu while the TV is playing ---------------------------------------------------
def test_upload_from_the_menu_stops_playback_and_resumes_afterwards(tmp_path):
    r = Rig(tmp_path)
    try:
        assert r.state["mode"] == "play" and r.player.alive()
        r.open_menu()
        r.tap_row(4)                                                           # Settings
        r.swipe(320, 380, 320, 200, duration=0.15)                             # scroll to the bottom of the settings page
        assert wait_for(lambda: r.app.menu.top == 2)
        r.tap_row(4)                                                           # Upload videos
        assert wait_for(lambda: r.state["mode"] == "transfer")
        assert not r.player.alive() and not r.app.menu_open
        assert wait_for(lambda: r.display.frame565 is not None)
        r.hold()
        assert wait_for(lambda: r.state["mode"] == "play" and r.player.alive(), timeout=6)
    finally:
        r.stop()


def test_a_port_that_is_taken_is_reported_not_fatal(tmp_path):
    import socket
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, web_port=blocker.getsockname()[1])
    try:
        r.tap(*button_centre("upload"))
        time.sleep(0.8)
        assert r.state["mode"] == "empty" and r.app.web is None
    finally:
        r.stop()
        blocker.close()


def test_boot_screen_steps_aside_when_there_are_no_videos(tmp_path, capsys, monkeypatch):
    from tubeless import bootscreen
    from tubeless.config import Config
    from tubeless.hal import pi
    from tubeless.hardware import load_hardware
    cfg = Config(videos_dir=str(tmp_path / "videos"))
    (tmp_path / "videos").mkdir()
    fb = tmp_path / "fb"; fb.write_text("")
    sysdir = tmp_path / "sys"; sysdir.mkdir(); (sysdir / "virtual_size").write_text("480,640")
    monkeypatch.setattr(pi, "FB_DEV", str(fb)); monkeypatch.setattr(pi, "FB_SYS", str(sysdir))
    monkeypatch.setattr(pi, "Framebuffer", lambda hw: (_ for _ in ()).throw(AssertionError("drew on the screen")))
    bootscreen.run_real(cfg, load_hardware(cfg.hardware, cfg.hardware_overrides))
    assert "no videos" in capsys.readouterr().out


def test_the_greeting_is_saved_for_the_next_boot(tmp_path):
    from tubeless.config import DEFAULT_GREETING
    rig = Rig(tmp_path)
    try:
        assert rig.app.set_greeting("Wake up, Homer...") == "Wake up, Homer..."
        assert json.loads(rig.app.config_path.read_text())["boot_greeting"] == "Wake up, Homer..."
        assert rig.app.cfg.boot_greeting == "Wake up, Homer..."
        assert rig.app.set_greeting("") == DEFAULT_GREETING
        assert "boot_greeting" not in json.loads(rig.app.config_path.read_text())
    finally:
        rig.stop()
