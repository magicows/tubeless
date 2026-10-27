import json
import random
import urllib.error
import urllib.request

import pytest

from rig import Rig, wait_for
from tubeless import ads
from tubeless.ads import AdsManager, ad_slots, splice
from tubeless.library import Library, Playlists
from tubeless.state import Settings, Store
from tubeless.statics import edl_entry
from tubeless.web import Session, UploadManager, WebServer
from tubeless.youtube import Entry, FakeYtDlp, Listing


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "ads"
    path.mkdir()
    return path


@pytest.fixture
def manager(tmp_path, folder):
    return AdsManager(folder, Settings(Store(tmp_path / "data")), rng=random.Random(3))


def fill(folder, *names):
    for name in names:
        (folder / name).write_bytes(b"x")


# --- the entry ---------------------------------------------------------------------------------------------
def test_an_ad_is_joined_to_a_static_file_as_one_edl_entry():
    assert splice("/s.mp4", "/ads/a.mp4") == "edl://%6%/s.mp4;%10%/ads/a.mp4"


def test_an_ad_is_joined_to_the_tvs_own_static_clip_after_its_segments():
    own = edl_entry("/home/me/clip.mp4", 3.0, 2.0)
    joined = splice(own, "/ads/a b;c,d.mp4")
    assert joined == own + ";%16%/ads/a b;c,d.mp4"                    # the length prefix keeps odd file names whole
    assert joined.count("edl://") == 1


def test_every_nth_video_has_an_ad_but_never_the_first():
    assert ad_slots(8, 3) == [3, 6]
    assert ad_slots(8, 1) == [1, 2, 3, 4, 5, 6, 7]
    assert ad_slots(1, 1) == []


# --- the manager --------------------------------------------------------------------------------------------
def test_ads_are_off_by_default_and_the_choices_are_checked(manager):
    assert not manager.enabled and manager.every == ads.DEFAULT_EVERY
    manager.set_enabled(True)
    manager.set_every(5)
    assert manager.enabled and manager.every == 5
    for bad in (0, 7, True, "3", None):
        with pytest.raises(ValueError):
            manager.set_every(bad)
    with pytest.raises(ValueError):
        manager.set_enabled("yes")


def test_only_video_files_count_as_ads(manager, folder):
    fill(folder, "b.mp4", "a.mkv", "notes.txt", ".hidden.mp4", "c.mp4.part")
    (folder / "sub").mkdir()
    assert manager.clips() == ["a.mkv", "b.mp4"]


def test_the_page_is_warned_when_ads_are_on_and_the_folder_is_empty(manager, folder):
    assert manager.state()["warning"] is None                       # off: nothing to warn about
    manager.set_enabled(True)
    assert "empty" in manager.state()["warning"] and not manager.active()
    fill(folder, "a.mp4")
    assert manager.state()["warning"] is None and manager.active()


def test_a_missing_folder_is_an_empty_one(tmp_path):
    m = AdsManager(tmp_path / "nowhere", Settings(Store(tmp_path / "data")))
    m.set_enabled(True)
    assert m.clips() == [] and m.slot_entry("/s.mp4", 3) == "/s.mp4"


def test_the_slot_has_an_ad_only_when_due(manager, folder):
    fill(folder, "a.mp4")
    manager.set_every(2)
    assert [manager.slot_entry("/s.mp4", i) for i in range(5)] == ["/s.mp4"] * 5            # off
    manager.set_enabled(True)
    got = [manager.slot_entry("/s.mp4", i) for i in range(5)]
    assert got[0] == got[1] == got[3] == "/s.mp4"
    assert got[2] == got[4] == splice("/s.mp4", str(folder / "a.mp4"))


def test_an_ad_is_not_picked_twice_in_a_row_while_there_is_a_choice(manager, folder):
    fill(folder, "a.mp4", "b.mp4", "c.mp4")
    picks = [manager.pick() for _ in range(30)]
    assert all(x != y for x, y in zip(picks, picks[1:])) and len(set(picks)) == 3


def test_removing_an_ad_only_touches_the_folder(manager, folder):
    fill(folder, "a.mp4")
    for bad in ("../x.mp4", ".hidden", ""):
        with pytest.raises(ValueError):
            manager.delete(bad)
    manager.delete("a.mp4")
    assert manager.clips() == []


