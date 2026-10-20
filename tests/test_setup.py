import threading
import json
import time
from pathlib import Path

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


def test_upload_waits_for_a_saved_network_that_is_still_connecting_after_boot(tmp_path):
    from test_modes import button_centre
    wifi = FakeWifi(ip=None, saved=True)
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, wifi=wifi, setup_auto=False)
    try:
        threading.Timer(0.2, lambda: setattr(wifi, "ip", "192.168.1.50")).start()          # the saved network connects a moment later
        r.tap(*button_centre("upload"))
        assert wait_for(lambda: r.state["mode"] == "transfer", timeout=4)                 # straight to the upload page, no hotspot
        assert ("hotspot_up",) not in [c[:1] for c in wifi.calls]
    finally:
        r.stop()


def test_upload_offers_setup_if_the_saved_network_never_connects(tmp_path):
    from test_modes import button_centre
    never = FakeWifi(ip=None, saved=True)
    never.recovers = False                                                       # not even a chip reload brings it back
    r = Rig(tmp_path, channels={"1.nothing": ["notes.txt"]}, wifi=never, setup_auto=False)
    try:
        r.tap(*button_centre("upload"))
        assert wait_for(lambda: r.state["mode"] == "setup", timeout=4)                    # after the short wait
    finally:
        r.stop()


# ---- a crashed Wi-Fi chip is brought back ---------------------------------------------------------------------------------
def calls_of(wifi, name):
    return [c for c in wifi.calls if c[0] == name]


def test_when_the_network_vanishes_the_wifi_chip_is_reloaded_once_and_the_network_returns(tmp_path):
    wifi = FakeWifi(ip="192.168.1.9", saved=True)
    r = Rig(tmp_path, wifi=wifi, setup_auto=False)
    try:
        time.sleep(0.5)
        assert calls_of(wifi, "recover") == []                                    # a healthy network is left alone
        wifi.ip = None                                                            # the chip crashes
        assert wait_for(lambda: len(calls_of(wifi, "recover")) == 1, timeout=4)
        assert wait_for(lambda: wifi.connected_ip() is not None)                  # and recovery brought it back
        time.sleep(1.0)
        assert len(calls_of(wifi, "recover")) == 1                                # it does not keep poking a working network
    finally:
        r.stop()


def test_a_recovery_that_fails_is_retried_later_not_in_a_tight_loop(tmp_path):
    wifi = FakeWifi(ip=None, saved=True)
    wifi.recovers = False
    r = Rig(tmp_path, wifi=wifi, setup_auto=False)
    try:
        assert wait_for(lambda: len(calls_of(wifi, "recover")) == 1, timeout=4)
        time.sleep(0.5)
        assert len(calls_of(wifi, "recover")) == 1                                # the wait between tries is respected (0.8 s here)
        assert wait_for(lambda: len(calls_of(wifi, "recover")) == 2, timeout=4)
    finally:
        r.stop()


def test_no_chip_reload_without_a_saved_network_or_while_setup_is_running(tmp_path):
    wifi = FakeWifi(ip=None, saved=False)                                         # never had Wi-Fi set up: nothing to recover
    r = Rig(tmp_path, wifi=wifi, setup_auto=False)
    try:
        time.sleep(1.2)
        assert calls_of(wifi, "recover") == []
        r.app.mode = "setup"                                                      # the TV is hosting its own hotspot
        wifi.saved = True
        time.sleep(1.2)
        assert calls_of(wifi, "recover") == []
    finally:
        r.stop()


# ---- the root helper's `recover` ----------------------------------------------------------------------------------------
def load_helper():
    import importlib.machinery as m, importlib.util as u
    path = Path(__file__).resolve().parent.parent / "packaging" / "tubeless-wifi-helper"
    loader = m.SourceFileLoader("wifi_helper", str(path))
    h = u.module_from_spec(u.spec_from_loader("wifi_helper", loader))
    loader.exec_module(h)
    return h


