import json

import pytest

from tubeless import advice


def media(video=None, audio=None, general=None):
    tracks = [{"@type": "General", "Format": "MPEG-4", "Duration": "1200.5", "FileSize": "120000000", **(general or {})}]
    if video is not None:
        tracks.append({"@type": "Video", **video})
    if audio is not None:
        tracks.append({"@type": "Audio", **audio})
    return json.dumps({"media": {"track": tracks}})


GOOD = {"Format": "AVC", "Width": "640", "Height": "360", "FrameRate": "24.000", "BitRate": "900000", "BitDepth": "8"}


def texts(items):
    return [a.text for a in items]


def levels(items):
    return [a.level for a in items]


def test_parse_reads_the_facts_as_numbers():
    info = advice.parse_mediainfo(media(GOOD, {"Format": "AAC", "Channels": "2", "BitRate": "96000"}))
    assert info["container"] == "MPEG-4" and info["duration"] == 1200.5
    assert info["video"]["width"] == 640 and info["video"]["height"] == 360 and info["video"]["bitrate"] == 900000
    assert info["audio"]["channels"] == 2


def test_parse_copes_with_garbage():
    assert advice.parse_mediainfo("not json") is None
    assert advice.parse_mediainfo('{"media": null}') is None
    assert advice.parse_mediainfo("{}") is None


def test_a_good_file_gets_a_clean_bill_of_health():
    out = advice.advise(advice.parse_mediainfo(media(GOOD, {"Format": "AAC", "Channels": "2"})))
    assert levels(out) == ["ok"]


def test_big_hd_files_are_warned_about():
    info = advice.parse_mediainfo(media({**GOOD, "Width": "1920", "Height": "1080", "BitRate": "8000000"}))
    out = advice.advise(info)
    assert levels(out).count("warn") == 2 and any("1920x1080" in t for t in texts(out))
    assert any("Mbit/s" in t for t in texts(out))


def test_480p_to_720p_is_only_a_suggestion():
    out = advice.advise(advice.parse_mediainfo(media({**GOOD, "Width": "1280", "Height": "720", "BitRate": "1200000"})))
    assert levels(out) == ["info"] and "480p" in out[0].text


def test_hard_to_decode_codecs_and_bit_depth_warn():
    for codec in ("HEVC", "AV1", "VP9"):
        out = advice.advise(advice.parse_mediainfo(media({**GOOD, "Format": codec})))
        assert "warn" in levels(out), codec
    out = advice.advise(advice.parse_mediainfo(media({**GOOD, "BitDepth": "10"})))
    assert any("10-bit" in t for t in texts(out)) and "warn" in levels(out)


def test_moderate_bitrate_fps_and_surround_audio_are_gentle_hints():
    info = advice.parse_mediainfo(media({**GOOD, "BitRate": "3500000", "FrameRate": "60.000"}, {"Format": "AC-3", "Channels": "6"}))
    out = advice.advise(info)
    assert set(levels(out)) == {"info"} and len(out) == 3


def test_unreadable_files_are_still_accepted():
    assert advice.advise(None)[0].level == "info"
    assert advice.advise({"video": None})[0].level == "info"


def test_probe_without_mediainfo_installed_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(advice.shutil, "which", lambda _: None)
    assert advice.probe(str(tmp_path / "x.mp4")) is None
