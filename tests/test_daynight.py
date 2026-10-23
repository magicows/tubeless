import json
import time
from datetime import datetime, timezone

import pytest

from rig import Rig, wait_for
from tubeless import daynight
from tubeless.daynight import Night

LEVELS = (25, 50, 75, 100)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def hm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")


def minutes(text):
    return daynight.parse_clock(text)


# ---- sunrise and sunset -------------------------------------------------------------------------------------------------------
def test_the_sun_is_where_it_should_be_on_known_days():
    ev = dict((k, ts) for ts, k in daynight.sun_events(utc(2026, 3, 20, 12), 0, 0) if datetime.fromtimestamp(ts, timezone.utc).day == 20)
    assert abs(minutes(hm(ev["rise"])) - minutes("06:04")) <= 10 and abs(minutes(hm(ev["set"])) - minutes("18:10")) <= 10   # equator, equinox
    # New York, midsummer: sunrise about 05:25 and sunset about 20:30 local (EDT, UTC-4), which is just after midnight UTC
    ny = daynight.sun_events(utc(2026, 6, 21, 12), 40.71, -74.0)
    assert any(k == "rise" and abs(minutes(hm(ts)) - minutes("09:25")) <= 12 for ts, k in ny)
    assert any(k == "set" and abs(minutes(hm(ts)) - minutes("00:30")) <= 12 for ts, k in ny)


def test_the_midnight_sun_and_the_polar_night_have_no_events():
    assert daynight.sun_events(utc(2026, 6, 21, 12), 69.65, 18.96) == []              # Tromso in June
    assert daynight.sun_events(utc(2026, 12, 21, 12), 69.65, 18.96) == []             # and in December


# ---- the fade -----------------------------------------------------------------------------------------------------------------
def test_by_day_full_brightness_by_night_the_night_level_and_a_fade_between():
    n = Night(enabled=True, location=(51.5, -0.12))                         # London, 23 Oct 2026: sunrise ~06:37 UTC, sunset ~16:51 UTC
    assert n.level_at(100, utc(2026, 10, 23, 12)) == 100
    assert n.level_at(100, utc(2026, 10, 23, 20)) == 25 and n.level_at(100, utc(2026, 10, 23, 3)) == 25
    dusk = [n.level_at(100, utc(2026, 10, 23, 16, m)) for m in (51, 56, 59)] + [n.level_at(100, utc(2026, 10, 23, 17, m)) for m in (5, 15, 25)]
    assert dusk == sorted(dusk, reverse=True) and 25 < dusk[1] < 100                  # a smooth way down, not a step
    dawn = [n.level_at(100, utc(2026, 10, 23, 6, m)) for m in (0, 10, 20, 30, 37)]
    assert dawn == sorted(dawn) and dawn[-1] > 90 and dawn[0] == 25


def test_it_never_goes_brighter_than_the_day_level():
    n = Night(enabled=True, location=(51.5, -0.12), level=75)
    assert n.level_at(25, utc(2026, 10, 23, 20)) == 25                                 # day level 25, night 75: stays at 25
    assert Night(enabled=False, location=(51.5, -0.12)).level_at(100, utc(2026, 10, 23, 20)) == 100


def test_set_times_across_midnight_and_the_fade():
    n = Night(enabled=True, mode="times", start=minutes("21:00"), end=minutes("07:00"))
    local = lambda h, m=0: datetime(2026, 10, 23, h, m).timestamp()
    nextday = lambda h, m=0: datetime(2026, 10, 24, h, m).timestamp()
    assert n.level_at(100, local(12)) == 100 and n.level_at(100, local(23)) == 25 and n.level_at(100, nextday(3)) == 25
    assert n.level_at(100, nextday(8)) == 100
    assert 25 < n.level_at(100, local(21, 10)) < 100 and 25 < n.level_at(100, nextday(6, 40)) < 100
    same_day = Night(enabled=True, mode="times", start=minutes("13:00"), end=minutes("15:00"))      # a window inside one day works too
    assert same_day.level_at(100, local(14)) == 25 and same_day.level_at(100, local(16)) == 100


def test_no_location_or_a_polar_place_falls_back_to_the_set_times():
    local = lambda h: datetime(2026, 6, 21, h).timestamp()
    no_loc = Night(enabled=True, mode="sun", location=None, start=minutes("22:00"), end=minutes("06:00"))
    assert not no_loc.uses_sun and no_loc.level_at(100, local(23)) == 25 and no_loc.level_at(100, local(12)) == 100
    polar = Night(enabled=True, mode="sun", location=(69.65, 18.96), start=minutes("22:00"), end=minutes("06:00"))
    assert polar.level_at(100, local(23)) == 25 and polar.level_at(100, local(12)) == 100


