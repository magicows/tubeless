"""
Installing Tubeless on a fresh Raspberry Pi OS Lite (Pi Zero 2 W + the Waveshare 2.8" DPI screen).

    sudo python3 -m tubeless.installer all [--name "My TV"] [--dry-run]     do everything
    python3 -m tubeless.installer boot [--apply]                           only the boot files (shows a diff first)
    python3 -m tubeless.installer overlays --to DIR                        download + verify Waveshare's overlays

`all` is idempotent (each step checks whether it is already done) and runs, in order:
  1. preflight   Pi model, Python, power, and a write-then-read-back test of the SD card (a bad card corrupts silently)
  2. packages    mpv, Python libraries, pigpio: lean (`--no-install-recommends`), with retries
  3. display     Waveshare's overlays (downloaded from Waveshare, every file checked against a pinned SHA-256),
                 config.txt / cmdline.txt edits with a diff and backups
  4. console     the login prompt is masked on the panel; pigpiod enabled
  5. app         the code copied to ~/tubeless, a config file, a ~/videos folder
  6. services    systemd units, and a *narrow* sudoers rule (power off / reboot / restart Tubeless only)

Only the standard library is used, so it runs on a fresh Pi OS before anything else is installed.
Nothing is removed from your system, every file it changes is backed up once, and --dry-run changes nothing.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple


class InstallError(Exception):
    """Something is wrong enough that continuing would be unsafe or pointless."""


BEGIN = "# >>> tubeless (managed block: edit outside it) >>>"
END = "# <<< tubeless <<<"

# Waveshare's recommended set for this display (their wiki lists all five for Bullseye and Bookworm), plus the
# audio overlay that moves the PWM speaker to GPIO 18/19. `dtoverlay=vc4-kms-v3d` and `dtparam=audio=on` are
# Pi OS defaults and are added only if missing.
CONFIG_LINES = [
    "dtoverlay=vc4-kms-v3d",
    "dtoverlay=waveshare-28dpi-3b-4b",
    "dtoverlay=waveshare-28dpi-3b",
    "dtoverlay=waveshare-28dpi-4b",
    "dtoverlay=waveshare-touch-28dpi",
    "dtoverlay=vc4-kms-dpi-2inch8",
    "dtparam=audio=on",
    "dtoverlay=audremap,enable_jack,pins_18_19",
]

# Kernel command line: rotate the panel to landscape, keep the screen awake, hide the logo and the blinking cursor,
# and send the console to tty3 so the text console never shows on the panel.
VIDEO_TOKEN = "video=DPI-1:480x640@60,rotate=90"
CMDLINE_TOKENS = [VIDEO_TOKEN, "consoleblank=0", "logo.nologo", "vt.global_cursor_default=0"]


def _is_overlay_line(line: str) -> bool:
    return line.strip().startswith("dtoverlay=")


def apply_config_txt(text: str) -> str:
    """config.txt with everything Tubeless needs present exactly once (in a managed block at the end)."""
    lines = text.splitlines()
    if BEGIN in lines and END in lines:                              # drop a previous managed block; rebuilt below
        a, b = lines.index(BEGIN), lines.index(END)
        lines = lines[:a] + lines[b + 1:]
    while lines and not lines[-1].strip():
        lines.pop()
    present = {l.strip() for l in lines if l.strip() and not l.strip().startswith("#")}
    missing = [l for l in CONFIG_LINES if l not in present]
    if not missing:
        return "\n".join(lines) + "\n"
    # the block must be in the [all] section; a section header left open above would capture it
    sections = [l for l in lines if l.strip().startswith("[") and l.strip().endswith("]")]
    out = lines + [""]
    if sections and sections[-1].strip() != "[all]":
        out.append("[all]")
    out += [BEGIN] + missing + [END]
    return "\n".join(out) + "\n"


def apply_cmdline(text: str) -> str:
    """cmdline.txt (one line) with the Tubeless tokens in place and the text console moved off the panel."""
    out: List[str] = []
    have_video = False
    for t in text.split():
        if t.startswith("video=DPI-1:"):
            if not have_video:                                        # fix it where it is (never churn a correct line)
                out.append(VIDEO_TOKEN)
                have_video = True
            continue
        out.append("console=tty3" if t == "console=tty1" else t)
    for t in CMDLINE_TOKENS:
        if t not in out:
            out.append(t)
    return " ".join(out) + "\n"


def diff(name: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"{name} (now)", f"{name} (after)"))


def default_boot_dir() -> Path:
    return Path("/boot/firmware") if Path("/boot/firmware/config.txt").exists() else Path("/boot")


def plan_boot(boot_dir: Path) -> List[Tuple[Path, str, str]]:
    """[(file, current text, new text)] for the files that would change."""
    changes = []
    for name, fn in (("config.txt", apply_config_txt), ("cmdline.txt", apply_cmdline)):
        path = boot_dir / name
        before = path.read_text()
        after = fn(before)
        if after != before:
            changes.append((path, before, after))
    return changes


def backup_once(path: Path) -> Path:
    backup = path.with_name(path.name + ".before-tubeless")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def copy_overlays(source: Path, boot_dir: Path, apply: bool) -> List[str]:
    dest = boot_dir / "overlays"
    done = []
    for f in sorted(source.glob("*.dtbo")):
        if f.name.startswith("._"):                                   # macOS resource-fork junk
            continue
        target = dest / f.name
        if target.exists() and target.read_bytes() == f.read_bytes():
            continue
        if apply:
            if target.exists():
                backup_once(target)
            shutil.copy2(f, target)
        done.append(f.name)
    return done


# ─────────────────────────────────────────────
#  Waveshare's overlays: fetched at install time (not redistributed), every file checked
# ─────────────────────────────────────────────
WAVESHARE_URL = "https://files.waveshare.com/wiki/2.8inc-DPI-LCD/28DPI-DTBO.zip"
WAVESHARE_FILES = {
    "vc4-kms-dpi-2inch8.dtbo": "0d4575c83d69ad778e5010bbc6165b90b9242b25b7013e309a8db9e4b6b078d6",
    "waveshare-28dpi-3b-4b.dtbo": "968d684fa806926f745ea6ba4d16907435af90b14557ca27cfa888f553ecd2f3",
    "waveshare-28dpi-3b.dtbo": "55285088c23d55d3196c085b07ac19cd84061e4669e401cb7940b4d8a73d24b6",
    "waveshare-28dpi-4b.dtbo": "14cedae4f419804f0acda079c6e6bcb8b7e6ecffd3be4b7a3b01548b8e901b1c",
    "waveshare-touch-28dpi.dtbo": "3f3591e498ce424ff04b6a24ff2edeb6535ac8f8f6fc9006ee7981129fa6174a",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def overlays_from_zip(data: bytes, allow_changed: bool = False) -> Dict[str, bytes]:
    """The .dtbo files in Waveshare's zip, after checking each against its pinned SHA-256."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            found = {Path(n).name: z.read(n) for n in z.namelist()
                     if n.endswith(".dtbo") and not Path(n).name.startswith("._")}
    except zipfile.BadZipFile as e:
        raise InstallError(f"the overlay download is not a valid zip file ({e})")
    out: Dict[str, bytes] = {}
    for name, want in WAVESHARE_FILES.items():
        if name not in found:
            raise InstallError(f"{name} is missing from the Waveshare download")
        got = sha256(found[name])
        if got != want and not allow_changed:
            raise InstallError(
                f"{name} does not match the checksum Tubeless was tested with.\n"
                f"   expected {want}\n   got      {got}\n"
                "   Waveshare may have updated the file. If you trust it, run again with --allow-changed-overlays.")
        out[name] = found[name]
    return out


