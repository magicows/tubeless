import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tubeless import web
from tubeless.web import Session, UploadError, UploadManager, WebServer, sanitize_channel, sanitize_filename


# ---- names --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,clean", [
    ("Episode 1.mp4", "Episode 1.mp4"),
    ("../../etc/passwd.mp4", "passwd.mp4"),
    ("C:\\Users\\me\\movie.mkv", "movie.mkv"),
    (".hidden.mp4", "hidden.mp4"),
    ("a/b/c.mp4", "c.mp4"),
    ("bad:name?.mp4", "bad_name_.mp4"),
    ("line\nbreak.mp4", "line_break.mp4"),
    ("   ", None), ("", None), ("..", None), ("/", None),
])
def test_filenames_are_made_safe(raw, clean):
    assert sanitize_filename(raw) == clean


def test_long_filenames_keep_their_extension():
    out = sanitize_filename("x" * 400 + ".mp4")
    assert len(out) <= web.MAX_FILENAME and out.endswith(".mp4")


@pytest.mark.parametrize("raw,clean", [
    ("1.cartoons", "1.cartoons"), ("My Shows", "My Shows"), ("../up", ".._up".strip(". ")), ("a/b", "a_b"),
    ("..", None), (".", None), ("", None), ("x" * 41, None),
])
def test_channel_names(raw, clean):
    assert sanitize_channel(raw) == clean


# ---- upload manager ------------------------------------------------------------------------------------
class Stream:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n):
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


@pytest.fixture
def mgr(tmp_path):
    root = tmp_path / "videos"
    root.mkdir()
    return UploadManager(root, reserve=0)


def send(mgr, channel, name, data, offset=0, total=None):
    return mgr.receive(channel, name, offset, total or len(data), Stream(data), len(data))


def test_a_file_arrives_in_chunks_and_only_appears_when_complete(mgr):
    data = b"0123456789" * 100
    r1 = send(mgr, "1.shows", "Ep.mp4", data[:400], 0, 1000)
    assert r1 == {"received": 400, "done": False}
    assert (mgr.root / "1.shows" / "Ep.mp4.part").exists() and not (mgr.root / "1.shows" / "Ep.mp4").exists()
    r2 = send(mgr, "1.shows", "Ep.mp4", data[400:], 400, 1000)
    assert r2["done"] and r2["file"] == "Ep.mp4"
    assert (mgr.root / "1.shows" / "Ep.mp4").read_bytes() == data
    assert not list((mgr.root / "1.shows").glob("*.part"))
    assert mgr.snapshot()["completed"][0]["file"] == "Ep.mp4" and not mgr.snapshot()["active"]


def test_resume_after_a_dropped_connection(mgr):
    data = b"x" * 1000
    with pytest.raises(UploadError) as e:                                   # only 300 of the 500 promised bytes arrive
        mgr.receive("c", "a.mp4", 0, 1000, Stream(data[:300]), 500)
    assert e.value.extra["received"] == 300
    assert mgr.received("c", "a.mp4") == 300
    with pytest.raises(UploadError) as e:                                   # the client must resume at 300, not 500
        send(mgr, "c", "a.mp4", data[500:], 500, 1000)
    assert e.value.status == 409 and e.value.extra["received"] == 300
    send(mgr, "c", "a.mp4", data[300:700], 300, 1000)
    assert send(mgr, "c", "a.mp4", data[700:], 700, 1000)["done"]
    assert (mgr.root / "c" / "a.mp4").read_bytes() == data


def test_duplicate_names_are_not_overwritten(mgr):
    send(mgr, "c", "a.mp4", b"first")
    r = send(mgr, "c", "a.mp4", b"second!")
    assert r["file"] == "a (2).mp4"
    assert (mgr.root / "c" / "a.mp4").read_bytes() == b"first"


@pytest.mark.parametrize("channel,name", [("c", "notes.txt"), ("c", "script.sh"), ("c", "x.mp4.exe"), ("c", "")])
def test_only_video_files_are_accepted(mgr, channel, name):
    with pytest.raises(UploadError) as e:
        send(mgr, channel, name, b"data")
    assert e.value.status in (400, 415)


@pytest.mark.parametrize("channel", ["../outside", "..", "a/../../b", "", "x" * 50])
def test_a_channel_can_never_escape_the_videos_folder(mgr, channel, tmp_path):
    try:
        send(mgr, channel, "a.mp4", b"data")
    except UploadError:
        pass
    assert not list(tmp_path.glob("*.mp4")) and not list(tmp_path.parent.glob("outside*"))
    assert all(p.resolve().parent == mgr.root.resolve() for p in mgr.root.iterdir())


