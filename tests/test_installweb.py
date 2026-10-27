import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tubeless import installer, installweb
from tubeless.installweb import InstallPage, Progress, picture_kind

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


def make(tmp_path, pin="4321"):
    ctx = SimpleNamespace(name="Tubeless", with_youtube=False, home=tmp_path / "home")
    progress = Progress(["Checking", "Installing packages", "Installing Tubeless", "YouTube"])
    page = InstallPage(ctx, progress, pin=pin, host="127.0.0.1", ports=[0])
    page.start()
    page.base = f"http://127.0.0.1:{page.httpd.server_address[1]}"
    return ctx, progress, page


def call(page, method, path, body=None, token=None):
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(page.base + path, data=data, method=method)
    if token:
        req.add_header("X-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def login(page):
    return call(page, "POST", "/api/login", {"pin": page.pin})[1]["token"]


@pytest.fixture
def stack(tmp_path):
    ctx, progress, page = make(tmp_path)
    yield ctx, progress, page
    page.stop()


def test_the_page_is_served_but_everything_else_needs_the_pin(stack):
    ctx, progress, page = stack
    status, html = call(page, "GET", "/")
    assert status == 200 and b"Setting up your TV" in html
    for method, path in (("GET", "/api/state"), ("POST", "/api/settings"), ("POST", "/api/picture")):
        assert call(page, method, path, b"{}" if method == "POST" else None)[0] == 401
    assert call(page, "POST", "/api/login", {"pin": "0000"})[0] == 403
    assert call(page, "POST", "/api/login", {"pin": "4321"})[0] == 200


def test_progress_and_the_log_show_up(stack):
    ctx, progress, page = stack
    token = login(page)
    progress.begin(0)
    progress.end(0)
    progress.begin(1)
    progress.say("Unpacking mpv\nSetting up pigpio")
    state = call(page, "GET", "/api/state", token=token)[1]
    assert [s["state"] for s in state["steps"]] == ["done", "running", "waiting", "waiting"]
    assert state["log"][-2:] == ["Unpacking mpv", "Setting up pigpio"] and state["finished"] is None
    assert state["name"] == "Tubeless" and state["youtube"] is False and state["picture"] is False
    for i in range(200):
        progress.say(f"line {i}")
    assert len(call(page, "GET", "/api/state", token=token)[1]["log"]) == installweb.LOG_LINES                  # only the end is kept


def test_the_name_and_youtube_can_be_set_until_the_installer_uses_them(stack):
    ctx, progress, page = stack
    token = login(page)
    assert call(page, "POST", "/api/settings", {"name": "  Living Room "}, token)[0] == 200 and ctx.name == "Living Room"
    assert call(page, "POST", "/api/settings", {"youtube": True}, token)[0] == 200 and ctx.with_youtube is True
    assert call(page, "POST", "/api/settings", {"name": ""}, token)[0] == 400
    assert call(page, "POST", "/api/settings", {"name": "x" * 17}, token)[0] == 400
    assert call(page, "POST", "/api/settings", {"youtube": "yes"}, token)[0] == 400
    progress.lock("name", "It was used already.")
    status, body = call(page, "POST", "/api/settings", {"name": "Late"}, token)
    assert status == 400 and "used already" in body["error"] and ctx.name == "Living Room"                      # too late: the installer has used it
    assert call(page, "GET", "/api/state", token=token)[1]["locked"] == {"name": "It was used already."}
    progress.lock("youtube", "That step has started.")
    assert call(page, "POST", "/api/settings", {"youtube": False}, token)[0] == 400 and ctx.with_youtube is True


def test_a_standby_picture_is_checked_and_saved_for_the_tv(stack, tmp_path):
    ctx, progress, page = stack
    token = login(page)
    dest = tmp_path / "home" / ".local" / "share" / "tubeless" / "standby.jpg"
    assert call(page, "POST", "/api/picture", JPEG, token)[0] == 200 and dest.read_bytes() == JPEG
    assert call(page, "POST", "/api/picture", PNG, token)[0] == 200 and dest.read_bytes() == PNG                 # PIL opens it by content
    assert call(page, "GET", "/api/state", token=token)[1]["picture"] is True
    assert call(page, "POST", "/api/picture", b"GIF89a....", token)[0] == 400 and dest.read_bytes() == PNG       # not a picture: kept the old one
    assert call(page, "POST", "/api/picture", b"", token)[0] == 400
    assert not list(dest.parent.glob("*.part"))
    assert picture_kind(JPEG) == "jpeg" and picture_kind(PNG) == "png" and picture_kind(b"hello") == ""


def test_it_tries_the_next_port_and_reports_when_none_is_free(tmp_path):
    ctx, progress, first = make(tmp_path)
    try:
        taken = first.httpd.server_address[1]
        second = InstallPage(ctx, progress, host="127.0.0.1", ports=[taken, 0])
        assert second.start() != taken
        second.stop()
        third = InstallPage(ctx, progress, host="127.0.0.1", ports=[taken])
        with pytest.raises(OSError):
            third.start()
    finally:
        first.stop()


# ---- driven by the installer ---------------------------------------------------------------------------------------------------
def test_the_installer_drives_the_page_and_locks_each_field_when_its_step_starts(tmp_path, monkeypatch):
    seen = {}
    gate, resume = threading.Event(), threading.Event()

    def step_check(ctx):
        gate.set()
        assert resume.wait(10)

    def step_app(ctx):
        seen["name_used"] = ctx.name

    def step_youtube(ctx):
        seen["youtube_used"] = ctx.with_youtube

    monkeypatch.setattr(installer, "step_app", step_app)
    monkeypatch.setattr(installer, "step_youtube", step_youtube)
    monkeypatch.setattr(installer, "STEPS", [("Checking", step_check), ("Installing Tubeless", step_app), ("YouTube", step_youtube)])
    monkeypatch.setattr(installweb, "LINGER", 0.0)
    pages = []
    real = installer.start_install_page
    monkeypatch.setattr(installer, "start_install_page", lambda ctx: pages.append(real(ctx)) or pages[-1])
    runner = installer.Runner(dry_run=False, say=lambda t: None)
    ctx = installer.Context(user="nobody", home=tmp_path / "home", source=tmp_path, name="Tubeless", runner=runner, skip_sd_test=True)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", installer.install_all(ctx, live=True)))
    t.start()
    assert gate.wait(10)
    page, progress = pages[0]
    page.base = f"http://127.0.0.1:{page.port}"
    token = call(page, "POST", "/api/login", {"pin": page.pin})[1]["token"]
    assert call(page, "POST", "/api/settings", {"name": "Den", "youtube": True}, token)[0] == 200                # set during the long step
    state = call(page, "GET", "/api/state", token=token)[1]
    assert state["steps"][0]["state"] == "running" and state["locked"] == {}
    resume.set()
    t.join(15)
    assert result["rc"] == 0 and seen == {"name_used": "Den", "youtube_used": True}
    assert progress.finished["ok"] is True and [s["state"] for s in progress.snapshot()["steps"]] == ["done"] * 3
    assert set(progress.locked) == {"name", "youtube"}


def test_a_failed_step_is_shown_on_the_page(tmp_path, monkeypatch):
    def bad(ctx):
        raise installer.InstallError("the card is full")

    monkeypatch.setattr(installer, "STEPS", [("Checking", bad)])
    monkeypatch.setattr(installweb, "LINGER", 0.0)
    pages = []
    real = installer.start_install_page
    monkeypatch.setattr(installer, "start_install_page", lambda ctx: pages.append(real(ctx)) or pages[-1])
    ctx = installer.Context(user="nobody", home=tmp_path / "home", source=tmp_path, runner=installer.Runner(say=lambda t: None))
    assert installer.install_all(ctx, live=True) == 1
    progress = pages[0][1]
    assert progress.finished == {"ok": False, "message": "the card is full"} and progress.snapshot()["steps"][0]["state"] == "failed"


def test_the_page_is_off_for_a_dry_run_and_when_asked_and_the_option_exists(monkeypatch):
    seen = {}
    monkeypatch.setattr(installer, "install_all", lambda ctx, live=False: (seen.update(live=live), 0)[1])
    monkeypatch.setattr(installer, "resolve_user", lambda requested: ("sam", installer.Path("/tmp")))
    installer.main(["all", "--dry-run"])
    assert seen["live"] is False
    installer.main(["all", "--no-page"])
    assert seen["live"] is False
    installer.main(["all"])
    assert seen["live"] is True


def test_command_output_is_passed_to_the_log_now_and_then(tmp_path):
    runner = installer.Runner(say=lambda t: None)
    lines = []
    runner.tee = lines.append
    runner.run("sh", "-c", "echo first; sleep 1.2; echo second")
    assert lines[0] == "first" and lines[-1] == "second"