# ---- parsing --------------------------------------------------------------------------------------------------------------------
def test_locations_and_clock_times_are_parsed_and_refused_kindly():
    assert daynight.parse_location("51.5072, -0.1276") == (51.5072, -0.1276)
    assert daynight.parse_location("51.5072 -0.1276") == (51.5072, -0.1276)
    assert daynight.parse_location("  ") is None
    for bad in ("London", "51.5", "91, 0", "0, 181", "1, 2, 3"):
        with pytest.raises(ValueError):
            daynight.parse_location(bad)
    assert daynight.parse_clock("07:05") == 425 and daynight.format_clock(425) == "07:05"
    for bad in ("7", "25:00", "12:60", "noon", ""):
        with pytest.raises(ValueError):
            daynight.parse_clock(bad)


def test_damaged_settings_fall_back_to_the_defaults():
    n = Night.from_settings({"night_mode": "yes", "night_brightness": 33, "night_schedule": "moon", "night_location": "x",
                             "night_from": "zz", "night_to": 5}.get, LEVELS)
    assert n.enabled is False and n.level == 25 and n.mode == "sun" and n.location is None and n.start == minutes("21:00")


# ---- in the app -----------------------------------------------------------------------------------------------------------------
def test_it_is_off_unless_switched_on_and_the_backlight_follows_it(tmp_path):
    r = Rig(tmp_path)
    try:
        app = r.app
        assert app.night.enabled is False and app.current_brightness() == 100
        app.set_web_setting("night_schedule", "times")
        now = datetime.now()
        app.set_web_setting("night_from", "%02d:%02d" % ((now.hour + 23) % 24, now.minute))      # the night began an hour ago ...
        app.set_web_setting("night_to", "%02d:%02d" % ((now.hour + 5) % 24, now.minute))         # ... and ends in five
        assert app.current_brightness() == 100                                                    # still off
        app.set_web_setting("night_mode", True)
        assert wait_for(lambda: r.backlight.percent == 25)                                        # the screen dims at once
        assert app.web_settings()["night"]["night"] is True and app.web_settings()["values"]["night_mode"] is True
        app.set_web_setting("night_mode", False)
        assert wait_for(lambda: r.backlight.percent == 100)
    finally:
        r.stop()


def test_the_menu_brightness_row_changes_the_level_for_the_time_of_day_it_is(tmp_path):
    r = Rig(tmp_path)
    try:
        app = r.app
        now = datetime.now()
        app.set_web_setting("night_schedule", "times")
        app.set_web_setting("night_from", "%02d:%02d" % ((now.hour + 23) % 24, now.minute))
        app.set_web_setting("night_to", "%02d:%02d" % ((now.hour + 5) % 24, now.minute))
        app.set_web_setting("night_mode", True)
        app.cycle_brightness()                                                                    # at night: steps the night level
        assert app.night.level == 50 and app.brightness == 100 and app.current_brightness() == 50
        app.set_web_setting("night_mode", False)
        app.cycle_brightness()                                                                    # by day: steps the daytime level
        assert app.brightness == 25 and app.night.level == 50
    finally:
        r.stop()


def test_settings_are_validated_saved_and_survive_a_restart(tmp_path):
    r = Rig(tmp_path)
    try:
        app = r.app
        with pytest.raises(ValueError):
            app.set_web_setting("night_location", "somewhere nice")
        with pytest.raises(ValueError):
            app.set_web_setting("night_from", "late")
        with pytest.raises(ValueError):
            app.set_web_setting("night_mode", "yes")
        with pytest.raises(ValueError):
            app.set_web_setting("night_brightness", 33)
        app.set_web_setting("night_location", "51.5072, -0.1276")
        app.set_web_setting("night_brightness", 50)
        app.set_web_setting("night_mode", True)
        assert wait_for(lambda: json.loads((r.cfg.data / "settings.json").read_text()).get("night_mode") is True)
        saved = json.loads((r.cfg.data / "settings.json").read_text())
        assert saved["night_location"] == [51.5072, -0.1276] and saved["night_brightness"] == 50
        assert app.web_settings()["values"]["night_location"] == "51.5072, -0.1276"
    finally:
        r.stop()
    again = Rig(tmp_path, start=False).app
    assert again.night.enabled and again.night.level == 50 and again.night.location == (51.5072, -0.1276)


