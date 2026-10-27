import time

import pytest

from rig import Rig, wait_for
from tubeless import live
from tubeless.live import broadcast_order, slot, total_seconds

ORDER, DUR = ["a", "b", "c"], [100.0, 50.0, 200.0]
GAP = 0.6


# ---- the schedule ------------------------------------------------------------------------------------------------------------
def test_the_slot_is_whatever_is_on_air_by_the_clock():
    assert slot(ORDER, DUR, 0, GAP) == ("a", 0.0)
    assert slot(ORDER, DUR, 99.9, GAP) == ("a", pytest.approx(99.9))
    assert slot(ORDER, DUR, 100.3, GAP) == ("b", 0.0)                          # in the static between a and b: b starts from the top
    assert slot(ORDER, DUR, 100.7, GAP) == ("b", pytest.approx(0.1))
    assert slot(ORDER, DUR, 151.0, GAP) == ("c", 0.0)
    total = total_seconds(DUR, GAP)
    assert slot(ORDER, DUR, total + 5, GAP) == ("a", pytest.approx(5.0))       # and round again
    assert slot(ORDER, DUR, 100 * total + 5, GAP) == ("a", pytest.approx(5.0, abs=1e-3))
    assert slot(ORDER, DUR, total - 0.3, GAP)[0] == "a"                         # the gap after the last video leads back to the first


def test_every_moment_of_the_loop_lands_on_a_real_video_and_a_sensible_offset():
    total = total_seconds(DUR, GAP)
    lengths = dict(zip(ORDER, DUR))
    t = 0.0
    while t < total * 2:
        rel, pos = slot(ORDER, DUR, t, GAP)
        assert rel in lengths and 0 <= pos <= lengths[rel]
        t += 0.37


def test_no_schedule_unless_every_length_is_known():
    assert slot(ORDER, [100.0, None, 200.0], 0) is None and slot(ORDER, [100.0, 0, 200.0], 0) is None
    assert slot([], [], 0) is None and slot(ORDER, [1.0], 0) is None


def test_the_running_order_is_stable_and_a_new_video_only_slots_in():
    names = [f"v{i}.mp4" for i in range(12)]
    first = broadcast_order("1.shows", names)
    assert first == broadcast_order("1.shows", list(reversed(names))) == broadcast_order("1.shows", names)      # not dependent on how it is listed
    assert first != broadcast_order("2.other", names) and sorted(first) == sorted(names)                         # but it differs by channel
    grown = broadcast_order("1.shows", names + ["new.mp4"])
    assert [r for r in grown if r != "new.mp4"] == first                          # the rest keep their order
    assert broadcast_order("1.shows", names, in_order=lambda r: sorted(r)) == sorted(names)


# ---- in the app --------------------------------------------------------------------------------------------------------------
def make(tmp_path, seconds=100.0, **kw):
    r = Rig(tmp_path, channels={"1.cartoons": [f"c{i}.mp4" for i in range(4)], "2.nights": [f"n{i}.mp4" for i in range(3)]},
            duration=lambda p: seconds, **kw)
    return r


def known(r, channel_names, seconds=100.0):
    for c in r.app.library.channels():
        if c.name in channel_names:
            for rel in r.app.library.videos(c):
                r.app.index._entries[f"{c.name}/{rel}"] = {"duration": seconds}


def test_it_is_off_by_default_and_a_setting(tmp_path):
    r = make(tmp_path)
    try:
        assert r.app.live_tv is False and r.app.web_settings()["values"]["live_tv"] is False and r.app.web_settings()["live"] is None
        for bad in ("yes", 1, None):
            with pytest.raises(ValueError):
                r.app.set_web_setting("live_tv", bad)
        r.app.set_web_setting("live_tv", True)
        assert r.app.live_tv is True and r.app.web_settings()["live"] is not None
        assert wait_for(lambda: r.app.settings.get("live_tv") is True)
    finally:
        r.stop()
    assert make(tmp_path, start=False).app.live_tv is True                           # kept across a restart


def test_tuning_in_joins_what_is_on_air_and_off_means_the_old_ways(tmp_path):
    r = make(tmp_path)
    try:
        app = r.app
        known(r, ["1.cartoons"])
        channel = app.channels()[0]
        before = app.playlists.plan(channel, True)
        assert app._live_slot(channel) is None                                         # off: nothing is scheduled
        app.live_tv = True
        order, rel, pos = app._live_slot(channel)
        assert sorted(order) == sorted(app.library.videos(channel)) and rel in order and 0 <= pos <= 100
        plan = app.playlists.plan(channel, True)
        assert plan.order == order and plan.current == rel
        assert app.playlists.progress(channel)["current"] == rel and abs(app.playlists.progress(channel)["pos"] - pos) < 5
        # the same moment gives the same answer; a later moment, a later place
        again = app._live_slot(channel)
        assert again[0] == order
        # a channel whose lengths are not all known plays as usual
        other = app.channels()[1]
        assert app._live_slot(other) is None
        status = {c["name"]: c for c in app.live_status()["channels"]}
        assert status["1.cartoons"]["ready"] and status["1.cartoons"]["minutes"] == round((4 * 100.6) / 60) and not status["2.nights"]["ready"]
        assert status["2.nights"]["missing"] == 3
    finally:
        r.stop()


