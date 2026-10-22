import io
import os
import shutil
import stat
import subprocess
import time

import pytest

from rig import Rig, wait_for
from tubeless import statics
from tubeless.config import ASSETS_DIR, Config
from tubeless.state import Settings, Store


@pytest.fixture(autouse=True)
def no_mediainfo(monkeypatch):
    """These tests use made-up bytes for clips; whether mediainfo is installed on the machine running them must not matter."""
    from types import SimpleNamespace
    monkeypatch.setattr(statics, "advice", SimpleNamespace(probe=lambda *a, **k: None))


def manager(tmp_path, ffmpeg=None, **kw):
    cfg = Config(data_dir=str(tmp_path / "data"), videos_dir=str(tmp_path / "home" / "videos"),
                 static_short="/builtin_short.mp4", static_long="/builtin_long.mp4")
    home = tmp_path / "home"
    (home / "videos" / "1.shows").mkdir(parents=True)
    changes = []
    settings = Settings(Store(cfg.data))
    mgr = statics.StaticManager(cfg, settings, home / "videos", on_change=lambda: changes.append(1), ffmpeg=ffmpeg, home=home, **kw)
    mgr.changes = changes
    return mgr


def put(path, data=b"x" * 100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---- the home folder picker ------------------------------------------------------------------------------------
def test_home_listing_skips_the_videos_library_hidden_and_deep_folders(tmp_path):
    mgr = manager(tmp_path)
    home = tmp_path / "home"
    put(home / "clips" / "static1.mp4")
    put(home / "top.mkv")
    put(home / "videos" / "1.shows" / "episode.mp4")                      # the library: never offered
    put(home / ".cache" / "x.mp4")                                          # hidden
    put(home / "tubeless" / "tubeless" / "assets" / "static_short.mp4")     # the app's own files
    put(home / "a" / "b" / "c" / "d" / "toodeep.mp4")
    put(home / "notes.txt")
    names = {f["path"] for f in mgr.home_files("clip")}
    assert names == {"clips/static1.mp4", "top.mkv"}
    put(home / "music" / "hiss.mp3")
    assert {f["path"] for f in mgr.home_files("sound")} == {"music/hiss.mp3"}
    with pytest.raises(ValueError):
        mgr.home_files("picture")


def test_a_home_pick_is_checked_again_on_the_way_in(tmp_path):
    mgr = manager(tmp_path)
    home = tmp_path / "home"
    put(home / "clips" / "ok.mp4")
    put(home / "videos" / "1.shows" / "ep.mp4")
    put(home / ".secret" / "x.mp4")
    put(tmp_path / "outside.mp4")
    put(home / "clips" / "notes.txt")
    assert statics.check_home_pick(home, home / "videos", "clips/ok.mp4", "clip").name == "ok.mp4"
    for bad in ("../outside.mp4", "/etc/passwd", "videos/1.shows/ep.mp4", ".secret/x.mp4", "clips/missing.mp4", "clips/notes.txt", ""):
        with pytest.raises(ValueError):
            statics.check_home_pick(home, home / "videos", bad, "clip")


# ---- the choices -----------------------------------------------------------------------------------------------
def test_the_default_is_todays_static_and_the_presets_swap_the_files(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.paths() == ("/builtin_short.mp4", "/builtin_long.mp4") and mgr.state()["choice"]["name"] == "sound"
    mgr.choose_preset("silent")
    assert [os.path.basename(p) for p in mgr.paths()] == ["static_short_silent.mp4", "static_long_silent.mp4"]
    mgr.choose_preset("none")
    assert [os.path.basename(p) for p in mgr.paths()] == ["static_blank.mp4"] * 2 and mgr.changes
    with pytest.raises(ValueError):
        mgr.choose_preset("loud")
    for name in ("static_short_silent.mp4", "static_long_silent.mp4", "static_blank.mp4", "static_noise.m4a"):
        assert (ASSETS_DIR / name).is_file()


def test_the_channel_change_hold_follows_the_clip_but_never_exceeds_the_usual(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.delay(3.0) == 3.0
    mgr.choose_preset("none")
    assert mgr.delay(3.0) == 0.3                                            # a quick blank: no 3 second black screen
    clip = put(tmp_path / "data" / "static" / "clip" / "mine.mp4")
    mgr._select({"kind": "upload", "dir": "clip", "name": "mine.mp4"}, 1.5)
    assert mgr.delay(3.0) == 3.0                                            # your clip: held for the chosen change length (3 s by default)
    mgr.set_lengths(change=5.0)
    assert mgr.delay(3.0) == 5.0
    mgr._select({"kind": "upload", "dir": "clip", "name": "mine.mp4"}, 20.0)
    assert mgr.paths()[0].startswith("edl://") and str(clip) in mgr.paths()[0]


def test_a_chosen_clip_that_has_gone_falls_back_to_the_default(tmp_path):
    mgr = manager(tmp_path)
    clip = put(tmp_path / "data" / "static" / "clip" / "mine.mp4")
    mgr._select({"kind": "upload", "dir": "clip", "name": "mine.mp4"}, None)
    assert str(clip) in mgr.paths()[0]
    clip.unlink()
    assert mgr.paths() == ("/builtin_short.mp4", "/builtin_long.mp4")
    assert mgr.state()["choice"].get("missing") is True


# ---- uploads ---------------------------------------------------------------------------------------------------
def test_uploads_are_kept_by_kind_and_checked(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.save_upload("clip", "My Clip.mp4", io.BytesIO(b"v" * 50), 50) == "My Clip.mp4"
    assert mgr.save_upload("sound", "hiss.wav", io.BytesIO(b"a" * 20), 20) == "hiss.wav"
    assert mgr.state()["uploads"] == {"clips": ["My Clip.mp4"], "sounds": ["hiss.wav"]}
    with pytest.raises(ValueError):
        mgr.save_upload("clip", "notes.txt", io.BytesIO(b"x"), 1)             # not a video
    with pytest.raises(ValueError):
        mgr.save_upload("sound", "clip.mp4", io.BytesIO(b"x"), 1)             # not a sound
    with pytest.raises(ValueError):
        mgr.save_upload("clip", "big.mp4", io.BytesIO(b""), statics.MAX_UPLOAD["clip"] + 1)
    with pytest.raises(ValueError):
        mgr.save_upload("clip", "short.mp4", io.BytesIO(b"abc"), 10)           # cut short
    with pytest.raises(ValueError):
        mgr.save_upload("clip", "empty.mp4", io.BytesIO(b""), 0)
    assert mgr.state()["uploads"]["clips"] == ["My Clip.mp4"]                  # nothing half-written is left behind
    assert not [f for f in (tmp_path / "data" / "static" / "clip").iterdir() if f.name.startswith(".")]


def test_using_a_clip_with_its_own_sound_needs_no_ffmpeg(tmp_path):
    mgr = manager(tmp_path, ffmpeg="")                                     # "" = no ffmpeg (None would look for the real one)
    mgr.save_upload("clip", "mine.mp4", io.BytesIO(b"v" * 50), 50)
    state = mgr.use("upload", "mine.mp4", "own")
    assert state["choice"]["label"] == "mine.mp4" and "mine.mp4" in mgr.paths()[0]
    put(tmp_path / "home" / "pick" / "home.mp4")
    mgr.use("home", "pick/home.mp4", "own")
    assert str((tmp_path / "home" / "pick" / "home.mp4").resolve()) in mgr.paths()[0]


def test_changing_a_clips_sound_needs_ffmpeg_and_says_so(tmp_path):
    mgr = manager(tmp_path, ffmpeg="")
    mgr.save_upload("clip", "mine.mp4", io.BytesIO(b"v" * 50), 50)
    for sound in ("static", "silent", "file"):
        with pytest.raises(ValueError) as e:
            mgr.use("upload", "mine.mp4", sound)
        assert "needs ffmpeg" in str(e.value)
    with pytest.raises(ValueError):
        mgr.use("upload", "missing.mp4", "own")
    with pytest.raises(ValueError):
        mgr.use("upload", "mine.mp4", "loud")


def test_ffmpeg_commands_for_each_sound_choice():
    static = statics.ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", "static", "noise.m4a")
    assert static[static.index("-stream_loop") + 1] == "-1" and "-shortest" in static and static[static.index("-t") + 1] == "5"
    assert "1:a:0" in static and "libx264" in static and static[-1] == "out.mp4"
    silent = statics.ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", "silent")
    assert "-an" in silent and "-stream_loop" not in silent
    with pytest.raises(ValueError):
        statics.ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", "file", None)
    with pytest.raises(ValueError):
        statics.ffmpeg_command("ffmpeg", "in.mp4", "out.mp4", "own")


def fake_ffmpeg(tmp_path, ok=True):
    path = tmp_path / "fake-ffmpeg"
    path.write_text('#!/bin/sh\nfor last; do :; done\n' + ('printf converted > "$last"\n' if ok else 'echo "Invalid data found" >&2; exit 1\n'))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_a_clip_is_converted_once_and_the_new_file_is_what_plays(tmp_path):
    mgr = manager(tmp_path, ffmpeg=fake_ffmpeg(tmp_path))
    mgr.save_upload("clip", "mine.mp4", io.BytesIO(b"v" * 50), 50)
    mgr.use("upload", "mine.mp4", "static")
    assert mgr.state()["choice"]["label"] == "mine-static-sound.mp4" and "mine-static-sound.mp4" in mgr.paths()[0]
    assert open(tmp_path / "data" / "static" / "clip" / "mine-static-sound.mp4").read() == "converted"
    assert mgr.state()["uploads"]["clips"] == ["mine-static-sound.mp4", "mine.mp4"]       # the original is kept, nothing in the way


def test_a_failed_conversion_changes_nothing_and_says_why(tmp_path):
    mgr = manager(tmp_path, ffmpeg=fake_ffmpeg(tmp_path, ok=False))
    mgr.save_upload("clip", "mine.mp4", io.BytesIO(b"v" * 50), 50)
    with pytest.raises(ValueError) as e:
        mgr.use("upload", "mine.mp4", "silent")
    assert "couldn't convert" in str(e.value) and "Invalid data" in str(e.value)
    assert mgr.state()["choice"]["name"] == "sound" and mgr.state()["uploads"]["clips"] == ["mine.mp4"]


def test_a_sound_file_can_be_chosen_only_once_it_is_uploaded(tmp_path):
    mgr = manager(tmp_path, ffmpeg=fake_ffmpeg(tmp_path))
    mgr.save_upload("clip", "mine.mp4", io.BytesIO(b"v" * 50), 50)
    with pytest.raises(ValueError):
        mgr.use("upload", "mine.mp4", "file", "hiss.wav")
    mgr.save_upload("sound", "hiss.wav", io.BytesIO(b"a" * 20), 20)
    mgr.use("upload", "mine.mp4", "file", "hiss.wav")
    assert mgr.state()["choice"]["label"] == "mine-own-sound.mp4"


def test_deleting_the_clip_in_use_goes_back_to_the_default(tmp_path):
    mgr = manager(tmp_path)
    mgr.save_upload("clip", "mine.mp4", io.BytesIO(b"v" * 50), 50)
    mgr.use("upload", "mine.mp4", "own")
    mgr.delete("clip", "mine.mp4")
    assert mgr.state()["choice"]["name"] == "sound" and mgr.state()["uploads"]["clips"] == []
    for bad in ("../x", ".hidden", "a/b"):
        with pytest.raises(ValueError):
            mgr.delete("clip", bad)


# ---- with a real ffmpeg (where there is one) ---------------------------------------------------------------------
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg is not installed here")


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.split()
    return sorted(out)


@needs_ffmpeg
def test_real_ffmpeg_makes_a_silent_clip_and_a_clip_with_the_static_sound(tmp_path):
    mgr = manager(tmp_path, ffmpeg=shutil.which("ffmpeg"))
    shutil.copy(ASSETS_DIR / "static_long.mp4", tmp_path / "pretend.mp4")
    with open(tmp_path / "pretend.mp4", "rb") as f:
        mgr.save_upload("clip", "pretend.mp4", f, (tmp_path / "pretend.mp4").stat().st_size)
    mgr.use("upload", "pretend.mp4", "silent")
    assert streams(tmp_path / "data" / "static" / "clip" / "pretend-silent.mp4") == ["video"]
    mgr.use("upload", "pretend.mp4", "static")
    out = str(tmp_path / "data" / "static" / "clip" / "pretend-static-sound.mp4")
    assert streams(out) == ["audio", "video"]
    length = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
                                  capture_output=True, text=True).stdout)
    assert 2.5 < length <= statics.CAP_SECONDS + 0.5                          # as long as the picture, never past the cap


# ---- in the app --------------------------------------------------------------------------------------------------
def test_the_app_plays_the_chosen_clip_between_videos_and_on_channel_change(tmp_path):
    r = Rig(tmp_path)
    try:
        clip = put(tmp_path / "data" / "static" / "clip" / "mine.mp4")
        r.app.statics._select({"kind": "upload", "dir": "clip", "name": "mine.mp4"}, 20.0)               # a long clip: only the first part plays
        assert str(clip) in r.app.playlists.static_path and r.app.playlists.static_path.startswith("edl://")
        r.app.start_playback()
        entries = open(r.app.m3u_path).read().splitlines()
        between = r.app.statics.paths()[0]
        assert between.startswith("edl://") and str(clip) in between and between.endswith(",0,0.6")     # the first 0.6 s of the clip
        assert entries[0] == between and entries.count(between) == 5              # before each of the five videos
        r.player.commands.clear()
        r.app.switch_channel(1)
        loads = [c for c in r.player.commands if c[:1] == ("loadfile",)]
        assert loads and loads[0][1] == r.app.statics.paths()[1] and loads[0][1].endswith(",0,3")       # up to 3 s when changing channel
        assert r.app.web_state()["static"]["choice"]["label"] == "mine.mp4"
    finally:
        r.stop()


def test_a_file_with_no_picture_is_refused_as_a_clip_and_one_with_no_sound_as_a_sound(tmp_path, monkeypatch):
    from types import SimpleNamespace
    mgr = manager(tmp_path)
    monkeypatch.setattr(statics, "advice", SimpleNamespace(probe=lambda path, timeout=0: {"video": None, "audio": {"codec": "AAC"}, "duration": 2.0}))
    with pytest.raises(ValueError) as e:
        mgr.save_upload("clip", "song.mp4", io.BytesIO(b"v" * 50), 50)                 # only audio in it
    assert "no picture" in str(e.value)
    assert mgr.save_upload("sound", "song.mp3", io.BytesIO(b"v" * 50), 50) == "song.mp3"
    monkeypatch.setattr(statics, "advice", SimpleNamespace(probe=lambda path, timeout=0: {"video": {"codec": "AVC"}, "audio": None, "duration": 2.0}))
    assert mgr.save_upload("clip", "ok.mp4", io.BytesIO(b"v" * 50), 50) == "ok.mp4"
    with pytest.raises(ValueError) as e:
        mgr.save_upload("sound", "silent-film.mp3", io.BytesIO(b"v" * 50), 50)         # only a picture in it
    assert "no sound" in str(e.value)
    mgr.use("upload", "ok.mp4", "own")
    assert mgr.state()["choice"]["label"] == "ok.mp4"
    monkeypatch.setattr(statics, "advice", SimpleNamespace(probe=lambda path, timeout=0: {"video": None, "audio": None, "duration": 0}))
    (tmp_path / "data" / "static" / "clip" / "blank.mp4").write_bytes(b"x")
    with pytest.raises(ValueError) as e:
        mgr.use("upload", "blank.mp4", "own")
    assert "no picture" in str(e.value)


# ---- one clip, two lengths ---------------------------------------------------------------------------------------------
def test_edl_entry_plays_the_first_part_or_repeats_a_short_clip_to_fill_the_time():
    path = "/home/me/my clip, v2;final.mp4"                                        # spaces, a comma and a semicolon in the name
    n = len(path.encode())
    assert statics.edl_entry(path, 0.6, 3.2) == f"edl://%{n}%{path},0,0.6"        # longer than needed: just the first part
    assert statics.edl_entry(path, 3.0, None) == f"edl://%{n}%{path},0,3"         # length unknown: ask for it, mpv stops at the end
    segment = f"%{n}%{path},0,"
    three = statics.edl_entry(path, 3.0, 1.0)                                      # a 1 s clip for 3 s: three times over
    assert three.count(segment) == 3 and three.endswith(segment + "1") and three.count(segment + "1") == 3
    parts = statics.edl_entry(path, 2.5, 1.0)
    assert parts.count(segment) == 3 and parts.endswith(segment + "0.5")           # 1 + 1 + 0.5
    assert statics.edl_entry("/é/ü.mp4", 1.0, 5.0).startswith(f"edl://%{len('/é/ü.mp4'.encode())}%")      # the length is in bytes


def test_the_lengths_default_to_the_original_projects_and_are_chosen_from_a_short_list(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.lengths() == (0.6, 3.0)
    mgr.set_lengths(between=2.0)
    assert mgr.lengths() == (2.0, 3.0)
    mgr.set_lengths(change=5.0, between=1.0)
    assert mgr.lengths() == (1.0, 5.0) and mgr.changes
    for bad in ({"between": 0.1}, {"change": 99.0}, {"between": 3.5}):
        with pytest.raises(ValueError):
            mgr.set_lengths(**bad)
    assert mgr.lengths() == (1.0, 5.0)                                              # a refused change changes nothing


def test_a_clip_shorter_than_the_time_asked_for_is_repeated(tmp_path):
    mgr = manager(tmp_path)
    clip = put(tmp_path / "data" / "static" / "clip" / "mine.mp4")
    mgr._select({"kind": "upload", "dir": "clip", "name": "mine.mp4"}, 1.0)        # a 1 s clip
    mgr.set_lengths(between=2.0, change=4.0)
    between, change = mgr.paths()
    assert between.count(str(clip)) == 2 and change.count(str(clip)) == 4          # twice, and four times
    assert mgr.state()["lengths"]["applies"] is True
    mgr.choose_preset("sound")
    assert mgr.state()["lengths"]["applies"] is False and mgr.paths() == ("/builtin_short.mp4", "/builtin_long.mp4")


def test_the_players_stand_in_understands_a_cut_clip_entry():
    from tubeless.player import FakePlayer
    p = FakePlayer("/s.mp4", "/l.mp4")
    assert p.is_static("/s.mp4") and p.is_static("edl://%5%/a.mp4,0,0.6") and not p.is_static("/videos/a.mp4")
    assert abs(p._dur("edl://%5%/a.mp4,0,1;%5%/a.mp4,0,0.5") - 1.5) < 1e-9
