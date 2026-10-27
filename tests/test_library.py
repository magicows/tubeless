import random

import pytest

from conftest import make_tree
from tubeless.library import Library, Playlists


def test_channels_sorted_numerically_then_by_name(lib):
    assert [c.name for c in lib.channels()] == ["1.cartoons", "2.nights", "10.empty", "arnold"]
    assert [c.label for c in lib.channels()] == ["cartoons", "nights", "empty", "arnold"]


def test_videos_found_recursively_and_non_videos_ignored(lib):
    ch = lib.channels()[0]
    vids = lib.videos(ch)
    assert "extras/x.mkv" in vids and "c0.mp4" in vids
    assert not any(v.endswith(".txt") for v in vids)
    assert len(vids) == 7


def test_empty_channels_are_skipped_by_next_playable(lib):
    names = [c.name for c in lib.channels()]
    assert lib.has_videos(lib.channels()[0]) and not lib.has_videos(lib.channels()[2])
    assert names[lib.next_playable(2, +1)] == "arnold"
    assert names[lib.next_playable(2, -1)] == "2.nights"
    assert names[lib.next_playable(3, +1)] == "arnold"
    assert lib.next_playable(0, -1) == 0                      # `start` itself counts if it is playable
    assert lib.next_playable(0 - 1, -1) == 3                  # callers pass current-1: wraps backwards past the start


def test_all_empty_returns_none(tmp_path):
    root = make_tree(tmp_path / "v", {"a": ["x.txt"], "b": []})
    assert Library(root).next_playable(0, +1) is None


def test_first_plan_is_a_shuffle_of_everything(lib, playlists):
    ch = lib.channels()[0]
    plan = playlists.plan(ch, shuffle=True)
    assert sorted(plan.order) == lib.videos(ch)
    assert plan.index == 0 and plan.cycle == 0


def test_plan_is_saved_and_not_reshuffled(lib, store):
    ch = lib.channels()[0]
    a = Playlists(lib, store, "/s.mp4", rng=random.Random(1)).plan(ch, True)
    b = Playlists(lib, store, "/s.mp4", rng=random.Random(999)).plan(ch, True)   # different rng, same saved order
    assert a.order == b.order


def test_sorted_mode_tracks_the_disk_and_keeps_your_place(lib, playlists):
    ch = lib.channels()[0]
    playlists.set_progress(ch, "c3.mp4", 12.0)
    plan = playlists.plan(ch, shuffle=False)
    assert plan.order == lib.videos(ch)
    assert plan.current == "c3.mp4"
    assert plan.rotated[0] == "c3.mp4" and plan.rotated[-1] == plan.order[plan.index - 1]


def test_toggling_shuffle_keeps_the_current_episode_first(lib, playlists):
    ch = lib.channels()[0]
    playlists.set_progress(ch, "c4.mp4", 0)
    plan = playlists.plan(ch, shuffle=True)
    assert plan.current == "c4.mp4" and plan.index == 0


def test_new_videos_slot_into_the_unwatched_part_and_removed_ones_vanish(tree, lib, playlists):
    ch = lib.channels()[0]
    plan = playlists.plan(ch, True)
    watched = plan.order[:3]
    playlists.set_progress(ch, plan.order[3], 0)
    (tree / "1.cartoons" / "brand_new.mp4").write_bytes(b"")
    (tree / "1.cartoons" / plan.order[-1]).unlink()
    plan2 = playlists.plan(ch, True)
    assert "brand_new.mp4" in plan2.order
    assert plan.order[-1] not in plan2.order
    assert plan2.order.index("brand_new.mp4") > plan2.index          # not among those already seen
    assert plan2.order[:3] == watched and plan2.current == plan.order[3]


def test_new_cycle_reshuffles_and_does_not_open_with_the_last_episode(lib, playlists):
    ch = lib.channels()[0]
    for _ in range(30):
        plan = playlists.plan(ch, True)
        last = plan.order[-1]
        playlists.set_progress(ch, last, 99)
        nxt = playlists.new_cycle(ch, True)
        assert nxt.order[0] != last
        assert sorted(nxt.order) == sorted(plan.order) and nxt.cycle == plan.cycle + 1
        assert playlists.progress(ch)["current"] == nxt.order[0]


