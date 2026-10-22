import logging
import time

from rig import Rig, wait_for
from tubeless import health


def test_throttled_flags_are_put_in_words():
    assert health.decode_throttled(0x0) == {"now": [], "ever": []}
    d = health.decode_throttled(0x50005)
    assert d["now"] == ["UNDER-VOLTAGE NOW", "THROTTLED NOW"]
    assert d["ever"] == ["under-voltage has occurred since boot", "throttling has occurred since boot"]
    assert health.decode_throttled(0x50000)["now"] == []                                # it happened, but not now


def test_the_pis_own_text_is_parsed():
    assert health.parse_throttled("throttled=0x50005\n") == 0x50005 and health.parse_throttled("") is None
    assert health.parse_temp("temp=51.2'C\n") == 51.2 and health.parse_temp("nope") is None
    wireless = ("Inter-| sta-|   Quality        |   Discarded packets\n face | tus | link level noise |  nwid\n"
                " wlan0: 0000   58.  -52.  -256        0\n")
    assert health.parse_wireless(wireless) == -52 and health.parse_wireless(wireless, "eth0") is None
    assert health.parse_wireless("") is None


def test_the_log_line_says_what_matters():
    ok = health.format_line({"throttled": 0, "temp": 51.24, "load": 1.3, "free_mb": 214, "wifi_dbm": -58})
    assert ok == "health: power ok | temp 51.2C | load 1.30 | free 214 MB | wifi -58 dBm"
    now = health.format_line({"throttled": 0x50001, "temp": None, "load": None, "free_mb": None, "wifi_dbm": None})
    assert "POWER: UNDER-VOLTAGE NOW (0x50001)" in now and "no signal reading" in now
    ever = health.format_line({"throttled": 0x10000})
    assert "power ok now, but under-voltage has occurred since boot (0x10000)" in ever
    assert "power unknown" in health.format_line({"throttled": None})
    assert health.is_worrying({"throttled": 0x1}) and not health.is_worrying({"throttled": 0x50000}) and not health.is_worrying({})


def test_the_app_logs_a_note_each_minute_and_warns_when_a_power_problem_appears(tmp_path, monkeypatch, caplog):
    values = iter([0x0, 0x0, 0x50000, 0x50001, 0x50000])                        # fine, fine, it happened, happening now, over
    monkeypatch.setattr(health, "has_vcgencmd", lambda: True)
    monkeypatch.setattr(health, "snapshot", lambda interface="wlan0": {"throttled": next(values, 0x50000), "temp": 50.0, "load": 1.0,
                                                                       "free_mb": 200, "wifi_dbm": -60})
    caplog.set_level(logging.INFO, logger="tubeless.app")
    r = Rig(tmp_path, start=False)
    r.app.health_every = 0.1
    r.app.start_background()
    try:
        assert wait_for(lambda: sum(1 for x in caplog.records if x.getMessage().startswith("health:")) >= 5, timeout=4)
    finally:
        r.stop()
    notes = [(x.levelno, x.getMessage()) for x in caplog.records if x.getMessage().startswith("health:")][:5]
    assert [lvl for lvl, _ in notes] == [logging.INFO, logging.INFO, logging.WARNING, logging.WARNING, logging.INFO]
    assert "power ok" in notes[0][1] and "under-voltage has occurred" in notes[2][1] and "UNDER-VOLTAGE NOW" in notes[3][1]


def test_nothing_is_logged_off_a_pi(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(health, "has_vcgencmd", lambda: False)
    caplog.set_level(logging.INFO, logger="tubeless.app")
    r = Rig(tmp_path, start=False)
    r.app.health_every = 0.05
    r.app.start_background()
    try:
        time.sleep(0.4)
    finally:
        r.stop()
    assert not [x for x in caplog.records if x.getMessage().startswith("health:")]