def test_traversal_in_the_file_name_is_flattened(mgr):
    r = send(mgr, "c", "../../evil.mp4", b"data")
    assert r["file"] == "evil.mp4" and (mgr.root / "c" / "evil.mp4").exists()


def test_not_enough_free_space_is_refused_before_anything_is_written(tmp_path):
    m = UploadManager(tmp_path / "v", reserve=0, disk_free=lambda p: 1000)
    with pytest.raises(UploadError) as e:
        m.receive("c", "big.mp4", 0, 5_000_000, Stream(b"x" * 10), 10)
    assert e.value.status == 507 and "Not enough free space" in e.value.message
    assert not list((tmp_path / "v").rglob("*.part"))


def test_bad_chunk_parameters(mgr):
    for offset, total, length in [(0, 0, 5), (0, 5, 0), (3, 5, 5), (-1, 5, 5), (0, 5, web.MAX_CHUNK + 1)]:
        with pytest.raises(UploadError):
            mgr.receive("c", "a.mp4", offset, total, Stream(b"x" * 5), length)


def test_uploading_to_a_missing_channel_midway_is_an_error_but_offset_zero_creates_it(mgr):
    with pytest.raises(UploadError) as e:
        send(mgr, "nope", "a.mp4", b"abc", 1, 6)
    assert e.value.status == 404
    send(mgr, "fresh", "a.mp4", b"abc", 0, 6)
    assert (mgr.root / "fresh").is_dir()


# ---- the PIN ---------------------------------------------------------------------------------------------
def test_pin_login_and_lockout():
    s = Session(pin="1234")
    assert s.login("0000") == (None, 0)
    token, _ = s.login("1234")
    assert token and s.valid(token) and not s.valid("forged") and not s.valid("")
    s2 = Session(pin="1234")
    for _ in range(web.LOGIN_ATTEMPTS - 1):
        assert s2.login("9999") == (None, 0)
    _, wait = s2.login("9999")
    assert wait == web.LOCKOUT_SECONDS
    assert s2.login("1234")[0] is None                                      # even the right PIN is refused while locked out


def test_generated_pins_are_four_digits():
    for _ in range(50):
        pin = Session().pin
        assert len(pin) == 4 and pin.isdigit()


# ---- over HTTP, like a phone would -------------------------------------------------------------------------
class Hooks:
    def __init__(self):
        self.name, self.uploads = "Test TV", []

    def state(self):
        return {"name": self.name, "channels": [{"name": "1.shows", "label": "shows", "count": 2}]}

    def set_name(self, name):
        if not name.strip():
            raise ValueError("The name can't be empty.")
        self.name = name.strip()
        return self.name

    def uploaded(self, info):
        self.uploads.append(info)

    def standby_path(self):
        return str(Path(web.__file__).parent / "assets" / "standby.jpg")

    def set_standby(self, data):
        if not data.startswith(b"\xff\xd8"):
            raise ValueError("That doesn't look like a picture the TV can read (try a JPEG or PNG).")
        self.standby = data

    def reset_standby(self):
        self.standby = None

    def set_setting(self, key, value):
        if key != "brightness":
            raise ValueError("That isn't a setting you can change here.")
        self.settings = {key: value}

    def status(self):
        return {"python": "3.13.0", "uptime_s": 5}

    def logs(self, lines):
        return "line 1\nline 2\n"

    def rescan(self, force=False):
        self.rescanned = force
        return {"running": True, "done": 0, "total": 3, "mediainfo": True, "scanned_at": None}

    def update_status(self):
        return {"current": "0.1.0", "state": "idle"}

    def update_check(self):
        return {"current": "0.1.0", "latest": "0.2.0", "available": True, "state": "idle"}

    def update_apply(self):
        if getattr(self, "no_update", False):
            raise ValueError("This TV runs from a git checkout.")
        return {"state": "working"}

    def update_rollback(self):
        raise ValueError("There is no previous version to go back to.")


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "videos"
    root.mkdir()
    hooks = Hooks()
    srv = WebServer(UploadManager(root, reserve=0), Session(pin="4321"), hooks, host="127.0.0.1", port=0)
    port = srv.start()
    srv.base = f"http://127.0.0.1:{port}"
    srv.hooks, srv.root = hooks, root
    yield srv
    srv.stop()


