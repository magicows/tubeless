"""A pretend Jellyfin server (the bits Tubeless uses), for tests. Like Jellyfin 12 it only takes the Authorization header for the API,
and (like it always has for streams) the token as api_key in the address."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SERIES, EP1, EP2, MOVIE = "a" * 32, "b" * 32, "c" * 32, "d" * 32
LIB = "e" * 32
USER = "f" * 32


class FakeJellyfin:
    def __init__(self, password="hunter2", quick=True, stream_ok=True, version="12.4.0"):
        self.password, self.quick, self.stream_ok, self.version = password, quick, stream_ok, version
        self.fail_params = set()                        # a stream request carrying any of these query parameters fails with a 500
        self.stream_bps = None                          # bytes per second the stream is sent at (a transcode that cannot keep up); None = at once
        self.net_bps = None                             # likewise for the plain bitrate test; "off" = the server has no such endpoint
        self.token = "tok-" + "1" * 28
        self.secret, self.code, self.approved = "sec" + "2" * 29, "123456", False
        self.requests = []                              # (method, path, query, authorization header)
        self.items = [
            {"Id": SERIES, "Name": "The Simpsons", "Type": "Series", "ProductionYear": 1989, "ChildCount": 35},
            {"Id": MOVIE, "Name": "Big Film", "Type": "Movie", "ProductionYear": 2001, "RunTimeTicks": 90 * 60 * 10_000_000},
        ]
        self.boxset = {"Id": "9" * 32, "Name": "Cartoon Films", "Type": "BoxSet"}
        self.boxset_children = [{"Id": "8" * 32, "Name": "Film One", "Type": "Movie", "RunTimeTicks": 80 * 60 * 10_000_000},
                                {"Id": "7" * 32, "Name": "Film Two", "Type": "Movie", "RunTimeTicks": 70 * 60 * 10_000_000}]
        self.items.append(self.boxset)
        self.episodes = [
            {"Id": EP1, "Name": "Simpsons Roasting", "ParentIndexNumber": 1, "IndexNumber": 1, "RunTimeTicks": 1320 * 10_000_000},
            {"Id": EP2, "Name": "Bart the Genius", "ParentIndexNumber": 1, "IndexNumber": 2, "RunTimeTicks": 1300 * 10_000_000},
        ]
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, raw=None, ctype="application/json"):
                body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _paced(self, size, bps, ctype):
                """Send `size` bytes at `bps` bytes a second (a reader that goes away early is fine)."""
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                try:
                    sent = 0
                    while sent < size:
                        n = min(max(1, int(bps / 20)), size - sent)
                        self.wfile.write(b"x" * n)
                        sent += n
                        time.sleep(n / bps)
                except OSError:
                    pass

            def _authed(self, q):
                auth = self.headers.get("Authorization", "")
                return (auth.startswith("MediaBrowser ") and f'Token="{fake.token}"' in auth) or q.get("api_key", [""])[0] == fake.token

            def _body(self):
                n = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(n) or b"{}")

            def _route(self, method):
                if self.headers.get("User-Agent", "").startswith("Python-urllib"):
                    return self._send(403)                      # what Cloudflare and some proxies do to a library's default agent
                u = urlparse(self.path)
                q = parse_qs(u.query)
                fake.requests.append((method, u.path, {k: v[0] for k, v in q.items()}, self.headers.get("Authorization", "")))
                p = u.path
                if p == "/System/Info/Public":
                    return self._send(200, {"ServerName": "Fake", "Version": fake.version, "Id": "srv"})
                if p == "/QuickConnect/Enabled":
                    return self._send(200, fake.quick)
                if p == "/QuickConnect/Initiate" and method == "POST":
                    return self._send(200, {"Secret": fake.secret, "Code": fake.code, "Authenticated": False})
                if p == "/QuickConnect/Connect":
                    return self._send(200, {"Authenticated": fake.approved, "Secret": fake.secret, "Code": fake.code})
                if p == "/Users/AuthenticateByName" and method == "POST":
                    if not self.headers.get("Authorization", "").startswith("MediaBrowser "):
                        return self._send(401)
                    b = self._body()
                    if b.get("Username") == "sam" and b.get("Pw") == fake.password:
                        return self._send(200, {"AccessToken": fake.token, "User": {"Id": USER, "Name": "sam"}})
                    return self._send(401)
                if p == "/Users/AuthenticateWithQuickConnect" and method == "POST":
                    if fake.approved and self._body().get("Secret") == fake.secret:
                        return self._send(200, {"AccessToken": fake.token, "User": {"Id": USER, "Name": "sam"}})
                    return self._send(401)
                if p.startswith("/Videos/") and (p.endswith("/stream.mp4") or p.endswith("/stream.mkv")):
                    if not self._authed(q):
                        return self._send(401)
                    if not fake.stream_ok or any(k in q for k in fake.fail_params):
                        return self._send(500)
                    if fake.stream_bps:
                        return self._paced(4_000_000, fake.stream_bps, "video/mp4")
                    return self._send(200, raw=b"\x00\x00\x00\x18ftypmp42" + b"x" * 40000, ctype="video/mp4")
                if p == "/Playback/BitrateTest":
                    if not self._authed(q):
                        return self._send(401)
                    if fake.net_bps == "off":
                        return self._send(404)
                    size = int(q.get("size", ["100000"])[0])
                    if fake.net_bps:
                        return self._paced(size, fake.net_bps, "application/octet-stream")
                    return self._send(200, raw=b"x" * size, ctype="application/octet-stream")
                if not self._authed(q):
                    return self._send(401)
                if p == "/UserViews":
                    return self._send(200, {"Items": [{"Id": LIB, "Name": "Shows", "CollectionType": "tvshows"}]})
                if p == "/Items":
                    term = q.get("searchTerm", [""])[0].lower()
                    items = [i for i in fake.items if term in i["Name"].lower()]
                    if q.get("ids"):
                        wanted = q["ids"][0].split(",")
                        items = [i for i in fake.items + fake.episodes if i["Id"] in wanted]
                    if q.get("parentId", [""])[0] == fake.boxset["Id"]:
                        items = fake.boxset_children
                    return self._send(200, {"Items": items, "TotalRecordCount": len(items)})
                if p.startswith("/Playlists/") and p.endswith("/Items"):
                    return self._send(200, {"Items": fake.boxset_children, "TotalRecordCount": len(fake.boxset_children)})
                if p.startswith("/Shows/") and p.endswith("/Episodes"):
                    return self._send(200, {"Items": fake.episodes, "TotalRecordCount": len(fake.episodes)})
                return self._send(404)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