def fetch_overlays(url: str = WAVESHARE_URL, allow_changed: bool = False, timeout: float = 60.0) -> Dict[str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read(5_000_000)
    except Exception as e:
        raise InstallError(f"could not download the overlays from {url} ({e})")
    return overlays_from_zip(data, allow_changed)


def write_overlays(files: Dict[str, bytes], boot_dir: Path, apply: bool) -> List[str]:
    """Put overlay files in <boot_dir>/overlays; returns the names that were (or would be) changed."""
    dest = boot_dir / "overlays"
    changed = []
    for name, content in sorted(files.items()):
        target = dest / name
        if target.exists() and target.read_bytes() == content:
            continue
        if apply:
            if target.exists():
                backup_once(target)
            target.write_bytes(content)
        changed.append(name)
    return changed


# ─────────────────────────────────────────────
#  Checks on the machine
# ─────────────────────────────────────────────
def sd_readback_test(directory: str = "/var/tmp", megabytes: int = 64, rounds: int = 2) -> Tuple[bool, str]:
    """
    Write random data, flush it, read it back and compare. A failing or counterfeit SD card silently returns
    different data than was written, which later shows up as 'corrupt package' errors and random crashes.
    """
    path = Path(directory) / ".tubeless-sdtest"
    chunk = 1024 * 1024
    try:
        for n in range(1, rounds + 1):
            written = hashlib.sha256()
            with open(path, "wb") as f:
                for _ in range(megabytes):
                    block = os.urandom(chunk)
                    written.update(block)
                    f.write(block)
                f.flush()
                os.fsync(f.fileno())
            try:
                with open("/proc/sys/vm/drop_caches", "w") as c:        # make sure we read the card, not memory
                    os.sync()
                    c.write("3")
            except OSError:
                pass
            read = hashlib.sha256()
            with open(path, "rb") as f:
                if hasattr(os, "posix_fadvise"):
                    os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                while True:
                    block = f.read(chunk)
                    if not block:
                        break
                    read.update(block)
            if written.digest() != read.digest():
                return False, f"round {n}: the card returned different data than was written"
        return True, f"{megabytes} MB x {rounds} written and read back correctly"
    except OSError as e:
        return False, f"could not run the test ({e})"
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def read_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_bytes().decode(errors="ignore").strip("\x00\n ")
    except OSError:
        return ""


# ─────────────────────────────────────────────
#  Pieces that are pure text, so they can be tested
# ─────────────────────────────────────────────
PACKAGES = ["mpv", "python3-evdev", "python3-psutil", "python3-pil", "python3-numpy", "pigpio", "python3-pigpio",
            "mediainfo"]
OPTIONAL_PACKAGES = ["python3-segno", "python3-qrcode"]       # QR code on the upload screen; first one the OS has wins
UNITS = ["tubeless.service", "tubeless-boot.service"]


def render_unit(template: str, user: str, appdir: str, name: str) -> str:
    return template.replace("@USER@", user).replace("@APPDIR@", appdir).replace("@NAME@", name)


WIFI_HELPER = "/usr/local/lib/tubeless/wifi-helper"          # root-owned, so the user can't change what sudo runs
CAPTIVE_DNS = "# Installed by Tubeless: while the TV hosts its Wi-Fi setup hotspot, every name leads to the setup page\n" \
              "address=/#/10.42.0.1\n"


def sudoers_rule(user: str) -> str:
    """Just what Tubeless needs from root: power off, reboot, restarting itself, and its one Wi-Fi helper."""
    cmds = ", ".join(["/usr/bin/systemctl poweroff", "/usr/bin/systemctl reboot",
                      "/usr/bin/systemctl restart tubeless", "/usr/bin/systemctl restart tubeless.service", WIFI_HELPER])
    return f"# Installed by Tubeless: lets the TV power itself off, restart its own service and set up Wi-Fi, nothing more.\n" \
           f"{user} ALL=(root) NOPASSWD: {cmds}\n"


WATCHDOG_CONF = ("# Installed by Tubeless: if the whole TV ever freezes, the Pi's hardware watchdog restarts it instead of leaving it dead.\n"
                 "[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n")
JOURNAL_CONF = ("# Installed by Tubeless: keep the system log across restarts (a small amount), so a freeze can be looked into afterwards.\n"
                "[Journal]\nStorage=persistent\nSystemMaxUse=30M\nRuntimeMaxUse=15M\n")


WIFI_POWERSAVE_CONF = ("# Installed by Tubeless: the Pi Zero 2 W's Wi-Fi chip can drop off the network and stay off while its power saving is on.\n"
                       "[connection]\nwifi.powersave = 2\n")


def default_config(name: str, greeting: str = "") -> str:
    import json
    data = {"name": name}
    if greeting:
        data["boot_greeting"] = greeting
    return json.dumps(data, indent=2) + "\n"


# ─────────────────────────────────────────────
#  Doing things (every action goes through here, so --dry-run and tests can intercept)
# ─────────────────────────────────────────────
STALL_SECONDS = 600              # a long command that prints nothing for this long gets a warning


def stall_advice(cmd: Sequence[str], quiet_for: float, last_line: str) -> str:
    mins = max(1, int(quiet_for // 60))
    what = " ".join(cmd)[:70]
    return (f"\n!! No output from `{what}` for {mins} minutes.\n"
            f"   It may only be slow (a Pi Zero 2 W unpacking big packages is), but it could be stuck. The last thing it said:\n"
            f"       {last_line or '(nothing yet)'}\n"
            f"   To check, in a second SSH session:  ps aux | grep -E 'dpkg|apt'\n"
            f"   If dpkg is using 100% CPU and the newest *.dpkg-new file under /usr/lib has not grown for a few minutes,\n"
            f"   it is stuck: press Ctrl-C here, run  sudo dpkg --configure -a && sudo apt-get -f install -y\n"
            f"   and then run this installer again (it is safe to run again).\n")


class Runner:
    def __init__(self, dry_run: bool = False, say: Callable[[str], None] = print, stall_seconds: float = STALL_SECONDS):
        self.dry_run = dry_run
        self.say = say
        self.stall_seconds = stall_seconds

    def run(self, *cmd: str, check: bool = True, env: Optional[dict] = None) -> "subprocess.CompletedProcess":
        if self.dry_run:
            self.say("      would run: " + " ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        full_env = dict(os.environ, **(env or {}))
        rc = self._run_watched(cmd, full_env)
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, list(cmd))
        return subprocess.CompletedProcess(cmd, rc)

    def _run_watched(self, cmd: Sequence[str], env: dict) -> int:
        """Run a command with its output on the terminal as usual (through a pty, so apt keeps its progress display), and
        say something if it goes quiet for a long time: a frozen dpkg looks just like a slow one."""
        import pty
        import select
        master, slave = pty.openpty()
        try:
            import fcntl
            import struct
            import termios
            if sys.stdout.isatty():
                fcntl.ioctl(slave, termios.TIOCSWINSZ, fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)))
        except (ImportError, OSError):
            pass
        proc = subprocess.Popen(list(cmd), stdout=slave, stderr=slave, env=env)
        os.close(slave)
        last_output = time.time()
        last_line, warned_at = "", 0.0
        try:
            while True:
                ready, _, _ = select.select([master], [], [], 1.0)
                if ready:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    last_output, warned_at = time.time(), 0.0
                    text = data.decode("utf-8", "replace")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    tail = text.replace("\r", "\n").strip().splitlines()
                    if tail:
                        last_line = tail[-1].strip()[:120]
                elif proc.poll() is not None:
                    break
                quiet = time.time() - last_output
                if quiet >= self.stall_seconds and time.time() - warned_at >= self.stall_seconds:
                    warned_at = time.time()
                    self.say(stall_advice(cmd, quiet, last_line))
            return proc.wait()
        finally:
            if proc.poll() is None:
                proc.terminate()
            os.close(master)

    def query(self, *cmd: str) -> "subprocess.CompletedProcess":
        """A read-only command whose result we look at (runs even in dry-run mode)."""
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")

    def write(self, path: Path, content: str, mode: int = 0o644, owner: Optional[str] = None) -> None:
        if self.dry_run:
            self.say(f"      would write {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        os.chmod(path, mode)
        if owner:
            info = pwd.getpwnam(owner)
            os.chown(path, info.pw_uid, info.pw_gid)


@dataclass
class Context:
    user: str
    home: Path
    source: Path
    name: str = "Tubeless"
    boot_dir: Optional[Path] = None
    etc: Path = Path("/etc")
    local_lib: Path = Path("/usr/local/lib")
    dry_run: bool = False
    skip_sd_test: bool = False
    allow_changed_overlays: bool = False
    with_youtube: bool = False
    greeting: str = ""
    runner: Runner = field(default_factory=Runner)
    reboot_needed: bool = False

    @property
    def appdir(self) -> Path:
        return self.home / "tubeless"

    @property
    def boot(self) -> Path:
        return self.boot_dir or default_boot_dir()

    def say(self, text: str) -> None:
        self.runner.say(text)

    def did(self, past: str, would: str) -> str:
        """Past tense normally, 'would ...' in a dry run, so the log never claims something that was not done."""
        return would if self.dry_run else past


def _chown_tree(path: Path, user: str) -> None:
    info = pwd.getpwnam(user)
    for p in [path] + list(path.rglob("*")):
        try:
            os.chown(p, info.pw_uid, info.pw_gid)
        except OSError:
            pass


# ─────────────────────────────────────────────
#  The steps
# ─────────────────────────────────────────────
def step_preflight(ctx: Context) -> None:
    if not ctx.dry_run and os.geteuid() != 0:
        raise InstallError("run this with sudo")
    model = read_model()
    if not model:
        raise InstallError("this does not look like a Raspberry Pi (no /proc/device-tree/model)")
    ctx.say(f"      board: {model}")
    if "Zero 2" not in model:
        ctx.say("      note: Tubeless is built and tested for the Pi Zero 2 W; other boards are untested")
    if sys.version_info < (3, 9):
        raise InstallError("Python 3.9 or newer is needed")
    if not ctx.dry_run and not shutil.which("apt-get"):
        raise InstallError("apt-get was not found: this installer is for Raspberry Pi OS (Debian)")
    if not (ctx.boot / "config.txt").exists():
        raise InstallError(f"no config.txt in {ctx.boot}")
    r = ctx.runner.query("vcgencmd", "get_throttled") if shutil.which("vcgencmd") else None
    if r is not None and r.returncode == 0:
        flag = r.stdout.strip().split("=")[-1]
        ctx.say(f"      power flags: {flag}" + ("  (fine)" if flag == "0x0" else "  (under-voltage or throttling has happened: check the power supply)"))
    if ctx.skip_sd_test or ctx.dry_run:
        ctx.say("      SD card test skipped")
        return
    ok, detail = sd_readback_test()
    ctx.say(f"      SD card: {detail}")
    if not ok:
        raise InstallError(
            "The SD card cannot be trusted. Re-seat it and try again, or replace it with a genuine card.\n"
            "   (Counterfeit cards can be checked on a computer with f3: https://fight-flash-fraud.readthedocs.io/)")


def _missing_packages(ctx: Context) -> List[str]:
    missing = []
    for pkg in PACKAGES:
        r = ctx.runner.query("dpkg", "-s", pkg)
        if r.returncode != 0 or "install ok installed" not in r.stdout:
            missing.append(pkg)
    return missing


def step_packages(ctx: Context) -> None:
    missing = _missing_packages(ctx)
    if not missing:
        ctx.say("      already installed")
        return
    ctx.say(f"      installing: {', '.join(missing)}")
    env = {"DEBIAN_FRONTEND": "noninteractive"}
    ctx.runner.run("apt-get", "-o", "Acquire::Retries=5", "update", env=env)
    # --no-install-recommends matters: mpv's "recommended" extras pull in ~280 desktop packages, which also runs
    # a 512 MB Pi out of memory while apt works out what to install.
    ctx.runner.run("apt-get", "-o", "Acquire::Retries=5", "install", "-y", "--no-install-recommends", *missing, env=env)
    _install_optional_qr(ctx, env)


def _install_optional_qr(ctx: Context, env) -> None:
    """A QR code is a nicety: take the first QR library the OS offers, and carry on without one."""
    for pkg in OPTIONAL_PACKAGES:
        have = ctx.runner.query("dpkg", "-s", pkg)
        if have.returncode == 0 and "install ok installed" in have.stdout:
            return
    for pkg in OPTIONAL_PACKAGES:
        try:
            ctx.runner.run("apt-get", "install", "-y", "--no-install-recommends", pkg, env=env)
            return
        except Exception:
            continue
    ctx.say("      (no QR library available; the upload screen will show the address only)")


def step_display(ctx: Context) -> None:
    ctx.say(f"      boot files: {ctx.boot}")
    files = fetch_overlays(allow_changed=ctx.allow_changed_overlays) if not ctx.dry_run else None
    if files is None:
        ctx.say(f"      would download and verify Waveshare's overlays from {WAVESHARE_URL}")
    else:
        changed = write_overlays(files, ctx.boot, apply=True)
        ctx.say("      overlays: " + (", ".join(changed) + " installed (checksums verified)" if changed else "already in place"))
        if changed:
            ctx.reboot_needed = True
    changes = plan_boot(ctx.boot)
    if not changes:
        ctx.say("      config.txt and cmdline.txt already have everything Tubeless needs")
        return
    for path, before, after in changes:
        for line in diff(path.name, before, after).splitlines():
            ctx.say("        " + line)
        if not ctx.dry_run:
            backup_once(path)
            path.write_text(after)
            ctx.say(f"      changed {path} (original kept as {path.name}.before-tubeless)")
    if not ctx.dry_run:
        ctx.reboot_needed = True


def _unit_state(ctx: Context, unit: str) -> str:
    return ctx.runner.query("systemctl", "is-enabled", unit).stdout.strip()


def step_console(ctx: Context) -> None:
    if _unit_state(ctx, "getty@tty1.service") == "masked":
        ctx.say("      login prompt on the panel already masked")
    else:
        ctx.runner.run("systemctl", "mask", "getty@tty1.service")
        ctx.say("      " + ctx.did("login prompt on the panel masked (the screen shows only Tubeless)", "would mask the login prompt on the panel"))
    if ctx.runner.query("systemctl", "cat", "pigpiod.service").returncode != 0:
        ctx.say("      !! no pigpiod.service on this system: the backlight will be on/off only")
    elif _unit_state(ctx, "pigpiod.service") == "enabled":
        ctx.say("      pigpiod already enabled")
    else:
        ctx.runner.run("systemctl", "enable", "pigpiod.service")
        ctx.say("      " + ctx.did("pigpiod enabled (dims the backlight)", "would enable pigpiod (dims the backlight)"))


IGNORE_WHEN_COPYING = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv*", "snapshots", "sim_data",
                                             ".pytest_cache", "tests", "legacy")


def step_app(ctx: Context) -> None:
    if not (ctx.source / "tubeless" / "__init__.py").exists():
        raise InstallError(f"{ctx.source} does not contain the tubeless package")
    if ctx.source.resolve() == ctx.appdir.resolve():
        ctx.say(f"      running from {ctx.appdir} already")
    elif ctx.dry_run:
        ctx.say(f"      would copy the code from {ctx.source} to {ctx.appdir}")
    else:
        if ctx.appdir.exists():
            for sub in ("tubeless", "packaging", "tools"):
                shutil.rmtree(ctx.appdir / sub, ignore_errors=True)
        ctx.appdir.mkdir(parents=True, exist_ok=True)
        for sub in ("tubeless", "packaging", "tools"):
            if (ctx.source / sub).exists():
                shutil.copytree(ctx.source / sub, ctx.appdir / sub, ignore=IGNORE_WHEN_COPYING)
        for single in ("README.md", "LICENSE"):
            if (ctx.source / single).exists():
                shutil.copy2(ctx.source / single, ctx.appdir / single)
        _chown_tree(ctx.appdir, ctx.user)
        ctx.say(f"      code installed in {ctx.appdir}")
    cfg = ctx.home / ".config" / "tubeless" / "config.json"
    if cfg.exists():
        ctx.say(f"      config kept: {cfg}")
    else:
        ctx.runner.write(cfg, default_config(ctx.name, ctx.greeting), owner=ctx.user)
        if not ctx.dry_run:
            _chown_tree(cfg.parent, ctx.user)
        ctx.say("      " + ctx.did(f"config created: {cfg}  (name: {ctx.name})", f"would create {cfg}  (name: {ctx.name})"))
    videos = ctx.home / "videos"
    if videos.exists():
        ctx.say(f"      videos folder exists: {videos}")
    elif ctx.dry_run:
        ctx.say(f"      would create {videos}")
    else:
        videos.mkdir(parents=True)
        _chown_tree(videos, ctx.user)
        ctx.say("      " + ctx.did(f"created {videos}: put one folder per channel in it, each with some videos", f"would create {videos}"))


def step_services(ctx: Context) -> None:
    for unit in UNITS:
        template = ctx.appdir / "packaging" / (unit + ".in")
        if not template.exists():
            template = ctx.source / "packaging" / (unit + ".in")
        if not template.exists():
            raise InstallError(f"missing {unit}.in in the packaging folder")
        text = render_unit(template.read_text(), ctx.user, str(ctx.appdir), ctx.name)
        target = ctx.etc / "systemd" / "system" / unit
        if target.exists() and target.read_text() == text:
            ctx.say(f"      {unit} already up to date")
        else:
            ctx.runner.write(target, text)
            ctx.say("      " + ctx.did(f"{unit} written", f"would write {unit}"))
    ctx.runner.run("systemctl", "daemon-reload")
    ctx.runner.run("systemctl", "enable", *UNITS)

    step_wifi_setup(ctx)
    step_reliability(ctx)

    rule = sudoers_rule(ctx.user)
    sudoers = ctx.etc / "sudoers.d" / "tubeless"
    if sudoers.exists() and sudoers.read_text() == rule:
        ctx.say("      sudoers rule already in place")
    else:
        if not ctx.dry_run:
            with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tmp:
                tmp.write(rule)
            check = ctx.runner.query("visudo", "-cf", tmp.name)
            os.unlink(tmp.name)
            if check.returncode != 0:
                raise InstallError("the generated sudoers rule did not validate (visudo): " + check.stdout + check.stderr)
        ctx.runner.write(sudoers, rule, mode=0o440)
        ctx.say("      " + ctx.did("sudoers rule installed (power off / reboot / restart Tubeless only)",
                                 "would install a sudoers rule (power off / reboot / restart Tubeless only)"))


def step_reliability(ctx: Context) -> None:
    """A hardware watchdog (a frozen TV restarts itself) and a system log that survives restarts (so a freeze can be diagnosed)."""
    wrote = False
    for rel, content, what in (("systemd/system.conf.d/tubeless-watchdog.conf", WATCHDOG_CONF, "hardware watchdog"),
                               ("systemd/journald.conf.d/tubeless-journal.conf", JOURNAL_CONF, "persistent system log (30 MB at most)")):
        target = ctx.etc / rel
        if target.exists() and target.read_text() == content:
            ctx.say(f"      {what} already set up")
        else:
            ctx.runner.write(target, content)
            ctx.say("      " + ctx.did(f"{what} set up", f"would set up the {what}"))
            wrote = True
    if wrote:
        ctx.runner.run("systemctl", "daemon-reexec")                  # the watchdog setting is read by systemd itself
        ctx.runner.run("systemctl", "restart", "systemd-journald")
    if (ctx.etc / "NetworkManager").is_dir():                         # Wi-Fi power saving off (2 = disabled)
        target = ctx.etc / "NetworkManager" / "conf.d" / "tubeless-wifi.conf"
        if target.exists() and target.read_text() == WIFI_POWERSAVE_CONF:
            ctx.say("      Wi-Fi power saving already off")
        else:
            ctx.runner.write(target, WIFI_POWERSAVE_CONF)
            ctx.say("      " + ctx.did("Wi-Fi power saving turned off (takes effect at the next restart)", "would turn Wi-Fi power saving off"))
            ctx.reboot_needed = True


def step_wifi_setup(ctx: Context) -> None:
    """The root-owned Wi-Fi helper, and the DNS rule that makes phones pop up the setup page. Needs NetworkManager."""
    if not (ctx.etc / "NetworkManager").is_dir():
        ctx.say("      NetworkManager not found: Wi-Fi setup from the TV's screen won't be offered on this system")
        return
    source = ctx.appdir / "packaging" / "tubeless-wifi-helper"
    if not source.exists():
        source = ctx.source / "packaging" / "tubeless-wifi-helper"
    if not source.exists():
        raise InstallError("missing packaging/tubeless-wifi-helper")
    target = ctx.local_lib / "tubeless" / "wifi-helper"
    if target.exists() and target.read_text() == source.read_text():
        ctx.say("      Wi-Fi helper already up to date")
    else:
        ctx.runner.write(target, source.read_text(), mode=0o755)
        ctx.say("      " + ctx.did("Wi-Fi helper installed (root-owned)", "would install the Wi-Fi helper"))
    dns = ctx.etc / "NetworkManager" / "dnsmasq-shared.d" / "tubeless-captive.conf"
    if dns.exists() and dns.read_text() == CAPTIVE_DNS:
        ctx.say("      setup-page DNS rule already in place")
    else:
        ctx.runner.write(dns, CAPTIVE_DNS)
        ctx.say("      " + ctx.did("setup-page DNS rule installed", "would install the setup-page DNS rule"))


YTDLP_VENV = Path(".local/share/tubeless/ytdlp")           # under the user's home; Tubeless looks for yt-dlp here


def step_youtube(ctx: Context) -> None:
    """Optional: what YouTube channels need (yt-dlp, kept up to date by pip, and ffmpeg to join video and sound)."""
    if not ctx.with_youtube:
        ctx.say("      not requested (add --with-youtube to enable YouTube channels; see the README for what that involves)")
        return
    ffmpeg = ctx.runner.query("dpkg", "-s", "ffmpeg")
    if ffmpeg.returncode != 0 or "install ok installed" not in ffmpeg.stdout:
        ctx.say("      installing ffmpeg")
        env = {"DEBIAN_FRONTEND": "noninteractive"}
        ctx.runner.run("apt-get", "-o", "Acquire::Retries=5", "install", "-y", "--no-install-recommends", "ffmpeg", env=env)
    venv = ctx.home / YTDLP_VENV
    if (venv / "bin" / "yt-dlp").exists():
        ctx.say("      yt-dlp already installed (update it any time with: python3 -m tubeless youtube update)")
        return
    ctx.say("      installing yt-dlp into " + str(venv))
    ctx.runner.run("python3", "-m", "venv", str(venv))
    ctx.runner.run(str(venv / "bin" / "pip"), "install", "--upgrade", "yt-dlp")
    if not ctx.dry_run and venv.exists():
        _chown_tree(ctx.home / ".local", ctx.user)


STEPS: List[Tuple[str, Callable[[Context], None]]] = [
    ("Checking this system", step_preflight),
    ("Installing packages", step_packages),
    ("Setting up the display and touch", step_display),
    ("Console and backlight", step_console),
    ("Installing Tubeless", step_app),
    ("Services and permissions", step_services),
    ("YouTube support (optional)", step_youtube),
]


def install_all(ctx: Context) -> int:
    ctx.say(f"Tubeless installer: user {ctx.user}, TV name {ctx.name!r}" + ("  [DRY RUN: nothing will be changed]" if ctx.dry_run else ""))
    for n, (title, fn) in enumerate(STEPS, 1):
        ctx.say(f"\n[{n}/{len(STEPS)}] {title}")
        try:
            fn(ctx)
        except InstallError as e:
            ctx.say(f"\n!! {e}")
            return 1
        except subprocess.CalledProcessError as e:
            ctx.say(f"\n!! a command failed: {' '.join(map(str, e.cmd))} (exit {e.returncode})")
            return 1
    ctx.say("")
    if ctx.dry_run:
        ctx.say("That was a dry run. Run again without --dry-run to make these changes.")
    elif ctx.reboot_needed:
        ctx.say("Done. Reboot now (sudo reboot): the display, touch and audio changes take effect at boot.")
    else:
        ctx.say("Done. Restart with: sudo systemctl restart tubeless")
    ctx.say(f"Put your videos in {ctx.home / 'videos'} (one folder per channel), then reboot or restart.")
    return 0


def resolve_user(requested: Optional[str]) -> Tuple[str, Path]:
    user = requested or os.environ.get("SUDO_USER") or ""
    if not user or user == "root":
        raise InstallError("say which user the TV runs as: sudo python3 -m tubeless.installer all --user <name>")
    try:
        return user, Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        raise InstallError(f"no such user: {user}")


def cmd_all(args) -> int:
    try:
        user, home = resolve_user(args.user)
    except InstallError as e:
        print(f"!! {e}", file=sys.stderr)
        return 2
    source = Path(args.source).resolve() if args.source else Path(__file__).resolve().parent.parent
    ctx = Context(user=user, home=home, source=source, name=args.name, boot_dir=Path(args.boot_dir) if args.boot_dir else None,
                  dry_run=args.dry_run, skip_sd_test=args.skip_sd_test, allow_changed_overlays=args.allow_changed_overlays,
                  with_youtube=args.with_youtube, greeting=args.greeting or "", runner=Runner(args.dry_run))
    return install_all(ctx)


def cmd_overlays(args) -> int:
    try:
        files = fetch_overlays(allow_changed=args.allow_changed_overlays)
    except InstallError as e:
        print(f"!! {e}", file=sys.stderr)
        return 1
    dest = Path(args.to)
    dest.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (dest / name).write_bytes(content)
    print(f"verified and wrote {len(files)} overlay files to {dest}")
    return 0


def cmd_boot(args) -> int:
    boot = Path(args.boot_dir) if args.boot_dir else default_boot_dir()
    if not (boot / "config.txt").exists():
        print(f"no config.txt in {boot}", file=sys.stderr)
        return 2
    print(f"boot files: {boot}")
    if args.overlays_from:
        names = copy_overlays(Path(args.overlays_from), boot, args.apply)
        print(("copied" if args.apply else "would copy") + f" overlays: {', '.join(names) or 'none needed (already in place)'}")
    changes = plan_boot(boot)
    if not changes:
        print("config.txt and cmdline.txt already have everything Tubeless needs.")
        return 0
    for path, before, after in changes:
        print()
        print(diff(path.name, before, after))
    if not args.apply:
        print("(dry run: nothing was changed. Add --apply, as root, to make these changes.)")
        return 0
    for path, before, after in changes:
        backup = backup_once(path)
        path.write_text(after)
        print(f"changed {path} (original kept as {backup.name})")
    print("a reboot is needed for the display, touch and audio changes to take effect.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tubeless.installer")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("boot", help="edit config.txt / cmdline.txt and place the display overlays")
    p.add_argument("--boot-dir")
    p.add_argument("--overlays-from")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_boot)

    p = sub.add_parser("all", help="do everything (run with sudo)")
    p.add_argument("--user", help="the user the TV runs as (default: the user who ran sudo)")
    p.add_argument("--name", default="Tubeless", help="the TV's display name (default: Tubeless)")
    p.add_argument("--greeting", help="the line typed out on the boot screen (default: Wake up, couch potato...)")
    p.add_argument("--source", help="the checkout to install from (default: the one this file is in)")
    p.add_argument("--boot-dir")
    p.add_argument("--dry-run", action="store_true", help="show what would be done, change nothing")
    p.add_argument("--skip-sd-test", action="store_true")
    p.add_argument("--with-youtube", action="store_true",
                   help="also install what YouTube channels need (ffmpeg, and yt-dlp from PyPI)")
    p.add_argument("--allow-changed-overlays", action="store_true",
                   help="accept Waveshare overlay files that differ from the ones Tubeless was tested with")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("overlays", help="download and verify Waveshare's overlays into a folder")
    p.add_argument("--to", required=True)
    p.add_argument("--allow-changed-overlays", action="store_true")
    p.set_defaults(func=cmd_overlays)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