def call(srv, method, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(srv.base + path, data=data, method=method)
    if token:
        req.add_header("X-Token", token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}") if r.headers.get("Content-Type", "").startswith("application/json") else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def login(srv):
    return call(srv, "POST", "/api/login", {"pin": "4321"})[1]["token"]


def test_the_page_is_served_without_a_pin_but_everything_else_needs_one(server):
    status, page = call(server, "GET", "/")
    assert status == 200 and b"Enter the PIN" in page
    for method, path in [("GET", "/api/state"), ("POST", "/api/name"), ("POST", "/api/channel"), ("PUT", "/api/upload?channel=c&name=a.mp4&total=1"),
                         ("GET", "/api/upload/status?channel=c&name=a.mp4")]:
        status, body = call(server, method, path, {} if method == "POST" else None, raw=b"x" if method == "PUT" else None)
        assert status == 401 and "PIN" in body["error"], (method, path)


def test_wrong_pin_then_right_pin_then_lockout(server):
    assert call(server, "POST", "/api/login", {"pin": "0000"})[0] == 403
    assert call(server, "POST", "/api/login", {"pin": "4321"})[0] == 200
    for _ in range(web.LOGIN_ATTEMPTS):
        status, body = call(server, "POST", "/api/login", {"pin": "1111"})
    assert status == 429 and body["retry_after"] > 0


def test_state_name_and_channel(server):
    token = login(server)
    status, state = call(server, "GET", "/api/state", token=token)
    assert status == 200 and state["name"] == "Test TV" and state["free_bytes"] > 0 and "version" in state
    assert call(server, "POST", "/api/name", {"name": "  Den "}, token)[1] == {"name": "Den"}
    assert call(server, "POST", "/api/name", {"name": ""}, token)[0] == 400
    assert call(server, "POST", "/api/channel", {"name": "3.new stuff"}, token)[1] == {"channel": "3.new stuff"}
    assert (server.root / "3.new stuff").is_dir()
    status, body = call(server, "POST", "/api/channel", {"name": "../x"}, token)
    assert status == 200 and body["channel"] == "_x" and (server.root / "_x").is_dir()        # flattened, inside the videos folder
    assert not (server.root.parent / "x").exists()
    assert call(server, "POST", "/api/channel", {"name": ".."}, token)[0] == 400


def test_upload_over_http_in_chunks_with_resume_and_a_hook(server):
    token = login(server)
    data = bytes(range(256)) * 40                                          # 10,240 bytes
    base = f"/api/upload?channel=1.shows&name=Clip%20One.mp4&total={len(data)}"
    assert call(server, "GET", "/api/upload/status?channel=1.shows&name=Clip%20One.mp4", token=token) [0] == 404   # channel doesn't exist yet
    assert call(server, "PUT", base + "&offset=0", token=token, raw=data[:4000])[1] == {"received": 4000, "done": False}
    assert call(server, "GET", "/api/upload/status?channel=1.shows&name=Clip%20One.mp4", token=token)[1] == {"received": 4000}
    status, body = call(server, "PUT", base + "&offset=9000", token=token, raw=data[9000:])               # wrong place: told where to resume
    assert status == 409 and body["received"] == 4000
    status, done = call(server, "PUT", base + "&offset=4000", token=token, raw=data[4000:])
    assert status == 200 and done["done"] and done["file"] == "Clip One.mp4"
    assert isinstance(done["advice"], list) and done["advice"][0]["level"] in ("info", "ok", "warn")
    assert (server.root / "1.shows" / "Clip One.mp4").read_bytes() == data
    assert server.hooks.uploads[0]["file"] == "Clip One.mp4" and server.hooks.uploads[0]["channel"] == "1.shows"


def test_http_rejects_non_video_files_and_bad_requests(server):
    token = login(server)
    status, body = call(server, "PUT", "/api/upload?channel=c&name=run.sh&total=4&offset=0", token=token, raw=b"echo")
    assert status == 415 and "video" in body["error"].lower()
    assert call(server, "PUT", "/api/upload?channel=c&name=a.mp4&total=abc&offset=0", token=token, raw=b"x")[0] == 400
    assert call(server, "GET", "/nope")[0] == 404
    assert call(server, "POST", "/api/name", None, token, raw=b"{not json")[0] == 400
    assert call(server, "POST", "/api/name", None, token, raw=b"x" * (web.MAX_JSON + 1))[0] == 413


def test_two_uploads_at_once(server):
    token = login(server)
    results = []

    def one(i):
        data = bytes([i]) * 3000
        base = f"/api/upload?channel=c{i}&name=f.mp4&total=3000"
        results.append(call(server, "PUT", base + "&offset=0", token=token, raw=data)[1]["done"])

    threads = [threading.Thread(target=one, args=(i,)) for i in range(1, 5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [True] * 4
    assert all((server.root / f"c{i}" / "f.mp4").read_bytes() == bytes([i]) * 3000 for i in range(1, 5))


def test_the_server_stops_cleanly(tmp_path):
    srv = WebServer(UploadManager(tmp_path), Session(), Hooks(), host="127.0.0.1", port=0)
    port = srv.start()
    srv.stop()
    with pytest.raises(Exception):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)


def test_standby_picture_can_be_read_replaced_and_reset(server):
    token = call(server, "POST", "/api/login", {"pin": "4321"})[1]["token"]
    assert call(server, "GET", "/api/standby")[0] == 401
    req = urllib.request.Request(server.base + "/api/standby", headers={"X-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers["Content-Type"] == "image/jpeg" and r.read(2) == b"\xff\xd8"
    assert call(server, "PUT", "/api/standby", token=token, raw=b"\xff\xd8fake")[0] == 200
    assert server.hooks.standby == b"\xff\xd8fake"
    assert call(server, "PUT", "/api/standby", token=token, raw=b"not a picture")[0] == 400
    assert call(server, "POST", "/api/standby/reset", {}, token=token)[0] == 200
    assert server.hooks.standby is None


def test_update_routes_need_the_pin_and_pass_hook_errors_to_the_page(server):
    for method, path in (("GET", "/api/update"), ("POST", "/api/update/check"), ("POST", "/api/update/apply")):
        assert call(server, method, path, {} if method == "POST" else None)[0] == 401
    token = call(server, "POST", "/api/login", {"pin": "4321"})[1]["token"]
    assert call(server, "GET", "/api/update", token=token)[1]["current"] == "0.1.0"
    assert call(server, "POST", "/api/update/check", {}, token=token)[1]["available"] is True
    assert call(server, "POST", "/api/update/apply", {}, token=token)[1] == {"state": "working"}
    server.hooks.no_update = True
    code, body = call(server, "POST", "/api/update/apply", {}, token=token)[:2]
    assert code == 400 and "git checkout" in body["error"]
    assert call(server, "POST", "/api/update/rollback", {}, token=token)[0] == 400


def test_library_rescan_needs_the_pin_and_passes_force_on(server):
    assert call(server, "POST", "/api/library/rescan", {})[0] == 401
    token = call(server, "POST", "/api/login", {"pin": "4321"})[1]["token"]
    assert call(server, "POST", "/api/library/rescan", {"force": True}, token=token)[1]["running"] is True
    assert server.hooks.rescanned is True


def test_settings_status_and_logs_over_http(server):
    assert call(server, "POST", "/api/settings", {"brightness": 50})[0] == 401
    token = call(server, "POST", "/api/login", {"pin": "4321"})[1]["token"]
    assert call(server, "POST", "/api/settings", {"brightness": 50}, token=token)[0] == 200
    assert server.hooks.settings == {"brightness": 50}
    code, body = call(server, "POST", "/api/settings", {"colour": "red"}, token=token)[:2]
    assert code == 400 and "isn't a setting" in body["error"]
    assert call(server, "GET", "/api/status", token=token)[1]["python"] == "3.13.0"
    req = urllib.request.Request(server.base + "/api/logs?lines=5", headers={"X-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.read().decode() == "line 1\nline 2\n"


def test_post_routes_leave_the_connection_usable_for_the_next_request(server):
    import http.client
    token = call(server, "POST", "/api/login", {"pin": "4321"})[1]["token"]
    conn = http.client.HTTPConnection("127.0.0.1", int(server.base.rsplit(":", 1)[1]))
    h = {"X-Token": token, "Content-Type": "application/json"}
    for path in ("/api/update/check", "/api/standby/reset", "/api/library/rescan", "/api/update/apply"):
        conn.request("POST", path, body="{}", headers=h)
        assert conn.getresponse().read()
        conn.request("GET", "/api/update", headers=h)                              # same connection straight after
        r = conn.getresponse()
        assert r.status == 200, (path, r.status)
        r.read()
    conn.close()
