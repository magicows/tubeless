import os

import pytest

from tubeless.libindex import LibraryIndex
from tubeless.library import Library
from tubeless.state import Store


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "videos"
    for ch, names in {"1.shows": ["a.mp4", "b.mp4"], "2.more": ["c.mkv"]}.items():
        for n in names:
            (root / ch).mkdir(parents=True, exist_ok=True)
            (root / ch / n).write_bytes(b"x" * 10)
    return Library(root), Store(tmp_path / "data")


def make(lib, durations, available=True):
    library, store = lib
    calls = []

    def probe(path):
        calls.append(os.path.basename(path))
        d = durations.get(os.path.basename(path))
        return {"duration": d} if d is not None else None

    return LibraryIndex(library, store, probe=probe, available=lambda: available), calls


def test_scan_records_durations_and_summarises_each_channel(lib):
    idx, calls = make(lib, {"a.mp4": 1300.0, "b.mp4": 1311.0, "c.mkv": 600.0})
    assert idx.scan() == {"scanned": 3, "pending": 0}
    shows, more = idx.library.channels()
    assert idx.summary(shows) == {"files": 2, "known": 2, "unread": 0, "seconds": 2611}
    assert idx.summary(more)["seconds"] == 600
    assert idx.duration(shows, "a.mp4") == 1300.0


def test_only_new_or_changed_files_are_read_again(lib):
    idx, calls = make(lib, {"a.mp4": 1.0, "b.mp4": 2.0, "c.mkv": 3.0})
    idx.scan()
    calls.clear()
    idx.scan()
    assert calls == []
    shows = idx.library.channels()[0]
    path = os.path.join(shows.path, "b.mp4")
    open(path, "wb").write(b"y" * 99)                                          # changed size
    (open(os.path.join(shows.path, "new.mp4"), "wb")).write(b"z")
    idx.scan()
    assert sorted(calls) == ["b.mp4", "new.mp4"]
    calls.clear()
    idx.scan(force=True)
    assert len(calls) == 4


def test_unreadable_files_are_counted_and_gone_files_forgotten(lib):
    idx, _ = make(lib, {"a.mp4": 100.0})                                        # b.mp4 and c.mkv can't be read
    idx.scan()
    shows = idx.library.channels()[0]
    assert idx.summary(shows) == {"files": 2, "known": 1, "unread": 1, "seconds": 100}
    os.remove(os.path.join(shows.path, "a.mp4"))
    idx.scan()
    assert idx.summary(shows)["files"] == 1 and "1.shows/a.mp4" not in idx._entries


def test_the_index_survives_a_restart(lib):
    idx, _ = make(lib, {"a.mp4": 5.0, "b.mp4": 6.0, "c.mkv": 7.0})
    idx.scan()
    again, calls = make(lib, {})
    assert again.summary(again.library.channels()[0])["seconds"] == 11
    again.scan()
    assert calls == []


def test_without_mediainfo_nothing_is_read_and_the_status_says_so(lib):
    idx, calls = make(lib, {"a.mp4": 5.0}, available=False)
    assert idx.scan() == {"scanned": 0, "pending": 3} and calls == []
    assert idx.status()["mediainfo"] is False


def test_background_scan_runs_and_a_second_request_goes_round_again(lib):
    idx, calls = make(lib, {"a.mp4": 1.0, "b.mp4": 2.0, "c.mkv": 3.0})
    assert idx.start_scan() is True
    idx.wait()
    assert idx.status()["running"] is False and idx.status()["scanned_at"]
    assert sorted(calls) == ["a.mp4", "b.mp4", "c.mkv"]
