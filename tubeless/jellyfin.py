"""
A small client for a Jellyfin server: sign in (password or Quick Connect), browse what it has, and work out stream addresses.

Written against Jellyfin 12's published API description. Sign-in is the one `Authorization: MediaBrowser ...` header (older servers also took
`X-Emby-Authorization`; the newest only take this one). Only a token is kept, never the password, in `jellyfin.json` in the TV's data folder
(readable by the TV's user only). The page's "test" button asks the server for the start of a stream and says what came back, because
how a server answers a transcode request varies and is better tested than assumed.

Standard library only (urllib).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import List, Optional

from . import __version__

log = logging.getLogger(__name__)

TICKS = 10_000_000                      # Jellyfin counts time in ticks of 100 ns
CLIENT, DEVICE = "Tubeless", "Tubeless TV"
USER_AGENT = f"Tubeless/{__version__} (a Raspberry Pi TV; Jellyfin client)"       # Python's own default is refused by Cloudflare and some proxies
KINDS = ("Series", "Movie", "BoxSet", "Playlist")
ID_RE = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class JellyfinError(ValueError):
    """A message fit to show on the web page (a ValueError, so the web layer shows it as a 400)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _ServerFailed(Exception):
    """The server answered a stream request with a 5xx: its transcoder failed."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def valid_item_id(text) -> bool:
    return isinstance(text, str) and bool(ID_RE.match(text))


def normalise_server(text: str) -> str:
    """'jelly.example.com' or 'https://jelly.example.com/web/index.html' -> 'https://jelly.example.com'. Raises JellyfinError."""
    text = str(text or "").strip()
    if not text:
        raise JellyfinError("Enter the address of your Jellyfin server, like jelly.example.com or http://192.168.1.20:8096")
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise JellyfinError("That doesn't look like a server address.")
    if parts.username or parts.password:
        raise JellyfinError("Leave the user name and password out of the address: you give them separately.")
    path = re.sub(r"/web(/.*)?$", "", parts.path).rstrip("/")
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return f"{parts.scheme}://{host}{':%d' % parts.port if parts.port else ''}{path}"


def mask(url: str) -> str:
    """A stream address with the token hidden, for the log."""
    return re.sub(r"(api_key=)[^&]+", r"\1***", url, flags=re.I)


# ─────────────────────────────────────────────
#  Where the token is kept
# ─────────────────────────────────────────────
class Credentials:
    """jellyfin.json: {server, token, user_id, user_name, device_id}. Only the token, never the password."""

    NAME = "jellyfin.json"

    def __init__(self, folder: Path):
        self.path = Path(folder) / self.NAME
        self.data: dict = {}
        try:
            with open(self.path) as f:
                loaded = json.load(f)
            self.data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self.data = {}
        if not self.data.get("device_id"):
            self.data["device_id"] = uuid.uuid4().hex                  # names this TV to the server, so it can be revoked there

    @property
    def server(self) -> str:
        return str(self.data.get("server", ""))

    @property
    def token(self) -> str:
        return str(self.data.get("token", ""))

    @property
    def signed_in(self) -> bool:
        return bool(self.server and self.token and self.data.get("user_id"))

    def save(self, **fields) -> None:
        self.data.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".part")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)          # the token is as good as a password
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)

    def forget(self) -> None:
        self.data = {"device_id": self.data.get("device_id") or uuid.uuid4().hex}
        self.save()


# ─────────────────────────────────────────────
#  The client
# ─────────────────────────────────────────────
class Client:
    def __init__(self, server: str, token: str = "", user_id: str = "", device_id: str = "", timeout: float = 15.0):
        self.server = normalise_server(server)
        self.token, self.user_id = token, user_id
        self.device_id = device_id or uuid.uuid4().hex
        self.timeout = timeout

    # --- plumbing -------------------------------------------------------------------------------------------------------
    def auth_header(self) -> str:
        value = f'MediaBrowser Client="{CLIENT}", Device="{DEVICE}", DeviceId="{self.device_id}", Version="{__version__}"'
        return value + (f', Token="{self.token}"' if self.token else "")

    def _url(self, path: str, query: Optional[dict] = None) -> str:
        url = self.server + path
        if query:
            url += "?" + urllib.parse.urlencode([(k, str(v).lower() if isinstance(v, bool) else v) for k, v in query.items() if v is not None])
        return url

    def _call(self, method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._url(path, query), data=data, method=method)
        req.add_header("Authorization", self.auth_header())
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(8 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            raise self._http_error(e.code, path)
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise JellyfinError(f"Couldn't reach {urllib.parse.urlsplit(self.server).netloc}: {reason}", 502)
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            raise JellyfinError("That server's answer wasn't what Jellyfin sends. Is the address right?", 502)

    @staticmethod
    def _http_error(status: int, path: str) -> JellyfinError:
        if status == 401:
            return JellyfinError("The server didn't accept that sign-in." if "Authenticate" in path else "The server says this TV isn't signed in any more. Connect again.", 401)
        if status == 403:
            return JellyfinError("That user isn't allowed to do that on the server.", 403)
        if status == 404:
            return JellyfinError("The server doesn't know that (is it a Jellyfin server, and is the address right?).", 404)
        return JellyfinError(f"The server answered with an error ({status}).", 502)

    # --- who is this ----------------------------------------------------------------------------------------------------
    def public_info(self) -> dict:
        info = self._call("GET", "/System/Info/Public")
        if not isinstance(info, dict) or "Version" not in info:
            raise JellyfinError("That doesn't look like a Jellyfin server.", 502)
        return {"name": info.get("ServerName", ""), "version": info.get("Version", ""), "id": info.get("Id", "")}

    def _signed_in(self, result: dict) -> dict:
        token = result.get("AccessToken")
        user = result.get("User") or {}
        if not token or not user.get("Id"):
            raise JellyfinError("The server's answer had no sign-in in it.", 502)
        self.token, self.user_id = token, user["Id"]
        return {"token": token, "user_id": user["Id"], "user_name": user.get("Name", "")}

    def login(self, username: str, password: str) -> dict:
        if not str(username).strip():
            raise JellyfinError("Enter the user name.")
        return self._signed_in(self._call("POST", "/Users/AuthenticateByName", body={"Username": str(username).strip(), "Pw": str(password)}))

    # --- Quick Connect: a code to approve on a device that is already signed in ---------------------------------------
    def quick_enabled(self) -> bool:
        return self._call("GET", "/QuickConnect/Enabled") is True

    def quick_start(self) -> dict:
        if not self.quick_enabled():
            raise JellyfinError("Quick Connect is switched off on that server. Sign in with a user name and password instead.")
        result = self._call("POST", "/QuickConnect/Initiate") or {}
        if not result.get("Secret") or not result.get("Code"):
            raise JellyfinError("The server didn't give a Quick Connect code.", 502)
        return {"secret": result["Secret"], "code": result["Code"]}

    def quick_check(self, secret: str) -> bool:
        return bool((self._call("GET", "/QuickConnect/Connect", {"secret": secret}) or {}).get("Authenticated"))

    def quick_finish(self, secret: str) -> dict:
        return self._signed_in(self._call("POST", "/Users/AuthenticateWithQuickConnect", body={"Secret": secret}))

    # --- what the server has --------------------------------------------------------------------------------------------
    def libraries(self) -> List[dict]:
        out = self._call("GET", "/UserViews", {"userId": self.user_id}) or {}
        return [{"id": v["Id"], "name": v.get("Name", ""), "kind": v.get("CollectionType", "")} for v in out.get("Items", [])]

    def items(self, parent_id: str = "", search: str = "", kinds=KINDS, start: int = 0, limit: int = 60) -> dict:
        if parent_id and not valid_item_id(parent_id):
            raise JellyfinError("That isn't a library.")
        out = self._call("GET", "/Items", {
            "userId": self.user_id, "parentId": parent_id or None, "includeItemTypes": ",".join(kinds), "recursive": True,
            "searchTerm": search or None, "sortBy": "SortName", "sortOrder": "Ascending", "startIndex": start, "limit": limit,
            "fields": "ProductionYear,RunTimeTicks,ChildCount"}) or {}
        items = [self._item(i) for i in out.get("Items", []) if i.get("Id")]
        return {"items": items, "total": out.get("TotalRecordCount", len(items))}

    @staticmethod
    def _item(raw: dict) -> dict:
        return {"id": raw["Id"], "name": raw.get("Name", ""), "kind": raw.get("Type", ""), "year": raw.get("ProductionYear"),
                "seconds": round(raw.get("RunTimeTicks", 0) / TICKS, 1) if raw.get("RunTimeTicks") else None,
                "children": raw.get("ChildCount")}

    def episodes(self, series_id: str) -> List[dict]:
        if not valid_item_id(series_id):
            raise JellyfinError("That isn't a series.")
        out = self._call("GET", f"/Shows/{series_id}/Episodes", {"userId": self.user_id, "fields": "RunTimeTicks"}) or {}
        return [{"id": e["Id"], "title": e.get("Name", ""), "season": e.get("ParentIndexNumber"), "episode": e.get("IndexNumber"),
                 "seconds": round(e.get("RunTimeTicks", 0) / TICKS, 1) if e.get("RunTimeTicks") else None}
                for e in out.get("Items", []) if e.get("Id")]

    def resolve(self, ids: List[str], limit: int = 2000) -> List[dict]:
        """Turn what was ticked into playable videos: a movie or episode is itself, a series is all its episodes, a collection or playlist is
        what is in it. Each: {id, title, seconds, kind, series}. Raises JellyfinError."""
        ids = [i for i in ids if valid_item_id(i)]
        if not ids:
            raise JellyfinError("Tick something to add first.")
        out: List[dict] = []
        fields = "RunTimeTicks,SeriesName"
        for chunk in (ids[i:i + 40] for i in range(0, len(ids), 40)):
            found = self._call("GET", "/Items", {"userId": self.user_id, "ids": ",".join(chunk), "fields": fields}) or {}
            for raw in found.get("Items", []):
                kind = raw.get("Type", "")
                if kind in ("Movie", "Episode", "Video"):
                    out.append(self._video(raw))
                elif kind == "Series":
                    for e in self.episodes(raw["Id"]):
                        out.append({"id": e["id"], "title": f"{raw.get('Name', '')}: {e['title']}", "seconds": e["seconds"], "kind": "Episode",
                                    "series": raw.get("Name", "")})
                elif kind in ("BoxSet", "Playlist"):
                    path = f"/Playlists/{raw['Id']}/Items" if kind == "Playlist" else "/Items"
                    query = {"userId": self.user_id, "fields": fields, "limit": limit}
                    if kind == "BoxSet":
                        query.update({"parentId": raw["Id"], "includeItemTypes": "Movie,Episode", "recursive": True})
                    for child in (self._call("GET", path, query) or {}).get("Items", []):
                        if child.get("Type") in ("Movie", "Episode", "Video") and child.get("Id"):
                            out.append(self._video(child))
            if len(out) > limit:
                raise JellyfinError(f"That is more than {limit} videos at once. Add fewer things.")
        return out

    @staticmethod
    def _video(raw: dict) -> dict:
        title = raw.get("Name", "") or "Untitled"
        if raw.get("Type") == "Episode" and raw.get("SeriesName"):
            title = f"{raw['SeriesName']}: {title}"
        return {"id": raw["Id"], "title": title, "kind": raw.get("Type", ""), "series": raw.get("SeriesName", ""),
                "seconds": round(raw.get("RunTimeTicks", 0) / TICKS, 1) if raw.get("RunTimeTicks") else None}

    # --- playing --------------------------------------------------------------------------------------------------------
    STREAM_VARIANTS = (("the usual options", 0), ("fewer options (no forced H.264 profile or audio limits)", 1), ("a Matroska container instead of MP4", 2))

    def stream_url(self, item_id: str, height: int = 360, start_seconds: float = 0.0, variant: int = 0) -> str:
        """A progressive H.264 + AAC stream of one video, at most `height` lines tall, transcoded by the server if need be.
        The token rides in the address (`api_key`) because the player can't send a header for one playlist entry."""
        if not valid_item_id(item_id):
            raise JellyfinError("That isn't a video.")
        bitrate = {240: 500_000, 360: 900_000, 480: 1_400_000}.get(height, 900_000)
        query = {"static": False, "videoCodec": "h264", "audioCodec": "aac", "maxHeight": height, "videoBitRate": bitrate,
                 "audioBitRate": 96_000, "maxAudioChannels": 2, "requireAvc": True, "deviceId": self.device_id, "api_key": self.token}
        if variant >= 1:                                         # gentler requests, for a video the server's transcoder fails on
            for key in ("requireAvc", "audioBitRate", "maxAudioChannels"):
                query.pop(key)
        if start_seconds > 1:
            query["startTimeTicks"] = int(start_seconds * TICKS)
        return self._url(f"/Videos/{item_id}/stream.{'mkv' if variant >= 2 else 'mp4'}", query)

    def check_stream(self, item_id: str, height: int = 360, seconds: float = 6.0) -> dict:
        """Ask for the start of the stream and say what came back: for the page's test button. If the server's transcoder fails (a 5xx), try
        gentler requests and say which one works, so it is clear whether the video or the request is the problem."""
        failed = []
        for name, variant in self.STREAM_VARIANTS:
            try:
                result = self._probe(self.stream_url(item_id, height, variant=variant), seconds)
            except _ServerFailed as e:
                failed.append(e.code)
                continue
            result["variant"] = name
            if failed:
                result["note"] = (f"The server failed ({failed[0]}) with {self.STREAM_VARIANTS[0][0]} but worked with {name}. "
                                  "So it is how the TV asks, not the video: tell the author which video it was.")
            return result
        raise JellyfinError(f"Jellyfin's transcoder failed ({failed[0]}) on this video however it was asked, so the problem is on the server. "
                            "Look in Jellyfin's dashboard under Logs for the FFmpeg transcode log of this video (a damaged file, an unusual "
                            "audio or subtitle track, or hardware transcoding that doesn't cope are the usual causes).", 502)

    def _probe(self, url: str, seconds: float) -> dict:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-", "User-Agent": USER_AGENT})
        started, got = time.time(), 0
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, kind = resp.status, resp.headers.get("Content-Type", "")
                first = None
                while time.time() - started < seconds and got < 512 * 1024:
                    chunk = resp.read(16 * 1024)
                    if not chunk:
                        break
                    if first is None:
                        first = time.time() - started
                    got += len(chunk)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise _ServerFailed(e.code)
            raise JellyfinError(f"The server refused the stream ({e.code})." + (" The token in the address wasn't accepted." if e.code == 401 else ""), 502)
        except (urllib.error.URLError, OSError) as e:
            raise JellyfinError(f"Couldn't read the stream: {getattr(e, 'reason', e)}", 502)
        return {"status": status, "content_type": kind, "bytes": got, "first_byte_seconds": round(first, 1) if first is not None else None,
                "seconds": round(time.time() - started, 1), "ok": got > 0 and ("video" in kind or "octet" in kind or "mp4" in kind)}
