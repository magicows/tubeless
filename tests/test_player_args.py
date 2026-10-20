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
