import json
import time

import pytest

from rig import Rig, http, wait_for
from tubeless import render, wifi
from tubeless.setupweb import check_credentials
from tubeless.wifi import FakeWifi, Network, parse_scan, wifi_qr_text


# ---- pure bits -----------------------------------------------------------------------------------------------
def test_the_wifi_qr_text_escapes_what_phones_need_escaped():
    assert wifi_qr_text("Tubeless-AB12", "pa55word") == "WIFI:T:WPA;S:Tubeless-AB12;P:pa55word;;"
    assert wifi_qr_text('a;b:c', 'x,y"z') == 'WIFI:T:WPA;S:a\\;b\\:c;P:x\\,y\\"z;;'


def test_hotspot_credentials_are_fresh_and_readable():
    a, b = wifi.new_hotspot_credentials(), wifi.new_hotspot_credentials()
    assert a != b
    assert a[0].startswith("Tubeless-") and len(a[0]) == 13 and len(a[1]) == 8
    assert not set(a[1]) & set("01oOlLI")


def test_scan_output_from_the_helper_is_parsed_defensively():
    nets = parse_scan('[{"ssid":"Home","signal":80,"secured":true},{"ssid":"","signal":1},{"nope":1},"x"]')
    assert [(n.ssid, n.signal, n.secured) for n in nets] == [("Home", 80, True)]
    assert parse_scan("not json") == [] and parse_scan("") == [] and parse_scan('{"a":1}') == []


@pytest.mark.parametrize("ssid,pw,ok", [
    ("Home", "correct horse", True), ("Home", "", True), ("", "correct horse", False), ("x" * 33, "correct horse", False),
    ("Home", "short", False), ("Home", "p" * 64, False), ("Home", "a" * 64, True), ("Ho\nme", "correct horse", False),
    (None, "correct horse", False), ("Home", 12345678, False), ("Home", "pässword1", False),
])
def test_credentials_are_checked(ssid, pw, ok):
    assert (check_credentials(ssid, pw) is None) == ok


def test_the_helper_script_parses_nmcli_escapes_and_validates():
    import importlib.machinery as m, importlib.util as u
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "packaging" / "tubeless-wifi-helper"
    loader = m.SourceFileLoader("wifi_helper", str(path))
    h = u.module_from_spec(u.spec_from_loader("wifi_helper", loader)); loader.exec_module(h)
    assert h.split_terse(r"My\:Net:77:WPA2") == ["My:Net", "77", "WPA2"]
    assert h.split_terse(r"back\\slash::") == ["back\\slash", "", ""]
    assert h.valid_psk("hunter22") and h.valid_psk("") and not h.valid_psk("short") and not h.valid_psk("pässword1")
    assert h.valid_ssid("Home") and not h.valid_ssid("") and not h.valid_ssid("a" * 33) and not h.valid_ssid("a\nb")
    assert h.main(["helper", "rm", "-rf"]) == 64                          # anything unexpected is refused
    assert h.main(["helper", "connect"]) == 64


# ---- the screen --------------------------------------------------------------------------------------------------
def test_the_setup_screen_draws():
    from tubeless.hardware import Hardware
    img = render.render_setup(Hardware(), "Den", "Tubeless-AB12", "pa55word", None, "10.42.0.1", ["Waiting..."])
    assert img.size == (640, 480) and img.getbbox() is not None


# ---- the whole app ---------------------------------------------------------------------------------------------------
@pytest.fixture
def fake():
    return FakeWifi(ip=None, saved=False)


@pytest.fixture
def tv(tmp_path, fake):
    r = Rig(tmp_path, wifi=fake, setup_auto=False)
    yield r
    r.stop()


def settings_rows(rig):
    from tubeless.menu import MenuModel
    m = MenuModel(); m.open_page("settings")
    return [i.label for i in m.items(rig.app._menu_ctx())]


