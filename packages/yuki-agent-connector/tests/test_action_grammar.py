"""The computer action grammar both paths share: keys, modifiers, coordinates, arguments.

Failure cases, written before ``yuki_agent_connector/actions.py`` (task book
docs/architecture/Computer-Use任务书-2026-11-15.md §11): C7–C9, C16 (coordinates and
``scale``), Z2–Z3 (zoom regions), A3, A8, A10, A11, A14 (modifiers, repeat, keys,
``element`` against ``x,y``), V4 (``paste``). Pure functions, so every case runs on all
three CI platforms and inside the environment image, which mounts the same module.
"""

from __future__ import annotations

import pytest

from yuki_agent_connector.actions import (
    OutOfRange,
    parse_keys,
    parse_modifiers,
    resolve_point,
    resolve_region,
    validate,
)


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("Return", ((), "return")),
        ("enter", ((), "return")),
        ("KP_Enter", ((), "return")),
        ("ctrl+l", (("ctrl",), "l")),
        ("Control+Shift+T", (("ctrl", "shift"), "t")),
        ("cmd+c", (("cmd",), "c")),
        ("cmd+space", (("cmd",), "space")),
        ("super+e", (("cmd",), "e")),
        ("alt+F4", (("alt",), "f4")),
        ("option+Left", (("alt",), "left")),
        ("Page_Down", ((), "pagedown")),
        ("Prior", ((), "pageup")),
        ("BackSpace", ((), "backspace")),
        ("ctrl+/", (("ctrl",), "/")),
        ("shift", ((), "shift")),
        ("Escape", ((), "escape")),
    ],
)
def test_key_combinations(combo: str, expected: tuple[tuple[str, ...], str]) -> None:
    assert parse_keys(combo) == expected


@pytest.mark.parametrize("combo", ["", "ctrl+", "ctrl+nonsense", "ctrl+a+b", "F25", "XF86Mail"])
def test_unknown_keys_are_refused_on_both_paths(combo: str) -> None:
    # A11: the environment used to hand any text to xdotool; now both paths refuse it.
    with pytest.raises(ValueError):
        parse_keys(combo)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("shift", ("shift",)),
        ("ctrl+shift", ("ctrl", "shift")),
        ("Control + Alt", ("ctrl", "alt")),
        ("cmd", ("cmd",)),
        ("super", ("cmd",)),
        ("win", ("cmd",)),
        ("meta", ("cmd",)),
        ("option", ("alt",)),
        ("shift+shift", ("shift",)),
    ],
)
def test_modifiers(text: str, expected: tuple[str, ...]) -> None:
    assert parse_modifiers(text) == expected


@pytest.mark.parametrize("text", ["", "+", "shift+", "hyper", "ctrl+a", "fn"])
def test_unknown_modifiers_are_refused(text: str) -> None:
    # A3: no input when a modifier is not one the platforms can hold.
    with pytest.raises(ValueError):
        parse_modifiers(text)


def test_pixel_coordinates_are_checked_against_the_image() -> None:
    assert resolve_point(0, 0, 1430, 804, None) == (0, 0)
    assert resolve_point(1429, 803, 1430, 804, None) == (1429, 803)
    # C7: the first pixel outside, either axis, negative values.
    for x, y in ((1430, 0), (0, 804), (-1, 5), (5, -1), (2560, 1440)):
        with pytest.raises(OutOfRange) as caught:
            resolve_point(x, y, 1430, 804, None)
        facts = caught.value.result
        assert facts["error"] == "coordinate_out_of_range" and facts["status"] == "failed"
        assert (facts["width"], facts["height"]) == (1430, 804)
        assert "1429" in facts["message"] and "803" in facts["message"]
        assert "scale=1000" in facts["message"] and facts["coordinates"]


def test_normalized_coordinates_scale_both_axes_and_clamp_the_far_edge() -> None:
    # C8: 0..1000 on both axes; 1000 is the last pixel, not one past it.
    assert resolve_point(500, 500, 1430, 804, 1000) == (715, 402)
    assert resolve_point(1000, 1000, 1430, 804, 1000) == (1429, 803)
    assert resolve_point(0, 0, 1430, 804, 1000) == (0, 0)
    # C9: outside 0..1000.
    for x, y in ((1001, 0), (0, -1)):
        with pytest.raises(OutOfRange) as caught:
            resolve_point(x, y, 1430, 804, 1000)
        assert "0–1000" in caught.value.result["message"]


