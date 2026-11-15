"""The ``computer`` action grammar shared by Yuki's own computer and the owner's computer.

Standard library only: the environment mounts this file next to ``cdp.py`` and its driver
imports it, so both paths read keys, modifiers, coordinates and arguments the same way
(task book docs/architecture/Computer-Use任务书-2026-11-15.md §6.2, §6.4).

Coordinates are pixels of the image the result comes with (a whole display or one window,
at the geometry read when the action runs) unless the call declares ``scale=1000``. A value
outside the declared system is refused before any input, never corrected or guessed.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

READS = frozenset({"screenshot", "resume", "zoom", "elements"})
CLICKS = {
    "click": ("left", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
}
POINTED = frozenset({*CLICKS, "move", "drag", "scroll"})
INPUTS = frozenset({*POINTED, "type", "key"})
# Actions whose ``text`` names the modifier keys held for exactly that action.
HOLDING = frozenset({*CLICKS, "drag", "scroll"})
# Actions that may name an accessibility node instead of x,y.
ELEMENT_ACTIONS = frozenset({*CLICKS, "move", "drag", "scroll", "type", "key"})
SCALE = 1000
REPEAT = range(1, 101)
COORDINATES = "x,y 为本图像素，左上为原点；传 scale=1000 时为 0–1000"
ELEMENT_ID = re.compile(r"^(?P<window>[^.\s]+)\.(?P<tag>[0-9a-f]{4})e(?P<number>\d+)$")

MODIFIERS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "cmd": "cmd",
    "command": "cmd",
    "super": "cmd",
    "meta": "cmd",
    "win": "cmd",
}
_NAMED = {
    "return": "return",
    "enter": "return",
    "kp_enter": "return",
    "tab": "tab",
    "escape": "escape",
    "esc": "escape",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "space": "space",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "page_up": "pageup",
    "prior": "pageup",
    "pagedown": "pagedown",
    "page_down": "pagedown",
    "next": "pagedown",
    "insert": "insert",
    **{f"f{number}": f"f{number}" for number in range(1, 21)},
}
PUNCTUATION = set("`-=[]\\;',./")
KEYS = (
    set(_NAMED.values())
    | set(MODIFIERS.values())
    | set("abcdefghijklmnopqrstuvwxyz0123456789")
    | PUNCTUATION
)


def parse_keys(combo: str) -> tuple[tuple[str, ...], str]:
    """``ctrl+shift+t`` → (("ctrl", "shift"), "t"); xdotool-style names, case-insensitive."""
    names = [part.strip().lower() for part in combo.split("+")]
    if any(not name for name in names):
        raise ValueError(f"未知的按键组合：{combo!r}")
    *held, last = names
    modifiers = []
    for name in held:
        if name not in MODIFIERS:
            raise ValueError(f"未知的按键组合：{combo!r}")
        modifiers.append(MODIFIERS[name])
    key = MODIFIERS.get(last) or _NAMED.get(last) or last
    if key not in KEYS:
        raise ValueError(f"未知的按键组合：{combo!r}")
    return tuple(modifiers), key


def parse_modifiers(text: str) -> tuple[str, ...]:
    """``ctrl+shift`` → ("ctrl", "shift"): keys held only while one click, drag or scroll runs."""
    names = [part.strip().lower() for part in text.split("+")]
    if any(name not in MODIFIERS for name in names):
        raise ValueError(f"未知的修饰键：{text!r}（可用 shift、ctrl、alt、cmd，用 + 连接）")
    return tuple(dict.fromkeys(MODIFIERS[name] for name in names))


class OutOfRange(ValueError):
    """A coordinate outside the declared system; ``result`` is the failure to return."""

    def __init__(self, width: int, height: int, scale: int | None) -> None:
        if scale == SCALE:
            message = (
                f"坐标超出 0–1000 归一化范围（scale=1000，两个轴都是 0–1000）；"
                f"本次截图宽 {width}、高 {height}。省略 scale 时坐标是截图像素。"
            )
        else:
            message = (
                f"坐标是本次截图的像素：宽 {width}、高 {height}"
                f"（x 0–{width - 1}，y 0–{height - 1}）；"
                "使用 0–1000 归一化坐标时请传 scale=1000。"
            )
        super().__init__(message)
        self.result = {
            "status": "failed",
            "error": "coordinate_out_of_range",
            "message": message + "没有执行任何输入。",
            "coordinates": COORDINATES,
            "width": width,
            "height": height,
        }


def _scaled(value: int, extent: int) -> int:
    return math.floor(value * extent / SCALE + 0.5)


def resolve_point(x: int, y: int, width: int, height: int, scale: int | None) -> tuple[int, int]:
    """The pixel of a ``width``x``height`` image that ``x,y`` names in the declared system."""
    if scale == SCALE:
        if not (0 <= x <= SCALE and 0 <= y <= SCALE):
            raise OutOfRange(width, height, scale)
        return min(_scaled(x, width), width - 1), min(_scaled(y, height), height - 1)
    if not (0 <= x < width and 0 <= y < height):
        raise OutOfRange(width, height, scale)
    return x, y


def resolve_region(
    region: list[int] | tuple[int, ...], width: int, height: int, scale: int | None
) -> tuple[int, int, int, int]:
    """``[x0,y0,x1,y1]`` (end exclusive) as pixels of the whole image."""
    x0, y0, x1, y1 = region
    if scale == SCALE:
        if not all(0 <= value <= SCALE for value in region):
            raise OutOfRange(width, height, scale)
        x0, x1 = _scaled(x0, width), _scaled(x1, width)
        y0, y1 = _scaled(y0, height), _scaled(y1, height)
    elif not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise OutOfRange(width, height, scale)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("region 换算后宽或高为零")
    return x0, y0, x1, y1


@dataclass(frozen=True)
class Parsed:
    """One call's arguments, checked before any geometry is read or any input is sent."""

    action: str
    scale: int | None = None
    modifiers: tuple[str, ...] = ()
    button: str = "left"
    count: int = 1
    keys: tuple[tuple[str, ...], str] | None = None
    repeat: int = 1
    dx: int = 0
    dy: int = 0
    region: tuple[int, int, int, int] | None = None
    element: tuple[str, str, int] | None = None


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(action: str, args: dict[str, Any]) -> Parsed:
    """``ValueError`` (→ ``invalid_arguments``) for anything not decided by geometry."""
    scale = args.get("scale")
    if scale is not None and (not _integer(scale) or scale != SCALE):
        raise ValueError("scale 只能是 1000（0–1000 归一化坐标）；省略时坐标是截图像素。")
    repeat = args.get("repeat")
    if repeat is not None and (action != "key" or not _integer(repeat) or repeat not in REPEAT):
        raise ValueError("repeat 只用于 key，取 1–100。")
    element = None
    if args.get("element") is not None:
        if action not in ELEMENT_ACTIONS:
            raise ValueError(f"{action} 不能用 element。")
        if args.get("x") is not None or args.get("y") is not None:
            raise ValueError("element 与 x,y 只能给一种。")
        found = ELEMENT_ID.match(str(args["element"]))
        if found is None:
            raise ValueError("element 用 elements 返回的节点 id（如 4242.ab12e7）。")
        element = (found["window"], found["tag"], int(found["number"]))
        if args.get("window") is not None and str(args["window"]) != element[0]:
            raise ValueError(f"element {args['element']} 属于窗口 {element[0]}，与 window 不一致。")
    for name in ("x", "y", "x2", "y2", "dx", "dy"):
        if args.get(name) is not None and not _integer(args[name]):
            raise ValueError(f"{name} 必须是整数。")
    pointed = args.get("x") is not None and args.get("y") is not None
    if (args.get("x") is None) != (args.get("y") is None):
        raise ValueError("x 与 y 要一起给。")
    if action in POINTED and not pointed and element is None:
        raise ValueError(f"{action} 需要 x 与 y（或 element）。")
    if action == "drag" and (args.get("x2") is None or args.get("y2") is None):
        raise ValueError("drag 需要 x2 与 y2。")
    modifiers: tuple[str, ...] = ()
    text = args.get("text")
    if action in HOLDING and text is not None:
        modifiers = parse_modifiers(str(text))
    elif action == "move" and text is not None:
        raise ValueError("move 不按住修饰键。")
    keys = parse_keys(str(text or "")) if action == "key" else None
    if action == "paste":
        if not isinstance(text, str) or not text:
            raise ValueError("paste 需要非空的 text。")
        if pointed:
            raise ValueError("paste 落在当前输入焦点，不接受 x,y；需要时先 click 输入框。")
    region = None
    if action == "zoom":
        raw = args.get("region")
        if not isinstance(raw, list) or len(raw) != 4 or not all(map(_integer, raw)):
            raise ValueError("zoom 需要 region=[x0,y0,x1,y1]（整张截图的坐标）。")
        if not (raw[0] < raw[2] and raw[1] < raw[3]):
            raise ValueError("region 需要 x0<x1、y0<y1。")
        region = (raw[0], raw[1], raw[2], raw[3])
    if action == "elements" and args.get("window") is None:
        raise ValueError("elements 需要 window（screenshot 返回的 windows[].id）。")
    button, count = CLICKS.get(action, ("left", 1))
    return Parsed(
        action=action,
        scale=scale,
        modifiers=modifiers,
        button=button,
        count=count,
        keys=keys,
        repeat=repeat or 1,
        dx=args.get("dx") or 0,
        dy=args.get("dy") or 0,
        region=region,
        element=element,
    )


