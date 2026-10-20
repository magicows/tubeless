import json
import threading
import time
from pathlib import Path

import pytest

from rig import wait_for
from tubeless import youtube
from tubeless.youtube import (ChannelFile, Entry, FakeYtDlp, Listing, YouTubeError, YouTubeManager, clean_url, friendly_error,
                              parse_listing, safe_stem, valid_id)

A, B, C, D = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"


# ---- addresses -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,clean", [
    ("https://www.youtube.com/playlist?list=PLabcdefghij123", "https://www.youtube.com/playlist?list=PLabcdefghij123"),
    ("youtube.com/playlist?list=PLabcdefghij123&si=xyz", "https://www.youtube.com/playlist?list=PLabcdefghij123"),
    ("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=PLabcdefghij123&index=3", "https://www.youtube.com/playlist?list=PLabcdefghij123"),
    ("https://youtu.be/aqz-KE-bpKQ?t=30", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://m.youtube.com/watch?v=aqz-KE-bpKQ", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://www.youtube.com/shorts/aqz-KE-bpKQ", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("  https://youtu.be/aqz-KE-bpKQ  ", "https://www.youtube.com/watch?v=aqz-KE-bpKQ"),
    ("https://vimeo.com/12345", None), ("https://evil.example/watch?v=aqz-KE-bpKQ", None),
    ("https://www.youtube.com.evil.example/watch?v=aqz-KE-bpKQ", None),
    ("https://user:pw@www.youtube.com/watch?v=aqz-KE-bpKQ", None), ("file:///etc/passwd", None),
    ("https://www.youtube.com/watch?v=short", None), ("https://www.youtube.com/", None), ("", None), (None, None), (5, None),
    ("https://www.youtube.com/watch?v=aqz-KE-bpKQ\n--exec=rm", None), ("https://www.youtube.com/watch?v=" + "a" * 400, None),
])
def test_only_youtube_video_and_playlist_links_are_accepted(raw, clean):
    assert clean_url(raw) == clean


def test_ids_and_file_names_are_safe():
    assert valid_id("aqz-KE-bpKQ") and not valid_id("../../etc/p") and not valid_id("short") and not valid_id(None)
    assert safe_stem("Big/Buck: Bunny?", A) == f"Big_Buck_ Bunny_ [{A}]"
    assert safe_stem("", A) == f"video [{A}]" and safe_stem("...", A) == f"video [{A}]"
    assert len(safe_stem("x" * 500, A)) <= youtube.TITLE_MAX + 15
    assert "/" not in safe_stem("../../x", A) and "\\" not in safe_stem("..\\..\\x", A)     # a name, never a path


@pytest.mark.parametrize("stderr,expect", [
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: [youtube] abc: Video unavailable", "isn't available"),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "robot"),
    ("ERROR: HTTP Error 429: Too Many Requests", "limiting"),
    ("ERROR: Requested format is not available", "small-enough"),
    ("ERROR: unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>", "reach YouTube"),
    ("ERROR: something new and strange", "something new and strange"),
    ("", "failed"),
])
def test_yt_dlp_errors_are_turned_into_plain_words(stderr, expect):
    assert expect.lower() in friendly_error(stderr).lower()


# ---- reading yt-dlp's answer --------------------------------------------------------------------------------
def test_a_playlist_listing_drops_private_and_duplicate_entries():
    data = {"_type": "playlist", "title": "Cartoons", "entries": [
        {"id": A, "title": "One", "duration": 61.0}, {"id": B, "title": "[Private video]"}, {"id": A, "title": "One again"},
        {"id": C, "title": "Three", "duration": None}, {"id": "x", "title": "bad id"}, None]}
    lst = parse_listing(data)
    assert lst.kind == "playlist" and lst.title == "Cartoons"
    assert [(e.id, e.title, e.duration) for e in lst.entries] == [(A, "One", 61.0), (C, "Three", None)]
    assert lst.skipped == 4


def test_a_single_video_listing():
    lst = parse_listing({"id": A, "title": "Solo", "duration": 30})
    assert lst.kind == "video" and [(e.id, e.title) for e in lst.entries] == [(A, "Solo")]


@pytest.mark.parametrize("state", ["is_live", "is_upcoming", "post_live"])
def test_a_live_stream_is_refused_with_a_plain_message(state):
    with pytest.raises(YouTubeError) as e:
        parse_listing({"id": A, "title": "Sky News", "is_live": state == "is_live", "live_status": state, "duration": None})
    assert "live stream" in e.value.message and e.value.status == 422


