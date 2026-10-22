import json

import pytest

from rig import Rig, http, wait_for
from tubeless import render, themes
from tubeless.hardware import Hardware
from tubeless.menu import MenuContext, MenuModel

HW = Hardware()
CTX = MenuContext(channels=["a", "b", "c"], current_channel=0)


@pytest.fixture(autouse=True)
def classic_again():
    yield
    themes.activate(themes.CLASSIC)                       # the active theme is global; leave it as found


# ---- turning pasted text into colours ---------------------------------------------------------------------------------------
def test_hex_codes_in_any_layout_are_found():
    want = [(38, 70, 83), (42, 157, 143), (233, 196, 106)]
    assert themes.parse_palette("264653 2a9d8f e9c46a") == want
    assert themes.parse_palette("#264653, #2A9D8F, #E9C46A") == want
    assert themes.parse_palette("264653\n2a9d8f\ne9c46a") == want


def test_a_coolors_link_is_understood():
    assert len(themes.parse_palette("https://coolors.co/palette/264653-2a9d8f-e9c46a-f4a261-e76f51")) == 5
    assert len(themes.parse_palette("https://coolors.co/264653-2a9d8f-e9c46a")) == 3


def test_nonsense_and_too_many_colours_are_refused_kindly():
    assert themes.parse_palette("hello there") == []
    with pytest.raises(ValueError):
        themes.make_custom("Bad", "hello there", False, False)
    with pytest.raises(ValueError):
        themes.make_custom("One", "264653", False, False)
    with pytest.raises(ValueError):
        themes.make_custom("", "264653 2a9d8f", False, False)
    with pytest.raises(ValueError):
        themes.make_custom("Big", " ".join("%06x" % (i * 99999) for i in range(11)), False, False)


# ---- the palette becomes a readable theme -----------------------------------------------------------------------------------
@pytest.mark.parametrize("colours,light", [
    ("264653 2a9d8f e9c46a f4a261 e76f51", False), ("264653 2a9d8f e9c46a f4a261 e76f51", True),
    ("ffffff eeeeee dddddd", False), ("000000 111111 222222", True), ("ff0000 00ff00", False), ("808080 888888 8c8c8c", True),
])
def test_text_and_accent_are_always_readable(colours, light):
    for brutal in (False, True):
        t = themes.from_palette("x", "x", themes.parse_palette(colours), light=light, brutal=brutal)
        assert themes.contrast(t.text, t.bg) >= 7.0
        assert themes.contrast(t.accent, t.bg) >= 3.0 or t.accent_text == t.text
        assert themes.contrast(t.on_accent, t.accent) >= 4.5
        assert themes.contrast(t.dim, t.bg) >= 4.4


def test_every_built_in_theme_is_readable_and_has_a_swatch_strip():
    for tid, t in themes.built_in().items():
        assert themes.contrast(t.text, t.panel) >= 4.5, tid
        assert themes.contrast(t.on_accent, t.accent) >= 4.5, tid
        assert len(t.to_dict()["swatches"]) == 4 and t.css().count("--") >= 0
    assert themes.built_in()["classic"].css() == ""                       # the page keeps its own look
    assert "--accent:" in themes.built_in()["neobrutalist"].css()


def test_a_custom_palette_round_trips_through_its_stored_form():
    entry = themes.make_custom("Ocean", "264653 2a9d8f e9c46a", False, True)
    assert entry["id"] == "c-ocean" and entry["brutal"] is True
    t = themes.build_custom(json.loads(json.dumps(entry)))
    assert t is not None and t.brutal
    assert themes.make_custom("Ocean", "264653 2a9d8f", False, False, taken=["c-ocean"])["id"] == "c-ocean-2"
    assert themes.build_custom({"id": "x", "name": "x", "colours": ["zzz"]}) is None            # a damaged entry is skipped, not fatal
    assert "c-ocean" in themes.all_themes([entry]) and len(themes.all_themes([{"junk": 1}])) == len(themes.built_in())


# ---- drawing ----------------------------------------------------------------------------------------------------------------
def _menu(theme_id, **kw):
    themes.activate(themes.built_in()[theme_id])
    return render.render_menu_landscape(MenuModel().items(CTX), 0, HW, title="Den", subtitle="Cartoons", **kw)


