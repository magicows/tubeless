import numpy as np

from tubeless import render
from tubeless.hardware import Hardware
from tubeless.menu import MenuContext, MenuModel, next_step

HW = Hardware()
CTX = MenuContext(channels=["simpsons", "nights", "peep", "catdog", "arnold", "friends", "kaything", "office"],
                  current_channel=1)


def to_view(dx, dy):
    """Drawing-space point -> where it ends up on the landscape view (after shrink + centre)."""
    cx, cy = HW.bezel_center
    return cx + (dx - 320) * HW.bezel_scale, cy + (dy - 240) * HW.bezel_scale


def test_every_page_renders_at_full_size():
    for page in ("main", "settings"):
        m = MenuModel()
        m.open_page(page)
        items = m.items(CTX)
        img = render.render_menu_landscape(items, 0, HW, title="Test", subtitle="channel")
        assert img.size == (640, 480) and img.mode == "RGBA"
        assert img.getbbox() is not None


def test_every_icon_draws_something():
    from PIL import Image, ImageDraw
    for kind in ("play", "pause", "next", "prev", "shuffle", "sun", "tv", "gear", "back", "power",
                 "moon", "target", "reshuffle"):
        img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
        render._icon(img, ImageDraw.Draw(img), kind, 50, 50, (255, 255, 255, 255))
        assert img.getbbox() is not None, kind


def test_hit_test_matches_the_drawn_rows():
    total = 13
    for top in (0, 3, 7):
        for n in range(min(total - top, render.VISIBLE_ROWS)):
            vx, vy = to_view(300, render.ROWS_Y0 + n * (render.ROW_H + render.ROW_GAP) + render.ROW_H / 2)
            assert render.hit_test(vx, vy, top, total, HW) == top + n


def test_hit_test_outside_header_footer():
    assert render.hit_test(5, 5, 0, 13, HW) == render.OUTSIDE
    assert render.hit_test(*to_view(300, 40), 0, 13, HW) is None
    assert render.hit_test(*to_view(300, 440), 0, 13, HW) is None
    assert render.hit_test(*to_view(300, 70), 0, 13, HW) is None


def test_header_buttons_are_hit_and_do_not_overlap_the_rows_or_each_other():
    rects = {n: render.button_rect(n) for n in render.HEADER_BUTTONS}
    assert rects["upload"][2] < rects["power"][0]                                        # left to right, a gap between
    for name, (x0, y0, x1, y1) in rects.items():
        assert render.header_hit(*to_view((x0 + x1) / 2, (y0 + y1) / 2), HW) == name
        assert y1 < render.ROWS_Y0 - 4 and x1 <= render.PANEL_RECT[2]
        assert render.hit_test(*to_view((x0 + x1) / 2, (y0 + y1) / 2), 0, 13, HW) is None   # not a row
    assert render.header_hit(*to_view(300, 150), HW) is None                             # a row is not a button
    assert render.header_hit(*to_view(60, 40), HW) is None                               # nor is the title


def test_title_then_folder_then_buttons_in_the_header():
    from PIL import Image
    plain = render.render_menu_landscape(MenuModel().items(CTX), 0, HW, title="Simpsons", subtitle="Cartoons")
    armed = render.render_menu_landscape(MenuModel().items(CTX), 0, HW, title="Simpsons", subtitle="Cartoons", armed=("power",))
    assert plain.tobytes() != armed.tobytes()                                            # an armed button looks different
    x0, y0, x1, y1 = render.button_rect("power")
    a = np.asarray(plain.convert("RGBA"))
    cx, cy = HW.bezel_center
    px, py = int(cx + ((x0 + x1) / 2 - 320) * HW.bezel_scale), int(cy + ((y0 + y1) / 2 - 240) * HW.bezel_scale)
    assert a[py, px, 3] > 0                                                              # the button is drawn there
    # nothing but the buttons is drawn right of the folder name: the right edge of the header (left of the first button) holds no text
    left_of_buttons = int(cx + (render.button_rect("upload")[0] - 12 - 320) * HW.bezel_scale)
    title_band = np.asarray(plain.convert("RGB"))[int(cy + (40 - 240) * HW.bezel_scale), :, :]
    assert title_band[left_of_buttons - 3:left_of_buttons].std() < 40                    # flat panel colour, no text running under a button


def test_menu_is_inside_the_bezel_opening():
    img = render.render_menu_landscape(MenuModel().items(CTX), 0, HW)
    x0, y0, x1, y1 = img.getchannel("A").getbbox()
    assert 40 < x0 and x1 < 600 and 25 < y0 and y1 < 450


def test_panel_bytes_round_trip_and_are_premultiplied():
    img = render.render_menu_landscape(MenuModel().items(CTX), 1, HW)
    raw, w, h = render.to_panel_bgra(img, HW)
    assert (w, h) == (480, 640) and len(raw) == 480 * 640 * 4
    back = render.from_panel_bgra(raw, w, h, HW)
    assert back.shape == (480, 640, 4)
    src = np.asarray(img, dtype=np.uint16)
    a = src[..., 3]
    want_b = (src[..., 2] * a + 127) // 255
    assert np.abs(back[..., 0].astype(int) - want_b.astype(int)).max() <= 1        # PIL and numpy may round differently
    assert np.array_equal(back[..., 3].astype(np.uint16), a)


