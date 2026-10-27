"""
A channel made of videos on your Jellyfin server: a folder in the videos folder holding one small file, `jellyfin-channel.json`, which lists
the chosen movies and episodes (their ids, titles and lengths). Nothing is copied: each video is streamed from the server when it plays.
A channel is only ever Jellyfin videos (no mixing with YouTube or files, for now).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

FILE = "jellyfin-channel.json"
PREFIX = "jf-"                                     # a video that is played from Jellyfin: "jf-<item id>"
_ID = re.compile(r"^[0-9a-fA-F]{32}$")


def rel_of(item_id: str) -> str:
    return PREFIX + item_id.replace("-", "").lower()


def item_id_of(rel: str) -> Optional[str]:
    if isinstance(rel, str) and rel.startswith(PREFIX) and _ID.match(rel[len(PREFIX):]):
        return rel[len(PREFIX):].lower()
    return None


class JellyfinChannelFile:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.items: List[dict] = []                # {"id", "title", "seconds", "kind": "Movie" | "Episode", "series"}

    @property
    def path(self) -> Path:
        return self.folder / FILE

    @classmethod
    def exists(cls, folder) -> bool:
        return (Path(folder) / FILE).is_file()

    @classmethod
    def load(cls, folder) -> "JellyfinChannelFile":
        cf = cls(folder)
        try:
            with open(cf.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cf
        for raw in data.get("items", []) if isinstance(data, dict) else []:
            if isinstance(raw, dict) and item_id_of(PREFIX + str(raw.get("id", ""))):
                cf.items.append({"id": str(raw["id"]).lower(), "title": str(raw.get("title", ""))[:200],
                                 "seconds": raw["seconds"] if isinstance(raw.get("seconds"), (int, float)) and raw["seconds"] > 0 else None,
                                 "kind": str(raw.get("kind", "")), "series": str(raw.get("series", ""))[:200]})
        return cf

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".part")
        with open(tmp, "w") as f:
            json.dump({"items": self.items}, f, indent=1)
            f.write("\n")
        os.replace(tmp, self.path)

    def find(self, item_id: str) -> Optional[dict]:
        return next((i for i in self.items if i["id"] == item_id), None)

    def add(self, entries: Iterable[dict]) -> Tuple[int, int]:
        """Add movies and episodes (dicts with id, title, seconds, kind, series). Returns (added, already there)."""
        added = skipped = 0
        for e in entries:
            item_id = str(e.get("id", "")).replace("-", "").lower()
            if not _ID.match(item_id):
                continue
            if self.find(item_id):
                skipped += 1
                continue
            seconds = e.get("seconds")
            self.items.append({"id": item_id, "title": str(e.get("title") or e.get("name") or "Untitled")[:200],
                               "seconds": seconds if isinstance(seconds, (int, float)) and seconds > 0 else None,
                               "kind": str(e.get("kind", "")), "series": str(e.get("series", ""))[:200]})
            added += 1
        return added, skipped

    def remove(self, ids: Iterable[str]) -> int:
        gone = {str(i).replace("-", "").lower() for i in ids}
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] not in gone]
        return before - len(self.items)

    def as_dict(self) -> dict:
        return {"items": [dict(i) for i in self.items], "count": len(self.items),
                "seconds": sum(i["seconds"] or 0 for i in self.items)}
