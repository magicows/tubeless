import math

import pytest

from tubeless.hardware import (Hardware, apply_affine, invert_affine, load_hardware,
                               solve_affine)


def to_view(hw, raw_x, raw_y, x_max=639, y_max=479):
    return apply_affine(hw.touch_affine, raw_x / x_max, raw_y / y_max)


def test_default_map_matches_the_taps_measured_on_the_real_tv():
    """The seven calibration touches recorded on the TV (view = where the finger really was)."""
    hw = Hardware()
    tl = to_view(hw, 159, 442)
    tr = to_view(hw, 87, 51)
    br = to_view(hw, 531, 45)
    bl = to_view(hw, 542, 444)
    assert tl[0] < 320 and tl[1] < 240
    assert tr[0] > 320 and tr[1] < 240
    assert br[0] > 320 and br[1] > 240
    assert bl[0] < 320 and bl[1] > 240
    # and the exact numbers the old code produced
    assert tl == pytest.approx((49.4, 119.4), abs=0.5)
    # swipe left->right goes rightwards; top->bottom goes downwards
    assert to_view(hw, 266, 26)[0] - to_view(hw, 310, 418)[0] > 300
    assert to_view(hw, 564, 270)[1] - to_view(hw, 75, 282)[1] > 300


def test_invert_is_a_round_trip():
    hw = Hardware()
    inv = invert_affine(hw.touch_affine)
    for vx, vy in [(0, 0), (640, 480), (123, 321), (555, 77)]:
        rx, ry = apply_affine(inv, vx, vy)
        assert apply_affine(hw.touch_affine, rx, ry) == pytest.approx((vx, vy), abs=1e-6)


def test_solve_recovers_a_known_map_and_reports_noise():
    truth = ((0.0, -640.0, 640.0), (480.0, 0.0, 0.0))
    raws = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8), (0.5, 0.5)]
    pairs = [((rx, ry), apply_affine(truth, rx, ry)) for rx, ry in raws]
    affine, rms = solve_affine(pairs)
    assert rms < 1e-6
    for got, want in zip(sum(affine, ()), sum(truth, ())):
        assert got == pytest.approx(want, abs=1e-6)
    noisy = [(r, (v[0] + (30 if i == 0 else 0), v[1])) for i, (r, v) in enumerate(pairs)]
    assert solve_affine(noisy)[1] > 5


def test_solve_rejects_degenerate_input():
    with pytest.raises(ValueError):
        solve_affine([((0.1, 0.1), (1, 1)), ((0.2, 0.2), (2, 2))])
    with pytest.raises(ValueError):
        solve_affine([((0.1, 0.1), (1, 1)), ((0.2, 0.2), (2, 2)), ((0.3, 0.3), (3, 3))])


def test_overrides_and_unknown_settings():
    assert load_hardware().backlight_pin == 18
    assert load_hardware(overrides={"backlight_pin": 12, "bezel_center": [300, 200]}).bezel_center == (300, 200)
    with pytest.raises(ValueError):
        load_hardware(overrides={"nope": 1})
    with pytest.raises(ValueError):
        load_hardware("no-such-board")
