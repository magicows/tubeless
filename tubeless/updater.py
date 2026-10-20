"""
Updating Tubeless from its GitHub releases, from the upload page.

  check()      asks GitHub for the latest release and says whether it is newer than this one
  apply()      downloads that release's tarball, checks its SHA-256, tries to import it, swaps the code in, restarts
  rollback()   puts the previous code back

Only the code is replaced (the `tubeless`, `packaging` and `tools` folders, README and LICENSE: what install.sh copies).
Videos, settings and config live elsewhere and are never touched. The code that was running is kept in `.previous/` so an
update can be undone. System files (service units, the sudoers rule) are installed by install.sh; if a release changes
them the page says so, and install.sh needs running again.

Nothing here runs by itself: it only checks when asked, and the TV is only on Wi-Fi in upload mode anyway.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from . import __version__

log = logging.getLogger(__name__)

REPO = "magicows/tubeless"
CODE_DIRS = ("tubeless", "packaging", "tools")
CODE_FILES = ("README.md", "LICENSE")
BACKUP = ".previous"
MAX_DOWNLOAD = 40 * 1024 * 1024
TIMEOUT = 20


class UpdateError(Exception):
    """With a message fit to show on the page."""


def parse_version(text: str) -> Tuple[int, ...]:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(p) for p in m.group(1).split("."))


def newer(latest: str, current: str) -> bool:
    try:
        a, b = parse_version(latest), parse_version(current)
    except ValueError:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def default_fetch(url: str, limit: int = MAX_DOWNLOAD) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "tubeless-updater", "Accept": "application/vnd.github+json, */*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("That download is bigger than expected, so it was refused.")
    return data


def default_latest_url(url: str) -> str:
    """Where github.com/<repo>/releases/latest leads: .../releases/tag/vX.Y.Z. A web page, not the API, so no anonymous rate limit."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "tubeless-updater"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.geturl()


def default_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "tubeless-updater"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            return True
    except (OSError, ValueError):
        return False


