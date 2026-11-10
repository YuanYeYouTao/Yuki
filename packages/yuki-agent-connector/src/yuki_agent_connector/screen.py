"""The owner's screen: display geometry, coordinate conversion and the native backend boundary.

Coordinates given by the model are pixels of the image attached to the call. They are
converted with the display geometry read when the action runs (task book §5.2), never
with a saved screenshot. ``Display`` rectangles are in the platform's input coordinate
space: global points on macOS (Retina is pixels/points), physical pixels on Windows
(the process is per-monitor-v2 DPI aware, so a 150 % display is not scaled twice).
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .cdp import image_scale


@dataclass(frozen=True)
class Display:
    id: str
    x: float
    y: float
    width: float
    height: float
    pixel_width: int
    pixel_height: int
    primary: bool
    # Reported only: macOS backing scale, Windows effective DPI / 96.
    scale: float

    def fact(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "bounds": {"x": self.x, "y": self.y, "width": self.width, "height": self.height},
            "scale": self.scale,
            "primary": self.primary,
        }


def frame(display: Display, max_dimension: int, max_pixels: int) -> tuple[int, int]:
    """Size of the image of ``display`` under the profile's limits; never upscaled."""
    scale = image_scale(display.pixel_width, display.pixel_height, max_dimension, max_pixels)
    return round(display.pixel_width * scale), round(display.pixel_height * scale)


def to_global(
    display: Display, max_dimension: int, max_pixels: int, x: float, y: float
) -> tuple[float, float]:
    """An image pixel of ``display`` in global input coordinates (may be negative)."""
    scale = image_scale(display.pixel_width, display.pixel_height, max_dimension, max_pixels)
    return (
        display.x + x / scale * display.width / display.pixel_width,
        display.y + y / scale * display.height / display.pixel_height,
    )


def to_virtual_desk(virtual: tuple[int, int, int, int], x: float, y: float) -> tuple[int, int]:
    """Windows ``MOUSEEVENTF_ABSOLUTE|VIRTUALDESK``: 0..65535 across the whole virtual desktop."""
    left, top, width, height = virtual
    return round((x - left) * 65535 / (width - 1)), round((y - top) * 65535 / (height - 1))


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
        raise ValueError(f"Unknown key combination: {combo!r}")
    *held, last = names
    modifiers = []
    for name in held:
        if name not in MODIFIERS:
            raise ValueError(f"Unknown key combination: {combo!r}")
        modifiers.append(MODIFIERS[name])
    key = MODIFIERS.get(last) or _NAMED.get(last) or last
    if key not in KEYS:
        raise ValueError(f"Unknown key combination: {combo!r}")
    return tuple(modifiers), key


class Backend(Protocol):
    """One platform's screen. Every method blocks; the caller runs it off the event loop."""

    def permissions(self) -> tuple[bool, bool]:
        """(screen capture, input posting) as the operating system's preflight reports."""

    def displays(self) -> list[Display]: ...

    def unavailable(self) -> dict[str, Any] | None:
        """A failure result when the screen cannot be seen or used now (lock, secure desktop)."""

    def capture(self, display: Display, width: int, height: int, path: Path) -> None:
        """Write a JPEG of exactly ``width``x``height`` showing ``display`` now."""

    def seconds_since_input(self) -> float:
        """Seconds since the most recent input event from any source, Yuki's included."""

    def elevated(self, point: tuple[float, float] | None) -> bool:
        """Whether input to the window at ``point`` (or the foreground) would be dropped."""

    def move(self, x: float, y: float) -> None: ...

    def click(self, x: float, y: float, button: str, count: int) -> None: ...

    def drag(self, x: float, y: float, x2: float, y2: float) -> None: ...

    def scroll(self, x: float, y: float, dx: int, dy: int) -> None: ...

    def key(self, modifiers: tuple[str, ...], key: str) -> None: ...

    def type(self, text: str) -> None: ...


# Every synthetic event carries this tag (macOS kCGEventSourceUserData, Windows dwExtraInfo).
EVENT_TAG = 0x59554B49  # "YUKI"


def native_backend() -> Backend | None:
    """The owner's screen on macOS 14+ and Windows; the Connector does no Linux GUI."""
    if sys.platform == "darwin":
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            return None
        from .screen_macos import MacScreen

        return MacScreen()
    if sys.platform == "win32":
        from .screen_windows import WindowsScreen

        return WindowsScreen()
    return None
