import io
import os
import pwd
import subprocess
import zipfile
from pathlib import Path

import pytest

from tubeless import installer
from tubeless.installer import Context, InstallError, Runner

from test_installer import TRIXIE_CMDLINE, TRIXIE_CONFIG

ME = pwd.getpwuid(os.getuid()).pw_name
FAKE_OVERLAYS = {"vc4-kms-dpi-2inch8.dtbo": b"A" * 50, "waveshare-touch-28dpi.dtbo": b"B" * 40}


@pytest.fixture(autouse=True)
def pinned_fake_hashes(monkeypatch):
    monkeypatch.setattr(installer, "WAVESHARE_FILES", {n: installer.sha256(c) for n, c in FAKE_OVERLAYS.items()})


def make_zip(files, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in {**files, **(extra or {})}.items():
            z.writestr("28DPI-DTBO/" + n, c)
    return buf.getvalue()


# ---- overlays ------------------------------------------------------------------------------------
def test_overlays_are_accepted_only_if_every_file_matches_its_pinned_hash():
    out = installer.overlays_from_zip(make_zip(FAKE_OVERLAYS, {"._junk.dtbo": b"x", "README.txt": b"hi"}))
    assert out == FAKE_OVERLAYS


def test_a_changed_overlay_is_refused_unless_you_say_so():
    bad = dict(FAKE_OVERLAYS, **{"vc4-kms-dpi-2inch8.dtbo": b"tampered"})
    with pytest.raises(InstallError, match="does not match the checksum"):
        installer.overlays_from_zip(make_zip(bad))
    assert installer.overlays_from_zip(make_zip(bad), allow_changed=True)["vc4-kms-dpi-2inch8.dtbo"] == b"tampered"


def test_missing_file_and_garbage_downloads_are_refused():
    with pytest.raises(InstallError, match="missing"):
        installer.overlays_from_zip(make_zip({"vc4-kms-dpi-2inch8.dtbo": FAKE_OVERLAYS["vc4-kms-dpi-2inch8.dtbo"]}))
    with pytest.raises(InstallError, match="not a valid zip"):
        installer.overlays_from_zip(b"<html>Not found</html>")


def test_write_overlays_backs_up_what_it_replaces_once(tmp_path):
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "vc4-kms-dpi-2inch8.dtbo").write_bytes(b"old")
    assert installer.write_overlays(FAKE_OVERLAYS, tmp_path, apply=False) == sorted(FAKE_OVERLAYS)
    assert (tmp_path / "overlays" / "vc4-kms-dpi-2inch8.dtbo").read_bytes() == b"old"            # dry run changes nothing
    assert installer.write_overlays(FAKE_OVERLAYS, tmp_path, apply=True) == sorted(FAKE_OVERLAYS)
    assert (tmp_path / "overlays" / "vc4-kms-dpi-2inch8.dtbo.before-tubeless").read_bytes() == b"old"
    assert installer.write_overlays(FAKE_OVERLAYS, tmp_path, apply=True) == []                    # again: nothing to do


# ---- the SD card test -----------------------------------------------------------------------------
def test_sd_test_passes_on_a_healthy_disk_and_cleans_up(tmp_path):
    ok, detail = installer.sd_readback_test(str(tmp_path), megabytes=2, rounds=2)
    assert ok, detail
    assert list(tmp_path.iterdir()) == []


def test_sd_test_catches_a_card_that_returns_different_data(tmp_path, monkeypatch):
    real = installer.hashlib.sha256
    made = []

    class Liar:
        def __init__(self):
            self.h = real()
            made.append(self)

        def update(self, data):
            self.h.update(data if len(made) % 2 else data[:-1])        # every second hash (the read-back) sees different data

        def digest(self):
            return self.h.digest()

    class Shim:
        sha256 = staticmethod(Liar)

    monkeypatch.setattr(installer, "hashlib", Shim)
    ok, detail = installer.sd_readback_test(str(tmp_path), megabytes=1, rounds=1)
    assert not ok and "different data" in detail
    assert list(tmp_path.iterdir()) == []


# ---- text pieces -------------------------------------------------------------------------------------
def test_unit_template_and_sudoers_rule():
    text = installer.render_unit("User=@USER@\nWorkingDirectory=@APPDIR@\nDescription=@NAME@", "sam", "/home/sam/tubeless", "Den")
    assert text == "User=sam\nWorkingDirectory=/home/sam/tubeless\nDescription=Den"
    rule = installer.sudoers_rule("sam")
    assert "sam ALL=(root) NOPASSWD:" in rule
    for allowed in ("poweroff", "reboot", "restart tubeless"):
        assert f"/usr/bin/systemctl {allowed}" in rule
    assert "ALL=(ALL)" not in rule and "NOPASSWD: ALL" not in rule            # never blanket


