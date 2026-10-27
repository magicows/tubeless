"""No static between videos: the playlist is video, video, ... (a clean cut), and an ad, where one is due, is the only break."""
import random

import pytest

from rig import Rig, wait_for
from tubeless.ads import AdsManager
from tubeless.library import Library, Playlists
from tubeless.skip import layout_target, skip_target
from tubeless.state import Settings, Store

OFF = 0.0                                                            # a static's length, off


# --- the layout and where skipping goes -----------------------------------------------------------------------------
def test_with_a_static_before_every_video_the_layout_skips_exactly_as_before():
    layout = [x for i in range(20) for x in (None, i)]
    for pos in range(len(layout)):
        for d in (+1, -1):
            assert layout_target(layout, pos, d) == skip_target(pos, len(layout), d), (pos, d)


def test_without_static_a_skip_goes_to_the_next_or_previous_video():
    layout = list(range(5))                                              # video, video, video, video, video
    assert layout_target(layout, 2, +1) == ("goto", 3) and layout_target(layout, 2, -1) == ("goto", 1)
    assert layout_target(layout, 4, +1) == ("end",)
    assert layout_target(layout, 0, -1) == ("goto", 4)                    # back from the first wraps to the last
    assert layout_target(layout, None, +1) is None and layout_target(layout, 9, +1) is None and layout_target([], 0, +1) is None


def test_an_ad_between_two_videos_is_where_the_next_one_starts():
    layout = [0, 1, None, 2, 3]                                          # an ad (position 2) in front of the third video
    assert layout_target(layout, 1, +1) == ("goto", 2)                    # the app then steps over it: a skip never plays an ad
    assert layout_target(layout, 3, -1) == ("goto", 1) and layout_target(layout, 4, -1) == ("goto", 2)
    assert layout_target(layout, 2, +1) == ("goto", 4) and layout_target(layout, 2, -1) == ("goto", 1)     # on the ad: it belongs to the video after


# --- the playlist ---------------------------------------------------------------------------------------------------
def build(tmp_path, count=7):
    root = tmp_path / "videos" / "1.shows"
    root.mkdir(parents=True)
    for i in range(count):
        (root / f"v{i}.mp4").write_bytes(b"")
    lib = Library(tmp_path / "videos")
    pl = Playlists(lib, Store(tmp_path / "pl"), "/s.mp4", rng=random.Random(1))
    return lib, pl, pl.plan(lib.channels()[0], False)


def test_without_static_the_playlist_is_just_the_videos(tmp_path):
    lib, pl, plan = build(tmp_path)
    pl.static_on = False
    out = pl.entries(plan)
    assert out == [lib.abspath(plan.channel, r) for r in plan.rotated] and pl.ad_positions == set()
    assert [pl.episode_at(plan, i) for i in range(7)] == plan.rotated and pl.episode_at(plan, 7) is None
    assert pl.next_video_pos(plan, 2) == 3 and pl.next_video_pos(plan, 6) is None


def test_before_a_playlist_is_written_the_layout_follows_the_static_setting(tmp_path):
    lib, pl, plan = build(tmp_path, 3)
    assert pl.layout(plan) == [None, 0, None, 1, None, 2]
    pl.static_on = False
    assert pl.layout(plan) == [0, 1, 2]


def test_without_static_an_ad_is_the_only_thing_between_two_videos(tmp_path):
    lib, pl, plan = build(tmp_path)
    folder = tmp_path / "ads"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"x")
    ads = AdsManager(folder, Settings(Store(tmp_path / "data")), rng=random.Random(3))
    ads.set_enabled(True)
    ads.set_every(3)
    pl.static_slot, pl.static_on = ads.slot_entry, False
    out = pl.entries(plan)
    assert len(out) == 9 and pl.ad_positions == {3, 7}                   # ads in front of videos 3 and 6
    assert out[3] == str(folder / "a.mp4") and out[7] == str(folder / "a.mp4") and not any(e.startswith("edl://") for e in out)
    assert [pl.episode_at(plan, i) for i in range(9)] == [plan.rotated[0], plan.rotated[1], plan.rotated[2], None, plan.rotated[3],
                                                          plan.rotated[4], plan.rotated[5], None, plan.rotated[6]]
    assert pl.next_video_pos(plan, 2) == 4                               # the look-ahead steps over the ad


def test_the_ads_slot_with_no_static(tmp_path):
    folder = tmp_path / "ads"
    folder.mkdir()
    ads = AdsManager(folder, Settings(Store(tmp_path / "data")), rng=random.Random(3))
    assert ads.slot_entry(None, 4) is None                               # off
    ads.set_enabled(True)
    ads.set_every(2)
    assert ads.slot_entry(None, 4) is None and ads.slot_entry("/s.mp4", 4) == "/s.mp4"          # on, but the folder is empty
    (folder / "a.mp4").write_bytes(b"x")
    assert ads.slot_entry(None, 4) == str(folder / "a.mp4") and ads.slot_entry(None, 3) is None and ads.slot_entry(None, 0) is None
    assert ads.slot_entry("/s.mp4", 4).startswith("edl://")              # with static the ad still follows it in one entry


