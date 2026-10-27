"""With the network down, channels that only stream are not offered or tuned to; a mixed channel plays just its downloaded videos."""
import random
import time

import pytest

from rig import Rig, wait_for
from test_streaming import A, B, C, stream_channel
from tubeless.ads import AdsManager
from tubeless.library import Library, Playlists
from tubeless.menu import MenuContext, MenuModel
from tubeless.state import Settings, Store
from tubeless.wifi import FakeWifi


def library(tmp_path):
    root = tmp_path / "videos"
    (root / "1.local").mkdir(parents=True)
    (root / "1.local" / "a.mp4").write_bytes(b"")
    stream_channel(root, name="2.stream")                                                   # streamed only
    stream_channel(root, name="3.mixed", files={A: f"Video a [{A}].mp4"})                    # one downloaded, two streamed
    lib = Library(root)
    return lib, {c.name: c for c in lib.channels()}


# --- the library and the playlist ------------------------------------------------------------------------------------
def test_with_the_network_down_only_downloaded_videos_count(tmp_path):
    lib, ch = library(tmp_path)
    down = {"v": False}
    lib.offline = lambda: down["v"]
    assert all(lib.has_videos(c) for c in ch.values()) and lib.next_playable(1, +1) == 1
    down["v"] = True
    assert lib.has_videos(ch["1.local"]) and lib.has_videos(ch["3.mixed"]) and not lib.has_videos(ch["2.stream"])
    assert lib.next_playable(1, +1) == 2 and lib.next_playable(1, -1) == 0                 # a swipe either way steps over the streaming channel
    assert len(lib.videos(ch["2.stream"])) == 3                                              # (what the channel holds does not change)
    down["v"] = False
    assert lib.has_videos(ch["2.stream"])                                                    # and it is back with the network


def test_a_mixed_channel_plays_only_its_downloaded_videos_offline_and_keeps_its_place_online(tmp_path):
    lib, ch = library(tmp_path)
    pl = Playlists(lib, Store(tmp_path / "pl"), "/s.mp4", rng=random.Random(1))
    plan = pl.plan(ch["3.mixed"], False)
    online = pl.entries(plan)
    assert len(online) == 6 and [pl.episode_at(plan, i) is not None for i in range(6)] == [False, True] * 3
    lib.offline = lambda: True
    out = pl.entries(plan)
    assert len(out) == 2 and out[0] == "/s.mp4" and out[1].endswith(f"Video a [{A}].mp4")   # static, the one downloaded video
    assert pl.episode_at(plan, 1) == f"Video a [{A}].mp4" and pl.episode_at(plan, 3) is None
    lib.offline = lambda: False
    assert pl.entries(plan) == online                                                        # the plan was never touched: all three again


def test_a_channel_with_nothing_downloaded_keeps_its_streamed_videos_so_the_tv_can_say_why_they_cannot_play(tmp_path):
    lib, ch = library(tmp_path)
    pl = Playlists(lib, Store(tmp_path / "pl"), "/s.mp4", rng=random.Random(1))
    lib.offline = lambda: True
    assert len(pl.entries(pl.plan(ch["2.stream"], False))) == 6


def test_ads_are_counted_among_the_videos_that_play(tmp_path):
    lib, ch = library(tmp_path)
    folder = tmp_path / "ads"
    folder.mkdir()
    (folder / "ad.mp4").write_bytes(b"x")
    ads = AdsManager(folder, Settings(Store(tmp_path / "data")), rng=random.Random(3))
    ads.set_enabled(True)
    ads.set_every(2)
    pl = Playlists(lib, Store(tmp_path / "pl"), "/s.mp4", rng=random.Random(1))
    pl.static_slot = ads.slot_entry
    lib.offline = lambda: True
    stream_channel(tmp_path / "videos", name="4.more", files={A: "a1.mp4", B: "a2.mp4", C: "a3.mp4"}, ids=(A, B, C))
    plan = pl.plan([c for c in lib.channels() if c.name == "4.more"][0], False)
    out = pl.entries(plan)
    assert sum(e.startswith("edl://") for e in out) == 1                                     # an ad before the third video, as usual


# --- the menu and the guide ----------------------------------------------------------------------------------------
def test_the_menu_and_the_guide_mark_the_channels_that_need_wifi():
    ctx = MenuContext(channels=["a", "b", "c"], needs_wifi=[False, True, False], guide=["Next: x", "Needs Wi-Fi", "3 videos"])
    m = MenuModel()
    rows = m.items(ctx)[5:]
    assert [r.value for r in rows] == [None, "No Wi-Fi", None]
    m.open_page("guide")
    assert [r.detail for r in m.items(ctx)] == ["Next: x", "Needs Wi-Fi", "3 videos"]
    assert [r.value for r in MenuModel().items(MenuContext(channels=["a"]))[5:]] == [None]   # no flags: nothing marked


# --- in the app ------------------------------------------------------------------------------------------------------
def tv(tmp_path, wifi, **kw):
    root = tmp_path / "videos"
    stream_channel(root, name="2.stream")
    return Rig(tmp_path, channels={"1.local": ["a.mp4"], "3.local": ["c.mp4"]}, wifi=wifi, setup_auto=False, settings={"shuffle": False}, **kw)


def test_the_wifi_guard_keeps_the_offline_flag(tmp_path):
    wifi = FakeWifi(ip="192.168.1.9", saved=True)
    r = tv(tmp_path, wifi)
    try:
        assert r.app._offline is False
        wifi.ip = None
        r.app._wifi_guard()
        assert r.app._offline is True
        wifi.ip = "192.168.1.9"
        wifi.zombie = True                                                                   # an address, but the router does not answer
        r.app._wifi_guard()
        assert r.app._offline is True
        wifi.zombie = False
        r.app._wifi_guard()
        assert r.app._offline is False
    finally:
        r.stop()


def test_a_swipe_steps_over_a_streaming_channel_offline_and_the_menu_and_guide_say_why(tmp_path):
    r = tv(tmp_path, FakeWifi(ip="192.168.1.9", saved=True))
    try:
        app = r.app
        assert [c.name for c in app.channels()] == ["1.local", "2.stream", "3.local"] and r.state["channel"] == "1.local"
        app._set_offline(True)
        assert app._now_on(app.channels()[1]) == "Needs Wi-Fi" and app._now_on(app.channels()[0]).endswith("1 video")
        assert app._menu_ctx().needs_wifi == [False, True, False]
        app.switch_channel(app.channel_index + 1, step=+1)                                   # swipe up from channel 1
        assert wait_for(lambda: r.state["channel"] == "3.local")                             # lands on 3, over the streaming 2
        app.switch_channel(1)                                                                # tapping it in the menu does nothing
        time.sleep(0.4)
        assert r.state["channel"] == "3.local"
        app._set_offline(False)
        assert app._menu_ctx().needs_wifi == () and app._now_on(app.channels()[1]) == "3 videos"
        app.switch_channel(1)
        assert wait_for(lambda: r.state["channel"] == "2.stream")                            # back with the network
    finally:
        r.stop()


def test_starting_with_no_network_on_a_streaming_channel_starts_on_one_with_downloaded_videos(tmp_path):
    root = tmp_path / "videos"
    stream_channel(root, name="2.stream")
    r = Rig(tmp_path, channels={"1.local": ["a.mp4"]}, wifi=FakeWifi(ip=None, saved=True), setup_auto=False,
            settings={"channel": "2.stream", "shuffle": False})
    try:
        assert wait_for(lambda: r.state["channel"] == "1.local" and r.state["mode"] == "play")
        assert r.app.settings.get("channel") == "1.local"
    finally:
        r.stop()
