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
    assert [i.action[0] for i in settings] == ["page", "shuffle", "reshuffle", "sleep_timer", "auto_dim", "calibrate", "shutdown"]
    assert m.items(MenuContext(confirm_shutdown=True))[-1].label == "Tap again to confirm"


def test_next_step_cycles():
    assert next_step((0, 15, 30), 0) == 15
    assert next_step((0, 15, 30), 30) == 0
    assert next_step((25, 50, 75, 100), 100) == 25