def test_panel_orientation_is_a_quarter_turn_counter_clockwise():
    """A mark in the landscape top-left must land at the panel's bottom-left (up points left)."""
    from PIL import Image
    img = Image.new("RGBA", (640, 480), (0, 0, 0, 0))
    img.putpixel((5, 5), (255, 0, 0, 255))
    raw, w, h = render.to_panel_bgra(img, HW)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    ys, xs = np.nonzero(arr[..., 3])
    assert (xs[0], ys[0]) == (5, 640 - 1 - 5)


def test_calibration_and_message_screens():
    assert len(render.calibration_targets(HW)) == 5
    for i, t in enumerate(render.calibration_targets(HW)):
        img = render.render_calibration(HW, i, 5, t, name="Test")
        assert img.size == (640, 480) and img.getpixel((0, 0))[3] == 255          # opaque
    assert render.render_message(HW, "Shutting down", ["bye"]).getbbox() is not None


def test_menu_items_per_page():
    m = MenuModel()
    main = m.items(CTX)
    assert [i.action[0] for i in main[:5]] == ["playpause", "next", "prev", "brightness", "page"]
    assert [i.label for i in main[5:]] == CTX.channels
    assert main[6].current and not main[5].current
    m.open_page("settings")
    settings = m.items(CTX)
    assert [i.action[0] for i in settings] == ["page", "shuffle", "reshuffle", "sleep_timer", "auto_dim", "calibrate", "transfer", "shutdown"]
    assert m.items(MenuContext(confirm_shutdown=True))[-1].label == "Tap again to confirm"


def test_next_step_cycles():
    assert next_step((0, 15, 30), 0) == 15
    assert next_step((0, 15, 30), 30) == 0
    assert next_step((25, 50, 75, 100), 100) == 25


def test_bear_with_us_card_fills_the_screen():
    img = render.render_bear_with_us(HW)
    assert img.size == (HW.view_w, HW.view_h)


def test_standby_picture_is_fitted_to_the_screen_and_falls_back_to_the_bear(tmp_path):
    from tubeless.config import ASSETS_DIR
    img = render.render_standby(HW, str(ASSETS_DIR / "standby.jpg"))
    assert img.size == (HW.view_w, HW.view_h)
    assert render.render_standby(HW, str(tmp_path / "missing.jpg")).size == (HW.view_w, HW.view_h)


def test_long_names_in_the_header_are_shrunk_then_cut_and_never_run_under_the_buttons():
    cx, cy = HW.bezel_center
    row = int(cy + (40 - 240) * HW.bezel_scale)
    left_of_buttons = int(cx + (render.button_rect("upload")[0] - 12 - 320) * HW.bezel_scale)
    for title, sub in (("Sixteen Chars TV!", "Late Night Cartoons And Comedy Specials"), ("W" * 16, "X" * 60), ("Den", "")):
        img = render.render_menu_landscape(MenuModel().items(CTX), 0, HW, title=title, subtitle=sub)
        band = np.asarray(img.convert("RGB"))[row, :, :]
        assert band[left_of_buttons - 3:left_of_buttons].std() < 40
    short = render.render_menu_landscape(MenuModel().items(CTX), 0, HW, title="Den", subtitle="Cartoons")
    longer = render.render_menu_landscape(MenuModel().items(CTX), 0, HW, title="Living Room TV", subtitle="The Documentaries Channel")
    assert short.tobytes() != longer.tobytes()


def test_the_guide_page_lists_every_channel_with_its_number_and_what_is_on():
    ctx = MenuContext(channels=CTX.channels, current_channel=1, guide=[f"Next: episode {i}  ·  {i} min left" for i in range(8)])
    m = MenuModel()
    m.open_page("guide")
    items = m.items(ctx)
    assert [i.number for i in items] == list(range(1, 9)) and [i.label for i in items] == CTX.channels
    assert all(i.action == ("channel", n) for n, i in enumerate(items)) and items[1].current and not items[0].current
    assert items[3].detail == "Next: episode 3  ·  3 min left"
    assert [i.detail for i in m.items(MenuContext(channels=["a"]))] == [""]                    # a guide not filled in yet is blank, not a crash
    img = render.render_menu_landscape(items, 1, HW, title="Simpsons", subtitle="Cartoons", lit=("guide",))
    assert img.size == (HW.view_w, HW.view_h) and img.getbbox() is not None


def test_a_long_guide_line_is_cut_short_rather_than_run_under_the_dot():
    m = MenuModel()
    m.open_page("guide")
    short = render.render_menu_landscape(m.items(MenuContext(channels=["a"], guide=["Next: x"])), 5, HW)
    long_ = render.render_menu_landscape(m.items(MenuContext(channels=["a"], guide=["Next: " + "very long title " * 12])), 5, HW)
    x0, x1 = (int(HW.bezel_center[0] + (render.ROW_X1 - d - 320) * HW.bezel_scale) for d in (40, 30))     # the strip just left of the dot
    y0, y1 = (int(HW.bezel_center[1] + (render.ROWS_Y0 + d - 240) * HW.bezel_scale) for d in (30, 46))     # where the detail line runs
    assert short.crop((x0, y0, x1, y1)).tobytes() == long_.crop((x0, y0, x1, y1)).tobytes()
    assert short.tobytes() != long_.tobytes()                                                             # but the long one did draw something


def test_time_left_is_said_in_minutes():
    from tubeless.menu import time_left
    assert time_left(None) == "" and time_left(0) == "" and time_left(-5) == ""
    assert time_left(30) == "under a minute left" and time_left(60) == "1 min left" and time_left(12 * 60 + 20) == "12 min left"