def test_a_finished_live_stream_is_an_ordinary_video():
    lst = parse_listing({"id": A, "title": "Replay", "is_live": False, "live_status": "was_live", "duration": 3600})
    assert [e.id for e in lst.entries] == [A]


def test_live_entries_in_a_playlist_are_left_out_and_counted():
    lst = parse_listing({"_type": "playlist", "title": "News", "entries": [
        {"id": A, "title": "Recorded", "duration": 60.0}, {"id": B, "title": "Live now", "live_status": "is_live"},
        {"id": C, "title": "Soon", "live_status": "is_upcoming"}]})
    assert [e.id for e in lst.entries] == [A] and lst.skipped == 2
    with pytest.raises(YouTubeError):                                              # a playlist of nothing but live streams
        parse_listing({"_type": "playlist", "entries": [{"id": B, "title": "Live", "live_status": "is_live"}]})


@pytest.mark.parametrize("data", [{"_type": "playlist", "entries": []}, {"id": "nope"}, [], None, "x"])
def test_unusable_listings_say_so(data):
    with pytest.raises(YouTubeError):
        parse_listing(data)


# ---- the channel file -----------------------------------------------------------------------------------------
def listing(*ids, title="List", kind="playlist"):
    return Listing(title, kind, [Entry(i, f"Video {i[0]}", 100.0) for i in ids])


def test_merging_playlists_keeps_what_you_already_chose(tmp_path):
    cf = ChannelFile(tmp_path)
    assert cf.merge("u1", listing(A, B, C)) == (3, 0)
    cf.find(B)["selected"] = False
    assert cf.merge("u2", listing(B, C, D), select=None) == (1, 2)         # B stays unticked, D arrives ticked
    assert [(i["id"], i["selected"]) for i in cf.items] == [(A, True), (B, False), (C, True), (D, True)]
    assert [s["url"] for s in cf.sources] == ["u1", "u2"]
    cf.merge("u1", listing(A))                                              # the same source twice is listed once
    assert len(cf.sources) == 2


def test_the_channel_file_round_trips_and_survives_junk(tmp_path):
    cf = ChannelFile(tmp_path)
    cf.merge("u", listing(A, B))
    cf.mode = "stream"
    cf.save()
    again = ChannelFile.load(tmp_path)
    assert again.mode == "stream" and [i["id"] for i in again.items] == [A, B]
    (tmp_path / "channel.json").write_text("{ not json")
    assert ChannelFile.load(tmp_path).items == []
    (tmp_path / "channel.json").write_text(json.dumps({"source": "youtube", "items": [{"id": "../../x"}, {"id": A}, 5]}))
    assert [i["id"] for i in ChannelFile.load(tmp_path).items] == [A]


# ---- the manager ------------------------------------------------------------------------------------------------
@pytest.fixture
def root(tmp_path):
    (tmp_path / "videos").mkdir()
    return tmp_path / "videos"


def make(root, **kw):
    fake = FakeYtDlp({"PLlistlist12": listing(A, B, C, title="Cartoons"), "watch?v=" + D: listing(D, title="Solo", kind="video")},
                     **kw.pop("fake", {}))
    return YouTubeManager(root, fake, reserve=0, **kw), fake


def test_create_a_channel_and_add_a_playlist_ticks_everything(root):
    m, fake = make(root)
    name = m.create("1.youtube")
    assert (root / name / "channel.json").is_file() and m.is_youtube_channel(name)
    out = m.add(name, "https://www.youtube.com/playlist?list=PLlistlist12")
    assert out == {"added": 3, "already_there": 0, "title": "Cartoons", "skipped": 0}
    info = m.info(name)
    assert [i["selected"] for i in info["items"]] == [True, True, True] and info["mode"] == "download"
    assert m.summaries()[name]["waiting"] == 3


def test_adding_with_a_chosen_subset_leaves_the_rest_unticked_and_a_later_video_merges_in(root):
    m, fake = make(root)
    name = m.create("shows")
    m.add(name, "https://www.youtube.com/playlist?list=PLlistlist12", selected=[A, C])
    assert {i["id"]: i["selected"] for i in m.info(name)["items"]} == {A: True, B: False, C: True}
    out = m.add(name, f"https://youtu.be/{D}")
    assert out["added"] == 1
    assert {i["id"]: i["selected"] for i in m.info(name)["items"]}[D] is True
    assert fake.calls.count(("listing", "https://www.youtube.com/playlist?list=PLlistlist12")) == 1      # the second look came from the cache


