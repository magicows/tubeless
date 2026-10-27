import json
import os
import stat

import pytest

from jf_fake import EP1, EP2, MOVIE, SERIES, FakeJellyfin
from rig import Rig, wait_for
from tubeless.jfchannel import JellyfinChannelFile, item_id_of, rel_of
from tubeless.library import NOT_CONNECTED, Library

BOXSET = "9" * 32


@pytest.fixture
def stack(tmp_path):
    fake = FakeJellyfin()
    rig = Rig(tmp_path)
    rig.app.jellyfin("connect", {"server": fake.url, "username": "sam", "password": "hunter2"})
    yield rig, fake
    rig.stop()
    fake.stop()


# ---- the channel file ----------------------------------------------------------------------------------------------------------
def test_ids_and_rels():
    assert rel_of("A" * 32) == "jf-" + "a" * 32 and rel_of("12345678-1234-1234-1234-123456789abc") == "jf-12345678123412341234123456789abc"
    assert item_id_of("jf-" + "a" * 32) == "a" * 32
    for bad in ("jf-short", "yt-" + "a" * 32, "x", None, "jf-" + "g" * 32):
        assert item_id_of(bad) is None


def test_the_file_adds_skips_repeats_removes_and_survives_damage(tmp_path):
    cf = JellyfinChannelFile(tmp_path / "7.jelly")
    assert not JellyfinChannelFile.exists(cf.folder)
    added, skipped = cf.add([{"id": MOVIE, "title": "Big Film", "seconds": 5400, "kind": "Movie"},
                             {"id": MOVIE, "title": "Big Film again"}, {"id": "nope", "title": "junk"}, {"id": EP1, "title": "Ep", "seconds": None}])
    assert (added, skipped) == (2, 1) and [i["title"] for i in cf.items] == ["Big Film", "Ep"]
    cf.save()
    again = JellyfinChannelFile.load(cf.folder)
    assert [i["id"] for i in again.items] == [MOVIE, EP1] and again.items[1]["seconds"] is None
    assert again.as_dict()["seconds"] == 5400
    assert again.remove([MOVIE, "zzz"]) == 1 and [i["id"] for i in again.items] == [EP1]
    cf.path.write_text("not json")
    assert JellyfinChannelFile.load(cf.folder).items == []
    cf.path.write_text(json.dumps({"items": [{"id": "bad"}, {"id": MOVIE, "title": "ok", "seconds": -5}, 7]}))
    assert [(i["id"], i["seconds"]) for i in JellyfinChannelFile.load(cf.folder).items] == [(MOVIE, None)]


# ---- adding from the page ------------------------------------------------------------------------------------------------------
def test_ticked_movies_a_series_and_a_collection_become_a_new_channel(stack):
    rig, fake = stack
    app = rig.app
    got = app.jellyfin("add", {"new": "jellymovies", "ids": [MOVIE, BOXSET]})
    assert got == {"channel": "jellymovies", "added": 3, "skipped": 0, "total": 3}                # a movie + the two in the collection
    cf = JellyfinChannelFile.load(rig.cfg.videos / "jellymovies")
    assert [i["title"] for i in cf.items] == ["Big Film", "Film One", "Film Two"] and cf.items[0]["seconds"] == 5400
    got = app.jellyfin("add", {"new": "simpsons", "ids": [SERIES]})
    assert got["added"] == 2
    items = JellyfinChannelFile.load(rig.cfg.videos / "simpsons").items
    assert [i["title"] for i in items] == ["The Simpsons: Simpsons Roasting", "The Simpsons: Bart the Genius"] and items[0]["kind"] == "Episode"
    assert items[0]["series"] == "The Simpsons"


def test_adding_to_an_existing_jellyfin_channel_skips_what_is_already_there(stack):
    rig, fake = stack
    app = rig.app
    app.jellyfin("add", {"new": "jellymovies", "ids": [MOVIE]})
    got = app.jellyfin("add", {"channel": "jellymovies", "ids": [MOVIE, BOXSET]})
    assert got == {"channel": "jellymovies", "added": 2, "skipped": 1, "total": 3}
    assert app.jellyfin("channels", {})["channels"] == [{"name": "jellymovies", "label": "jellymovies", "count": 3}]


def test_what_cannot_be_added_says_why_and_creates_nothing(stack):
    rig, fake = stack
    app = rig.app
    with pytest.raises(ValueError):
        app.jellyfin("add", {"new": "x", "ids": []})
    with pytest.raises(ValueError):
        app.jellyfin("add", {"new": "", "ids": [MOVIE]})                                             # no name
    with pytest.raises(ValueError):
        app.jellyfin("add", {"new": "1.cartoons", "ids": [MOVIE]})                                   # a channel with that name exists
    with pytest.raises(ValueError) as e:
        app.jellyfin("add", {"channel": "1.cartoons", "ids": [MOVIE]})                               # a folder channel is not a Jellyfin one
    assert "isn't one of your Jellyfin channels" in str(e.value)
    with pytest.raises(ValueError):
        app.jellyfin("add", {"new": "bad", "ids": ["../../x"]})
    assert not (rig.cfg.videos / "bad").exists() and not (rig.cfg.videos / "x").exists()
    app.jellyfin("disconnect", {})
    with pytest.raises(ValueError) as e:
        app.jellyfin("add", {"new": "late", "ids": [MOVIE]})
    assert "Connect to your Jellyfin server first" in str(e.value) and not (rig.cfg.videos / "late").exists()


