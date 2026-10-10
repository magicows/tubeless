from tubeless.gestures import GestureRecognizer


def rec():
    return GestureRecognizer(long_press=1.0, move_px=25, swipe_min_px=70)


def test_tap():
    r = rec()
    r.update(True, 300, 200, 0.0, raw=(0.4, 0.6))
    r.update(True, 302, 203, 0.1)
    assert r.update(False, 0, 0, 0.15) == [("tap", 300, 200, (0.4, 0.6))]


def test_swipes_in_all_directions():
    cases = {"left": (500, 240, 300, 242), "right": (100, 240, 300, 242),
             "up": (300, 300, 305, 190), "down": (100, 100, 110, 260)}
    for want, (x0, y0, x1, y1) in cases.items():
        r = rec()
        r.update(True, x0, y0, 0.0)
        r.update(True, (x0 + x1) / 2, (y0 + y1) / 2, 0.3)
        r.update(True, x1, y1, 0.6)
        g = r.update(False, 0, 0, 0.7)
        assert g and g[0][:2] == ("swipe", want), (want, g)


def test_slow_swipe_is_not_a_long_press():
    """The calibration swipes on the real TV took ~0.9 s; they must stay swipes."""
    r = rec()
    r.update(True, 60, 240, 0.0)
    fired = []
    for i in range(1, 11):
        r.update(True, 60 + 55 * i, 240, i * 0.1)
        fired += r.poll(i * 0.1)
    g = r.update(False, 0, 0, 1.05)
    assert not fired
    assert g and g[0][1] == "right"


def test_short_drag_is_ignored():
    r = rec()
    r.update(True, 300, 240, 0.0)
    r.update(True, 330, 250, 0.3)          # moved past the tap tolerance, not far enough to swipe
    assert r.update(False, 0, 0, 0.4) == []


def test_long_press_fires_once_while_held_and_release_is_silent():
    r = rec()
    r.update(True, 300, 240, 0.0)
    assert r.poll(0.5) == []
    assert r.poll(1.01) == [("long_press",)]
    assert r.poll(1.5) == []
    assert r.update(False, 0, 0, 1.6) == []


def test_jitter_still_counts_as_a_long_press():
    r = rec()
    r.update(True, 300, 240, 0.0)
    r.update(True, 308, 236, 0.4)
    assert r.poll(1.0) == [("long_press",)]


def test_second_finger_cancels_until_everything_lifts():
    r = rec()
    r.update(True, 300, 240, 0.0)
    r.cancel()
    assert r.update(False, 0, 0, 0.2) == []
    r = rec()
    r.update(True, 300, 240, 0.0)
    r.cancel()
    assert r.update(True, 305, 240, 0.1) == []
    assert r.update(False, 0, 0, 0.3) == []
    assert r.update(True, 300, 240, 0.4) == [] and r.tracking       # a fresh touch works again


def test_next_timeout():
    r = rec()
    assert r.next_timeout(0) is None
    r.update(True, 300, 240, 0.0)
    assert r.next_timeout(0.25) == 0.75
    r.poll(1.1)
    assert r.next_timeout(1.2) is None