def test_m3u_is_static_then_video_starting_at_the_current_episode(lib, playlists, tmp_path):
    ch = lib.channels()[1]
    plan = playlists.plan(ch, False)
    playlists.set_progress(ch, "n2.mp4", 0)
    plan = playlists.plan(ch, False)
    lines = open(playlists.write_m3u(plan, tmp_path / "p.m3u")).read().splitlines()
    assert len(lines) == 8
    assert lines[0::2] == ["/static.mp4"] * 4
    assert [l.rsplit("/", 1)[1] for l in lines[1::2]] == ["n2.mp4", "n3.mp4", "n0.mp4", "n1.mp4"]


def test_episode_at(lib, playlists):
    ch = lib.channels()[1]
    playlists.set_progress(ch, "n1.mp4", 0)
    plan = playlists.plan(ch, False)
    assert playlists.episode_at(plan, 0) is None and playlists.episode_at(plan, 2) is None
    assert playlists.episode_at(plan, 1) == "n1.mp4"
    assert playlists.episode_at(plan, 3) == "n2.mp4"
    assert playlists.episode_at(plan, 7) == "n0.mp4"
    assert playlists.episode_at(plan, 99) is None


def test_no_videos_raises(lib, playlists):
    with pytest.raises(ValueError):
        playlists.plan(lib.channels()[2], True)



def test_a_youtube_channel_in_order_follows_its_playlist_not_its_file_names(tmp_path):
    import random
    from tubeless.library import Library, Playlists
    from tubeless.state import Store
    from tubeless.youtube import ChannelFile
    root = tmp_path / "videos"
    folder = root / "3.yt"
    folder.mkdir(parents=True)
    for name in ("Zebra [bbbbbbbbbbb].mp4", "Apple [aaaaaaaaaaa].mp4", "Mango [ccccccccccc].mp4"):
        (folder / name).write_bytes(b"")
    cf = ChannelFile(folder)
    cf.items = [{"id": "bbbbbbbbbbb", "title": "Zebra", "selected": True, "file": "Zebra [bbbbbbbbbbb].mp4", "error": None},
                {"id": "aaaaaaaaaaa", "title": "Apple", "selected": True, "file": "Apple [aaaaaaaaaaa].mp4", "error": None},
                {"id": "ccccccccccc", "title": "Mango", "selected": True, "file": "Mango [ccccccccccc].mp4", "error": None}]
    cf.save()
    lib = Library(root)
    channel = lib.channels()[0]
    plans = Playlists(lib, Store(tmp_path / "data"), "/static.mp4", rng=random.Random(1))
    assert [p.split(" [")[0] for p in plans.plan(channel, False).order] == ["Zebra", "Apple", "Mango"]       # the playlist's order
    assert sorted(lib.videos(channel))[0].startswith("Apple")                                                  # file names would say otherwise
    shuffled = plans.plan(channel, True)
    assert sorted(shuffled.order) == sorted(lib.videos(channel))


def test_channels_can_be_numbered_by_a_saved_order_and_the_rest_follow_by_name(tmp_path):
    for name in ("1.cartoons", "2.nights", "3.youtube", "10.last", "extra"):
        (tmp_path / name).mkdir()
    lib = Library(tmp_path)
    assert [c.name for c in lib.channels()] == ["1.cartoons", "2.nights", "3.youtube", "10.last", "extra"]       # as before
    lib.order = ["3.youtube", "1.cartoons", "ghost"]                                                               # a name that has gone is ignored
    assert [c.name for c in lib.channels()] == ["3.youtube", "1.cartoons", "2.nights", "10.last", "extra"]
    (tmp_path / "0.new").mkdir()
    assert [c.name for c in lib.channels()][-1] == "extra" and [c.name for c in lib.channels()][:2] == ["3.youtube", "1.cartoons"]
    lib.order = []
    assert [c.name for c in lib.channels()][0] == "0.new"