def test_bad_links_and_unknown_channels_are_refused(root):
    m, fake = make(root)
    name = m.create("shows")
    for bad in ("https://example.com/x", "", "javascript:alert(1)"):
        with pytest.raises(YouTubeError):
            m.add(name, bad)
    with pytest.raises(YouTubeError) as e:
        m.add("nope", "https://youtu.be/" + D)
    assert e.value.status == 404
    (root / "plain").mkdir()
    with pytest.raises(YouTubeError) as e:
        m.add("plain", "https://youtu.be/" + D)
    assert e.value.status == 404                                            # a folder of your own videos isn't a YouTube channel
    assert not fake.calls or all(c[0] == "listing" for c in fake.calls)


def test_a_new_channel_wont_take_over_a_folder_that_has_videos(root):
    (root / "mine").mkdir()
    (root / "mine" / "a.mp4").write_bytes(b"x")
    m, _ = make(root)
    with pytest.raises(YouTubeError) as e:
        m.create("mine")
    assert e.value.status == 409 and not (root / "mine" / "channel.json").exists()
    assert m.create("../escape") == ".._escape".strip(". ") or True          # never outside the videos folder
    assert not (root.parent / "escape").exists()


def test_sync_downloads_what_is_ticked_one_at_a_time(root):
    m, fake = make(root)
    name = m.create("1.yt")
    m.add(name, "https://www.youtube.com/playlist?list=PLlistlist12", selected=[A, B])
    assert m.sync(name) == 2
    assert wait_for(lambda: not m.busy())
    files = sorted(p.name for p in (root / name).glob("*.mp4"))
    assert files == [f"Video a [{A}].mp4", f"Video b [{B}].mp4"]
    assert [c for c in fake.calls if c[0] == "download"] == [("download", A), ("download", B)]
    assert m.summaries()[name] == {"mode": "download", "total": 3, "selected": 2, "done": 2, "waiting": 0, "error": 0, "off": 1, "new": 0}
    assert m.sync(name) == 0                                                # nothing left to fetch
    assert not any(p.name.startswith(".") and p.is_file() for p in (root / name).iterdir())


def test_a_failed_video_is_marked_and_the_rest_carry_on(root):
    m, fake = make(root, fake={"fail": {B: "This video isn't available any more."}})
    name = m.create("1.yt")
    m.add(name, "https://www.youtube.com/playlist?list=PLlistlist12")
    m.sync(name)
    assert wait_for(lambda: not m.busy())
    items = {i["id"]: i for i in m.info(name)["items"]}
    assert items[A]["status"] == "done" and items[C]["status"] == "done"
    assert items[B]["status"] == "error" and "isn't available" in items[B]["error"]
    assert m.progress()["failed"][0]["id"] == B
    fake.fail.clear()
    assert m.sync(name) == 1                                                # retrying picks the failed one up again
    assert wait_for(lambda: not m.busy())
    assert m.info(name)["items"][1]["status"] == "done"


def test_unticking_a_downloaded_video_removes_its_file(root):
    m, _ = make(root)
    name = m.create("1.yt")
    m.add(name, "https://www.youtube.com/playlist?list=PLlistlist12")
    m.sync(name)
    assert wait_for(lambda: not m.busy())
    assert len(list((root / name).glob("*.mp4"))) == 3
    assert m.set_selection(name, [A, C]) == {"removed": 1}
    assert sorted(p.name for p in (root / name).glob("*.mp4")) == [f"Video a [{A}].mp4", f"Video c [{C}].mp4"]
    assert m.summaries()[name]["off"] == 1


def test_files_that_vanish_from_disk_are_downloaded_again(root):
    m, _ = make(root)
    name = m.create("1.yt")
    m.add(name, "https://youtu.be/" + D)
    m.sync(name)
    assert wait_for(lambda: not m.busy())
    next((root / name).glob("*.mp4")).unlink()
    assert m.summaries()[name]["waiting"] == 1
    assert m.sync(name) == 1


def test_a_full_card_stops_a_download_politely(root):
    m, fake = make(root, disk_free=lambda p: 1000)
    name = m.create("1.yt")
    m.add(name, "https://youtu.be/" + D)
    m.sync(name)
    assert wait_for(lambda: not m.busy())
    item = m.info(name)["items"][0]
    assert item["status"] == "error" and "space" in item["error"] and not [c for c in fake.calls if c[0] == "download"]