def open_setup_from_menu(rig):
    rig.open_menu()
    rig.tap_row(4)                                                           # Settings
    rows = settings_rows(rig)
    rig.swipe(320, 380, 320, 200, duration=0.15)
    assert wait_for(lambda: rig.app.menu.top > 0)
    top = rig.app.menu.top
    rig.tap_row(rows.index("Wi-Fi setup") - top)


def base_of(rig):
    server = rig.app._setup_server
    return f"http://127.0.0.1:{server.port}"


def test_the_menu_only_offers_wifi_setup_where_it_can_work(tmp_path, tv):
    assert "Wi-Fi setup" in settings_rows(tv)
    plain = Rig(tmp_path / "plain")
    try:
        assert "Wi-Fi setup" not in settings_rows(plain)
    finally:
        plain.stop()


def test_wifi_setup_from_the_menu_makes_a_hotspot_and_shows_how_to_join(tv, fake):
    open_setup_from_menu(tv)
    assert wait_for(lambda: tv.state["mode"] == "setup")
    assert not tv.player.alive()
    assert wait_for(lambda: fake.hotspot is not None and tv.app._setup_server is not None)
    info = tv.state["wifi_setup"]
    assert info["ssid"] == fake.hotspot[0] and info["ssid"].startswith("Tubeless-")
    assert "password" not in info                                              # not leaked through the debug state
    status, body = http(base_of(tv), "GET", "/api/wifi/networks")
    assert status == 200 and [n["ssid"] for n in body["networks"]] == ["HomeWiFi", "Neighbour", "CafeOpen"]
    assert body["name"] == "Tubeless"
    status, page = http(base_of(tv), "GET", "/")
    assert status == 200 and b"Wi-Fi" in page


