"""
Checks that need yt-dlp itself but no network: that our format choice and command-line options still mean what we think
they mean. The 'yt-dlp watch' GitHub workflow runs these against each new yt-dlp release.
"""
import re
import subprocess
import sys

import pytest

yt_dlp = pytest.importorskip("yt_dlp")

from tubeless import youtube


def fmt(fid, ext, vcodec="none", acodec="none", height=None, fps=None, tbr=100, proto="https"):
    return {"format_id": fid, "ext": ext, "vcodec": vcodec, "acodec": acodec, "height": height, "width": height and int(height * 16 / 9),
            "fps": fps, "tbr": tbr, "protocol": proto, "url": "https://example.invalid/" + fid, "format_note": ""}


# what YouTube offers for an ordinary video (recorded from a real listing)
TYPICAL = [
    fmt("139", "m4a", acodec="mp4a.40.5", tbr=49), fmt("140", "m4a", acodec="mp4a.40.2", tbr=129),
    fmt("251", "webm", acodec="opus", tbr=129),
    fmt("160", "mp4", "avc1.4D400C", height=144, fps=30, tbr=55), fmt("133", "mp4", "avc1.4D4015", height=240, fps=30, tbr=118),
    fmt("134", "mp4", "avc1.4D401E", height=360, fps=30, tbr=231), fmt("135", "mp4", "avc1.4D401F", height=480, fps=30, tbr=356),
    fmt("244", "webm", "vp9", height=480, fps=30, tbr=418), fmt("397", "mp4", "av01.0.04M.08", height=480, fps=30, tbr=328),
    fmt("298", "mp4", "avc1.4D4020", height=720, fps=60, tbr=1898), fmt("299", "mp4", "avc1.64002A", height=1080, fps=60, tbr=3248),
]


def choose(formats, selector):
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    picked = list(ydl.build_format_selector(selector)({"formats": formats, "incomplete_formats": False}))
    return picked[0] if picked else None


def ids(picked):
    return None if picked is None else [f["format_id"] for f in picked.get("requested_formats", [picked])]


def test_the_download_format_picks_480p_h264_video_with_aac_sound():
    assert ids(choose(TYPICAL, f"{youtube.FORMAT_JOINED}/{youtube.FORMAT_SINGLE}")) == ["135", "140"]


@pytest.mark.parametrize("height,expected", [(240, ["133", "140"]), (360, ["134", "140"]), (480, ["135", "140"])])
def test_each_quality_picks_the_best_h264_video_up_to_that_height(height, expected):
    joined, single = youtube.formats(height)
    assert ids(choose(TYPICAL, f"{joined}/{single}")) == expected


def test_a_quality_that_the_video_lacks_falls_back_to_the_best_below_it():
    no_360 = [f for f in TYPICAL if f["format_id"] != "134"]
    joined, single = youtube.formats(360)
    assert ids(choose(no_360, f"{joined}/{single}")) == ["133", "140"]                 # 240p, not 480p


def test_it_never_picks_vp9_av1_or_60_fps_even_when_they_are_the_only_big_ones():
    only_hd = [f for f in TYPICAL if f["format_id"] in ("140", "298", "299", "244", "397")]
    got = choose(only_hd, f"{youtube.FORMAT_JOINED}/{youtube.FORMAT_SINGLE}")
    assert got is None or all(f["format_id"] not in ("298", "299", "244", "397") for f in got.get("requested_formats", [got]))


def test_without_aac_sound_it_does_not_pick_opus_for_the_mp4():
    no_aac = [f for f in TYPICAL if f["format_id"] != "140" and f["format_id"] != "139"]
    got = choose(no_aac, youtube.FORMAT_JOINED)
    assert got is None


def test_a_ready_made_single_file_is_used_when_there_is_no_separate_stream():
    single = [fmt("18", "mp4", "avc1.42001E", "mp4a.40.2", height=360, fps=30, tbr=360)]
    assert ids(choose(single, f"{youtube.FORMAT_JOINED}/{youtube.FORMAT_SINGLE}")) == ["18"]


def test_every_option_we_pass_is_still_known_to_yt_dlp():
    help_text = subprocess.run([sys.executable, "-m", "yt_dlp", "--help"], capture_output=True, text=True).stdout
    known = set(re.findall(r"(--[a-z][a-z0-9-]*)", help_text))
    used = {"--no-update", "--no-warnings", "--no-config", "--ignore-config", "--no-cache-dir", "--flat-playlist", "--playlist-items",
            "--newline", "--no-playlist", "--continue", "--merge-output-format", "--progress-template", "--dump-single-json"}
    missing = used - known
    assert not missing, f"yt-dlp no longer has: {sorted(missing)}"


def test_the_output_template_and_paths_options_still_work_together(tmp_path):
    from yt_dlp import YoutubeDL
    ydl = YoutubeDL({"paths": {"home": str(tmp_path), "temp": str(tmp_path / ".downloading")}, "outtmpl": "Title [id].%(ext)s",
                     "quiet": True})
    assert ydl.prepare_filename({"id": "id", "ext": "mp4", "title": "t"}).endswith("Title [id].mp4")