def test_the_real_unit_templates_render_without_leftover_placeholders():
    for unit in installer.UNITS:
        template = (Path(__file__).resolve().parent.parent / "packaging" / (unit + ".in")).read_text()
        assert "@" not in installer.render_unit(template, "sam", "/home/sam/tubeless", "Den")


# ---- the whole install on a fake system -------------------------------------------------------------
class FakeRunner(Runner):
    def __init__(self):
        super().__init__(dry_run=False, say=lambda text: self.lines.append(text))
        self.lines, self.commands = [], []
        self.unit_state = {"getty@tty1.service": "enabled", "pigpiod.service": "disabled"}
        self.installed = set()

    def run(self, *cmd, check=True, env=None):
        self.commands.append(cmd)
        if cmd[:2] == ("systemctl", "mask"):
            self.unit_state["getty@tty1.service"] = "masked"
        elif cmd[:2] == ("systemctl", "enable") and cmd[2] == "pigpiod.service":
            self.unit_state["pigpiod.service"] = "enabled"
        elif cmd[0] == "apt-get" and "install" in cmd:
            self.installed |= set(c for c in cmd if c in installer.PACKAGES)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def query(self, *cmd):
        if cmd[:2] == ("dpkg", "-s"):
            ok = cmd[2] in self.installed
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, "Status: install ok installed" if ok else "", "")
        if cmd[:2] == ("systemctl", "is-enabled"):
            return subprocess.CompletedProcess(cmd, 0, self.unit_state.get(cmd[2], "disabled") + "\n", "")
        if cmd[:2] == ("systemctl", "cat"):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")            # visudo -cf etc.


@pytest.fixture
def system(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    (boot / "overlays").mkdir(parents=True)
    (boot / "config.txt").write_text(TRIXIE_CONFIG)
    (boot / "cmdline.txt").write_text(TRIXIE_CMDLINE)
    home = tmp_path / "home" / "sam"
    home.mkdir(parents=True)
    source = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(installer, "read_model", lambda: "Raspberry Pi Zero 2 W Rev 1.0")
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.shutil, "which", lambda tool: "/usr/bin/apt-get" if tool == "apt-get" else None)
    monkeypatch.setattr(installer, "fetch_overlays", lambda allow_changed=False: dict(FAKE_OVERLAYS))
    monkeypatch.setattr(installer, "pwd", type("P", (), {"getpwnam": staticmethod(lambda n: pwd.getpwnam(ME))}))
    runner = FakeRunner()
    ctx = Context(user="sam", home=home, source=source, name="Den", boot_dir=boot, etc=tmp_path / "etc",
                  local_lib=tmp_path / "usr-local-lib", skip_sd_test=True, runner=runner)
    return ctx, runner


def test_a_full_install_on_a_fresh_system(system):
    ctx, runner = system
    ctx.greeting = "Wake up, Homer..."
    assert installer.install_all(ctx) == 0
    out = "\n".join(runner.lines)
    assert ctx.reboot_needed and "Reboot now" in out

    # packages: lean install of exactly what is missing
    apt = [c for c in runner.commands if c[0] == "apt-get" and "install" in c][0]
    assert "--no-install-recommends" in apt and "mpv" in apt and "python3-pigpio" in apt

    # display + touch
    assert sorted(p.name for p in (ctx.boot / "overlays").glob("*.dtbo")) == sorted(FAKE_OVERLAYS)
    assert installer.BEGIN in (ctx.boot / "config.txt").read_text()
    assert "console=tty3" in (ctx.boot / "cmdline.txt").read_text()
    assert (ctx.boot / "config.txt.before-tubeless").read_text() == TRIXIE_CONFIG

    # console + services
    assert runner.unit_state["getty@tty1.service"] == "masked" and runner.unit_state["pigpiod.service"] == "enabled"

    # the app, config, videos
    assert (ctx.appdir / "tubeless" / "app.py").exists() and (ctx.appdir / "packaging" / "tubeless.service.in").exists()
    assert not (ctx.appdir / "tests").exists() and not list(ctx.appdir.rglob("__pycache__"))
    assert '"name": "Den"' in (ctx.home / ".config" / "tubeless" / "config.json").read_text()
    assert '"boot_greeting": "Wake up, Homer..."' in (ctx.home / ".config" / "tubeless" / "config.json").read_text()
    assert (ctx.home / "videos").is_dir()

    # units and the narrow sudoers rule
    unit = (ctx.etc / "systemd" / "system" / "tubeless.service").read_text()
    assert "User=sam" in unit and f"WorkingDirectory={ctx.appdir}" in unit and "@" not in unit
    assert (ctx.etc / "sudoers.d" / "tubeless").read_text() == installer.sudoers_rule("sam")
    assert oct((ctx.etc / "sudoers.d" / "tubeless").stat().st_mode & 0o777) == "0o440"
    assert ("systemctl", "enable", "tubeless.service", "tubeless-boot.service") in runner.commands


