"""The phone page's YouTube channel features, over real HTTP against a running TV."""
import pytest

from rig import Rig, http, wait_for
from test_modes import button_centre
from tubeless.youtube import Entry, FakeYtDlp, Listing

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"
PLAYLIST = "https://www.youtube.com/playlist?list=PLlistlist12"


def lst():
    return Listing("Cartoons", "playlist", [Entry(A, "One", 60.0), Entry(B, "Two", 70.0), Entry(C, "Three", None)])


@pytest.fixture
def tv(tmp_path):
    fake = FakeYtDlp({"PLlistlist12": lst()})
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, ytdlp=fake)
    r.tap(*button_centre("upload"))
    assert wait_for(lambda: r.state["mode"] == "transfer")
    info = r.app.web_info
    r.base = f"http://127.0.0.1:{info['port']}"
    r.token = http(r.base, "POST", "/api/login", {"pin": info["pin"]})[1]["token"]
    r.fake = fake
    yield r
    r.stop()


def call(tv, method, path, body=None):
    return http(tv.base, method, path, body, tv.token)


def test_everything_needs_the_pin(tv):
    for method, path in (("GET", "/api/yt/progress"), ("GET", "/api/yt/channel?channel=x"), ("POST", "/api/yt/resolve"),
                         ("POST", "/api/yt/add"), ("POST", "/api/yt/sync"), ("POST", "/api/channel/delete")):
        status, _ = http(tv.base, method, path, {} if method == "POST" else None)
        assert status == 401, path


def test_building_a_youtube_channel_from_a_playlist(tv, tmp_path):
    status, body = call(tv, "POST", "/api/channel", {"name": "1.youtube", "kind": "youtube"})
    assert (status, body) == (200, {"channel": "1.youtube"})

    status, state = call(tv, "GET", "/api/state")
    kinds = {c["name"]: c["kind"] for c in state["channels"]}
    assert kinds == {"1.nothing": "folder", "1.youtube": "youtube"} and state["youtube"]["available"] is True

    status, found = call(tv, "POST", "/api/yt/resolve", {"url": PLAYLIST})
    assert status == 200 and found["title"] == "Cartoons" and [e["id"] for e in found["entries"]] == [A, B, C]

    status, added = call(tv, "POST", "/api/yt/add", {"channel": "1.youtube", "url": PLAYLIST, "selected": [A, C]})
    assert status == 200 and added["added"] == 3
    status, info = call(tv, "GET", "/api/yt/channel?channel=1.youtube")
    assert [(i["id"], i["selected"], i["status"]) for i in info["items"]] == [(A, True, "waiting"), (B, False, "off"), (C, True, "waiting")]

    assert call(tv, "POST", "/api/yt/sync", {"channel": "1.youtube"})[1] == {"queued": 2}
    assert wait_for(lambda: not tv.app.youtube.busy())
    status, info = call(tv, "GET", "/api/yt/channel?channel=1.youtube")
    assert [i["status"] for i in info["items"]] == ["done", "off", "done"]
    assert len(list((tmp_path / "videos" / "1.youtube").glob("*.mp4"))) == 2
    assert call(tv, "GET", "/api/yt/progress")[1]["finished"] == 2

    assert call(tv, "POST", "/api/yt/select", {"channel": "1.youtube", "selected": [A, B, C]})[1] == {"removed": 0}
    assert call(tv, "POST", "/api/yt/mode", {"channel": "1.youtube", "mode": "stream"})[1] == {"mode": "stream"}
    assert call(tv, "GET", "/api/state")[1]["channels"][1]["youtube"]["mode"] == "stream"


def test_a_downloaded_youtube_channel_plays_like_any_other_when_you_leave(tv, tmp_path):
    call(tv, "POST", "/api/channel", {"name": "1.youtube", "kind": "youtube"})
    call(tv, "POST", "/api/yt/add", {"channel": "1.youtube", "url": PLAYLIST})
    call(tv, "POST", "/api/yt/sync", {})
    assert wait_for(lambda: not tv.app.youtube.busy())
    tv.hold()
    assert wait_for(lambda: tv.state["mode"] == "play" and tv.player.alive(), timeout=6)
    assert tv.state["channel"] == "1.youtube" and tv.app.youtube is None
    assert not (tmp_path / "videos" / "1.youtube" / "channel.json").read_text() == ""