# --- in the playlist ----------------------------------------------------------------------------------------
def test_the_playlist_keeps_its_static_video_shape_with_ads_in_the_static_slots(tmp_path, manager, folder):
    fill(folder, "a.mp4")
    root = tmp_path / "videos" / "1.shows"
    root.mkdir(parents=True)
    for i in range(7):
        (root / f"v{i}.mp4").write_bytes(b"")
    lib = Library(tmp_path / "videos")
    pl = Playlists(lib, Store(tmp_path / "pl"), "/s.mp4", rng=random.Random(1))
    pl.static_slot = manager.slot_entry
    manager.set_enabled(True)
    manager.set_every(3)
    plan = pl.plan(lib.channels()[0], False)
    out = pl.entries(plan)
    assert len(out) == 14 and [e.startswith("edl://") for e in out[0::2]] == [False, False, False, True, False, False, True]
    assert all(not e.startswith("edl://") for e in out[1::2])
    assert pl.ad_positions == {6, 12}
    assert pl.episode_at(plan, 6) is None and pl.episode_at(plan, 7) == plan.rotated[3]       # still static, video, static, video


# --- in the app ---------------------------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, channels={"1.shows": [f"s{i}.mp4" for i in range(8)]}, ads_dir=str(tmp_path / "ads"),
            settings={"ads_on": True, "ads_every": 2})
    (tmp_path / "ads").mkdir()
    (tmp_path / "ads" / "ad.mp4").write_bytes(b"x")
    yield r
    r.stop()


def test_the_settings_are_on_the_web_page_and_can_be_changed(rig):
    st = rig.app.web_settings()
    assert st["values"]["ads"] is True and st["values"]["ads_every"] == 2 and st["options"]["ads_every"] == list(ads.EVERY_OPTIONS)
    rig.app.set_web_setting("ads_every", 5)
    rig.app.set_web_setting("ads", False)
    assert json.loads((rig.cfg.data / "settings.json").read_text())["ads_every"] == 5
    assert rig.app.web_state()["ads"]["enabled"] is False and rig.app.web_state()["ads"]["clips"] == ["ad.mp4"]
    for key, bad in (("ads", "yes"), ("ads_every", 4.5), ("ads_every", True)):
        with pytest.raises(ValueError):
            rig.app.set_web_setting(key, bad)


def test_a_playlist_built_with_ads_on_has_them_and_one_built_with_them_off_does_not(rig):
    rig.app.reload_channel()
    lines = open(rig.app.m3u_path).read().splitlines()
    assert [l.startswith("edl://") for l in lines[0::2]] == [False, False, True, False, True, False, True, False]
    rig.app.set_web_setting("ads", False)
    rig.app.reload_channel()
    assert not any(l.startswith("edl://") for l in open(rig.app.m3u_path).read().splitlines())


def test_a_swipe_to_skip_goes_straight_to_the_video_when_the_static_has_an_ad(rig):
    rig.app.reload_channel()
    assert rig.app.playlists.ad_positions == {4, 8, 12}
    rig.player.command("set_property", "playlist-pos", 3)
    rig.app.skip_video(+1)                                           # would land on the static at 4, which has an ad
    assert wait_for(lambda: rig.player.get("playlist-pos") == 5)


def test_skipping_to_a_static_without_an_ad_still_lands_on_the_static(rig):
    rig.app.reload_channel()
    rig.player.command("set_property", "playlist-pos", 5)
    rig.app.skip_video(-1)                                           # pos - 3 = 2, no ad there
    assert wait_for(lambda: rig.player.get("playlist-pos") == 2)


# --- downloading one from YouTube ---------------------------------------------------------------------------
def video(vid="a1b2c3d4e5f", title="Buy Our Soap", seconds=30.0):
    return Listing(title, "video", [Entry(vid, title, seconds)])


def wait_done(m):
    assert wait_for(lambda: m.download and m.download["stage"] in ("done", "error"))
    return m.download