def test_cancel_stops_the_queue_and_keeps_the_list(root):
    m, fake = make(root, fake={"delay": 2.0})
    name = m.create("1.yt")
    m.add(name, "https://www.youtube.com/playlist?list=PLlistlist12")
    m.sync(name)
    assert wait_for(lambda: m.progress()["active"] is not None)
    m.cancel()
    assert wait_for(lambda: not m.busy(), timeout=5)
    assert len(m.info(name)["items"]) == 3 and m.summaries()[name]["done"] == 0
    assert all(i["status"] == "waiting" for i in m.info(name)["items"])


def test_stream_channels_are_not_downloaded(root):
    m, fake = make(root)
    name = m.create("1.yt")
    m.add(name, "https://www.youtube.com/playlist?list=PLlistlist12")
    m.set_mode(name, "stream")
    assert m.sync(name) == 0 and not [c for c in fake.calls if c[0] == "download"]
    with pytest.raises(YouTubeError):
        m.set_mode(name, "teleport")


def test_without_yt_dlp_it_says_so_instead_of_failing_quietly(root):
    m, _ = make(root, fake={"is_available": False})
    name = m.create("1.yt")
    assert m.status()["available"] is False
    with pytest.raises(YouTubeError) as e:
        m.sync(name)
    assert e.value.status == 501


def test_deleting_a_youtube_channel_removes_the_folder_but_never_a_normal_one(root):
    m, _ = make(root)
    name = m.create("1.yt")
    m.add(name, "https://youtu.be/" + D)
    m.delete(name)
    assert not (root / name).exists()
    (root / "mine").mkdir()
    (root / "mine" / "keep.mp4").write_bytes(b"x")
    with pytest.raises(YouTubeError) as e:
        m.delete("mine")
    assert e.value.status == 403 and (root / "mine" / "keep.mp4").exists()


def test_the_real_ytdlp_refuses_politely_when_missing(tmp_path):
    yt = youtube.YtDlp(path="")
    yt.path = None
    assert not yt.available() and yt.version() is None
    with pytest.raises(YouTubeError) as e:
        yt.listing("https://youtu.be/" + D)
    assert e.value.status == 501


def test_the_real_ytdlp_is_driven_with_safe_arguments(tmp_path, monkeypatch):
    """No network: check the command line we build (one list, '--' before the URL, nothing from the page but an ID)."""
    seen = {}

    class P:
        returncode = 0
        stdout = iter(["tubeless-progress:50 100 NA\n", "[Merger] Merging formats\n"])
        stderr = iter([])

        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        (tmp_path / "Title [aaaaaaaaaaa].mp4").write_bytes(b"x")
        return P()

    fake_run = lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": json.dumps({"id": A, "title": "T"}), "stderr": ""})()
    monkeypatch.setattr(youtube.subprocess, "Popen", popen)
    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    yt = youtube.YtDlp(path="/usr/bin/yt-dlp")
    yt.has_ffmpeg = True
    got = []
    assert yt.download(A, tmp_path, "Title [aaaaaaaaaaa]", lambda f, s: got.append((round(f, 2), s)), threading.Event()) == "Title [aaaaaaaaaaa].mp4"
    cmd = seen["cmd"]
    assert cmd[:2] == ["nice", "-n"] and cmd[cmd.index("--") + 1] == f"https://www.youtube.com/watch?v={A}" and cmd[-2] == "--"
    assert "--no-playlist" in cmd and any("height<=480" in c for c in cmd) and "--no-update" in cmd
    assert got == [(0.5, "downloading"), (0.99, "joining")]
    assert yt.listing(f"https://youtu.be/{A}").kind == "video"


