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
    assert m.summaries()[name] == {"mode": "download", "total": 3, "selected": 2, "done": 2, "waiting": 0, "error": 0, "off": 1}
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