def test_running_it_again_changes_nothing(system):
    ctx, runner = system
    installer.install_all(ctx)
    config_after = (ctx.boot / "config.txt").read_text()
    cmdline_after = (ctx.boot / "cmdline.txt").read_text()
    (ctx.home / ".config" / "tubeless" / "config.json").write_text('{"name": "Edited by hand"}\n')
    runner.lines.clear()
    runner.commands.clear()
    ctx.reboot_needed = False

    assert installer.install_all(ctx) == 0
    out = "\n".join(runner.lines)
    assert not ctx.reboot_needed
    assert not [c for c in runner.commands if c[0] == "apt-get"]                    # nothing to install
    assert not [c for c in runner.commands if c[:2] in (("systemctl", "mask"),)]
    assert (ctx.boot / "config.txt").read_text() == config_after and (ctx.boot / "cmdline.txt").read_text() == cmdline_after
    assert "Edited by hand" in (ctx.home / ".config" / "tubeless" / "config.json").read_text()       # the user's config is never overwritten
    assert "already" in out


def test_dry_run_changes_nothing(system):
    ctx, runner = system
    ctx.dry_run = True
    ctx.runner = dry = Runner(dry_run=True, say=lambda t: runner.lines.append(t))
    assert installer.install_all(ctx) == 0
    assert (ctx.boot / "config.txt").read_text() == TRIXIE_CONFIG
    assert not (ctx.appdir).exists() and not (ctx.etc / "systemd").exists() and not (ctx.etc / "sudoers.d").exists()
    assert "dry run" in "\n".join(runner.lines).lower()


def test_a_bad_sd_card_stops_the_install_before_anything_is_changed(system, monkeypatch):
    ctx, runner = system
    ctx.skip_sd_test = False
    monkeypatch.setattr(installer, "sd_readback_test", lambda *a, **k: (False, "round 1: the card returned different data than was written"))
    assert installer.install_all(ctx) == 1
    assert "cannot be trusted" in "\n".join(runner.lines)
    assert (ctx.boot / "config.txt").read_text() == TRIXIE_CONFIG and not runner.commands


def test_refuses_to_run_as_a_normal_user_and_on_a_non_pi(system, monkeypatch):
    ctx, runner = system
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    assert installer.install_all(ctx) == 1 and "sudo" in "\n".join(runner.lines)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "read_model", lambda: "")
    runner.lines.clear()
    assert installer.install_all(ctx) == 1 and "does not look like a Raspberry Pi" in "\n".join(runner.lines)


def test_user_resolution(monkeypatch):
    monkeypatch.delenv("SUDO_USER", raising=False)
    with pytest.raises(InstallError):
        installer.resolve_user(None)
    with pytest.raises(InstallError):
        installer.resolve_user("root")
    assert installer.resolve_user(ME)[0] == ME
    monkeypatch.setenv("SUDO_USER", ME)
    assert installer.resolve_user(None)[0] == ME


# ---- Wi-Fi setup pieces ---------------------------------------------------------------------------------
def test_wifi_helper_and_captive_dns_are_installed_where_networkmanager_exists(system):
    ctx, runner = system
    (ctx.etc / "NetworkManager").mkdir(parents=True)
    assert installer.install_all(ctx) == 0
    helper = ctx.local_lib / "tubeless" / "wifi-helper"
    assert helper.read_text() == (ctx.source / "packaging" / "tubeless-wifi-helper").read_text()
    assert helper.stat().st_mode & 0o777 == 0o755
    assert (ctx.etc / "NetworkManager" / "dnsmasq-shared.d" / "tubeless-captive.conf").read_text() == installer.CAPTIVE_DNS
    rule = (ctx.etc / "sudoers.d" / "tubeless").read_text()
    assert installer.WIFI_HELPER in rule and "NOPASSWD: ALL" not in rule and "nmcli" not in rule
    runner.lines.clear()
    assert installer.install_all(ctx) == 0
    assert "Wi-Fi helper already up to date" in "\n".join(runner.lines)


