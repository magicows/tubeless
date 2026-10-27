import json
import stat

import pytest

from jf_fake import EP1, EP2, LIB, MOVIE, SERIES, USER, FakeJellyfin
from tubeless import jellyfin
from tubeless.jellyfin import Client, Credentials, JellyfinError, normalise_server


@pytest.fixture
def jf():
    server = FakeJellyfin()
    yield server
    server.stop()


def client(jf, **kw):
    return Client(jf.url, device_id="dev1", timeout=5, **kw)


# ---- addresses -----------------------------------------------------------------------------------------------------------------
def test_server_addresses_are_tidied_and_checked():
    assert normalise_server("jelly.example.com") == "https://jelly.example.com"
    assert normalise_server("  https://jelly.example.com/web/index.html#/home ") == "https://jelly.example.com"
    assert normalise_server("http://192.168.1.20:8096/") == "http://192.168.1.20:8096"
    assert normalise_server("example.com/jellyfin/web/") == "https://example.com/jellyfin"
    for bad in ("", "   ", "ftp://x.com", "http://", "https://user:pw@x.com"):
        with pytest.raises(JellyfinError):
            normalise_server(bad)


def test_item_ids_are_checked_and_tokens_are_hidden_in_logs():
    assert jellyfin.valid_item_id("a" * 32) and jellyfin.valid_item_id("12345678-1234-1234-1234-123456789abc")
    for bad in ("", "short", "../../etc", "a" * 31, None, 5):
        assert not jellyfin.valid_item_id(bad)
    assert "secret" not in jellyfin.mask("https://x/Videos/1/stream.mp4?api_key=secret&a=1") and "a=1" in jellyfin.mask("https://x/?api_key=secret&a=1")


# ---- signing in ----------------------------------------------------------------------------------------------------------------
def test_password_sign_in_uses_the_authorization_header_and_returns_only_a_token(jf):
    info = client(jf).public_info()
    assert info == {"name": "Fake", "version": "12.4.0", "id": "srv"}
    c = client(jf)
    got = c.login("sam", "hunter2")
    assert got == {"token": jf.token, "user_id": USER, "user_name": "sam"} and c.token == jf.token
    method, path, query, auth = [r for r in jf.requests if r[1] == "/Users/AuthenticateByName"][0]
    assert auth.startswith('MediaBrowser Client="Tubeless"') and 'DeviceId="dev1"' in auth and "Token=" not in auth        # no token before signing in
    with pytest.raises(JellyfinError) as e:
        client(jf).login("sam", "wrong")
    assert "didn't accept that sign-in" in str(e.value) and e.value.status == 401
    with pytest.raises(JellyfinError):
        client(jf).login("  ", "x")


def test_quick_connect_waits_for_approval_then_signs_in(jf):
    c = client(jf)
    start = c.quick_start()
    assert start == {"secret": jf.secret, "code": "123456"}
    assert c.quick_check(start["secret"]) is False
    with pytest.raises(JellyfinError):
        c.quick_finish(start["secret"])                                    # not approved yet
    jf.approved = True
    assert c.quick_check(start["secret"]) is True
    assert c.quick_finish(start["secret"])["token"] == jf.token
    jf.quick = False
    with pytest.raises(JellyfinError) as e:
        client(jf).quick_start()
    assert "switched off" in str(e.value)


def test_an_unreachable_or_wrong_server_gives_a_plain_message():
    with pytest.raises(JellyfinError) as e:
        Client("http://127.0.0.1:9", timeout=2).public_info()
    assert "Couldn't reach 127.0.0.1:9" in str(e.value)


# ---- browsing ------------------------------------------------------------------------------------------------------------------
def test_libraries_items_search_and_episodes(jf):
    c = client(jf, token=jf.token, user_id=USER)
    assert c.libraries() == [{"id": LIB, "name": "Shows", "kind": "tvshows"}]
    found = c.items()
    assert [i["name"] for i in found["items"]] == ["The Simpsons", "Big Film", "Cartoon Films"] and found["total"] == 3
    assert found["items"][1]["seconds"] == 5400.0 and found["items"][0]["kind"] == "Series" and found["items"][2]["kind"] == "BoxSet"
    assert [i["name"] for i in c.items(search="simp")["items"]] == ["The Simpsons"]
    eps = c.episodes(SERIES)
    assert [(e["id"], e["season"], e["episode"], e["seconds"]) for e in eps] == [(EP1, 1, 1, 1320.0), (EP2, 1, 2, 1300.0)]
    with pytest.raises(JellyfinError):
        c.episodes("../etc")
    with pytest.raises(JellyfinError):
        c.items(parent_id="nope")
    sent = [r for r in jf.requests if r[1] == "/Items"][0]
    assert sent[2]["userId"] == USER and sent[2]["includeItemTypes"] == "Series,Movie,BoxSet,Playlist" and sent[2]["recursive"] == "true"