def fake_sysfs(tmp_path, wifi_has_type=True):
    """/sys/class/mmc_host with the Pi's two hosts: mmc0 (the SD card the system boots from) and mmc1 (the Wi-Fi chip)."""
    root = tmp_path / "sys"
    for host, dev, drv in (("mmc0", "3f202000.mmc", "sdhost-bcm2835"), ("mmc1", "3f300000.mmcnr", "mmc-bcm2835")):
        (root / "devices" / dev).mkdir(parents=True)
        (root / "drivers" / drv).mkdir(parents=True)
        for f in ("bind", "unbind"):
            (root / "drivers" / drv / f).write_text("")
        (root / "devices" / dev / "driver").symlink_to(root / "drivers" / drv)
        (root / "class" / host).mkdir(parents=True)
        (root / "class" / host / "device").symlink_to(root / "devices" / dev)
    (root / "class" / "mmc0" / "mmc0:aaaa" / "block").mkdir(parents=True)               # the SD card has a disk on it
    (root / "class" / "mmc1" / "mmc1:0001").mkdir(parents=True)
    if wifi_has_type:
        (root / "class" / "mmc1" / "mmc1:0001" / "type").write_text("SDIO\n")
    return root


def test_the_helper_finds_the_wifi_chips_controller_and_never_the_sd_cards(tmp_path):
    h = load_helper()
    root = fake_sysfs(tmp_path)
    device, driver = h.sdio_host(str(root / "class"))
    assert device == "3f300000.mmcnr" and driver.endswith("drivers/mmc-bcm2835")
    root2 = fake_sysfs(tmp_path / "b", wifi_has_type=False)                              # the chip is dead: no card type to read
    assert h.sdio_host(str(root2 / "class"))[0] == "3f300000.mmcnr"                         # still found: it is the host with no disk
    only_disk = tmp_path / "c"
    (only_disk / "class" / "mmc0" / "mmc0:aaaa" / "block").mkdir(parents=True)
    assert h.sdio_host(str(only_disk / "class")) is None                                  # nothing but the boot SD card: touch nothing
    assert h.sdio_host(str(tmp_path / "missing")) is None


def test_the_helper_reloads_the_driver_and_reseats_the_bus_in_order(tmp_path, monkeypatch):
    h = load_helper()
    root = fake_sysfs(tmp_path)
    ran = []
    monkeypatch.setattr(h.subprocess, "run", lambda cmd, **kw: (ran.append(cmd), h.subprocess.CompletedProcess(cmd, 0, "", ""))[1])
    monkeypatch.setattr(h, "wifi_device", lambda: "wlan0")
    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    assert h.cmd_recover(str(root / "class"), settle=0, wait_for_device=2) == 0
    modprobes = [c[-2:] if c[-2] == "-r" else c[-1:] for c in ran if "modprobe" in c[0]]
    assert modprobes == [["-r", "brcmfmac_wcc"], ["-r", "brcmfmac_cyw"], ["-r", "brcmfmac_bca"], ["-r", "brcmfmac"], ["brcmfmac"]]
    drv = root / "drivers" / "mmc-bcm2835"
    assert (drv / "unbind").read_text() == "3f300000.mmcnr" and (drv / "bind").read_text() == "3f300000.mmcnr"
    assert (root / "drivers" / "sdhost-bcm2835" / "unbind").read_text() == ""             # the boot SD card's controller was not touched
    assert ["/usr/bin/nmcli", "radio", "wifi", "on"] in ran


def test_the_helper_says_so_when_the_chip_does_not_come_back_or_cannot_be_found(tmp_path, monkeypatch, capsys):
    h = load_helper()
    root = fake_sysfs(tmp_path)
    monkeypatch.setattr(h.subprocess, "run", lambda cmd, **kw: h.subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(h, "wifi_device", lambda: None)
    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    assert h.cmd_recover(str(root / "class"), settle=0, wait_for_device=2) == 1
    assert h.cmd_recover(str(tmp_path / "nothing"), settle=0, wait_for_device=1) == 3
    assert h.main(["helper", "recover", "extra"]) == 64                                     # no arguments are accepted