@dataclass(frozen=True)
class Entry:
    """One accessibility node as a platform read it, before the listing filters it."""

    depth: int
    role: str
    name: str
    visible: bool
    container: bool
    # Image pixels ``(x0, y0, x1, y1)`` of the window image; None when empty or outside.
    box: tuple[int, int, int, int] | None


def render_elements(
    entries: list[Entry],
    window: str,
    tag: str,
    text: str | None,
    scale: int | None,
    width: int,
    height: int,
) -> tuple[str, list[int]]:
    """Snapshot-style lines and the entry index of each id (``<window>.<tag>e<n>``, n from 1).

    Only visible nodes with a box are kept; a nameless container is dropped and its children
    move up. ``text`` keeps matching nodes and their kept ancestors. Nothing is truncated.
    """
    kept: list[tuple[int, int]] = []  # (level, entry index)
    stack: list[tuple[int, int]] = []  # (platform depth, level) of kept ancestors
    for index, entry in enumerate(entries):
        while stack and stack[-1][0] >= entry.depth:
            stack.pop()
        if entry.box is None or not entry.visible or (entry.container and not entry.name):
            continue
        level = stack[-1][1] + 1 if stack else 0
        stack.append((entry.depth, level))
        kept.append((level, index))
    shown = set(range(len(kept)))
    if text:
        wanted = text.casefold()
        shown = set()
        for position, (level, index) in enumerate(kept):
            entry = entries[index]
            if wanted in entry.name.casefold() or wanted in entry.role.casefold():
                shown.add(position)
                need = level
                for earlier in range(position - 1, -1, -1):
                    if kept[earlier][0] < need:
                        shown.add(earlier)
                        need = kept[earlier][0]
    lines = []
    for position, (level, index) in enumerate(kept):
        if position not in shown:
            continue
        entry = entries[index]
        assert entry.box is not None
        box = entry.box
        if scale == SCALE:
            box = (
                round(box[0] * SCALE / width),
                round(box[1] * SCALE / height),
                round(box[2] * SCALE / width),
                round(box[3] * SCALE / height),
            )
        name = json.dumps(entry.name, ensure_ascii=False)
        lines.append(
            f"{'  ' * level}- {entry.role} {name} [{window}.{tag}e{position + 1}] "
            f"({box[0]},{box[1]},{box[2]},{box[3]})"
        )
    return "\n".join(lines), [index for _, index in kept]


def image_box(
    bounds: tuple[float, float, float, float] | None,
    to_image: Any,
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """Global ``(x, y, w, h)`` bounds as image pixels; None when empty or outside the image."""
    if bounds is None or bounds[2] <= 0 or bounds[3] <= 0:
        return None
    x0, y0 = to_image(bounds[0], bounds[1])
    x1, y1 = to_image(bounds[0] + bounds[2], bounds[1] + bounds[3])
    box = (round(x0), round(y0), round(x1), round(y1))
    if box[2] <= 0 or box[3] <= 0 or box[0] >= width or box[1] >= height:
        return None
    return box