def test_each_theme_draws_a_different_menu():
    seen = {tid: _menu(tid).tobytes() for tid in themes.built_in()}
    assert len(set(seen.values())) == len(seen)


def test_the_neobrutalist_menu_has_hard_edges_and_stays_inside_the_bezel():
    img = _menu("neobrutalist")
    x0, y0, x1, y1 = img.getchannel("A").getbbox()
    assert 40 < x0 and x1 < 600 and 25 < y0 and y1 < 450
    px = img.load()
    cx, cy = HW.bezel_center
    # the panel's top-left corner is square: the pixel at the panel corner is solid ink, not transparent (a rounded corner would be)
    corner = (int(cx + (render.PANEL_RECT[0] + 2 - 320) * HW.bezel_scale), int(cy + (render.PANEL_RECT[1] + 2 - 240) * HW.bezel_scale))
    assert px[corner][3] > 200


def test_the_info_screens_follow_the_theme():
    names = {}
    for tid in ("classic", "neobrutalist", "nord"):
        themes.activate(themes.built_in()[tid])
        names[tid] = [render.render_transfer(HW, "Den", ["http://x:8080"], "1234", None, ["hi"], 0.5).tobytes(),
                      render.render_empty(HW, "Den", "~/videos").tobytes(),
                      render.render_message(HW, "SYSTEM HALT", ["bye"], "Den").tobytes(),
                      render.render_calibration(HW, 0, 5, (100, 100)).tobytes(),
                      render.render_setup(HW, "Den", "net", "pw", None, "10.42.0.1", []).tobytes()]
    for i in range(5):
        assert len({names[t][i] for t in names}) == 3


def test_the_classic_menu_is_unchanged_by_the_theme_work():
    img = _menu("classic")
    px = img.load()
    cx, cy = HW.bezel_center
    # the selected first row is the classic yellow
    probe = (int(cx + (60 - 320) * HW.bezel_scale), int(cy + (render.ROWS_Y0 + 10 - 240) * HW.bezel_scale))
    assert px[probe][:3] == (254, 217, 15)


# ---- the app and the web page -----------------------------------------------------------------------------------------------
def test_choosing_a_theme_is_saved_applied_and_survives_a_restart(tmp_path):
    r = Rig(tmp_path)
    try:
        assert r.app.theme_id == "classic" and r.app.web_state()["theme"] == "classic"
        r.app.set_web_setting("theme", "neobrutalist")
        assert themes.active().id == "neobrutalist" and r.app.theme_style() == "brutal" and "--accent" in r.app.theme_css()
        with pytest.raises(ValueError):
            r.app.set_web_setting("theme", "no-such-theme")
        assert wait_for(lambda: json.loads((r.cfg.data / "settings.json").read_text()).get("theme") == "neobrutalist")
    finally:
        r.stop()
    r2 = Rig(tmp_path, start=False)
    assert r2.app.theme_id == "neobrutalist" and themes.active().id == "neobrutalist"


def test_adding_and_removing_a_palette(tmp_path):
    r = Rig(tmp_path)
    try:
        entry = r.app.add_palette("Ocean", "https://coolors.co/palette/264653-2a9d8f-e9c46a", False, False)
        assert r.app.theme_id == entry["id"] and themes.active().id == entry["id"]
        listed = {t["id"]: t for t in r.app.web_state()["themes"]}
        assert listed[entry["id"]]["custom"] is True and listed["classic"]["custom"] is False
        with pytest.raises(ValueError):
            r.app.delete_palette("classic")                       # only your own palettes can be removed
        r.app.delete_palette(entry["id"])
        assert r.app.theme_id == "classic" and themes.active().id == "classic"
        for i in range(themes.MAX_CUSTOM):
            r.app.add_palette(f"P{i}", "264653 2a9d8f", False, False)
        with pytest.raises(ValueError):
            r.app.add_palette("One too many", "264653 2a9d8f", False, False)
    finally:
        r.stop()


def test_a_deleted_or_unknown_theme_falls_back_to_classic(tmp_path):
    r = Rig(tmp_path)
    try:
        r.app.settings.set(theme="gone")
    finally:
        r.stop()
    assert wait_for(lambda: True)
    assert Rig(tmp_path, start=False).app.theme_id == "classic"
