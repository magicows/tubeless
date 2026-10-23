"""
The upload page: a tiny web server that only runs while the TV is in "upload" mode.

* Anyone on the same Wi-Fi can open the page, but must enter the **PIN shown on the TV**, so only someone
  physically at the TV can add or change anything.
* Uploads are chunked and resumable (a Pi Zero's Wi-Fi manages a few MB/s, so a big file takes minutes), written to
  `<file>.part` and renamed when complete, so the player never sees half a file. Free disk space is checked first.
* File and channel names are sanitised and can never leave the videos folder.
* Standard library only, and it streams to disk, so it fits comfortably in a 512 MB Pi.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__, advice
from .config import ASSETS_DIR
from .library import VIDEO_EXTS
from .youtube import YouTubeError

log = logging.getLogger(__name__)

MAX_CHUNK = 16 * 1024 * 1024
MAX_JSON = 65536
MAX_STANDBY = 8 * 1024 * 1024          # a phone photo is fine; the TV shrinks it
RESERVE_BYTES = 300 * 1024 * 1024        # never fill the card completely
MAX_FILENAME = 150
MAX_CHANNEL = 40
LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 30


class UploadError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


# ─────────────────────────────────────────────
#  Names
# ─────────────────────────────────────────────
_BAD = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")


def sanitize_filename(name: str) -> Optional[str]:
    """A safe file name (no path, no control characters, not hidden, not absurdly long), or None."""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = _BAD.sub("_", name).lstrip(". ").rstrip(". ")
    if not name:
        return None
    if len(name) > MAX_FILENAME:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:MAX_FILENAME - len(ext) - 1] + "." + ext) if dot else name[:MAX_FILENAME]
    return name


def sanitize_channel(name: str) -> Optional[str]:
    name = _BAD.sub("_", (name or "").replace("\\", "/").strip()).strip(". ")
    if not name or name in (".", "..") or len(name) > MAX_CHANNEL:
        return None
    return name


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise UploadError(409, "too many files with that name")


# ─────────────────────────────────────────────
#  Uploads
# ─────────────────────────────────────────────
class UploadManager:
    def __init__(self, root, reserve: int = RESERVE_BYTES, disk_free: Optional[Callable[[Path], int]] = None):
        self.root = Path(root)
        self.reserve = reserve
        self._disk_free = disk_free or (lambda p: shutil.disk_usage(str(p)).free)
        self._lock = threading.Lock()
        self.active: Dict[Tuple[str, str], dict] = {}
        self.completed: List[dict] = []

    # -- channels
    def _channel_dir(self, channel: str, create: bool) -> Path:
        clean = sanitize_channel(channel)
        if clean is None:
            raise UploadError(400, "That channel name isn't allowed.")
        path = self.root / clean
        if path.resolve().parent != self.root.resolve():
            raise UploadError(400, "That channel name isn't allowed.")
        if not path.is_dir():
            if not create:
                raise UploadError(404, "No such channel.")
            path.mkdir(parents=True, exist_ok=True)
        return path

    def create_channel(self, name: str) -> str:
        path = self._channel_dir(name, create=True)
        return path.name

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return max(0, self._disk_free(self.root) - self.reserve)

    # -- files
    @staticmethod
    def _check_name(name: str) -> str:
        clean = sanitize_filename(name)
        if clean is None:
            raise UploadError(400, "That file name isn't allowed.")
        if not clean.lower().endswith(VIDEO_EXTS):
            raise UploadError(415, "Only video files are accepted (" + ", ".join(VIDEO_EXTS) + ").")
        return clean

    def received(self, channel: str, name: str) -> int:
        clean = self._check_name(name)
        part = self._channel_dir(channel, create=False) / (clean + ".part")
        return part.stat().st_size if part.exists() else 0

    def receive(self, channel: str, name: str, offset: int, total: int, stream, length: int) -> dict:
        """Append one chunk. Returns {"received", "done", "file"?}. Raises UploadError."""
        clean = self._check_name(name)
        if total <= 0 or length <= 0 or length > MAX_CHUNK or offset < 0 or offset + length > total:
            raise UploadError(400, "Bad chunk size or position.")
        chdir = self._channel_dir(channel, create=(offset == 0))
        part = chdir / (clean + ".part")
        have = part.stat().st_size if part.exists() else 0
        if offset != have:
            raise UploadError(409, "Chunk is not where the upload left off.", received=have)
        if offset == 0 and total > self.free_bytes():
            raise UploadError(507, f"Not enough free space: this file needs {total // 2**20} MB and only "
                                   f"{self.free_bytes() // 2**20} MB is available.")
        key = (chdir.name, clean)
        with self._lock:
            self.active[key] = {"channel": chdir.name, "name": clean, "received": have, "total": total, "ts": time.time()}
        remaining = length
        with open(part, "ab") as f:
            while remaining > 0:
                block = stream.read(min(65536, remaining))
                if not block:
                    break
                f.write(block)
                remaining -= len(block)
                with self._lock:
                    self.active[key]["received"] += len(block)
        if remaining > 0:                                           # the client went away mid-chunk; it can resume
            raise UploadError(400, "The connection dropped; resume to continue.", received=part.stat().st_size)
        got = have + length
        if got < total:
            return {"received": got, "done": False}
        final = unique_path(chdir / clean)
        os.replace(part, final)
        with self._lock:
            self.active.pop(key, None)
            self.completed.append({"channel": chdir.name, "file": final.name, "size": total, "ts": time.time()})
        return {"received": got, "done": True, "file": final.name, "path": str(final), "channel": chdir.name}

    # -- what the TV shows
    def snapshot(self) -> dict:
        with self._lock:
            return {"active": [dict(v) for v in self.active.values()], "completed": list(self.completed)}


# ─────────────────────────────────────────────
#  PIN login
# ─────────────────────────────────────────────
class Session:
    def __init__(self, pin: Optional[str] = None):
        self.pin = pin or f"{secrets.randbelow(10000):04d}"
        self._tokens: set = set()
        self._fails = 0
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def login(self, pin: str) -> Tuple[Optional[str], int]:
        """(token, 0) on success; (None, seconds_to_wait) when locked out; (None, 0) for a wrong PIN."""
        with self._lock:
            now = time.time()
            if now < self._locked_until:
                return None, int(self._locked_until - now) + 1
            if secrets.compare_digest(str(pin).strip(), self.pin):
                self._fails = 0
                token = secrets.token_urlsafe(24)
                self._tokens.add(token)
                return token, 0
            self._fails += 1
            if self._fails >= LOGIN_ATTEMPTS:
                self._fails = 0
                self._locked_until = now + LOCKOUT_SECONDS
                return None, LOCKOUT_SECONDS
            return None, 0

    def valid(self, token: str) -> bool:
        with self._lock:
            return any(secrets.compare_digest(token or "", t) for t in self._tokens)


def local_ip() -> str:
    """The address other devices on the network can reach this machine at."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))                        # no packet is sent; this just picks the interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ─────────────────────────────────────────────