def test_a_channel_lists_and_removes_its_videos(stack):
    rig, fake = stack
    app = rig.app
    app.jellyfin("add", {"new": "jm", "ids": [BOXSET]})
    listing = app.jellyfin("channel", {"channel": "jm"})
    assert listing["count"] == 2 and listing["seconds"] == (80 + 70) * 60
    assert app.jellyfin("remove", {"channel": "jm", "ids": ["8" * 32]}) == {"removed": 1, "left": 1}
    assert [i["title"] for i in app.jellyfin("channel", {"channel": "jm"})["items"]] == ["Film Two"]
    with pytest.raises(ValueError):
        app.jellyfin("channel", {"channel": "1.cartoons"})


# ---- playing them ---------------------------------------------------------------------------------------------------------------
def test_the_library_treats_jellyfin_videos_as_streams_with_addresses_titles_and_lengths(stack):
    rig, fake = stack
    app = rig.app
    app.jellyfin("add", {"new": "jm", "ids": [MOVIE, BOXSET]})
    lib = app.library
    channel = [c for c in lib.channels() if c.name == "jm"][0]
    rels = lib.videos(channel)
    assert rels == sorted(rel_of(i) for i in (MOVIE, "8" * 32, "7" * 32)) and lib.has_videos(channel)
    rel = rel_of(MOVIE)
    assert lib.is_jellyfin(rel) and lib.is_stream(rel) and not lib.is_youtube(rel) and not lib.is_jellyfin("yt-aaaaaaaaaaa")
    url = lib.abspath(channel, rel)
    assert url.startswith(fake.url + f"/Videos/{MOVIE}/stream.mp4?") and f"api_key={fake.token}" in url and "maxHeight=360" in url
    assert lib.title_of(channel, rel) == "Big Film"
    assert lib.stream_seconds(channel)[rel] == 5400.0
    assert lib.in_order(channel, rels) == [rel_of(MOVIE), rel_of("8" * 32), rel_of("7" * 32)]        # the order they were added in
    app.jellyfin("disconnect", {})
    assert lib.abspath(channel, rel) == NOT_CONNECTED                                               # not signed in: fails at once, not a crash


def test_a_jellyfin_channel_plays_through_the_normal_playlist_and_the_playlist_file_is_private(stack):
    rig, fake = stack
    app = rig.app
    app.jellyfin("add", {"new": "jm", "ids": [BOXSET]})
    app.channel_index = [c.name for c in app.channels()].index("jm")
    app._net_waited = True
    app.start_playback()
    assert wait_for(lambda: rig.player.alive())
    text = open(app.m3u_path).read()
    assert f"api_key={fake.token}" in text and text.count("/Videos/") == 2
    assert stat.S_IMODE(os.stat(app.m3u_path).st_mode) == 0o600                                       # the token is in it
    pos = rig.player.get("playlist-pos")
    assert app.playlists.episode_at(app.plan, 1) in (rel_of("8" * 32), rel_of("7" * 32))


def test_a_jellyfin_video_never_resumes_into_the_middle_of_a_stream(stack):
    rig, fake = stack
    app = rig.app
    app.jellyfin("add", {"new": "jm", "ids": [BOXSET]})
    channel = [c for c in app.channels() if c.name == "jm"][0]
    rel = app.library.videos(channel)[0]
    app.playlists.set_progress(channel, rel, 1500.0)
    app.channel_index = [c.name for c in app.channels()].index("jm")
    app._net_waited = True
    app.start_playback()
    assert app._pending_resume is None                                                              # (a transcoded stream can't be sought into)


def test_live_tv_mode_uses_the_lengths_the_server_gave(stack):
    rig, fake = stack
    app = rig.app
    app.jellyfin("add", {"new": "jm", "ids": [MOVIE, BOXSET]})
    app.live_tv = True
    channel = [c for c in app.channels() if c.name == "jm"][0]
    found = app._live_channel(channel)
    assert found is not None and sorted(found[1]) == [70 * 60.0, 80 * 60.0, 90 * 60.0]


def test_the_page_lists_a_jellyfin_channel_with_its_own_kind(stack):
    rig, fake = stack
    rig.app.jellyfin("add", {"new": "jm", "ids": [BOXSET]})
    entry = [c for c in rig.app.web_state()["channels"] if c["name"] == "jm"][0]
    assert entry["kind"] == "jellyfin" and entry["jellyfin"]["count"] == 2 and entry["count"] == 2 and "items" not in entry["jellyfin"]
    assert "kind" not in [c for c in rig.app.web_state()["channels"] if c["name"] == "1.cartoons"][0]


def test_a_failed_jellyfin_video_says_where_the_problem_is(stack, caplog):
    import logging
    rig, fake = stack
    app = rig.app
    app.jellyfin("add", {"new": "jm", "ids": [BOXSET]})
    channel = [c for c in app.channels() if c.name == "jm"][0]
    rel = app.library.videos(channel)[0]
    app.plan = app.playlists.plan(channel, True)
    caplog.set_level(logging.WARNING)
    app._stream_failed({"rel": rel})
    assert "from Jellyfin: Your Jellyfin server wouldn't play it, so it was skipped" in caplog.text
    app.jellyfin("disconnect", {})
    caplog.clear()
    app._stream_failed({"rel": rel})
    assert "Jellyfin isn't connected: sign in on the upload page" in caplog.text