def test_anything_a_phone_probes_is_sent_to_the_setup_page(tv, fake):
    open_setup_from_menu(tv)
    assert wait_for(lambda: tv.app._setup_server is not None)
    import http.client
    for path in ("/generate_204", "/hotspot-detect.html", "/connecttest.txt", "/whatever/else"):
        c = http.client.HTTPConnection("127.0.0.1", tv.app._setup_server.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        assert r.status == 302 and r.getheader("Location") == "http://10.42.0.1/"
        r.read(); c.close()


def test_joining_a_network_with_the_right_password_returns_to_the_tv(tv, fake):
    open_setup_from_menu(tv)
    assert wait_for(lambda: tv.app._setup_server is not None)
    base = base_of(tv)
    status, body = http(base, "POST", "/api/wifi/connect", {"ssid": "HomeWiFi", "password": "correct horse"})
    assert status == 202
    assert wait_for(lambda: ("connect", "HomeWiFi") in fake.calls)
    assert wait_for(lambda: tv.state["mode"] == "play" and tv.player.alive(), timeout=8)
    assert fake.connected_ip() == "192.168.1.50" and fake.hotspot is None
    assert tv.app._setup_server is None and tv.state["wifi_setup"] is None


def test_a_wrong_password_brings_the_hotspot_back_for_another_go(tv, fake):
    open_setup_from_menu(tv)
    assert wait_for(lambda: tv.app._setup_server is not None)
    first = fake.hotspot
    http(base_of(tv), "POST", "/api/wifi/connect", {"ssid": "HomeWiFi", "password": "wrong password"})
    assert wait_for(lambda: ("connect", "HomeWiFi") in fake.calls)
    assert wait_for(lambda: fake.hotspot == first and tv.state["wifi_setup"] and not tv.app.setup["busy"])
    assert tv.state["mode"] == "setup"
    assert "Couldn't join HomeWiFi" in tv.state["wifi_setup"]["status"]
    http(base_of(tv), "POST", "/api/wifi/connect", {"ssid": "HomeWiFi", "password": "correct horse"})   # and a second attempt works
    assert wait_for(lambda: tv.state["mode"] == "play", timeout=8)


def test_bad_requests_to_the_setup_page_are_refused_without_touching_wifi(tv, fake):
    open_setup_from_menu(tv)
    assert wait_for(lambda: tv.app._setup_server is not None)
    base = base_of(tv)
    for body in ({"ssid": "", "password": "x"}, {"ssid": "Home", "password": "short"}, {"ssid": "x" * 40, "password": ""}):
        assert http(base, "POST", "/api/wifi/connect", body)[0] == 400
    assert http(base, "POST", "/api/wifi/connect", raw=b"{not json")[0] == 400
    assert http(base, "POST", "/api/other", {})[0] == 404
    assert not any(c[0] == "connect" for c in fake.calls)


def test_two_connect_requests_at_once_only_join_once(tmp_path):
    fake = FakeWifi(delay=0.5)
    r = Rig(tmp_path, wifi=fake, setup_auto=False)
    try:
        open_setup_from_menu(r)
        assert wait_for(lambda: r.app._setup_server is not None)
        assert http(base_of(r), "POST", "/api/wifi/connect", {"ssid": "HomeWiFi", "password": "correct horse"})[0] == 202
        assert http(base_of(r), "POST", "/api/wifi/connect", {"ssid": "HomeWiFi", "password": "correct horse"})[0] == 409
    finally:
        r.stop()


def test_holding_the_screen_cancels_setup_and_removes_the_hotspot(tv, fake):
    open_setup_from_menu(tv)
    assert wait_for(lambda: fake.hotspot is not None)
    tv.tap(); tv.swipe_left()
    time.sleep(0.2)
    assert tv.state["mode"] == "setup"
    tv.hold()
    assert wait_for(lambda: tv.state["mode"] == "play" and tv.player.alive(), timeout=6)
    assert fake.hotspot is None and tv.app._setup_server is None


def test_the_upload_button_on_a_tv_with_no_network_does_wifi_first_then_the_upload_page(tmp_path, fake):
    from test_modes import button_centre
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, wifi=fake, setup_auto=False)
    try:
        assert r.state["mode"] == "empty"
        r.tap(*button_centre("upload"))
        assert wait_for(lambda: r.state["mode"] == "setup")
        assert wait_for(lambda: r.app._setup_server is not None)
        http(base_of(r), "POST", "/api/wifi/connect", {"ssid": "HomeWiFi", "password": "correct horse"})
        assert wait_for(lambda: r.state["mode"] == "transfer", timeout=8)           # straight on to the upload page
        assert r.state["upload_page"]["pin"]
    finally:
        r.stop()


def test_a_tv_that_has_a_network_goes_straight_to_the_upload_page(tmp_path):
    from test_modes import button_centre
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, wifi=FakeWifi(ip="192.168.1.9", saved=True))
    try:
        r.tap(*button_centre("upload"))
        assert wait_for(lambda: r.state["mode"] == "transfer")
    finally:
        r.stop()


def test_first_boot_with_no_wifi_saved_starts_setup_by_itself(tmp_path, fake):
    r = Rig(tmp_path, wifi=fake)
    try:
        assert wait_for(lambda: r.state["mode"] == "setup", timeout=6)
    finally:
        r.stop()


def test_a_tv_with_wifi_saved_but_out_of_range_keeps_playing(tmp_path):
    r = Rig(tmp_path, wifi=FakeWifi(ip=None, saved=True))
    try:
        time.sleep(1.2)
        assert r.state["mode"] == "play" and r.player.alive()
    finally:
        r.stop()


def test_a_tv_that_is_online_never_starts_setup_by_itself(tmp_path):
    r = Rig(tmp_path, wifi=FakeWifi(ip="192.168.1.9", saved=False))
    try:
        time.sleep(1.2)
        assert r.state["mode"] == "play"
    finally:
        r.stop()


def test_auto_setup_can_be_switched_off(tmp_path, fake):
    r = Rig(tmp_path, wifi=fake, setup_auto=False)
    try:
        time.sleep(1.2)
        assert r.state["mode"] == "play"
    finally:
        r.stop()