def test_without_networkmanager_the_install_still_works_and_says_so(system):
    ctx, runner = system
    assert installer.install_all(ctx) == 0
    assert not (ctx.local_lib / "tubeless").exists()
    assert "NetworkManager not found" in "\n".join(runner.lines)


def test_the_service_may_bind_port_80_without_root():
    text = (Path(__file__).resolve().parent.parent / "packaging" / "tubeless.service.in").read_text()
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in text and "NoNewPrivileges" not in text


# ---- YouTube support (optional) -----------------------------------------------------------------------------
def test_youtube_support_is_off_unless_asked_for(system):
    ctx, runner = system
    assert installer.install_all(ctx) == 0
    assert not [c for c in runner.commands if "yt-dlp" in " ".join(c) or "ffmpeg" in c]
    assert "--with-youtube" in "\n".join(runner.lines)


def test_with_youtube_installs_ffmpeg_and_ytdlp_into_the_users_folder(system):
    ctx, runner = system
    ctx.with_youtube = True
    assert installer.install_all(ctx) == 0
    venv = ctx.home / installer.YTDLP_VENV
    assert any(c[0] == "apt-get" and "ffmpeg" in c for c in runner.commands)
    assert ("python3", "-m", "venv", str(venv)) in runner.commands
    assert (str(venv / "bin" / "pip"), "install", "--upgrade", "yt-dlp") in runner.commands


def test_with_youtube_leaves_an_existing_ytdlp_alone(system):
    ctx, runner = system
    ctx.with_youtube = True
    (ctx.home / installer.YTDLP_VENV / "bin").mkdir(parents=True)
    (ctx.home / installer.YTDLP_VENV / "bin" / "yt-dlp").write_text("")
    runner.installed.add("ffmpeg")
    assert installer.install_all(ctx) == 0
    assert not [c for c in runner.commands if c[0] == "python3" or c[0].endswith("/pip")]
    assert "already installed" in "\n".join(runner.lines)



def test_default_config_only_mentions_the_greeting_when_given():
    assert installer.default_config("Den") == '{\n  "name": "Den"\n}\n'
    assert '"boot_greeting": "Hi"' in installer.default_config("Den", "Hi")


def test_greeting_flag_is_accepted_by_the_installer_command_line(monkeypatch):
    seen = {}
    monkeypatch.setattr(installer, "cmd_all", lambda args: (seen.update(vars(args)), 0)[1])   # main() builds its parser on each call
    assert installer.main(["all", "--name", "Den", "--greeting", "Wake up, Homer...", "--dry-run"]) == 0
    assert seen["greeting"] == "Wake up, Homer..." and seen["name"] == "Den"


def test_runner_passes_output_and_exit_status_through(capsys):
    r = installer.Runner(stall_seconds=60)
    assert r.run("sh", "-c", "echo hello; exit 0").returncode == 0
    assert "hello" in capsys.readouterr().out
    with pytest.raises(subprocess.CalledProcessError) as e:
        r.run("sh", "-c", "echo oops; exit 3")
    assert e.value.returncode == 3
    assert r.run("sh", "-c", "exit 4", check=False).returncode == 4


def test_a_command_that_goes_quiet_for_too_long_gets_a_warning_with_its_last_line(capsys):
    said = []
    r = installer.Runner(say=said.append, stall_seconds=0.6)
    r.run("sh", "-c", "echo Unpacking libllvm19; sleep 2.2; echo done")
    out = capsys.readouterr().out
    assert "Unpacking libllvm19" in out and "done" in out                            # the command's own output is untouched
    assert said and "No output from" in said[0] and "Unpacking libllvm19" in said[0] and "dpkg --configure -a" in said[0]
    assert len(said) <= 4                                                             # it warns now and then, not every second


def test_a_chatty_command_gets_no_warning():
    said = []
    r = installer.Runner(say=said.append, stall_seconds=0.8)
    r.run("sh", "-c", "for i in 1 2 3 4 5 6; do echo tick; sleep 0.3; done")
    assert said == []
