import json
import subprocess
import threading
import time

import pytest

from rig import Rig, wait_for
from test_streaming import A, B, C, tv
from tubeless import health, lookahead
from tubeless.lookahead import Lookahead, build_entry

V, AU = "https://rr1.example.com/video?id=1&sig=abc", "https://rr1.example.com/audio?id=1&sig=def"


# ---- the address ---------------------------------------------------------------------------------------------------------
def test_two_addresses_are_joined_the_way_mpvs_own_hook_joins_them():
    entry = build_entry([V, AU])
    assert entry == (f"edl://!new_stream;!no_clip;!no_chapters;%{len(V)}%{V};!new_stream;!no_clip;!no_chapters;%{len(AU)}%{AU}")
    assert build_entry([V]) == V                                          # one ready-made file: used as it is
    odd = "https://x.example.com/a;b,c d?q=1"
    assert f"%{len(odd)}%{odd}" in build_entry([odd, odd])                # characters that are special in an edl:// entry are safe
    assert f"%{len('https://x/é'.encode())}%" in build_entry(["https://x/é", V])   # the length is in bytes


@pytest.mark.parametrize("urls", [[], [""], [V, AU, V], ["http://insecure.example.com/x"], ["file:///etc/passwd"], [V, "ftp://x/y"],
                                  ["https://x.example.com/" + "a" * 9000]])
def test_anything_odd_is_refused_so_the_video_just_loads_the_usual_way(urls):
    assert build_entry(urls) is None


# ---- asking yt-dlp -------------------------------------------------------------------------------------------------------
class FakeRun:
    def __init__(self, stdout=V + "\n" + AU + "\n", returncode=0, error=None):
        self.stdout, self.returncode, self.error, self.cmds = stdout, returncode, error, []

    def __call__(self, cmd, **kw):
        self.cmds.append((cmd, kw))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_resolve_asks_yt_dlp_politely_for_the_chosen_quality_and_builds_the_entry():
    run = FakeRun()
    la = Lookahead(lambda: "/usr/bin/yt-dlp", lambda: 240, runner=run)
    assert la.resolve("aaaaaaaaaaa") == build_entry([V, AU])
    cmd, kw = run.cmds[0]
    assert cmd[:3] == ["nice", "-n", "19"] and "-g" in cmd and "--no-playlist" in cmd and cmd[-1].endswith("watch?v=aaaaaaaaaaa")
    assert cmd[cmd.index("--") - 0] == "--" and "height<=240" in cmd[cmd.index("-f") + 1] and "height<=360" not in cmd[cmd.index("-f") + 1]
    assert kw["timeout"] == 75.0                                           # it can never hang for ever


@pytest.mark.parametrize("run,path", [(FakeRun(returncode=1), "/usr/bin/yt-dlp"), (FakeRun(stdout=""), "/usr/bin/yt-dlp"),
                                      (FakeRun(error=OSError("no such file")), "/usr/bin/yt-dlp"),
                                      (FakeRun(error=subprocess.TimeoutExpired("x", 75)), "/usr/bin/yt-dlp"), (FakeRun(), None)])
def test_resolve_gives_up_quietly(run, path):
    assert Lookahead(lambda: path, runner=run).resolve("aaaaaaaaaaa") is None


def test_one_video_at_a_time_and_a_ready_address_is_remembered_then_forgotten():
    gate = threading.Event()
    la = Lookahead(lambda: "/x")
    la.resolver = lambda vid: (gate.wait(5), f"entry-for-{vid}")[1]
    assert la.start("aaaaaaaaaaa") is True and la.busy()
    assert la.start("bbbbbbbbbbb") is False                                 # one at a time: a second does not pile on
    assert la.ready("aaaaaaaaaaa") is None
    gate.set()
    assert wait_for(lambda: not la.busy())
    entry, took = la.ready("aaaaaaaaaaa")
    assert entry == "entry-for-aaaaaaaaaaa" and took >= 0
    la.forget("aaaaaaaaaaa")
    assert la.ready("aaaaaaaaaaa") is None