def test_the_tv_shows_youtube_progress(tmp_path):
    fake = FakeYtDlp({"PLlistlist12": lst()}, delay=1.5)
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, ytdlp=fake)
    try:
        r.tap(*button_centre("upload"))
        assert wait_for(lambda: r.state["mode"] == "transfer")
        info = r.app.web_info
        base = f"http://127.0.0.1:{info['port']}"
        tok = http(base, "POST", "/api/login", {"pin": info["pin"]})[1]["token"]
        http(base, "POST", "/api/channel", {"name": "yt", "kind": "youtube"}, tok)
        http(base, "POST", "/api/yt/add", {"channel": "yt", "url": PLAYLIST}, tok)
        import hashlib
        before = hashlib.md5(r.display.frame565.tobytes()).hexdigest()
        http(base, "POST", "/api/yt/sync", {}, tok)
        assert wait_for(lambda: r.app.youtube.progress()["active"] is not None)
        assert wait_for(lambda: hashlib.md5(r.display.frame565.tobytes()).hexdigest() != before, timeout=4)
        r.hold()                                                               # leaving the page stops the downloads...
        assert wait_for(lambda: r.state["mode"] == "play" or r.state["mode"] == "empty", timeout=8)
    finally:
        r.stop()


def test_bad_requests_get_clear_errors(tv):
    assert call(tv, "POST", "/api/yt/resolve", {"url": "https://vimeo.com/1"})[0] == 400
    assert call(tv, "POST", "/api/yt/resolve", {"url": "https://www.youtube.com/playlist?list=PLnotknown123"})[0] == 502
    assert call(tv, "POST", "/api/yt/add", {"channel": "nope", "url": PLAYLIST})[0] == 404
    assert call(tv, "POST", "/api/yt/add", {"channel": "1.nothing", "url": PLAYLIST})[0] == 404       # an ordinary folder
    assert call(tv, "POST", "/api/yt/select", {"channel": "1.nothing", "selected": "all"})[0] == 400
    assert call(tv, "POST", "/api/yt/other", {})[0] == 404
    assert call(tv, "POST", "/api/channel", {"name": "../x", "kind": "youtube"})[0] in (200, 400)     # never outside the folder
    assert call(tv, "POST", "/api/channel/delete", {"name": "1.nothing"})[0] == 403


def test_deleting_a_youtube_channel(tv, tmp_path):
    call(tv, "POST", "/api/channel", {"name": "gone", "kind": "youtube"})
    assert (tmp_path / "videos" / "gone").is_dir()
    assert call(tv, "POST", "/api/channel/delete", {"name": "gone"}) == (200, {"deleted": "gone"})
    assert not (tmp_path / "videos" / "gone").exists()


def test_without_yt_dlp_the_page_is_told_and_syncing_is_refused(tmp_path):
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, ytdlp=FakeYtDlp(is_available=False))
    try:
        r.tap(*button_centre("upload"))
        assert wait_for(lambda: r.state["mode"] == "transfer")
        info = r.app.web_info
        base = f"http://127.0.0.1:{info['port']}"
        tok = http(base, "POST", "/api/login", {"pin": info["pin"]})[1]["token"]
        assert http(base, "GET", "/api/state", None, tok)[1]["youtube"]["available"] is False
        http(base, "POST", "/api/channel", {"name": "yt", "kind": "youtube"}, tok)
        assert http(base, "POST", "/api/yt/sync", {}, tok)[0] == 501
    finally:
        r.stop()


def test_ytdlp_can_be_checked_and_updated_from_the_page(tv):
    assert http(tv.base, "GET", "/api/yt/ytdlp")[0] == 401
    assert http(tv.base, "POST", "/api/yt/ytdlp/update", {})[0] == 401
    tv.app.youtube._fetch_json = lambda url: {"tag_name": "2026.02.02"}                # no real network in tests
    status, info = call(tv, "GET", "/api/yt/ytdlp")
    assert status == 200 and info["version"] == "2026.01.01" and info["can_update"] is True and info["update_available"] is False
    status, info = call(tv, "POST", "/api/yt/ytdlp/check", {})
    assert status == 200 and info["update_available"] is True and info["latest"] == "2026.02.02"
    assert call(tv, "POST", "/api/yt/ytdlp/update", {})[0] == 200
    assert wait_for(lambda: call(tv, "GET", "/api/yt/ytdlp")[1]["state"] == "done")
    assert call(tv, "GET", "/api/yt/ytdlp")[1]["version"] == "2026.02.02"
    tv.fake.can_update_it = False
    status, body = call(tv, "POST", "/api/yt/ytdlp/update", {})
    assert status == 400 and "wasn't installed by Tubeless" in body["error"]
