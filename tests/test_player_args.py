"""What mpv is started with: the options that keep a 512 MB Pi alive."""
import subprocess

from rig import HW
from tubeless.config import Config
from tubeless.player import MpvPlayer


def test_mpv_is_started_with_a_small_stream_cache(tmp_path, monkeypatch):
    seen = {}

    class FakeProc:
        pid = 1

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (seen.setdefault("cmd", cmd), FakeProc())[1])
    cfg = Config(data_dir=str(tmp_path / "data"), mpv_log=str(tmp_path / "mpv.log"), mpv_socket=str(tmp_path / "mpv.sock"),
                 ytdlp_path="/nonexistent")
    MpvPlayer(cfg, HW).start(str(tmp_path / "p.m3u"))
    cmd = seen["cmd"]
    assert "--demuxer-max-bytes=24MiB" in cmd and "--demuxer-max-back-bytes=4MiB" in cmd and "--cache-secs=20" in cmd


def test_streaming_asks_youtube_for_the_chosen_quality(tmp_path, monkeypatch):
    import os
    seen = {}

    class FakeProc:
        pid = 1

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: (seen.setdefault("cmd", cmd), FakeProc())[1])
    ytdlp = tmp_path / "yt-dlp"
    ytdlp.write_text("#!/bin/sh\n")
    ytdlp.chmod(0o755)
    cfg = Config(data_dir=str(tmp_path / "data"), mpv_log=str(tmp_path / "mpv.log"), mpv_socket=str(tmp_path / "mpv.sock"),
                 ytdlp_path=str(ytdlp))
    player = MpvPlayer(cfg, HW)
    assert player.youtube_height == 360                                         # the default: no HD
    player.start(str(tmp_path / "p.m3u"))
    fmt = [c for c in seen["cmd"] if c.startswith("--ytdl-format=")][0]
    assert "height<=360" in fmt and "height<=480" not in fmt
    seen.clear()
    player.youtube_height = 240
    player.start(str(tmp_path / "p.m3u"))
    assert "height<=240" in [c for c in seen["cmd"] if c.startswith("--ytdl-format=")][0]
