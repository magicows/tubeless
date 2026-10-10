"""
Small JSON files with atomic writes (a power cut can never leave a half-written file).

    settings.json     brightness, shuffle, channel, touch calibration, sleep/dim options ...
    progress.json     per channel: which episode and how far into it
    channels/*.json   per channel: the shuffled order of its episodes
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class Store:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, name: str) -> Path:
        return self.dir / name

    def load(self, name: str, default: Any = None) -> Any:
        try:
            with open(self.path(name)) as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); using defaults", name, e)
            return default

    def save(self, name: str, obj: Any) -> None:
        target = self.path(name)
        with self._lock:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                with open(tmp, "w") as f:
                    json.dump(obj, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
            except OSError as e:
                log.warning("could not save %s: %s", name, e)


class Settings:
    """A dict of simple values backed by settings.json."""

    NAME = "settings.json"

    def __init__(self, store: Store, legacy_file: Optional[Path] = None):
        self.store = store
        data = store.load(self.NAME, None)
        self.fresh = data is None                  # no settings yet = a brand new install
        if data is None:
            data = {}
            if legacy_file and Path(legacy_file).is_file():
                try:
                    with open(legacy_file) as f:
                        old = json.load(f)
                    data = {k: old[k] for k in ("brightness", "shuffle", "channel") if k in old}
                    self.fresh = False             # an existing TV: don't greet it as brand new
                    log.info("imported settings from %s", legacy_file)
                except (OSError, ValueError):
                    pass
            store.save(self.NAME, data)
        self._data = data
        self._lock = threading.Lock()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, **updates: Any) -> None:
        with self._lock:
            self._data.update(updates)
            snapshot = dict(self._data)
        self.store.save(self.NAME, snapshot)
