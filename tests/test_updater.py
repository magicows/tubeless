import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from tubeless.updater import UpdateError, Updater, newer, parse_version

PKG_FILES = {"__init__.py": '__version__ = "%s"\n', "__main__.py": "", "app.py": "", "web.py": "", "hardware.py": ""}


def write_tree(root: Path, version: str, packaging="unit v1", broken=False):
    pkg = root / "tubeless"
    pkg.mkdir(parents=True, exist_ok=True)
    for name, body in PKG_FILES.items():
        (pkg / name).write_text(body % version if "%s" in body else body)
    if broken:
        (pkg / "app.py").write_text("this is not python (\n")
    (root / "packaging").mkdir(exist_ok=True)
    (root / "packaging" / "tubeless.service.in").write_text(packaging)
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools" / "deploy.sh").write_text("#!/bin/sh\n")
    (root / "README.md").write_text("readme " + version)
    (root / "LICENSE").write_text("MIT")


def make_tarball(tmp_path, version, extra=None, **kw):
    src = tmp_path / ("src-" + version)
    write_tree(src, version, **kw)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(src, arcname=f"tubeless-{version}")
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Release:
    """A fake GitHub: the 'latest release' JSON and the files it points at."""

    def __init__(self, tmp_path, version="0.2.0", **kw):
        self.blob = make_tarball(tmp_path, version, **kw)
        self.sha = hashlib.sha256(self.blob).hexdigest()
        name = f"tubeless-{version}.tar.gz"
        self.files = {"https://dl/" + name: self.blob, "https://dl/" + name + ".sha256": f"{self.sha}  {name}\n".encode()}
        self.api = {"tag_name": "v" + version, "body": "What's new", "html_url": "https://github.com/x/y/releases/v" + version,
                    "assets": [{"name": name, "browser_download_url": "https://dl/" + name},
                               {"name": name + ".sha256", "browser_download_url": "https://dl/" + name + ".sha256"}]}

    def fetch(self, url, limit=None):
        if url.endswith("/releases/latest"):
            return json.dumps(self.api).encode()
        if url not in self.files:
            raise OSError("404 " + url)
        return self.files[url]


@pytest.fixture
def app(tmp_path):
    appdir = tmp_path / "home" / "tubeless"
    write_tree(appdir, "0.1.0")
    return appdir


def make(app, rel, restarts=None):
    return Updater(app, current="0.1.0", fetch=rel.fetch, restart=lambda: (restarts if restarts is not None else []).append(1),
                   python=sys.executable)


def test_version_comparison():
    assert parse_version("v0.10.1") == (0, 10, 1)
    assert newer("0.2.0", "0.1.0") and newer("v0.10.0", "0.9.9") and newer("1.0", "0.9.9")
    assert not newer("0.1.0", "0.1.0") and not newer("0.1.0", "0.2.0") and not newer("garbage", "0.1.0")
    with pytest.raises(ValueError):
        parse_version("v1.x")


def test_check_says_whether_a_newer_release_exists(app, tmp_path):
    st = make(app, Release(tmp_path, "0.2.0")).check()
    assert st["available"] and st["latest"] == "0.2.0" and st["notes"] == "What's new" and st["state"] == "idle"
    st = make(app, Release(tmp_path, "0.1.0")).check()
    assert not st["available"]


def test_check_reports_a_release_with_no_files_and_a_dead_network(app, tmp_path):
    rel = Release(tmp_path, "0.2.0")
    rel.api["assets"] = []
    assert "no installable file" in make(app, rel).check()["error"]
    def down(url, limit=None):
        raise OSError("offline")
    st = Updater(app, current="0.1.0", fetch=down).check()
    assert st["state"] == "error" and "online" in st["error"]


def test_apply_installs_the_release_keeps_the_old_code_and_restarts(app, tmp_path):
    rel, restarts = Release(tmp_path, "0.2.0", packaging="unit v2"), []
    u = make(app, rel, restarts)
    u.check()
    st = u.apply()
    assert st["state"] == "restarting" and st["system_changed"] is True and restarts == [1]
    assert '"0.2.0"' in (app / "tubeless" / "__init__.py").read_text()
    assert (app / "README.md").read_text() == "readme 0.2.0"
    assert u.previous_version() == "0.1.0" and u.can_rollback()
    assert not list(app.glob("tmp*"))                                           # no scratch folder left behind