def _restart_service() -> None:
    """Restart Tubeless (this process included) through the one sudo rule the installer allows."""
    subprocess.Popen(["sudo", "-n", "systemctl", "restart", "tubeless"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _safe_extract(blob: bytes, dest: Path) -> Path:
    """Unpack a release tarball into dest and return its single top-level folder. Refuses anything that escapes dest."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = tar.getmembers()
        root = os.path.realpath(dest)
        for m in members:
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == root or target.startswith(root + os.sep)):
                raise UpdateError("The release file is not safe to unpack, so nothing was changed.")
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                raise UpdateError("The release file has links in it, so nothing was changed.")
        tar.extractall(dest, members=members, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise UpdateError("The release file has an unexpected layout.")
    return tops[0]


def _dir_digest(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_dir():
        for p in sorted(path.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(path)).encode())
                h.update(p.read_bytes())
    return h.hexdigest()


class Updater:
    def __init__(self, appdir, current: str = __version__, repo: str = REPO,
                 fetch: Callable[[str], bytes] = default_fetch, restart: Callable[[], None] = _restart_service,
                 latest_url: Callable[[str], str] = default_latest_url, exists: Callable[[str], bool] = default_exists,
                 python: str = sys.executable, clock: Callable[[], float] = time.time,
                 notify: Callable[[str, str], None] = lambda kind, version: None, marker: Optional[Path] = None):
        self.latest_url, self.exists = latest_url, exists
        self.notify, self.marker = notify, Path(marker) if marker else None
        self.appdir = Path(appdir)
        self.current, self.repo, self.fetch, self.restart = current, repo, fetch, restart
        self.python, self.clock = python, clock
        self._lock = threading.Lock()
        self._status: Dict[str, object] = {"state": "idle"}      # idle | checking | working | restarting | error
        self._latest: Optional[dict] = None
        self._checked_at: Optional[float] = None

    # --- what the page shows ---------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            out = {"current": self.current, "can_update": self.can_update(), "can_rollback": self.can_rollback(),
                   "previous": self.previous_version(), "checked_at": self._checked_at}
            out.update(self._status)
            if self._latest:
                out.update({"latest": self._latest["version"], "notes": self._latest["notes"], "url": self._latest["url"],
                            "available": newer(self._latest["version"], self.current)})
            return out

    def _set(self, **kw) -> None:
        with self._lock:
            self._status = kw

    def can_update(self) -> Optional[str]:
        """None if this install can update itself, else why not (shown on the page)."""
        if (self.appdir / ".git").exists():
            return "This TV runs from a git checkout; update it with tools/deploy.sh from your computer."
        if not os.access(self.appdir, os.W_OK):
            return "The code folder is not writable by the TV's user."
        return None

    def can_rollback(self) -> bool:
        return (self.appdir / BACKUP / "tubeless" / "__init__.py").exists()

    def previous_version(self) -> Optional[str]:
        init = self.appdir / BACKUP / "tubeless" / "__init__.py"
        try:
            m = re.search(r'__version__ = "(.+?)"', init.read_text())
        except OSError:
            return None
        return m.group(1) if m else None

    # --- check -----------------------------------------------------------------------------------
    def check(self) -> dict:
        """Find the latest release. The version comes from the releases page (no API, so GitHub's anonymous rate limit that a
        whole household shares can't stop it); the API is only asked for the release notes, and its failure is ignored."""
        self._set(state="checking")
        try:
            final = self.latest_url(f"https://github.com/{self.repo}/releases/latest")
            if "/releases/tag/" not in final:
                raise UpdateError("No release has been published yet.")
            tag = final.rstrip("/").rsplit("/", 1)[-1]
            version = tag.lstrip("v")
            parse_version(version)
            name = f"tubeless-{version}.tar.gz"
            base = f"https://github.com/{self.repo}/releases/download/{tag}/"
            if not (self.exists(base + name) and self.exists(base + name + ".sha256")):
                raise UpdateError(f"Release {version} has no installable file attached yet.")
            notes = ""
            try:
                notes = str(json.loads(self.fetch(f"https://api.github.com/repos/{self.repo}/releases/tags/{tag}")).get("body") or "")[:4000]
            except Exception:                                              # rate-limited, offline, odd answer: the notes are optional
                pass
            self._latest = {"version": version, "notes": notes, "url": f"https://github.com/{self.repo}/releases/tag/{tag}",
                            "tar": base + name, "sha": base + name + ".sha256", "name": name}
            self._checked_at = self.clock()
            self._set(state="idle")
        except UpdateError as e:
            self._set(state="error", error=str(e))
        except urllib.error.HTTPError as e:
            self._set(state="error", error="GitHub is limiting requests from this network. Try again in a while." if e.code in (403, 429)
                      else "No release has been published yet." if e.code == 404 else f"GitHub said no ({e.code}).")
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.info("update check failed: %s", e)
            self._set(state="error", error="Couldn't check for updates (is the TV online?).")
        return self.status()

    # --- apply -----------------------------------------------------------------------------------
    def apply(self) -> dict:
        """Install the release `check()` found. Runs in the caller's thread; the page polls `status()`."""
        why = self.can_update()
        if why:
            raise UpdateError(why)
        if not self._latest or not newer(self._latest["version"], self.current):
            raise UpdateError("There is no newer release to install.")
        rel = self._latest
        try:
            self._set(state="working", step="Downloading")
            blob = self.fetch(rel["tar"])
            expected = self.fetch(rel["sha"]).decode("ascii", "replace").split()
            if not expected or hashlib.sha256(blob).hexdigest() != expected[0].lower():
                raise UpdateError("The download doesn't match its checksum, so nothing was changed.")
            with tempfile.TemporaryDirectory(dir=self.appdir) as tmp:
                self._set(state="working", step="Checking")
                new = _safe_extract(blob, Path(tmp))
                self._verify(new, rel["version"])
                self._set(state="working", step="Installing")
                system_changed = self._install(new)
            log.info("updated to %s; restarting", rel["version"])
            self._set(state="restarting", version=rel["version"], system_changed=system_changed)
            self._announce("update", rel["version"])
            self.restart()
        except UpdateError as e:
            self._set(state="error", error=str(e))
        except Exception as e:                                           # a full disk, a flaky network...
            log.exception("update failed")
            self._set(state="error", error=f"The update failed and nothing was changed ({e}).")
        return self.status()

    def _announce(self, kind: str, version: str) -> None:
        """Leave a note for the next start (it confirms the new version is running) and tell the TV's screen, before the restart."""
        if self.marker:
            try:
                self.marker.parent.mkdir(parents=True, exist_ok=True)
                self.marker.write_text(json.dumps({"kind": kind, "from": self.current, "to": version}))
            except OSError:
                pass
        try:
            self.notify(kind, version)
        except Exception:
            log.exception("could not show the update message on the TV")

    def _verify(self, new: Path, version: str) -> None:
        init = new / "tubeless" / "__init__.py"
        if not init.is_file() or not (new / "tubeless" / "__main__.py").is_file():
            raise UpdateError("The release doesn't contain the Tubeless program.")
        m = re.search(r'__version__ = "(.+?)"', init.read_text())
        if not m or m.group(1) != version:
            raise UpdateError("The release's version doesn't match its name, so nothing was changed.")
        check = subprocess.run([self.python, "-c", "import tubeless.app, tubeless.web, tubeless.hardware"], cwd=str(new),
                               capture_output=True, text=True, timeout=120, env=dict(os.environ, PYTHONPATH=str(new)))
        if check.returncode != 0:
            log.error("new release does not import: %s", check.stderr[-500:])
            raise UpdateError("The new version won't start on this TV, so nothing was changed.")

    def _install(self, new: Path) -> bool:
        """Swap the code in, keeping the old one in .previous. Returns True if the system files (packaging/) changed."""
        backup = self.appdir / BACKUP
        system_changed = _dir_digest(new / "packaging") != _dir_digest(self.appdir / "packaging")
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir()
        for sub in CODE_DIRS:
            if (self.appdir / sub).exists():
                shutil.move(str(self.appdir / sub), str(backup / sub))
        for single in CODE_FILES:
            if (self.appdir / single).exists():
                shutil.copy2(self.appdir / single, backup / single)
        try:
            for sub in CODE_DIRS:
                if (new / sub).exists():
                    shutil.copytree(new / sub, self.appdir / sub)
            for single in CODE_FILES:
                if (new / single).exists():
                    shutil.copy2(new / single, self.appdir / single)
        except Exception:
            self._restore()                                              # half-copied: put the working code straight back
            raise
        return system_changed

    def _restore(self) -> None:
        backup = self.appdir / BACKUP
        for sub in CODE_DIRS:
            if (backup / sub).exists():
                shutil.rmtree(self.appdir / sub, ignore_errors=True)
                shutil.move(str(backup / sub), str(self.appdir / sub))
        for single in CODE_FILES:
            if (backup / single).exists():
                shutil.copy2(backup / single, self.appdir / single)

    # --- rollback --------------------------------------------------------------------------------
    def rollback(self) -> dict:
        if not self.can_rollback():
            raise UpdateError("There is no previous version to go back to.")
        version = self.previous_version()
        self._restore()
        shutil.rmtree(self.appdir / BACKUP, ignore_errors=True)
        log.info("rolled back to %s; restarting", version)
        self._set(state="restarting", version=version, rolled_back=True)
        self._announce("rollback", version)
        self.restart()
        return self.status()