def test_an_old_address_is_dropped_because_they_expire():
    now = [1000.0]
    la = Lookahead(lambda: "/x", clock=lambda: now[0])
    la.resolver = lambda vid: "entry"
    la.start("aaaaaaaaaaa")
    assert wait_for(lambda: la.ready("aaaaaaaaaaa") is not None)
    now[0] += lookahead.TTL + 1
    assert la.ready("aaaaaaaaaaa") is None


def test_a_resolver_that_crashes_leaves_nothing_stuck():
    la = Lookahead(lambda: "/x")
    la.resolver = lambda vid: 1 / 0
    la.start("aaaaaaaaaaa")
    assert wait_for(lambda: not la.busy())
    assert la.ready("aaaaaaaaaaa") is None and la.start("bbbbbbbbbbb") is True


# ---- in the app ---------------------------------------------------------------------------------------------------------
def entry_for(vid):
    return f"edl://!new_stream;!no_clip;!no_chapters;%{len('https://x/' + vid)}%https://x/{vid}"


def watch(tmp_path, **kw):
    """A TV on a YouTube channel that streams: playlist [static, A, static, B, static, C]; look-ahead on and quick."""
    r = tv(tmp_path)
    app = r.app
    app.lookahead_enabled, app.lookahead_after = True, 0.2
    calls = []
    app.lookahead.resolver = lambda vid: (calls.append(vid), entry_for(vid))[1]
    r.calls = calls
    return r


def url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


def test_the_next_videos_address_is_swapped_in_while_the_current_one_plays(tmp_path):
    r = watch(tmp_path)
    try:
        assert wait_for(lambda: r.player.alive() and len(r.player.playlist) == 6)
        before = list(r.player.playlist)
        r.player.command("set_property", "playlist-pos", 1)                    # video A
        assert wait_for(lambda: r.player.playlist[3].startswith("edl://"), timeout=6)
        after = r.player.playlist
        assert r.calls == [B] and len(after) == 6                              # asked for B only, and nothing was added or lost
        assert after[3] == entry_for(B) and after[1] == url(A) and after[5] == url(C)
        assert after[0] == after[2] == after[4] == before[0]                    # the statics are where they were
        assert r.app.playlists.episode_at(r.app.plan, 3) == "yt-" + B           # position 3 is still video B, so everything keeps working
        assert r.player.get("playlist-pos") == 1                                # and the video playing was not disturbed
        r.player.command("set_property", "playlist-pos", 3)
        assert wait_for(lambda: r.app._cur_rel == "yt-" + B and r.player.get("time-pos") is not None, timeout=4)
    finally:
        r.stop()


def test_nothing_happens_when_the_switch_is_off(tmp_path):
    r = watch(tmp_path)
    try:
        r.app.lookahead_enabled = False
        r.player.command("set_property", "playlist-pos", 1)
        time.sleep(1.0)
        assert r.calls == [] and not r.player.playlist[3].startswith("edl://")
    finally:
        r.stop()


def test_nothing_happens_on_the_last_video_or_on_a_channel_of_files_or_when_mpv_is_too_old(tmp_path):
    r = watch(tmp_path)
    try:
        r.player.command("set_property", "playlist-pos", 5)                    # the last video: nothing after it
        time.sleep(0.8)
        assert r.calls == []
        r.player.supports_lookahead = False
        r.player.command("set_property", "playlist-pos", 1)
        time.sleep(0.8)
        assert r.calls == []                                                   # an mpv without `insert-at`: stay out of it
    finally:
        r.stop()
    local = Rig(tmp_path / "local")
    try:
        local.app.lookahead_enabled, local.app.lookahead_after = True, 0.1
        local.app.lookahead.resolver = lambda vid: pytest.fail("a channel of files has nothing to look up")
        local.player.command("set_property", "playlist-pos", 1)
        time.sleep(0.8)
    finally:
        local.stop()


