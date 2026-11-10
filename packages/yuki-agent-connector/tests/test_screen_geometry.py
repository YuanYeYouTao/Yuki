"""M6: image coordinates to the platform's input coordinates, and key combinations.

Written before the implementation (docs/operations/computer-browser-device-3a-20261110.md
§1 M6). Pure functions, so every case runs on all three CI platforms.
"""

from __future__ import annotations

import pytest

from yuki_agent_connector.screen import Display, frame, parse_keys, to_global, to_virtual_desk

# A Retina main display: 1512x982 points, 3024x1964 pixels.
RETINA = Display("1", 0, 0, 1512, 982, 3024, 1964, primary=True, scale=2.0)
# A 1920x1080 external display to the left of and above the main display (points == pixels).
LEFT = Display("2", -1920, -98, 1920, 1080, 1920, 1080, primary=False, scale=1.0)
# Windows per-monitor-v2: rectangles in physical pixels; scale is reported, not applied.
WIN_150 = Display(r"\\.\DISPLAY2", 2560, 0, 3840, 2160, 3840, 2160, primary=False, scale=1.5)
WIN_LEFT = Display(r"\\.\DISPLAY3", -1280, -200, 1280, 1024, 1280, 1024, primary=False, scale=1.0)


def test_frame_never_upscales_and_obeys_both_limits() -> None:
    assert frame(LEFT, 4096, 16_777_216) == (1920, 1080)
    # The longest side is capped by max_dimension.
    assert frame(RETINA, 1568, 16_777_216) == (1568, 1018)
    # The pixel count is capped by max_pixels.
    width, height = frame(WIN_150, 8192, 1_000_000)
    assert width * height <= 1_000_000 and abs(width / height - 3840 / 2160) < 0.01


def test_retina_image_coordinates_are_points_not_pixels() -> None:
    width, height = frame(RETINA, 1568, 16_777_216)
    # The image centre is the display centre in global points, not in pixels.
    x, y = to_global(RETINA, 1568, 16_777_216, width // 2, height // 2)
    assert x == pytest.approx(756, abs=1) and y == pytest.approx(491, abs=1)
    # The image's bottom-right pixel stays on the display.
    x, y = to_global(RETINA, 1568, 16_777_216, width - 1, height - 1)
    assert 1510 < x < 1512 and 980 < y < 982


def test_secondary_display_left_and_above_has_negative_global_coordinates() -> None:
    assert to_global(LEFT, 4096, 16_777_216, 0, 0) == (-1920, -98)
    x, y = to_global(LEFT, 4096, 16_777_216, 960, 540)
    assert (x, y) == (-960, 442)


def test_windows_dpi_is_not_applied_twice() -> None:
    # Physical pixels in, physical pixels out: 150% only changes the reported scale.
    width, height = frame(WIN_150, 1920, 16_777_216)
    assert (width, height) == (1920, 1080)
    x, y = to_global(WIN_150, 1920, 16_777_216, 960, 540)
    assert (x, y) == (2560 + 1920, 1080)


def test_virtual_desk_normalization_covers_negative_origins() -> None:
    # Virtual desktop: WIN_LEFT (-1280,-200) .. WIN_150 right edge (6400, 2160).
    virtual = (-1280, -200, 7680, 2360)
    assert to_virtual_desk(virtual, -1280, -200) == (0, 0)
    assert to_virtual_desk(virtual, 6399, 2159) == (65535, 65535)
    x, y = to_global(WIN_LEFT, 4096, 16_777_216, 0, 0)
    assert to_virtual_desk(virtual, x, y) == (0, 0)
    # The primary display's origin is inside the range, not at 0.
    nx, ny = to_virtual_desk(virtual, 0, 0)
    assert nx == round(1280 * 65535 / 7679) and ny == round(200 * 65535 / 2359)


def test_coordinates_follow_the_current_geometry_not_a_saved_screenshot() -> None:
    # The same image point maps by whatever display geometry is read at action time.
    before = Display("1", 0, 0, 1440, 900, 2880, 1800, primary=True, scale=2.0)
    after = Display("1", 0, 0, 1728, 1117, 3456, 2234, primary=True, scale=2.0)
    assert to_global(before, 1568, 16_777_216, 100, 100) != to_global(
        after, 1568, 16_777_216, 100, 100
    )


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("Return", ((), "return")),
        ("enter", ((), "return")),
        ("ctrl+l", (("ctrl",), "l")),
        ("Control+Shift+T", (("ctrl", "shift"), "t")),
        ("cmd+space", (("cmd",), "space")),
        ("super+e", (("cmd",), "e")),
        ("alt+F4", (("alt",), "f4")),
        ("option+Left", (("alt",), "left")),
        ("Page_Down", ((), "pagedown")),
        ("BackSpace", ((), "backspace")),
        ("ctrl+/", (("ctrl",), "/")),
        ("shift", ((), "shift")),
    ],
)
def test_key_combinations(combo: str, expected: tuple[tuple[str, ...], str]) -> None:
    assert parse_keys(combo) == expected


@pytest.mark.parametrize("combo", ["", "ctrl+", "ctrl+nonsense", "ctrl+a+b", "F25"])
def test_unknown_key_combinations_are_refused_before_any_input(combo: str) -> None:
    with pytest.raises(ValueError):
        parse_keys(combo)
