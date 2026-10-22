import pytest

from tubeless.skip import skip_target

COUNT = 542   # 271 videos, each preceded by a static


@pytest.mark.parametrize("pos,direction,expected", [
    # forward: from a video land on the static in front of the next one; from a static skip a video
    (1, +1, ("goto", 2)),
    (7, +1, ("goto", 8)),
    (4, +1, ("goto", 6)),
    # back: always the static in front of the previous video
    (9, -1, ("goto", 6)),
    (5, -1, ("goto", 2)),
    (3, -1, ("goto", 0)),
    (6, -1, ("goto", 4)),
    (2, -1, ("goto", 0)),
    # back past the start wraps to the last video's static
    (1, -1, ("goto", COUNT - 2)),
    (0, -1, ("goto", COUNT - 2)),
    # forward past the end asks for a new cycle
    (COUNT - 1, +1, ("end",)),
    (COUNT - 2, +1, ("end",)),
])
def test_targets(pos, direction, expected):
    assert skip_target(pos, COUNT, direction) == expected


def test_every_target_is_a_static():
    for pos in range(COUNT):
        for d in (+1, -1):
            t = skip_target(pos, COUNT, d)
            if t and t[0] == "goto":
                assert t[1] % 2 == 0, (pos, d, t)


def test_nothing_to_skip():
    assert skip_target(None, COUNT, +1) is None
    assert skip_target(0, 1, +1) is None        # only the channel-change static is loaded
    assert skip_target(0, None, -1) is None


# ---- a channel with no static between videos (it streams from YouTube): one entry per video ---------------------------------
@pytest.mark.parametrize("pos,direction,expected", [
    (0, +1, ("goto", 1)), (5, +1, ("goto", 6)), (9, -1, ("goto", 8)), (1, -1, ("goto", 0)),
    (0, -1, ("goto", 9)),                         # back past the start wraps to the last video
    (9, +1, ("end",)),                            # forward past the end asks for a new cycle
])
def test_targets_without_static(pos, direction, expected):
    assert skip_target(pos, 10, direction, static=False) == expected


def test_nothing_to_skip_without_a_position_or_with_an_empty_playlist():
    assert skip_target(None, 10, +1, static=False) is None and skip_target(0, None, +1, static=False) is None
    assert skip_target(0, 0, +1, static=False) is None
    assert skip_target(0, 1, +1, static=False) == ("end",)         # a one-video channel: forward means a new cycle
    assert skip_target(0, 1, -1, static=False) == ("goto", 0)       # back replays it