def test_mixed_conventions_within_range_follow_the_declared_one() -> None:
    # C10 (accepted residual risk): both values in range cannot be told apart.
    assert resolve_point(700, 900 // 2, 1430, 804, None) == (700, 450)


def test_zoom_regions() -> None:
    assert resolve_region([0, 0, 1430, 804], 1430, 804, None) == (0, 0, 1430, 804)
    assert resolve_region([100, 50, 300, 150], 1430, 804, None) == (100, 50, 300, 150)
    # Z3: a region in 0..1000 covers the same share of the image.
    assert resolve_region([0, 0, 1000, 1000], 1430, 804, 1000) == (0, 0, 1430, 804)
    assert resolve_region([250, 250, 750, 750], 1430, 804, 1000) == (358, 201, 1073, 603)
    # Z2: past the image.
    with pytest.raises(OutOfRange):
        resolve_region([0, 0, 1431, 804], 1430, 804, None)
    with pytest.raises(OutOfRange):
        resolve_region([0, 0, 1001, 10], 1430, 804, 1000)


def test_dx_dy_and_repeat_are_not_scaled() -> None:
    # C16: only positions follow scale; validate keeps counts as given.
    parsed = validate("scroll", {"x": 500, "y": 500, "dx": 3, "dy": -7, "scale": 1000})
    assert (parsed.dx, parsed.dy) == (3, -7)
    parsed = validate("key", {"text": "Down", "repeat": 40, "scale": 1000})
    assert parsed.repeat == 40


@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("click", {"x": 1, "y": 1, "scale": 100}),  # only 1000 is a declared system
        ("click", {"x": 1, "y": 1, "scale": True}),
        ("click", {"x": 1}),  # a point needs both values
        ("click", {}),  # nothing to aim at
        ("drag", {"x": 1, "y": 1}),  # no end
        ("key", {"text": "Return", "repeat": 0}),  # A8
        ("key", {"text": "Return", "repeat": 101}),
        ("click", {"x": 1, "y": 1, "repeat": 2}),  # repeat belongs to key
        ("click", {"x": 1, "y": 1, "element": "7.ab12e1"}),  # A14
        ("click", {"element": "7.ab12e1", "y": 3}),
        ("click", {"element": "not-an-id"}),
        ("click", {"x": 1, "y": 1, "text": "hyper"}),  # A3
        ("move", {"x": 1, "y": 1, "text": "shift"}),  # move holds nothing
        ("zoom", {}),  # Z2
        ("zoom", {"region": [10, 10, 5, 20]}),  # reversed
        ("zoom", {"region": [10, 10, 10, 20]}),  # zero width
        ("zoom", {"region": [1, 2, 3]}),
        ("elements", {}),  # needs window
        ("paste", {}),  # V4
        ("paste", {"text": ""}),
        ("paste", {"text": "abc", "x": 1, "y": 1}),
        ("key", {}),
    ],
)
def test_invalid_arguments_are_refused_before_any_geometry(
    action: str, args: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        validate(action, args)


def test_parsed_arguments() -> None:
    click = validate("triple_click", {"x": 3, "y": 4, "text": "ctrl+shift"})
    assert (click.button, click.count, click.modifiers) == ("left", 3, ("ctrl", "shift"))
    middle = validate("middle_click", {"x": 3, "y": 4})
    assert (middle.button, middle.count, middle.modifiers) == ("middle", 1, ())
    key = validate("key", {"text": "ctrl+l"})
    assert (key.keys, key.repeat) == ((("ctrl",), "l"), 1)
    element = validate("click", {"element": "4242.ab12e7", "window": "4242"})
    assert element.element == ("4242", "ab12", 7)
    zoom = validate("zoom", {"region": [1, 2, 30, 40]})
    assert zoom.region == (1, 2, 30, 40)
    # type keeps its text meaning; modifiers are only for clicks, scroll and drag.
    assert validate("type", {"text": "shift"}).modifiers == ()
    assert validate("scroll", {"x": 1, "y": 1, "dy": 2, "text": "ctrl"}).modifiers == ("ctrl",)
