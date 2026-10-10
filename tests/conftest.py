import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tubeless.library import Library, Playlists          # noqa: E402
from tubeless.state import Store                          # noqa: E402


def make_tree(root: Path, channels: dict) -> Path:
    """channels = {"1.cartoons": ["a.mp4", "sub/b.mp4"], ...}  (files are empty)"""
    for name, files in channels.items():
        for rel in files:
            p = root / name / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")
    return root


@pytest.fixture
def tree(tmp_path):
    return make_tree(tmp_path / "videos", {
        "2.nights": [f"n{i}.mp4" for i in range(4)],
        "1.cartoons": [f"c{i}.mp4" for i in range(6)] + ["extras/x.mkv", "notes.txt"],
        "10.empty": ["readme.txt"],
        "arnold": ["a.mp4"],
    })


@pytest.fixture
def lib(tree):
    return Library(tree)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def playlists(lib, store):
    return Playlists(lib, store, "/static.mp4", rng=random.Random(1234))