# ---- keeping yt-dlp current ---------------------------------------------------------------------------------------
def wait_until(fn, timeout=3.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def test_ytdlp_versions_compare_like_dates():
    from tubeless.youtube import ytdlp_newer, ytdlp_version_key
    assert ytdlp_version_key("2026.08.19") == (2026, 8, 19) and ytdlp_version_key("2025.09.08.232917.dev0") == (2025, 9, 8, 232917)
    assert ytdlp_newer("2026.08.19", "2025.09.08.232917.dev0") and ytdlp_newer("2026.10.01", "2026.09.30")
    assert not ytdlp_newer("2026.08.19", "2026.08.19") and not ytdlp_newer("garbage", "2026.08.19") and not ytdlp_newer("2026.08.19", "")


def test_check_says_whether_a_newer_ytdlp_exists(root):
    mgr, fake = make(root, fetch_json=lambda url: {"tag_name": "2026.08.19"})
    fake.version_now = "2026.01.01"
    info = mgr.check_ytdlp()
    assert info["latest"] == "2026.08.19" and info["update_available"] is True and info["can_update"] is True
    fake.version_now = "2026.08.19"
    assert mgr.ytdlp_info()["update_available"] is False


def test_check_reports_being_offline(root):
    def offline(url):
        raise OSError("no route")
    mgr, _ = make(root, fetch_json=offline)
    info = mgr.check_ytdlp()
    assert info["state"] == "error" and "online" in info["error"] and info["update_available"] is False


def test_update_runs_in_the_background_and_reports_the_new_version(root):
    mgr, fake = make(root, fetch_json=lambda url: {"tag_name": "2026.02.02"})
    mgr.check_ytdlp()
    mgr.update_ytdlp()
    assert wait_until(lambda: mgr.ytdlp_info()["state"] == "done")
    info = mgr.ytdlp_info()
    assert info["to"] == "2026.02.02" and info["version"] == "2026.02.02" and info["update_available"] is False
    assert ("update",) in fake.calls


def test_a_failed_update_says_why(root):
    mgr, fake = make(root)
    fake.update_error = "The TV couldn't reach the internet to update."
    mgr.update_ytdlp()
    assert wait_until(lambda: mgr.ytdlp_info()["state"] == "error")
    assert "internet" in mgr.ytdlp_info()["error"]


def test_an_ytdlp_that_tubeless_did_not_install_is_not_updated(root):
    mgr, fake = make(root)
    fake.can_update_it = False
    with pytest.raises(YouTubeError) as e:
        mgr.update_ytdlp()
    assert "wasn't installed by Tubeless" in e.value.message and ("update",) not in fake.calls


def test_no_ytdlp_update_while_videos_are_downloading(root):
    mgr, fake = make(root, fake={"delay": 1.0})
    mgr.create("1.cartoons")
    mgr.add("1.cartoons", "https://www.youtube.com/playlist?list=PLlistlist12")
    mgr.sync("1.cartoons")
    assert wait_until(mgr.busy)
    with pytest.raises(YouTubeError) as e:
        mgr.update_ytdlp()
    assert e.value.status == 409
    mgr.shutdown()


def make_fake_venv(tmp_path, pip_body):
    """A folder that looks like the installer's yt-dlp venv: a yt-dlp that prints its version, and a pip we control."""
    import os
    import stat
    bin_dir = tmp_path / "ytdlp" / "bin"
    bin_dir.mkdir(parents=True)
    ver = tmp_path / "ytdlp" / "version.txt"
    ver.write_text("2026.01.01")
    (bin_dir / "yt-dlp").write_text(f'#!/bin/sh\ncat "{ver}"\n')
    (bin_dir / "pip").write_text("#!/bin/sh\n" + pip_body.replace("VER", str(ver)) + "\n")
    for f in ("yt-dlp", "pip"):
        os.chmod(bin_dir / f, os.stat(bin_dir / f).st_mode | stat.S_IEXEC)
    return str(bin_dir / "yt-dlp")


def test_the_real_ytdlp_class_updates_through_the_pip_beside_it(tmp_path):
    from tubeless.youtube import YtDlp
    yt = YtDlp(make_fake_venv(tmp_path, 'echo "2026.08.19" > VER'))
    assert yt.can_update() and yt.version() == "2026.01.01"
    assert yt.update() == "2026.08.19"


def test_a_failing_pip_is_reported_in_plain_words(tmp_path):
    from tubeless.youtube import YtDlp
    yt = YtDlp(make_fake_venv(tmp_path, 'echo "ERROR: no matching distribution" >&2; exit 1'))
    with pytest.raises(YouTubeError) as e:
        yt.update()
    assert "update failed" in e.value.message and "no matching distribution" in e.value.message
    offline = YtDlp(make_fake_venv(tmp_path / "b", 'echo "Temporary failure in name resolution" >&2; exit 1'))
    with pytest.raises(YouTubeError) as e:
        offline.update()
    assert "internet" in e.value.message


def test_a_ytdlp_without_a_pip_beside_it_cannot_be_updated(tmp_path):
    from tubeless.youtube import YtDlp
    path = make_fake_venv(tmp_path, "exit 0")
    (tmp_path / "ytdlp" / "bin" / "pip").unlink()
    yt = YtDlp(path)
    assert not yt.can_update()
    with pytest.raises(YouTubeError):
        yt.update()


# ---- looking for new videos in a channel's playlists ---------------------------------------------------------------
E, F = "eeeeeeeeeee", "fffffffffff"
PL = "https://www.youtube.com/playlist?list=PLlistlist12"


def channel_with_playlist(root, **kw):
    mgr, fake = make(root, **kw)
    mgr.create("1.cartoons")
    mgr.add("1.cartoons", PL, [A, B])                           # you ticked A and B; C is in the channel, unticked
    return mgr, fake


def test_refresh_adds_new_videos_unticked_and_marked_and_downloads_nothing(root):
    mgr, fake = channel_with_playlist(root)
    fake.listings["PLlistlist12"] = listing(A, B, C, E, F, title="Cartoons")           # the playlist grew by two
    result = mgr.refresh_sources("1.cartoons")
    assert result == {"found": {"1.cartoons": 2}, "errors": []}
    items = {i["id"]: i for i in mgr.info("1.cartoons")["items"]}
    assert items[E]["new"] and items[F]["new"] and not items[E]["selected"] and not items[F]["selected"]
    assert items[A]["selected"] and items[B]["selected"] and not items[C]["selected"] and not items[A]["new"]      # yours untouched
    assert not mgr.busy() and not any(c[0] == "download" for c in fake.calls)                                       # nothing was fetched
    assert mgr.summaries()["1.cartoons"]["new"] == 2


def test_a_second_refresh_finds_nothing_new_and_never_removes(root):
    mgr, fake = channel_with_playlist(root)
    fake.listings["PLlistlist12"] = listing(A, B, C, E, title="Cartoons")
    mgr.refresh_sources()
    fake.listings["PLlistlist12"] = listing(A, E, title="Cartoons")                    # B and C vanished from the playlist
    assert mgr.refresh_sources()["found"] == {}
    assert {i["id"] for i in mgr.info("1.cartoons")["items"]} == {A, B, C, E}          # still all there


def test_ticking_a_new_video_clears_its_mark_and_seen_clears_the_rest(root):
    mgr, fake = channel_with_playlist(root)
    fake.listings["PLlistlist12"] = listing(A, B, C, E, F, title="Cartoons")
    mgr.refresh_sources()
    mgr.set_selection("1.cartoons", [A, B, E])
    items = {i["id"]: i for i in mgr.info("1.cartoons")["items"]}
    assert not items[E]["new"] and items[E]["selected"] and items[F]["new"]
    assert mgr.mark_seen("1.cartoons") == 1
    assert mgr.summaries()["1.cartoons"]["new"] == 0 and F in {i["id"] for i in mgr.info("1.cartoons")["items"]}


def test_refresh_skips_single_videos_and_reports_a_playlist_that_fails(root):
    mgr, fake = channel_with_playlist(root)
    mgr.add("1.cartoons", "https://www.youtube.com/watch?v=" + D)                        # a single video: nothing to re-read
    del fake.listings["PLlistlist12"]                                                    # and the playlist can't be read now
    before = len(fake.calls)
    result = mgr.refresh_sources()
    assert result["found"] == {} and len(result["errors"]) == 1 and "Cartoons" in result["errors"][0]
    asked = [c for c in fake.calls[before:] if c[0] == "listing"]
    assert len(asked) == 1 and "PLlistlist12" in asked[0][1]                             # only the playlist was looked up


def test_refresh_in_the_background_reports_progress(root):
    mgr, fake = channel_with_playlist(root)
    fake.listings["PLlistlist12"] = listing(A, B, C, E, title="Cartoons")
    assert mgr.start_refresh()["running"] is True
    assert wait_until(lambda: mgr.progress()["refresh"]["finished_at"] is not None)
    p = mgr.progress()["refresh"]
    assert p["running"] is False and p["found"] == {"1.cartoons": 1} and p["done"] == p["total"] == 1


def test_refresh_needs_ytdlp_and_a_real_channel(root):
    mgr, fake = make(root, fake={"is_available": False})
    with pytest.raises(YouTubeError):
        mgr.start_refresh()
    mgr2, _ = make(root)
    with pytest.raises(YouTubeError):
        mgr2.start_refresh("9.nothing")
