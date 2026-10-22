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
        r.player.command("set_property", "playlist-pos", 0)                     # straight to the first YouTube video (a streaming channel has no static between videos: position 0)
        assert wait_for(lambda: r.player.overlay is not None, timeout=3)        # "loading from YouTube..."
        assert wait_for(lambda: r.player.overlay is None, timeout=4)            # ...cleared once it is playing
        assert r.player.get("time-pos") is not None
    finally:
        r.stop()


def test_a_video_that_wont_play_is_skipped_with_an_explanation(tmp_path):
    r = tv(tmp_path, url_delay=0.5, url_fail=(A,))
    try:
        r.player.command("set_property", "playlist-pos", 0)
        assert wait_for(lambda: r.player.overlay is not None, timeout=3)
        assert wait_for(lambda: r.app._stream_notice_until > 0, timeout=4)       # the 'couldn't play' notice was raised
        assert r.player.get("playlist-pos") == 1                                  # mpv moved on, to the next video
        assert wait_for(lambda: r.app._stream_notice_until == 0 and r.player.overlay is None, timeout=5)   # and it clears itself
    finally:
        r.stop()


def test_skipping_away_while_it_loads_is_not_reported_as_a_failure(tmp_path):
    r = tv(tmp_path, url_delay=5.0)
    try:
        r.player.command("set_property", "playlist-pos", 0)
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
        r.player.command("set_property", "playlist-pos", 0)
        assert wait_for(lambda: r.app._stream_notice_until > 0, timeout=5)
        assert r.app.wifi_available and not r.app.online()
    finally:
        r.stop()


def test_a_video_that_never_starts_loading_is_skipped_after_a_while(tmp_path):
    r = tv(tmp_path, url_delay=60.0)                                              # the load just hangs
    try:
        r.app.stream_give_up, r.app.stream_restart = 0.6, 99.0
        r.player.command("set_property", "playlist-pos", 0)
        assert wait_for(lambda: r.player.get("playlist-pos") is not None and r.player.get("playlist-pos") >= 1, timeout=5)   # moved on
        assert wait_for(lambda: r.app._stream_notice_until > 0, timeout=5)        # and said so
    finally:
        r.stop()


def test_if_mpv_stays_stuck_the_player_is_restarted_past_the_stuck_video(tmp_path):
    r = tv(tmp_path, url_delay=60.0)
    try:
        starts = []
        real = r.player.start
        r.player.start = lambda path: (starts.append(path), real(path))[1]
        r.app.stream_give_up, r.app.stream_restart = 99.0, 0.6                    # skipping did not help (or was not possible)
        r.player.command("set_property", "playlist-pos", 0)
        assert wait_for(lambda: len(starts) >= 1, timeout=5)
        prog = r.app.playlists.progress(r.app.plan.channel)
        assert prog["current"] != "yt-" + A and prog["current"].startswith("yt-")  # it carries on from the next video, not the stuck one
    finally:
        r.stop()


# ---- no static between videos on a streaming channel, so mpv can open the next one while this one plays ---------------------
def test_a_streaming_channel_has_no_static_between_its_videos_but_a_download_channel_does(tmp_path):
    from tubeless.library import Playlists
    from tubeless.state import Store
    import random
    root = tmp_path / "videos"
    stream_channel(root, name="1.stream")
    stream_channel(root, name="2.download", mode="download", files={A: f"Video a [{A}].mp4", B: f"Video b [{B}].mp4"})
    (root / "3.folder").mkdir()
    (root / "3.folder" / "x.mp4").write_bytes(b"x")
    lib = Library(root)
    plans = Playlists(lib, Store(tmp_path / "data"), "/static.mp4", rng=random.Random(1))
    s, d, f = lib.channels()
    ps = plans.plan(s, False)
    assert ps.static is False and ps.stride == 1
    entries = plans.entries(ps)
    assert entries == [f"https://www.youtube.com/watch?v={i}" for i in (A, B, C)]               # just the three videos
    assert [plans.episode_at(ps, n) for n in range(3)] == ["yt-" + A, "yt-" + B, "yt-" + C] and plans.episode_at(ps, 3) is None
    assert plans.plan(d, False).static is True and plans.plan(f, False).static is True          # the others keep their static
    assert plans.entries(plans.plan(f, False)) == ["/static.mp4", str(root / "3.folder" / "x.mp4")]
    assert plans.new_cycle(s, False).static is False


def test_swiping_on_a_streaming_channel_moves_one_video_at_a_time(tmp_path):
    r = tv(tmp_path)
    try:
        assert wait_for(lambda: r.state["channel"] == "3.youtube" and r.player.alive())
        playlist = Path(r.app.m3u_path).read_text().splitlines()
        assert len(playlist) == 3 and all("youtube.com" in p for p in playlist) and r.state["playlist_count"] == 3
        r.player.command("set_property", "playlist-pos", 0)
        r.swipe_left()
        assert wait_for(lambda: r.player.get("playlist-pos") == 1)
        r.swipe_left()
        assert wait_for(lambda: r.player.get("playlist-pos") == 2)
        r.swipe_right()
        assert wait_for(lambda: r.player.get("playlist-pos") == 1)
        assert wait_for(lambda: r.app.playlists.progress(r.app.plan.channel).get("current") == "yt-" + B)     # and it knows which one
    finally:
        r.stop()


def test_the_channel_change_static_is_not_mistaken_for_the_first_video(tmp_path):
    """With no static in the playlist, position 0 is a video: while the one-off channel-change static plays, the TV must not
    think the first YouTube video has started (it would stop watching for it to load, and never show 'loading')."""
    stream_channel(tmp_path / "videos")
    r = Rig(tmp_path, channels={"1.local": ["a.mp4", "b.mp4"]}, settings={"channel": "1.local", "shuffle": False},
            player_args=dict(url_delay=0.4))
    try:
        r.app.switch_delay = 1.0                                                  # a longer static, so there is time to look at it
        r.app.switch_channel(1)                                                   # to the streaming channel
        assert wait_for(lambda: r.app.player.get("path") in r.app.statics.paths(), timeout=2)       # the static is playing
        r.app._track()
        assert r.app._stream is None and r.app._cur_rel is None                   # not mistaken for a video
        r.swipe_left()                                                            # a swipe during the static does nothing
        assert r.player.get("path") in r.app.statics.paths()
        assert wait_for(lambda: "youtube.com" in (r.player.get("path") or ""), timeout=4)           # then the playlist loads
        assert wait_for(lambda: r.app._stream is not None or r.app._cur_rel is not None, timeout=4)
    finally:
        r.stop()


def test_how_long_a_stream_took_to_start_is_logged(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="tubeless.app")
    r = tv(tmp_path, url_delay=0.6)
    try:
        r.player.command("set_property", "playlist-pos", 0)
        assert wait_for(lambda: any("stream started playing" in x.getMessage() for x in caplog.records), timeout=5)
    finally:
        r.stop()
    line = [x.getMessage() for x in caplog.records if "stream started playing" in x.getMessage()][0]
    assert "s after the TV reached it" in line


def test_a_stream_channels_playlist_end_is_noticed_without_statics(tmp_path):
    r = tv(tmp_path)
    try:
        assert wait_for(lambda: r.player.alive())
        r.player.command("set_property", "playlist-pos", 2)                      # the last video
        r.app._track()
        assert r.app._last_pos == (2, 3)
        r.app.plan.static = False
        # 'finished' = at (or one before) the end for this layout: pos >= count - stride
        assert r.app._last_pos[0] >= r.app._last_pos[1] - r.app.plan.stride
    finally:
        r.stop()