def test_the_boot_screen_starts_at_the_night_level_when_it_is_night(tmp_path):
    data = {"brightness": 100, "night_mode": True, "night_schedule": "times", "night_from": "00:00", "night_to": "23:59"}
    assert daynight.level_from_settings(data, LEVELS, now=datetime(2026, 10, 23, 12).timestamp()) == 25
    assert daynight.level_from_settings(dict(data, night_mode=False), LEVELS) == 100
    assert daynight.level_from_settings({}, LEVELS) == 100


# ---- only when the clock can be trusted ---------------------------------------------------------------------------------------------
def _night_now(app):
    now = datetime.now()
    app.set_web_setting("night_schedule", "times")
    app.set_web_setting("night_from", "%02d:%02d" % ((now.hour + 23) % 24, now.minute))
    app.set_web_setting("night_to", "%02d:%02d" % ((now.hour + 5) % 24, now.minute))
    app.set_web_setting("night_mode", True)


def test_it_waits_while_the_clock_has_not_been_set_from_the_network(tmp_path):
    r = Rig(tmp_path)
    try:
        app = r.app
        synced = {"yes": False}
        app.clock_synced = lambda: synced["yes"]
        app._clock_ok, app._clock_ok_until = False, 0.0
        _night_now(app)
        assert app.current_brightness() == 100 and r.backlight.percent == 100                  # night, but the time is not to be believed
        assert app.web_settings()["night"]["clock"] is False
        app.cycle_brightness()                                                                  # so the menu steps the daytime level
        assert app.brightness == 25 and app.night.level == 25
        synced["yes"] = True
        app._clock_ok_until = 0.0                                                               # (the answer is cached for half a minute)
        assert app.current_brightness() == min(app.brightness, 25)
        assert app.web_settings()["night"]["clock"] is True
        synced["yes"] = False                                                                   # once trusted, trusted for the boot
        app._clock_ok_until = 0.0
        assert app._clock_trusted(time.time()) is True
    finally:
        r.stop()


def test_the_boot_screen_does_not_dim_on_an_unset_clock():
    data = {"brightness": 100, "night_mode": True, "night_schedule": "times", "night_from": "00:00", "night_to": "23:59"}
    noon = datetime(2026, 10, 23, 12).timestamp()
    assert daynight.level_from_settings(data, LEVELS, now=noon, synced=False) == 100
    assert daynight.level_from_settings(data, LEVELS, now=noon, synced=True) == 25


def test_clock_synced_reads_what_the_system_says(monkeypatch):
    import subprocess

    def fake(output, code=0):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=output, stderr="")
    monkeypatch.setattr(daynight.subprocess, "run", fake("yes\n"))
    assert daynight.clock_synced() is True
    monkeypatch.setattr(daynight.subprocess, "run", fake("no\n"))
    assert daynight.clock_synced() is False
    monkeypatch.setattr(daynight.subprocess, "run", fake("", 1))
    assert daynight.clock_synced() is False                                                    # it could not be asked: not trusted

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(daynight.subprocess, "run", missing)
    assert daynight.clock_synced() is True                                                     # no timedatectl (a desktop): trust the clock


# ---- a place to start from: the time zone's main city -----------------------------------------------------------------------------
def test_the_time_zone_tables_give_a_rough_location(tmp_path):
    (tmp_path / "zone.tab").write_text("# comment\nGB\t+513030-0000731\tEurope/London\nUS\t+404251-0740023\tAmerica/New_York\tEastern\n"
                                       "AU\t-3352+15113\tAustralia/Sydney\tNew South Wales\n")
    assert daynight.timezone_location("Europe/London", str(tmp_path)) == ("Europe/London", (51.5083, -0.1253))
    assert daynight.timezone_location("America/New_York", str(tmp_path))[1] == (40.7142, -74.0064)
    assert daynight.timezone_location("Australia/Sydney", str(tmp_path))[1] == (-33.8667, 151.2167)        # south and east of the old short form
    assert daynight.timezone_location("Mars/Olympus", str(tmp_path)) is None
    assert daynight.timezone_location("Europe/London", str(tmp_path / "nowhere")) is None


def test_the_systems_time_zone_name_is_read_from_its_link(tmp_path):
    (tmp_path / "zoneinfo" / "Europe").mkdir(parents=True)
    (tmp_path / "zoneinfo" / "Europe" / "London").write_text("x")
    (tmp_path / "localtime").symlink_to(tmp_path / "zoneinfo" / "Europe" / "London")
    assert daynight.system_timezone(str(tmp_path / "localtime")) == "Europe/London"
    assert daynight.system_timezone(str(tmp_path / "missing")) == ""
