"""YouTube channels set to 'stream': played from the network, with loading / can't-play messages on the TV."""
import json
from pathlib import Path

import pytest

from rig import Rig, wait_for
from tubeless.library import Library
from tubeless.wifi import FakeWifi
from tubeless.youtube import ChannelFile, Entry, Listing

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def stream_channel(root: Path, name="3.youtube", mode="stream", ids=(A, B, C), files=None, selected=None):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    cf = ChannelFile(folder)
    cf.mode = mode
    cf.merge("u", Listing("List", "playlist", [Entry(i, f"Video {i[0]}", 100.0) for i in ids]))
    for item in cf.items:
        item["selected"] = True if selected is None else item["id"] in selected
    for vid, fname in (files or {}).items():
        (folder / fname).write_bytes(b"x")
        cf.find(vid)["file"] = fname
    cf.save()
    return folder


# ---- the library ------------------------------------------------------------------------------------------------
def test_a_stream_channel_lists_its_ticked_videos_as_youtube_entries(tmp_path):
    stream_channel(tmp_path, selected=[A, C])
    lib = Library(tmp_path)
    ch = lib.channels()[0]
    assert lib.videos(ch) == ["yt-" + A, "yt-" + C] and lib.has_videos(ch)
    assert lib.abspath(ch, "yt-" + A) == f"https://www.youtube.com/watch?v={A}"
    assert lib.title_of(ch, "yt-" + A) == "Video a" and lib.is_stream("yt-" + A) and not lib.is_stream("a.mp4")


def test_a_downloaded_video_in_a_stream_channel_plays_from_its_file(tmp_path):
    stream_channel(tmp_path, files={A: f"Video a [{A}].mp4"})
    lib = Library(tmp_path)
    assert lib.videos(lib.channels()[0]) == [f"Video a [{A}].mp4", "yt-" + B, "yt-" + C]


def test_download_channels_and_unticked_videos_never_stream(tmp_path):
    stream_channel(tmp_path, name="1.dl", mode="download")
    lib = Library(tmp_path)
    assert lib.videos(lib.channels()[0]) == [] and not lib.has_videos(lib.channels()[0])
    stream_channel(tmp_path, name="2.none", selected=[])
    assert not lib.has_videos(lib.channels()[1])


def test_a_made_up_youtube_name_is_not_turned_into_an_address(tmp_path):
    stream_channel(tmp_path)
    lib = Library(tmp_path)
    ch = lib.channels()[0]
    assert lib.abspath(ch, "yt-../../etc") == str(Path(ch.path) / "yt-../../etc") and not lib.is_stream("yt-../../etc")


# ---- on the TV ---------------------------------------------------------------------------------------------------
def tv(tmp_path, **player):
    stream_channel(tmp_path / "videos")
    return Rig(tmp_path, channels={"1.local": ["a.mp4"]}, player_args=player, settings={"channel": "3.youtube", "shuffle": False})


def overlay_text(rig):
    return rig.player.overlay


def test_streamed_videos_are_played_from_their_addresses(tmp_path):
    r = tv(tmp_path)
    try:
        assert wait_for(lambda: r.state["mode"] == "play" and r.player.alive())
        assert r.state["channel"] == "3.youtube"
        playlist = Path(r.app.m3u_path).read_text().splitlines()
        assert [p for p in playlist if "://" in p] == [f"https://www.youtube.com/watch?v={i}" for i in (A, B, C)]
    finally:
        r.stop()


def test_a_slow_load_shows_a_loading_message_that_goes_away(tmp_path):
    r = tv(tmp_path, url_delay=1.2, speed=1.0)
    try:
        r.player.command("set_property", "playlist-pos", 1)                     # straight to the first YouTube video
        assert wait_for(lambda: r.player.overlay is not None, timeout=3)        # "loading from YouTube..."
        assert wait_for(lambda: r.player.overlay is None, timeout=4)            # ...cleared once it is playing
        assert r.player.get("time-pos") is not None
    finally:
        r.stop()


def test_a_video_that_wont_play_is_skipped_with_an_explanation(tmp_path):
    r = tv(tmp_path, url_delay=0.5, url_fail=(A,))
    try:
        r.player.command("set_property", "playlist-pos", 1)
        assert wait_for(lambda: r.player.overlay is not None, timeout=3)
        assert wait_for(lambda: r.app._stream_notice_until > 0, timeout=4)       # the 'couldn't play' notice was raised
        assert r.player.get("playlist-pos") == 2                                  # mpv moved on, to the next static
        assert wait_for(lambda: r.app._stream_notice_until == 0 and r.player.overlay is None, timeout=5)   # and it clears itself
    finally:
        r.stop()


def test_skipping_away_while_it_loads_is_not_reported_as_a_failure(tmp_path):
    r = tv(tmp_path, url_delay=5.0)
    try:
        r.player.command("set_property", "playlist-pos", 1)
        assert wait_for(lambda: r.app._stream is not None, timeout=3)
        r.swipe_left()                                                            # you moved on yourself
        import time; time.sleep(0.8)
        assert r.app._stream_notice_until == 0
    finally:
        r.stop()


def test_with_no_wifi_the_message_says_so(tmp_path):
    stream_channel(tmp_path / "videos")
    r = Rig(tmp_path, channels={"1.local": ["a.mp4"]}, wifi=FakeWifi(ip=None, saved=True), setup_auto=False,
            player_args=dict(url_delay=0.4, url_fail=(A,)), settings={"channel": "3.youtube", "shuffle": False})
    try:
        r.player.command("set_property", "playlist-pos", 1)
        assert wait_for(lambda: r.app._stream_notice_until > 0, timeout=5)
        assert r.app.wifi_available and not r.app.online()
    finally:
        r.stop()
