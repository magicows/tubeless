"""
Channels (folders of videos), and the playlist for each one.

Playlist layout is  static, video, static, video, ...  so every video has a static
burst in front of it.

Shuffling is "non-repeating": a channel's shuffled order is saved, and you work
through it in order. It is only reshuffled when you have been through all of it
(or when you ask), and new videos are slotted in among the ones you haven't seen.
mpv is given the order *rotated to start at the episode you are on*, so a reboot
resumes mid-episode and "previous" from the first entry wraps to the one before.
"""
from __future__ import annotations

import os
import random
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import expand
from .jfchannel import JellyfinChannelFile, PREFIX as JF_PREFIX, item_id_of as jf_item_id
from .state import Store
from .youtube import ChannelFile, valid_id

VIDEO_EXTS = (".mp4", ".mkv", ".m4v", ".mov", ".avi", ".webm")
_NUMBERED = re.compile(r"^(\d+)\.(.*)$")

NOT_CONNECTED = "http://jellyfin.invalid/not-connected"      # what a Jellyfin video's address is while no server is signed in: fails at once
STREAM_PREFIX = "yt-"                  # a video that is played from YouTube instead of from a file: "yt-<11 character id>"


def _order_key(name: str):
    m = _NUMBERED.match(name)
    return (int(m.group(1)), m.group(2)) if m else (999, name)


@dataclass(frozen=True)
class Channel:
    name: str          # folder name, e.g. "1.simpsons"
    path: str

    @property
    def label(self) -> str:
        m = _NUMBERED.match(self.name)
        return (m.group(2) if m else self.name).strip().replace("_", " ")