def test_an_update_that_changes_no_system_files_says_so(app, tmp_path):
    u = make(app, Release(tmp_path, "0.2.0", packaging="unit v1"))
    u.check()
    assert u.apply()["system_changed"] is False


def test_rollback_puts_the_previous_code_back(app, tmp_path):
    restarts = []
    u = make(app, Release(tmp_path, "0.2.0"), restarts)
    u.check(); u.apply()
    st = u.rollback()
    assert st["state"] == "restarting" and len(restarts) == 2
    assert '"0.1.0"' in (app / "tubeless" / "__init__.py").read_text()
    assert not u.can_rollback() and not (app / ".previous").exists()
    with pytest.raises(UpdateError):
        u.rollback()


def test_a_download_that_does_not_match_its_checksum_changes_nothing(app, tmp_path):
    rel, restarts = Release(tmp_path, "0.2.0"), []
    rel.files["https://dl/tubeless-0.2.0.tar.gz.sha256"] = b"0" * 64 + b"  tubeless-0.2.0.tar.gz\n"
    u = make(app, rel, restarts)
    u.check()
    st = u.apply()
    assert st["state"] == "error" and "checksum" in st["error"] and restarts == []
    assert '"0.1.0"' in (app / "tubeless" / "__init__.py").read_text() and not u.can_rollback()


def test_a_release_that_will_not_import_is_refused(app, tmp_path):
    restarts = []
    u = make(app, Release(tmp_path, "0.2.0", broken=True), restarts)
    u.check()
    st = u.apply()
    assert st["state"] == "error" and "won't start" in st["error"] and restarts == []
    assert '"0.1.0"' in (app / "tubeless" / "__init__.py").read_text()


def test_a_tarball_that_escapes_its_folder_is_refused(app, tmp_path):
    rel = Release(tmp_path, "0.2.0", extra={"../evil.txt": b"x"})
    rel.sha = hashlib.sha256(rel.blob).hexdigest()
    rel.files["https://dl/tubeless-0.2.0.tar.gz"] = rel.blob
    rel.files["https://dl/tubeless-0.2.0.tar.gz.sha256"] = f"{rel.sha}  x\n".encode()
    u = make(app, rel)
    u.check()
    st = u.apply()
    assert st["state"] == "error" and "not safe" in st["error"]
    assert not (tmp_path / "home" / "evil.txt").exists()


def test_a_release_whose_version_does_not_match_its_name_is_refused(app, tmp_path):
    rel = Release(tmp_path, "0.2.0")
    rel.api["tag_name"] = "v0.3.0"
    name = "tubeless-0.3.0.tar.gz"
    rel.api["assets"] = [{"name": name, "browser_download_url": "https://dl/" + name},
                         {"name": name + ".sha256", "browser_download_url": "https://dl/" + name + ".sha256"}]
    rel.files["https://dl/" + name] = rel.blob
    rel.files["https://dl/" + name + ".sha256"] = f"{rel.sha}  {name}\n".encode()
    u = make(app, rel)
    u.check()
    assert "version doesn't match" in u.apply()["error"]


def test_a_git_checkout_does_not_update_itself(app, tmp_path):
    (app / ".git").mkdir()
    u = make(app, Release(tmp_path, "0.2.0"))
    u.check()
    assert "git checkout" in u.status()["can_update"]
    with pytest.raises(UpdateError):
        u.apply()


def test_nothing_to_install_when_up_to_date(app, tmp_path):
    u = make(app, Release(tmp_path, "0.1.0"))
    u.check()
    with pytest.raises(UpdateError):
        u.apply()


def test_no_release_published_yet_is_said_plainly(app):
    import urllib.error
    def none_yet(url, limit=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    assert "No release has been published" in Updater(app, current="0.1.0", fetch=none_yet).check()["error"]