def test_the_channel_keeps_running_while_you_are_away(tmp_path, monkeypatch):
    r = make(tmp_path)
    try:
        app = r.app
        known(r, ["1.cartoons"])
        app.live_tv = True
        channel = app.channels()[0]
        clock = {"now": 1_000_000.0}
        monkeypatch.setattr(time, "time", lambda: clock["now"])
        first = app._live_slot(channel)
        clock["now"] += 250                                                              # two and a half videos later
        later = app._live_slot(channel)
        assert later[0] == first[0]                                                      # the same running order: no reshuffle
        total = live.total_seconds([100.0] * 4, app.statics.lengths()[0])
        clock["now"] = 1_000_000.0 + total                                               # a whole loop later: the very same spot
        assert app._live_slot(channel)[1:] == (first[1], pytest.approx(first[2], abs=1e-3))
        assert first[1:] != later[1:]
    finally:
        r.stop()


def test_a_finished_loop_returns_to_what_is_on_air_instead_of_reshuffling(tmp_path):
    r = make(tmp_path)
    try:
        app = r.app
        known(r, ["1.cartoons"])
        app.live_tv = True
        channel = app.channels()[0]
        plan = app.playlists.new_cycle(channel, True)
        assert plan.order == app._live_slot(channel)[0]
    finally:
        r.stop()


def test_streamed_videos_use_the_lengths_their_playlist_gave(tmp_path):
    from test_streaming import stream_channel
    stream_channel(tmp_path / "videos")
    r = Rig(tmp_path, channels={"1.cartoons": ["x.mp4"]})
    try:
        app = r.app
        app.live_tv = True
        channel = [c for c in app.channels() if c.name == "3.youtube"][0]
        found = app._live_channel(channel)
        assert found is not None and sorted(found[1]) == [100.0, 100.0, 100.0]
        assert app._live_slot(channel)[1].startswith("yt-")
    finally:
        r.stop()


def test_starting_a_channel_seeks_to_the_position_that_is_on_air(tmp_path, monkeypatch):
    r = make(tmp_path)
    try:
        app = r.app
        known(r, ["1.cartoons"])
        app.live_tv = True
        channel = app.channels()[0]
        total = live.total_seconds([100.0] * 4, app.statics.lengths()[0])
        monkeypatch.setattr(time, "time", lambda: 1_000_000.0 - (1_000_000.0 % total) + 150.0)       # 150 s into the loop: 49 s into the second video
        app.channel_index = 0
        app._net_waited = True
        app.start_playback()
        want = app._live_slot(channel)
        assert want[1] == app.plan.current and app._pending_resume is not None
        path, seconds, rel = app._pending_resume
        assert rel == want[1] and 40 < seconds < 52 and path.endswith(rel)                           # joined partway through, a few seconds back
    finally:
        r.stop()


# ---- reshuffling ---------------------------------------------------------------------------------------------------------------
def test_a_new_seed_gives_a_new_stable_order():
    names = [f"v{i}.mp4" for i in range(12)]
    first = broadcast_order("1.shows", names)
    assert broadcast_order("1.shows", names, seed=0) == first                      # seed 0 is how it always was
    second = broadcast_order("1.shows", names, seed=1)
    assert second != first and sorted(second) == sorted(names) and second == broadcast_order("1.shows", names, seed=1)
    assert broadcast_order("1.shows", names, seed=2) not in (first, second)


def test_reshuffling_changes_the_running_order_keeps_it_across_restarts_and_refuses_when_it_makes_no_sense(tmp_path):
    r = make(tmp_path)
    try:
        app = r.app
        known(r, ["1.cartoons"])
        channel = app.channels()[0]
        with pytest.raises(ValueError):
            app.reshuffle_live("1.cartoons")                                       # Live TV mode is off
        app.live_tv = True
        before = app._live_slot(channel)[0]
        app.reshuffle_live("1.cartoons")
        after = app._live_slot(channel)[0]
        assert after != before and sorted(after) == sorted(before) and app.live_seeds["1.cartoons"] >= 1
        assert app._live_slot(channel)[0] == after                                 # and it stays that way
        with pytest.raises(ValueError):
            app.reshuffle_live("9.none")
        app.set_channel_order("1.cartoons", "order")
        with pytest.raises(ValueError):
            app.reshuffle_live("1.cartoons")                                       # in order: nothing to reshuffle
        assert wait_for(lambda: r.app.settings.get("live_seeds") == r.app.live_seeds)
    finally:
        r.stop()
    again = make(tmp_path, start=False).app
    assert again.live_seeds["1.cartoons"] >= 1


def test_in_live_mode_the_orders_are_per_channel_settings_and_in_order_means_file_name_order(tmp_path):
    r = make(tmp_path)
    try:
        app = r.app
        known(r, ["1.cartoons"])
        app.live_tv = True
        channel = app.channels()[0]
        app.set_channel_order("1.cartoons", "order")
        assert app._live_slot(channel)[0] == sorted(app.library.videos(channel))   # by file name
        app.set_channel_order("1.cartoons", "shuffle")
        assert app._live_slot(channel)[0] != sorted(app.library.videos(channel)) or len(app.library.videos(channel)) < 3
    finally:
        r.stop()