class Library:
    def __init__(self, root):
        self.root = expand(root)
        self.jf_url: Optional[Callable[[str], str]] = None     # set by the app: a Jellyfin item's id -> the address to stream it from
        self.order: List[str] = []         # channel numbers chosen on the web page: these first, in this order; the rest by folder name after them

    def channels(self) -> List[Channel]:
        try:
            names = [e.name for e in os.scandir(self.root) if e.is_dir() and not e.name.startswith(".")]
        except OSError:
            return []
        chosen = [n for n in self.order if n in names]
        rest = sorted((n for n in names if n not in set(chosen)), key=_order_key)
        return [Channel(n, str(self.root / n)) for n in chosen + rest]

    def videos(self, channel: Channel) -> List[str]:
        """Video files in a channel, as paths relative to the channel folder, sorted."""
        found = []
        for dirpath, dirnames, files in os.walk(channel.path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in files:
                if f.lower().endswith(VIDEO_EXTS) and not f.startswith("."):
                    found.append(os.path.relpath(os.path.join(dirpath, f), channel.path).replace(os.sep, "/"))
        found += self._streamed(channel, found)
        return sorted(found)

    @staticmethod
    def _streamed(channel: Channel, on_disk: List[str]) -> List[str]:
        """A YouTube channel set to "stream": the ticked videos that aren't downloaded are played from YouTube. A Jellyfin channel: all its videos."""
        if JellyfinChannelFile.exists(Path(channel.path)):
            return [JF_PREFIX + i["id"] for i in JellyfinChannelFile.load(Path(channel.path)).items]
        if not ChannelFile.exists(Path(channel.path)):
            return []
        cf = ChannelFile.load(Path(channel.path))
        if cf.mode != "stream":
            return []
        have = set(on_disk)
        return [STREAM_PREFIX + i["id"] for i in cf.items
                if i.get("selected") and not (i.get("file") and i["file"] in have)]

    def in_order(self, channel: Channel, rels: List[str]) -> List[str]:
        """The videos 'in order': a YouTube channel plays in its playlist's order; a folder plays by file name."""
        if JellyfinChannelFile.exists(Path(channel.path)):                 # the order they were added in
            position = {JF_PREFIX + i["id"]: n for n, i in enumerate(JellyfinChannelFile.load(Path(channel.path)).items)}
            return sorted(rels, key=lambda r: (position.get(r, 10 ** 9), r))
        if not ChannelFile.exists(Path(channel.path)):
            return sorted(rels)
        cf = ChannelFile.load(Path(channel.path))
        position = {}
        for n, item in enumerate(cf.items):
            if item.get("file"):
                position[item["file"]] = n
            position[STREAM_PREFIX + item["id"]] = n
        return sorted(rels, key=lambda r: (position.get(r, 10 ** 9), r))

    def has_videos(self, channel: Channel) -> bool:
        for _, dirnames, files in os.walk(channel.path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if any(f.lower().endswith(VIDEO_EXTS) and not f.startswith(".") for f in files):
                return True
        return bool(self._streamed(channel, []))

    def abspath(self, channel: Channel, rel: str) -> str:
        """Where to play a video from: its file, or the address for a streamed one (YouTube, or your Jellyfin server)."""
        if rel.startswith(STREAM_PREFIX) and valid_id(rel[len(STREAM_PREFIX):]):
            return "https://www.youtube.com/watch?v=" + rel[len(STREAM_PREFIX):]
        item = jf_item_id(rel)
        if item:
            try:
                return self.jf_url(item) if self.jf_url else NOT_CONNECTED
            except Exception:
                return NOT_CONNECTED                       # not signed in: an address that fails at once, so the TV can say why
        return os.path.join(channel.path, rel)

    @staticmethod
    def is_youtube(rel: Optional[str]) -> bool:
        return bool(rel) and rel.startswith(STREAM_PREFIX) and valid_id(rel[len(STREAM_PREFIX):])

    @staticmethod
    def is_jellyfin(rel: Optional[str]) -> bool:
        return jf_item_id(rel) is not None

    @classmethod
    def is_stream(cls, rel: Optional[str]) -> bool:
        """Played from the network (YouTube or Jellyfin) rather than from a file."""
        return cls.is_youtube(rel) or cls.is_jellyfin(rel)

    def stream_seconds(self, channel: Channel) -> Dict[str, float]:
        """How long each streamed video of a channel is, as the playlist listing gave it: {rel: seconds} (unknown ones left out)."""
        if JellyfinChannelFile.exists(Path(channel.path)):
            return self._jf_seconds(channel)
        if not ChannelFile.exists(Path(channel.path)):
            return {}
        return {STREAM_PREFIX + i["id"]: float(i["duration"]) for i in ChannelFile.load(Path(channel.path)).items
                if isinstance(i.get("duration"), (int, float)) and i["duration"] > 0}

    @staticmethod
    def _jf_seconds(channel: Channel) -> Dict[str, float]:
        return {JF_PREFIX + i["id"]: float(i["seconds"]) for i in JellyfinChannelFile.load(Path(channel.path)).items if i["seconds"]}

    def title_of(self, channel: Channel, rel: str) -> str:
        if self.is_jellyfin(rel) and JellyfinChannelFile.exists(Path(channel.path)):
            item = JellyfinChannelFile.load(Path(channel.path)).find(jf_item_id(rel))
            if item:
                return item["title"]
        if self.is_youtube(rel) and ChannelFile.exists(Path(channel.path)):
            item = ChannelFile.load(Path(channel.path)).find(rel[len(STREAM_PREFIX):])
            if item:
                return item["title"]
        return os.path.splitext(os.path.basename(rel))[0]

    def next_playable(self, start: int, step: int) -> Optional[int]:
        """Index of the first channel at/after `start` (moving by `step`, wrapping) that has videos."""
        channels = self.channels()
        n = len(channels)
        for i in range(n):
            idx = (start + i * step) % n
            if self.has_videos(channels[idx]):
                return idx
        return None


@dataclass
class Plan:
    channel: Channel
    order: List[str]            # relative paths, in this cycle's order
    index: int                  # the episode you are on
    cycle: int = 0
    shuffle: bool = True

    @property
    def rotated(self) -> List[str]:
        """The order starting at the current episode and wrapping round."""
        return self.order[self.index:] + self.order[:self.index]

    @property
    def current(self) -> str:
        return self.order[self.index]


def _slug(channel: Channel) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", channel.name)[:40]
    return f"{safe}-{zlib.crc32(channel.name.encode()) & 0xffff:04x}"


class Playlists:
    PROGRESS = "progress.json"

    def __init__(self, library: Library, store: Store, static_path: str, rng: Optional[random.Random] = None):
        self.library = library
        self.store = store
        self.static_path = static_path
        self.rng = rng or random.Random()
        self.live: Optional[Callable[[Channel], Optional[Tuple[List[str], str, float]]]] = None     # Live TV mode (see live.py), set by the app

    def _live_plan(self, channel: Channel, shuffle: bool) -> Optional[Plan]:
        """With Live TV mode on and every length known: the plan that is on air now (its running order, the video and the position in it)."""
        slot = self.live(channel) if self.live is not None else None
        if not slot:
            return None
        order, current, position = slot
        saved = self.store.load(self._file(channel), None) or {}
        self.set_progress(channel, current, position)                    # the app's resume step seeks to it
        plan = Plan(channel, order, order.index(current), int(saved.get("cycle", 0)), shuffle)
        self._save(plan)
        return plan

    # --- progress: which episode, how far in ------------------------------------
    def progress(self, channel: Channel) -> dict:
        return self.store.load(self.PROGRESS, {}).get(channel.name, {})

    def set_progress(self, channel: Channel, current: str, position: float) -> None:
        data = self.store.load(self.PROGRESS, {})
        data[channel.name] = {"current": current, "pos": round(float(position), 1)}
        self.store.save(self.PROGRESS, data)

    # --- plans ---------------------------------------------------------------------
    def _file(self, channel: Channel) -> str:
        return f"channels/{_slug(channel)}.json"

    def _shuffled(self, items: List[str], first: Optional[str] = None, avoid_first: Optional[str] = None) -> List[str]:
        items = list(items)
        self.rng.shuffle(items)
        if first in items:                                 # keep this one at the front
            items.remove(first)
            items.insert(0, first)
        elif avoid_first is not None and len(items) > 1 and items[0] == avoid_first:
            j = self.rng.randrange(1, len(items))          # don't open the new cycle with what just played
            items[0], items[j] = items[j], items[0]
        return items

    def plan(self, channel: Channel, shuffle: bool) -> Plan:
        """The saved plan for a channel, brought up to date with what is on disk."""
        on_disk = self.library.videos(channel)
        if not on_disk:
            raise ValueError(f"No videos found in {channel.path}.")
        live = self._live_plan(channel, shuffle)
        if live is not None:
            return live
        saved = self.store.load(self._file(channel), None) or {}
        current = self.progress(channel).get("current")
        disk = set(on_disk)

        saved_order = [p for p in saved.get("order", []) if p in disk]
        cycle = int(saved.get("cycle", 0))
        if not saved_order or saved.get("shuffle") != shuffle:
            # nothing saved yet, or shuffle was switched: start a fresh order (keeping the current episode first)
            order = self._shuffled(on_disk, first=current) if shuffle else self.library.in_order(channel, on_disk)
        elif shuffle:
            order = saved_order
            idx = order.index(current) if current in order else 0
            new = [p for p in on_disk if p not in set(order)]
            self.rng.shuffle(new)
            for p in new:                                  # slot new videos among the ones not yet seen
                pos = self.rng.randint(idx + 1, len(order))
                order.insert(pos, p)
        else:
            order = self.library.in_order(channel, on_disk)          # in order simply tracks the disk
        index = order.index(current) if current in order else 0
        plan = Plan(channel, order, index, cycle, shuffle)
        self._save(plan)
        return plan

    def new_cycle(self, channel: Channel, shuffle: bool) -> Plan:
        """You have been through everything (or asked): reshuffle and start again. (In Live TV mode: back to what is on air now.)"""
        on_disk = self.library.videos(channel)
        if not on_disk:
            raise ValueError(f"No videos found in {channel.path}.")
        live = self._live_plan(channel, shuffle)
        if live is not None:
            return live
        last = self.progress(channel).get("current")
        saved = self.store.load(self._file(channel), None) or {}
        order = self._shuffled(on_disk, avoid_first=last) if shuffle else self.library.in_order(channel, on_disk)
        plan = Plan(channel, order, 0, int(saved.get("cycle", 0)) + 1, shuffle)
        self._save(plan)
        self.set_progress(channel, order[0], 0.0)
        return plan

    def _save(self, plan: Plan) -> None:
        self.store.save(self._file(plan.channel),
                        {"order": plan.order, "cycle": plan.cycle, "shuffle": plan.shuffle})

    # --- what mpv plays ----------------------------------------------------------
    def entries(self, plan: Plan) -> List[str]:
        """static, video, static, video ... starting at the current episode."""
        out: List[str] = []
        for rel in plan.rotated:
            out.append(self.static_path)
            out.append(self.library.abspath(plan.channel, rel))
        return out

    def write_m3u(self, plan: Plan, path) -> str:
        path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)       # a Jellyfin address carries the sign-in token
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(self.entries(plan)) + "\n")
        return path

    def episode_at(self, plan: Plan, playlist_pos: int) -> Optional[str]:
        """Relative path of the episode at an mpv playlist position (None for a static)."""
        if playlist_pos is None or playlist_pos % 2 == 0:
            return None
        rotated = plan.rotated
        i = (playlist_pos - 1) // 2
        return rotated[i] if 0 <= i < len(rotated) else None
