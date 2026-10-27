"""
The install-time page: while the installer runs (30 to 45 minutes on a Zero 2 W, nearly all of it installing packages) it serves a
small web page so you can watch what it is doing and set the TV up in the meantime.

  * live progress: the steps, the current one, and the end of the installer's output;
  * the TV's name, whether to add YouTube support, and a standby ("technical difficulties") picture: each can be changed until the
    installer reaches the step that uses it, and the page says when one has been used.

Standard library only, because it starts before anything has been installed. It needs the PIN printed in the terminal, like the
TV's upload page. The installer owns the work; this only reads its state and sets a few of its fields.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional
from collections import deque
from urllib.parse import urlparse

from .config import ASSETS_DIR, MAX_NAME_LENGTH, expand

log = logging.getLogger(__name__)

PORTS = tuple(range(8080, 8090))
MAX_PICTURE = 8 * 1024 * 1024
LOG_LINES = 60
LINGER = 20.0                       # seconds the page stays up after the install finishes, so the last state is seen


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def picture_kind(data: bytes) -> str:
    """'jpeg', 'png' or '' from the file's first bytes (no image library yet at this point)."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return ""


class Progress:
    """What the installer is doing, written by the installer's thread and read by the page's."""

    def __init__(self, titles: List[str]):
        self.titles = list(titles)
        self.state = ["waiting"] * len(titles)
        self.log: Deque[str] = deque(maxlen=LOG_LINES)
        self.locked: Dict[str, str] = {}               # field -> why it can no longer be changed
        self.finished: Optional[dict] = None           # {"ok": bool, "message": str}
        self.started = time.time()
        self._lock = threading.Lock()

    def begin(self, index: int) -> None:
        with self._lock:
            self.state[index] = "running"

    def end(self, index: int, ok: bool = True) -> None:
        with self._lock:
            self.state[index] = "done" if ok else "failed"

    def say(self, line: str) -> None:
        with self._lock:
            for part in str(line).splitlines():
                if part.strip():
                    self.log.append(part.rstrip())

    def lock(self, field: str, why: str) -> None:
        with self._lock:
            self.locked[field] = why

    def finish(self, ok: bool, message: str) -> None:
        with self._lock:
            self.finished = {"ok": ok, "message": message}

    def snapshot(self) -> dict:
        with self._lock:
            return {"steps": [{"title": t, "state": s} for t, s in zip(self.titles, self.state)], "log": list(self.log),
                    "locked": dict(self.locked), "finished": self.finished, "seconds": int(time.time() - self.started)}


class InstallPage:
    """
    ctx: the installer's Context (this reads and sets ctx.name and ctx.with_youtube, and writes the standby picture into its home).
    chown: called on paths this creates, so the TV's user owns them (the installer runs as root).
    """

    def __init__(self, ctx, progress: Progress, pin: Optional[str] = None, chown: Optional[Callable[[Path], None]] = None,
                 host: str = "0.0.0.0", ports=PORTS):
        self.ctx, self.progress = ctx, progress
        self.pin = pin or "%04d" % secrets.randbelow(10000)
        self.chown = chown or (lambda p: None)
        self.host, self.ports = host, tuple(ports)
        self.tokens: set = set()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.port = 0
        self._wrong = 0
        self._lock = threading.Lock()

    # --- what the page may change ---------------------------------------------------------------------------------
    def state(self) -> dict:
        snap = self.progress.snapshot()
        picture = self.picture_path()
        snap.update({"name": self.ctx.name, "youtube": bool(self.ctx.with_youtube), "picture": picture.is_file(),
                     "max_name": MAX_NAME_LENGTH})
        return snap

    def picture_path(self) -> Path:
        return self.ctx.home / ".local" / "share" / "tubeless" / "standby.jpg"

    def set_field(self, data: dict) -> None:
        """Raises ValueError with a message fit to show."""
        with self._lock:
            locked = self.progress.locked
            if "name" in data:
                if "name" in locked:
                    raise ValueError(locked["name"])
                name = str(data["name"]).strip()
                if not name:
                    raise ValueError("The name can't be empty.")
                if len(name) > MAX_NAME_LENGTH:
                    raise ValueError(f"That name is too long ({len(name)} characters; the menu title fits {MAX_NAME_LENGTH}).")
                self.ctx.name = name
                self.progress.say(f"[page] TV name set to {name!r}")
            if "youtube" in data:
                if "youtube" in locked:
                    raise ValueError(locked["youtube"])
                if not isinstance(data["youtube"], bool):
                    raise ValueError("That is yes or no.")
                self.ctx.with_youtube = data["youtube"]
                self.progress.say("[page] YouTube support " + ("will be installed" if data["youtube"] else "will not be installed"))

    def save_picture(self, data: bytes) -> None:
        if not data:
            raise ValueError("That file is empty.")
        if len(data) > MAX_PICTURE:
            raise ValueError("That picture is over 8 MB: please use a smaller one.")
        if not picture_kind(data):
            raise ValueError("That doesn't look like a JPEG or PNG picture.")
        dest = self.picture_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        for p in (dest.parent.parent, dest.parent, dest):
            self.chown(p)
        self.progress.say("[page] standby picture saved")

    # --- the server ---------------------------------------------------------------------------------------------------
    def start(self) -> int:
        page = self
        html = (ASSETS_DIR / "install.html").read_bytes()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 30

            def log_message(self, fmt, *args):
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode())

            def _fail(self, code: int, message: str) -> None:
                self._json(code, {"error": message})

            def _authed(self) -> bool:
                if self.headers.get("X-Token", "") in page.tokens:
                    return True
                self._fail(401, "Enter the PIN shown in the terminal.")
                return False

            def _body(self, limit: int = 64 * 1024) -> Optional[bytes]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if length < 0 or length > limit:
                    self._fail(413, "That is too big.")
                    return None
                return self.rfile.read(length)

            def do_GET(self):
                route = urlparse(self.path).path
                if route in ("/", "/index.html"):
                    self._send(200, html, "text/html; charset=utf-8")
                elif route == "/api/state":
                    if self._authed():
                        self._json(200, page.state())
                else:
                    self._fail(404, "Not found.")

            def do_POST(self):
                route = urlparse(self.path).path
                body = self._body(MAX_PICTURE + 1024 if route == "/api/picture" else 64 * 1024)
                if body is None:
                    return
                if route == "/api/login":
                    try:
                        pin = str(json.loads(body or b"{}").get("pin", ""))
                    except ValueError:
                        return self._fail(400, "That didn't make sense.")
                    if page._wrong >= 5:
                        time.sleep(2.0)                                     # slow down guessing the four digits
                    if secrets.compare_digest(pin, page.pin):
                        token = secrets.token_hex(16)
                        page.tokens.add(token)
                        return self._json(200, {"token": token})
                    page._wrong += 1
                    return self._fail(403, "That isn't the PIN shown in the terminal.")
                if not self._authed():
                    return
                try:
                    if route == "/api/settings":
                        page.set_field(json.loads(body or b"{}"))
                    elif route == "/api/picture":
                        page.save_picture(body)
                    else:
                        return self._fail(404, "Not found.")
                except ValueError as e:
                    return self._fail(400, str(e))
                self._json(200, {"ok": True})

        for port in self.ports:
            try:
                self.httpd = ThreadingHTTPServer((self.host, port), Handler)
                self.port = self.httpd.server_address[1]
                break
            except OSError:
                continue
        if self.httpd is None:
            raise OSError("no free port for the install page")
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