def test_a_signed_out_token_is_reported_as_such(jf):
    with pytest.raises(JellyfinError) as e:
        client(jf, token="old", user_id=USER).libraries()
    assert "isn't signed in any more" in str(e.value) and e.value.status == 401


# ---- streams -------------------------------------------------------------------------------------------------------------------
def test_stream_addresses_ask_for_small_h264_and_carry_the_token(jf):
    c = client(jf, token=jf.token, user_id=USER)
    url = c.stream_url(MOVIE, 360)
    assert url.startswith(jf.url + f"/Videos/{MOVIE}/stream.mp4?") and "videoCodec=h264" in url and "maxHeight=360" in url
    assert "audioCodec=aac" in url and f"api_key={jf.token}" in url and "static=false" in url and "startTimeTicks" not in url
    assert "videoBitRate=500000" in c.stream_url(MOVIE, 240) and "videoBitRate=1400000" in c.stream_url(MOVIE, 480)
    assert "startTimeTicks=600000000" in c.stream_url(MOVIE, 360, start_seconds=60)
    with pytest.raises(JellyfinError):
        c.stream_url("../../x")


def test_the_stream_test_reports_what_the_server_sent(jf):
    c = client(jf, token=jf.token, user_id=USER)
    got = c.check_stream(MOVIE, seconds=2)
    assert got["ok"] and got["status"] == 200 and got["content_type"] == "video/mp4" and got["bytes"] > 1000
    jf.stream_ok = False
    with pytest.raises(JellyfinError) as e:
        c.check_stream(MOVIE, seconds=2)
    assert "transcoder failed (500)" in str(e.value) and "Logs" in str(e.value)
    with pytest.raises(JellyfinError) as e:
        client(jf, token="wrong", user_id=USER).check_stream(MOVIE, seconds=2)
    assert "token in the address wasn't accepted" in str(e.value)


# ---- the saved sign-in ---------------------------------------------------------------------------------------------------------
def test_only_the_token_is_kept_privately_and_forgetting_keeps_the_device_name(tmp_path):
    cred = Credentials(tmp_path)
    assert not cred.signed_in and len(cred.data["device_id"]) == 32
    device = cred.data["device_id"]
    cred.save(server="https://x.example.com", token="tok", user_id=USER, user_name="sam")
    assert cred.signed_in and stat.S_IMODE(cred.path.stat().st_mode) == 0o600
    again = Credentials(tmp_path)
    assert again.signed_in and again.token == "tok" and again.data["device_id"] == device
    assert "hunter2" not in cred.path.read_text()
    again.forget()
    assert not Credentials(tmp_path).signed_in and Credentials(tmp_path).data["device_id"] == device
    (tmp_path / "jellyfin.json").write_text("not json")
    assert not Credentials(tmp_path).signed_in                                        # a damaged file is just "not signed in"


def test_it_names_itself_so_a_proxy_that_blocks_default_library_agents_lets_it_through(jf):
    import urllib.request
    # the pretend server answers 403 to Python's default agent, like Cloudflare does
    with pytest.raises(Exception) as e:
        urllib.request.urlopen(jf.url + "/System/Info/Public", timeout=5)
    assert "403" in str(e.value)
    c = client(jf)
    assert c.public_info()["version"] == "12.4.0"
    assert c.login("sam", "hunter2")["token"] == jf.token
    assert client(jf, token=jf.token, user_id=USER).check_stream(MOVIE, seconds=2)["ok"]
    assert jellyfin.USER_AGENT.startswith("Tubeless/")


def test_when_the_servers_transcoder_fails_it_tries_gentler_requests_and_says_which_worked(jf):
    c = client(jf, token=jf.token, user_id=USER)
    jf.fail_params = {"requireAvc"}                                    # the usual request fails, a gentler one works
    got = c.check_stream(MOVIE, seconds=2)
    assert got["ok"] and got["variant"].startswith("fewer options") and "how the TV asks" in got["note"]
    jf.fail_params = {"requireAvc", "static"}                          # everything fails (all variants carry `static`)
    with pytest.raises(JellyfinError) as e:
        c.check_stream(MOVIE, seconds=2)
    assert "problem is on the server" in str(e.value)
    jf.fail_params = set()
    assert "note" not in c.check_stream(MOVIE, seconds=2)              # no failure: no note
    assert c.stream_url(MOVIE, 360, variant=2).startswith(jf.url + f"/Videos/{MOVIE}/stream.mkv?") and "requireAvc" not in c.stream_url(MOVIE, 360, variant=1)