#  The server
# ─────────────────────────────────────────────
class WebServer:
    """
    hooks (provided by the app):
        state() -> dict                       name, channels, ...
        set_name(name) -> str                 raises ValueError("why not")
        timezone_location() -> dict | None    {'zone', 'location': 'lat, lon'} for the TV's time zone's main city
        theme_css() / theme_style()           the chosen theme, for the page (the colours as CSS; 'soft' or 'brutal')
        add_palette(name, text, light, brutal) -> dict      a pasted palette, saved and chosen; delete_palette(id); raise ValueError("why not")
        set_greeting(text) -> str             the boot screen's typed line; blank = the placeholder; raises ValueError("why not")
        uploaded(info: dict) -> None          after a file completes (info includes advice)
        standby_path() -> str                 the picture shown while the player restarts
        set_standby(data: bytes) -> None      raises ValueError("why not")
        reset_standby() -> None               back to the picture that ships with Tubeless
        set_setting(key, value) -> None       raises ValueError("why not")
        set_channel_order(channel, order)     'shuffle' | 'order' | 'default'; raises ValueError("why not")
        statics                               a statics.StaticManager (the TV static clip and its files; raises ValueError("why not"))
        status() -> dict                      uptime, temperature, memory ...
        logs(lines: int) -> str               the end of the app's log
        rescan(force: bool) -> dict           read video durations in the background; returns the scan status
        update_status() / update_check() / update_apply() / update_rollback() -> dict     (ValueError("why not"))
    """

    def __init__(self, manager: UploadManager, session: Session, hooks, host: str = "0.0.0.0", port: int = 8080,
                 youtube=None):
        self.manager, self.session, self.hooks = manager, session, hooks
        self.youtube = youtube                                      # a youtube.YouTubeManager, or None
        self.host, self.port = host, port
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self._page: Optional[bytes] = None

    def page(self) -> bytes:
        if self._page is None:
            self._page = (ASSETS_DIR / "upload.html").read_bytes()
        css = getattr(self.hooks, "theme_css", lambda: "")()                  # the chosen theme's colours, so there is no flash of the wrong ones
        style = getattr(self.hooks, "theme_style", lambda: "soft")()
        return self._page.replace(b'<style id="themecss"></style>', ('<style id="themecss">%s</style>' % css).encode()) \
                         .replace(b'data-style="soft"', ('data-style="%s"' % style).encode(), 1)

    def start(self) -> int:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 120
            server_version = "Tubeless/" + __version__

            def log_message(self, fmt, *args):
                log.debug("web: " + fmt, *args)

            # -- plumbing
            def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: Optional[dict] = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, str(v))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any, extra: Optional[dict] = None):
                self._send(code, json.dumps(obj).encode(), extra=extra)

            def _fail(self, status: int, message: str, **extra):
                self.close_connection = True                       # we may not have read the request body
                self._json(status, dict(error=message, **extra))

            def _authed(self) -> bool:
                if server.session.valid(self.headers.get("X-Token", "")):
                    return True
                self._fail(401, "Enter the PIN shown on the TV.")
                return False

            def _body_json(self) -> Optional[dict]:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_JSON:
                    self._fail(413, "Request too large.")
                    return None
                try:
                    return json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._fail(400, "Bad request.")
                    return None

            def _yt(self) -> bool:
                if server.youtube is None:
                    self._fail(501, "YouTube channels aren't available.")
                    return False
                return True

            def _yt_call(self, fn, status: int = 200):
                try:
                    self._json(status, fn())
                except YouTubeError as e:
                    self._fail(e.status if 400 <= e.status < 600 else 400, e.message)
                except Exception:
                    log.exception("YouTube request failed")
                    self._fail(500, "The TV had a problem with that.")

            def _query(self) -> Dict[str, str]:
                return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

            def _route(self) -> str:
                return urlparse(self.path).path

            # -- GET
            def do_GET(self):
                route = self._route()
                if route in ("/", "/index.html"):
                    self._send(200, server.page(), "text/html; charset=utf-8")
                elif route == "/api/state":
                    if self._authed():
                        state = server.hooks.state()
                        state["free_bytes"] = server.manager.free_bytes()
                        state["version"] = __version__
                        yt = server.youtube
                        state["youtube"] = yt.status() if yt else {"available": False}
                        sums = yt.summaries() if yt else {}
                        for ch in state.get("channels", []):
                            ch["kind"] = "youtube" if ch["name"] in sums else "folder"
                            if ch["name"] in sums:
                                ch["youtube"] = sums[ch["name"]]
                        self._json(200, state)
                elif route == "/api/static/home":
                    if self._authed():
                        try:
                            self._json(200, {"files": server.hooks.statics.home_files(self._query().get("kind", "clip"))})
                        except ValueError as e:
                            self._fail(400, str(e))
                elif route == "/api/static/file":
                    if self._authed():
                        path = server.hooks.statics.current_file()
                        try:
                            ctype = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                                     ".gif": "image/gif"}.get(path.suffix.lower(), "application/octet-stream")
                            self._send(200, path.read_bytes(), ctype)
                        except OSError:
                            self._fail(404, "No clip.")
                elif route == "/api/timezone-location":
                    if self._authed():
                        found = server.hooks.timezone_location()
                        if found is None:
                            self._fail(404, "The TV can't tell where its time zone is. Type the coordinates instead.")
                        else:
                            self._json(200, found)
                elif route == "/api/status":
                    if self._authed():
                        self._json(200, server.hooks.status())
                elif route == "/api/logs":
                    if self._authed():
                        try:
                            n = int(self._query().get("lines", "200"))
                        except ValueError:
                            n = 200
                        self._send(200, server.hooks.logs(n).encode(), "text/plain; charset=utf-8")
                elif route == "/api/update":
                    if self._authed():
                        self._json(200, server.hooks.update_status())
                elif route == "/api/standby":
                    if self._authed():
                        try:
                            self._send(200, Path(server.hooks.standby_path()).read_bytes(), "image/jpeg")
                        except OSError:
                            self._fail(404, "No picture.")
                elif route == "/api/yt/ytdlp":
                    if self._authed() and self._yt():
                        self._json(200, server.youtube.ytdlp_info())
                elif route == "/api/yt/progress":
                    if self._authed() and self._yt():
                        self._json(200, server.youtube.progress())
                elif route == "/api/yt/channel":
                    if self._authed() and self._yt():
                        self._yt_call(lambda: server.youtube.info(self._query().get("channel", "")))
                elif route == "/api/upload/status":
                    if self._authed():
                        q = self._query()
                        try:
                            self._json(200, {"received": server.manager.received(q.get("channel", ""), q.get("name", ""))})
                        except UploadError as e:
                            self._fail(e.status, e.message)
                else:
                    self._fail(404, "Not found.")

            # -- POST
            def do_POST(self):
                route = self._route()
                if route == "/api/login":
                    data = self._body_json()
                    if data is None:
                        return
                    token, wait = server.session.login(str(data.get("pin", "")))
                    if token:
                        self._json(200, {"token": token})
                    elif wait:
                        self._fail(429, f"Too many wrong PINs. Try again in {wait} seconds.", retry_after=wait)
                    else:
                        self._fail(403, "That isn't the PIN shown on the TV.")
                elif route == "/api/name":
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    try:
                        self._json(200, {"name": server.hooks.set_name(str(data.get("name", "")))})
                    except ValueError as e:
                        self._fail(400, str(e))
                elif route == "/api/greeting":
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    try:
                        self._json(200, {"greeting": server.hooks.set_greeting(str(data.get("greeting", "")))})
                    except ValueError as e:
                        self._fail(400, str(e))
                elif route in ("/api/palette", "/api/palette/delete"):
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    try:
                        if route == "/api/palette":
                            entry = server.hooks.add_palette(str(data.get("name", "")), str(data.get("colours", "")),
                                                              data.get("light") is True, data.get("brutal") is True)
                            self._json(200, {"palette": entry, "css": server.hooks.theme_css(), "style": server.hooks.theme_style()})
                        else:
                            server.hooks.delete_palette(str(data.get("id", "")))
                            self._json(200, {"css": server.hooks.theme_css(), "style": server.hooks.theme_style()})
                    except ValueError as e:
                        self._fail(400, str(e))
                elif route == "/api/settings":
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    try:
                        for key, value in data.items():
                            server.hooks.set_setting(str(key), value)
                    except ValueError as e:
                        return self._fail(400, str(e))
                    self._json(200, {"ok": True})
                elif route in ("/api/static/preset", "/api/static/use", "/api/static/delete", "/api/static/lengths"):
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    st = server.hooks.statics
                    try:
                        if route.endswith("/preset"):
                            st.choose_preset(str(data.get("name", "")))
                            self._json(200, st.state())
                        elif route.endswith("/lengths"):
                            st.set_lengths(data.get("between"), data.get("change"))
                            self._json(200, st.state())
                        elif route.endswith("/use"):
                            self._json(200, st.use(str(data.get("kind", "")), str(data.get("ref", "")), str(data.get("sound", "own")),
                                                   str(data.get("sound_name", ""))))
                        else:
                            st.delete(str(data.get("kind", "")), str(data.get("name", "")))
                            self._json(200, st.state())
                    except ValueError as e:
                        self._fail(400, str(e))
                elif route == "/api/channel/order":
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    try:
                        server.hooks.set_channel_order(str(data.get("channel", "")), str(data.get("order", "")))
                    except ValueError as e:
                        return self._fail(400, str(e))
                    self._json(200, {"ok": True})
                elif route == "/api/library/rescan":
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    self._json(200, server.hooks.rescan(bool(data.get("force"))))
                elif route in ("/api/update/check", "/api/update/apply", "/api/update/rollback"):
                    if not self._authed() or self._body_json() is None:        # the body is read so the connection stays in step
                        return
                    call = {"check": server.hooks.update_check, "apply": server.hooks.update_apply,
                            "rollback": server.hooks.update_rollback}[route.rsplit("/", 1)[1]]
                    try:
                        self._json(200, call())
                    except ValueError as e:
                        self._fail(400, str(e))
                elif route == "/api/standby/reset":
                    if self._authed() and self._body_json() is not None:
                        server.hooks.reset_standby()
                        self._json(200, {"ok": True})
                elif route == "/api/channel":
                    if not self._authed():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    if data.get("kind") == "youtube":
                        if self._yt():
                            self._yt_call(lambda: {"channel": server.youtube.create(str(data.get("name", "")))})
                        return
                    try:
                        self._json(200, {"channel": server.manager.create_channel(str(data.get("name", "")))})
                    except UploadError as e:
                        self._fail(e.status, e.message)
                elif route.startswith("/api/yt/") or route == "/api/channel/delete":
                    if not self._authed() or not self._yt():
                        return
                    data = self._body_json()
                    if data is None:
                        return
                    yt, ch = server.youtube, str(data.get("channel", data.get("name", "")))
                    if route == "/api/yt/resolve":
                        def resolve():
                            lst = yt.resolve(str(data.get("url", "")))
                            return {"title": lst.title, "kind": lst.kind, "skipped": lst.skipped,
                                    "entries": [{"id": e.id, "title": e.title, "duration": e.duration} for e in lst.entries]}
                        self._yt_call(resolve)
                    elif route == "/api/yt/add":
                        sel = data.get("selected")
                        self._yt_call(lambda: yt.add(ch, str(data.get("url", "")), sel if isinstance(sel, list) else None))
                    elif route == "/api/yt/select":
                        sel = data.get("selected")
                        if not isinstance(sel, list):
                            return self._fail(400, "Bad request.")
                        self._yt_call(lambda: yt.set_selection(ch, sel))
                    elif route == "/api/yt/mode":
                        self._yt_call(lambda: (yt.set_mode(ch, str(data.get("mode", ""))), {"mode": data.get("mode")})[1])
                    elif route == "/api/yt/sync":
                        self._yt_call(lambda: {"queued": yt.sync(ch or None)})
                    elif route == "/api/yt/refresh":
                        self._yt_call(lambda: yt.start_refresh(ch or None))
                    elif route == "/api/yt/seen":
                        self._yt_call(lambda: {"cleared": yt.mark_seen(ch)})
                    elif route == "/api/yt/ytdlp/check":
                        self._yt_call(yt.check_ytdlp)
                    elif route == "/api/yt/ytdlp/update":
                        self._yt_call(yt.update_ytdlp)
                    elif route == "/api/yt/cancel":
                        self._yt_call(lambda: (yt.cancel(), {"ok": True})[1])
                    elif route == "/api/channel/delete":
                        self._yt_call(lambda: (yt.delete(ch), {"deleted": ch})[1])
                    else:
                        self._fail(404, "Not found.")
                else:
                    self._fail(404, "Not found.")

            # -- PUT (one chunk of an upload)
            def do_PUT(self):
                if self._route() == "/api/static/upload":
                    if not self._authed():
                        return
                    q = self._query()
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        name = server.hooks.statics.save_upload(q.get("kind", "clip"), q.get("name", ""), self.rfile, length)
                    except ValueError as e:
                        return self._fail(400, str(e))
                    return self._json(200, {"name": name, "static": server.hooks.statics.state()})
                if self._route() == "/api/standby":
                    if not self._authed():
                        return
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_STANDBY:
                        return self._fail(413, "That picture is too big (8 MB at most).")
                    try:
                        server.hooks.set_standby(self.rfile.read(length))
                    except ValueError as e:
                        return self._fail(400, str(e))
                    return self._json(200, {"ok": True})
                if self._route() != "/api/upload":
                    return self._fail(404, "Not found.")
                if not self._authed():
                    return
                q = self._query()
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    offset, total = int(q.get("offset", "0")), int(q.get("total", "0"))
                    result = server.manager.receive(q.get("channel", ""), q.get("name", ""), offset, total, self.rfile, length)
                except ValueError:
                    return self._fail(400, "Bad request.")
                except UploadError as e:
                    return self._fail(e.status, e.message, **e.extra)
                except Exception:
                    log.exception("upload failed")
                    return self._fail(500, "The TV had a problem saving that.")
                if result.get("done"):
                    path = result.pop("path")
                    adv = [a.as_dict() for a in advice.advise(advice.probe(path))]
                    result["advice"] = adv
                    try:
                        server.hooks.uploaded({"channel": result["channel"], "file": result["file"], "path": path, "advice": adv})
                    except Exception:
                        log.exception("upload hook failed")
                self._json(200, result)

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        log.info("upload page on port %d", self.port)
        return self.port

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