# --- the setting and the app ----------------------------------------------------------------------------------------
@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, channels={"1.shows": [f"s{i}.mp4" for i in range(6)], "2.other": ["o0.mp4", "o1.mp4"]},
            ads_dir=str(tmp_path / "ads"), settings={"static_between": OFF})
    yield r
    r.stop()


def test_turning_the_static_between_videos_off_takes_it_out_of_the_playlist(rig):
    st = rig.app.statics
    assert st.between_on() is False and st.gap() == 0.0 and rig.app.playlists.static_on is False
    st.set_lengths(between=0.6)
    assert st.between_on() is True and st.gap() == 0.6 and rig.app.playlists.static_on is True
    st.set_lengths(between=0.0)
    assert rig.app.playlists.static_on is False


def test_the_app_plays_video_to_video_and_knows_which_is_which(rig):
    lines = open(rig.app.m3u_path).read().splitlines()
    assert len(lines) == 6 and all(l.endswith(".mp4") and "static" not in l for l in lines)
    assert wait_for(lambda: rig.state["playlist_pos"] == 0)
    assert wait_for(lambda: rig.app.playlists.progress(rig.app.channels()[0]).get("current") == rig.app.plan.rotated[0])
    rig.app.skip_video(+1)
    assert wait_for(lambda: rig.state["playlist_pos"] == 1)
    assert wait_for(lambda: rig.app.playlists.progress(rig.app.channels()[0]).get("current") == rig.app.plan.rotated[1])
    rig.app.skip_video(-1)
    assert wait_for(lambda: rig.state["playlist_pos"] == 0)
    rig.app.skip_video(-1)                                               # back from the first wraps to the last
    assert wait_for(lambda: rig.state["playlist_pos"] == 5)


def test_changing_channel_does_not_mistake_the_static_for_the_first_video(rig):
    rig.app.switch_delay = 0.6
    other = rig.app.channels()[1]
    rig.app.switch_channel(1)
    assert wait_for(lambda: rig.state["channel"] == "2.other")
    assert rig.app._episode_at(0) is None                                # while the channel-change static shows, nothing counts as playing
    rig.app.skip_video(+1)                                               # and a skip then has nothing to skip
    assert rig.state["playlist_pos"] in (0, None)
    assert wait_for(lambda: rig.state["playlist_pos"] == 0 and rig.app._episode_at(0) == rig.app.plan.rotated[0], timeout=6)
    assert wait_for(lambda: rig.app.playlists.progress(other).get("current") == rig.app.plan.rotated[0])


def test_with_ads_on_and_no_static_the_ad_alone_sits_between_videos_and_a_skip_steps_over_it(tmp_path):
    r = Rig(tmp_path, channels={"1.shows": [f"s{i}.mp4" for i in range(8)]}, ads_dir=str(tmp_path / "ads"),
            settings={"static_between": OFF, "ads_on": True, "ads_every": 3})
    try:
        (tmp_path / "ads").mkdir()
        (tmp_path / "ads" / "ad.mp4").write_bytes(b"x")
        r.app.reload_channel()
        lines = open(r.app.m3u_path).read().splitlines()
        assert len(lines) == 10 and lines[3] == lines[7] == str(tmp_path / "ads" / "ad.mp4")      # ads in front of videos 3 and 6
        assert not any(l.startswith("edl://") for l in lines) and r.app.playlists.ad_positions == {3, 7}
        r.player.command("set_property", "playlist-pos", 2)
        r.app.skip_video(+1)                                         # the next start is the ad at 3: straight to the video at 4
        assert wait_for(lambda: r.player.get("playlist-pos") == 4)
        r.app.skip_video(-1)                                         # back: the start of the video before, position 2
        assert wait_for(lambda: r.player.get("playlist-pos") == 2)
    finally:
        r.stop()


def test_with_the_channel_change_static_off_the_new_channel_just_starts(tmp_path):
    r = Rig(tmp_path, settings={"static_change": OFF})
    try:
        r.app.switch_delay = 5.0                                         # were the static on, the new channel would not load for this long
        r.app.switch_channel(1)
        assert wait_for(lambda: r.state["channel"] == "2.nights", timeout=2)
        assert r.app._switch_timer is None
        assert wait_for(lambda: r.state["playlist_count"] == 6 and r.state["playlist_pos"] == 1, timeout=2)       # its own playlist, on its first video
        assert wait_for(lambda: r.app.playlists.progress(r.app.channels()[1]).get("current") == r.app.plan.rotated[0], timeout=3)
        r.app.skip_video(+1)                                             # and skipping works at once
        assert wait_for(lambda: r.state["playlist_pos"] == 3)
    finally:
        r.stop()