def test_a_youtube_video_is_downloaded_into_the_ads_folder(manager, folder):
    yt = FakeYtDlp({"watch?v=a1b2c3d4e5f": video()})
    manager.start_download(yt, "https://youtu.be/a1b2c3d4e5f")
    done = wait_done(manager)
    assert done["stage"] == "done" and done["file"] in manager.clips()


@pytest.mark.parametrize("url", ["https://example.com/x", "https://www.youtube.com/playlist?list=PLabcdefghijkl", "", "nonsense"])
def test_only_a_link_to_one_youtube_video_is_taken(manager, url):
    with pytest.raises(ValueError):
        manager.start_download(FakeYtDlp({}), url)


def test_a_long_video_playlist_or_failure_is_reported_not_downloaded(manager, folder):
    yt = FakeYtDlp({"watch?v=a1b2c3d4e5f": video(seconds=2000.0), "watch?v=b1b2c3d4e5f": video("b1b2c3d4e5f", seconds=20.0)},
                   fail={"b1b2c3d4e5f": "YouTube said no."})
    manager.start_download(yt, "youtu.be/a1b2c3d4e5f")
    assert "too long" in wait_done(manager)["error"]
    manager.start_download(yt, "youtu.be/b1b2c3d4e5f")
    assert wait_done(manager)["error"] == "YouTube said no."
    manager.start_download(FakeYtDlp({}), "youtu.be/c1b2c3d4e5f")
    assert wait_done(manager)["stage"] == "error"
    assert manager.clips() == []


def test_no_yt_dlp_means_no_download(manager):
    with pytest.raises(ValueError, match="yt-dlp"):
        manager.start_download(FakeYtDlp({}, is_available=False), "youtu.be/a1b2c3d4e5f")


# --- over HTTP ----------------------------------------------------------------------------------------------
class Hooks:
    def __init__(self, manager):
        self.ads, self.uploaded_info, self.name = manager, [], "TV"

    def state(self):
        return {"name": self.name, "channels": []}

    def ads_download(self, url):
        if "bad" in url:
            raise ValueError("Paste the link to one YouTube video.")
        self.url = url

    def uploaded(self, info):
        self.uploaded_info.append(info)


@pytest.fixture
def server(tmp_path, manager, folder):
    root = tmp_path / "videos"
    root.mkdir()
    hooks = Hooks(manager)
    srv = WebServer(UploadManager(root, reserve=0), Session(pin="4321"), hooks, host="127.0.0.1", port=0,
                    ad_uploads=UploadManager(folder.parent, reserve=0))
    srv.base = f"http://127.0.0.1:{srv.start()}"
    srv.hooks, srv.root = hooks, root
    yield srv
    srv.stop()


def call(srv, method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(srv.base + path, data=data, method=method)
    req.add_header("X-Token", json.loads(urllib.request.urlopen(urllib.request.Request(
        srv.base + "/api/login", data=b'{"pin": "4321"}', method="POST")).read())["token"])
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_a_clip_uploaded_to_the_ads_target_lands_in_the_ads_folder_and_not_the_library(server, folder):
    status, body = call(server, "PUT", "/api/upload?target=ads&channel=ads&name=soap.mp4&offset=0&total=5", raw=b"12345")
    assert status == 200 and body["done"] and (folder / "soap.mp4").read_bytes() == b"12345"
    assert server.hooks.uploaded_info == [] and not any(server.root.iterdir())
    assert call(server, "GET", "/api/upload/status?target=ads&channel=ads&name=x.mp4")[1] == {"received": 0}
    assert server.hooks.ads.clips() == ["soap.mp4"]


def test_the_ads_routes_download_remove_and_say_why_not(server, folder):
    fill(folder, "old.mp4")
    assert call(server, "POST", "/api/ads/youtube", {"url": "https://youtu.be/a1b2c3d4e5f"})[0] == 200
    assert server.hooks.url.endswith("a1b2c3d4e5f")
    status, body = call(server, "POST", "/api/ads/youtube", {"url": "bad"})
    assert status == 400 and "one YouTube video" in body["error"]
    status, body = call(server, "POST", "/api/ads/delete", {"name": "old.mp4"})
    assert status == 200 and body["clips"] == []
    assert call(server, "POST", "/api/ads/delete", {"name": "../x"})[0] == 400
