"""
The Wi-Fi setup page: a tiny web server that only runs while the TV is in Wi-Fi setup mode.

The TV's own hotspot is the gate: you can only reach this if you joined "Tubeless-XXXX" with the password shown on the
TV, so there is no PIN. Any address you type (or that your phone probes to see if it has internet) answers with a
redirect to the page, which is what makes phones pop the setup page up by themselves (a "captive portal").
"""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import ASSETS_DIR
from .wifi import HOTSPOT_IP

log = logging.getLogger(__name__)

MAX_JSON = 2048
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def check_credentials(ssid: Any, password: Any) -> Optional[str]:
    """Why these can't be a Wi-Fi network name and password, or None if they could."""
    if not isinstance(ssid, str) or not isinstance(password, str):
        return "Bad request."
    if not ssid.strip() or len(ssid.encode("utf-8")) > 32 or _CONTROL.search(ssid):
        return "That isn't a valid network name."
    if password and not (8 <= len(password) <= 63 and all(32 <= ord(c) < 127 for c in password)) \
            and not re.fullmatch(r"[0-9a-fA-F]{64}", password):
        return "Wi-Fi passwords are 8 to 63 characters (letters, numbers and symbols)."
    return None


class SetupServer:
    """
    hooks (provided by the app):
        name() -> str
        networks(rescan: bool) -> list of wifi.Network
        connect(ssid, password) -> None        returns at once; the TV does the joining in the background
    """

    def __init__(self, hooks, host: str = "0.0.0.0", port: int = 80):
        self.hooks, self.host, self.port = hooks, host, port
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self._page: Optional[bytes] = None

    def page(self) -> bytes:
        if self._page is None:
            self._page = (ASSETS_DIR / "setup.html").read_bytes()
        css = getattr(self.hooks, "theme_css", lambda: "")()              # the chosen theme (see themes.py)
        style = getattr(self.hooks, "theme_style", lambda: "soft")()
        return self._page.replace(b'<style id="themecss"></style>', ('<style id="themecss">%s</style>' % css).encode()) \
                         .replace(b'data-style="soft"', ('data-style="%s"' % style).encode(), 1)

    def start(self) -> int:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 30
            server_version = "Tubeless/" + __version__

            def log_message(self, fmt, *args):
                log.debug("setup web: " + fmt, *args)

            def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: Optional[dict] = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, str(v))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any):
                self._send(code, json.dumps(obj).encode())

            def _fail(self, status: int, message: str):
                self.close_connection = True
                self._json(status, {"error": message})

            def _redirect(self):
                self._send(302, b"", "text/plain", {"Location": f"http://{HOTSPOT_IP}/"})

            def do_GET(self):
                url = urlparse(self.path)
                if url.path in ("/", "/index.html"):
                    self._send(200, server.page(), "text/html; charset=utf-8")
                elif url.path == "/api/wifi/networks":
                    rescan = parse_qs(url.query).get("rescan", ["0"])[0] == "1"
                    try:
                        nets = server.hooks.networks(rescan)
                    except Exception:
                        log.exception("wifi scan failed")
                        return self._fail(500, "The TV couldn't search for networks.")
                    self._json(200, {"name": server.hooks.name(), "networks": [n.as_dict() for n in nets]})
                elif url.path.startswith("/api/"):
                    self._fail(404, "Not found.")
                else:
                    self._redirect()                       # phones' internet probes, and anything typed by hand

            def do_POST(self):
                if urlparse(self.path).path != "/api/wifi/connect":
                    return self._fail(404, "Not found.")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_JSON:
                    return self._fail(413, "Request too large.")
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._fail(400, "Bad request.")
                ssid, password = data.get("ssid"), data.get("password", "")
                problem = check_credentials(ssid, password)
                if problem:
                    return self._fail(400, problem)
                try:
                    server.hooks.connect(ssid, password)
                except RuntimeError as e:
                    return self._fail(409, str(e))
                self._json(202, {"ok": True})

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        log.info("wifi setup page on port %d", self.port)
        return self.port

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