def test_it_waits_for_the_current_video_to_settle_and_for_enough_memory(tmp_path, monkeypatch):
    r = watch(tmp_path)
    try:
        r.app.lookahead_after = 30.0                                           # not yet: the video has only just started
        r.player.command("set_property", "playlist-pos", 1)
        time.sleep(0.8)
        assert r.calls == []
        r.app.lookahead_after = 0.2
        monkeypatch.setattr(health, "available_mb", lambda: 20)                # memory is short: leave it
        time.sleep(0.8)
        assert r.calls == [] and not r.app.lookahead.busy()
        monkeypatch.setattr(health, "available_mb", lambda: 200)
        assert wait_for(lambda: r.calls == [B], timeout=4)                    # and when there is room, it goes ahead
    finally:
        r.stop()


def test_a_failed_lookup_is_not_repeated_every_few_seconds(tmp_path):
    r = watch(tmp_path)
    try:
        tries = []
        r.app.lookahead.resolver = lambda vid: (tries.append(vid), None)[1]     # yt-dlp could not do it
        r.player.command("set_property", "playlist-pos", 1)
        assert wait_for(lambda: len(tries) == 1, timeout=4)
        time.sleep(1.2)
        assert tries == [B] and not r.player.playlist[3].startswith("edl://")   # tried once; the video will load the usual way
    finally:
        r.stop()


def test_a_ready_address_is_not_swapped_in_if_its_moment_has_passed(tmp_path):
    r = watch(tmp_path)
    try:
        gate = threading.Event()
        r.app.lookahead.resolver = lambda vid: (gate.wait(5), entry_for(vid))[1]
        r.player.command("set_property", "playlist-pos", 1)
        assert wait_for(lambda: r.app.lookahead.busy(), timeout=4)
        r.player.command("set_property", "playlist-pos", 3)                    # the TV reached video B before the address came
        gate.set()
        time.sleep(1.0)
        assert r.player.playlist[3] == url(B) and len(r.player.playlist) == 6  # B is already loading the usual way: leave it alone
    finally:
        r.stop()


def test_a_playlist_entry_that_is_not_the_expected_link_is_left_alone(tmp_path):
    r = watch(tmp_path)
    try:
        gate = threading.Event()
        r.app.lookahead.resolver = lambda vid: (gate.wait(5), entry_for(vid))[1]
        r.player.command("set_property", "playlist-pos", 1)
        assert wait_for(lambda: r.app.lookahead.busy(), timeout=4)
        r.player.playlist[3] = "/somewhere/else.mp4"                           # the playlist changed under it
        gate.set()
        time.sleep(1.0)
        assert r.player.playlist[3] == "/somewhere/else.mp4" and len(r.player.playlist) == 6
    finally:
        r.stop()


def test_if_the_swap_leaves_the_playlist_the_wrong_shape_it_switches_off_and_rebuilds(tmp_path):
    r = watch(tmp_path)
    try:
        real = r.player.command
        r.player.command = lambda *a: None if a[0] == "playlist-remove" else real(*a)      # the removal does not happen
        real("set_property", "playlist-pos", 1)
        assert wait_for(lambda: r.app.lookahead_enabled is False, timeout=6)              # it noticed and gave up
        r.player.command = real
        assert wait_for(lambda: len(r.player.playlist) == 6 and r.player.playlist[3] == url(B), timeout=4)   # and rebuilt it as it should be
        assert r.app.playlists.episode_at(r.app.plan, 3) == "yt-" + B
    finally:
        r.stop()


# ---- the switch on the page ----------------------------------------------------------------------------------------------
def test_the_switch_is_a_saved_setting_that_is_off_by_default(tmp_path):
    r = Rig(tmp_path)
    try:
        assert r.app.lookahead_enabled is False and r.app.web_settings()["values"]["youtube_lookahead"] is False
        r.app.set_web_setting("youtube_lookahead", True)
        assert r.app.lookahead_enabled is True and r.app.web_settings()["values"]["youtube_lookahead"] is True
        for bad in ("yes", 1, None):
            with pytest.raises(ValueError):
                r.app.set_web_setting("youtube_lookahead", bad)
        assert json.loads((r.cfg.data / "settings.json").read_text())["youtube_lookahead"] is True
    finally:
        r.stop()
    assert Rig(tmp_path, start=False).app.lookahead_enabled is True            # kept across a restart
