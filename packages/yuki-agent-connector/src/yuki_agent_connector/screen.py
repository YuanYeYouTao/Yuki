"""The owner's screen: display geometry, coordinate conversion and the native backend boundary.

Coordinates given by the model are pixels of the image attached to the call. They are
converted with the display geometry read when the action runs (task book §5.2), never
with a saved screenshot. ``Display`` rectangles are in the platform's input coordinate
space: global points on macOS (Retina is pixels/points), physical pixels on Windows
(the process is per-monitor-v2 DPI aware, so a 150 % display is not scaled twice).

A ``Window`` (phase 3b) is the same kind of rectangle: its image is that window alone and
its coordinates are relative to that image; input to it goes to that window in the
background, never through the global cursor (task book §5.5 layer 2).
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Sequence
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


@dataclass(frozen=True)
class Window:
    """One top-level window as read now: ``id`` is what ``windows`` lists and ``window`` takes."""

    id: str
    app: str
    title: str
    x: float
    y: float
    width: float
    height: float
    pixel_width: int
    pixel_height: int


class Area(Protocol):
    """A display or a window: a rectangle in input coordinates and its native pixel size."""

    @property
    def x(self) -> float: ...
    @property
    def y(self) -> float: ...
    @property
    def width(self) -> float: ...
    @property
    def height(self) -> float: ...
    @property
    def pixel_width(self) -> int: ...
    @property
    def pixel_height(self) -> int: ...


class Refused(Exception):
    """The operating system cannot do this for the window; nothing was delivered by this call.

    ``cause`` is the fact behind a ``device_not_supported`` (task book §6.9; ``reason`` is
    already the receipt protocol's dispatch fact, which the journal and the server strip).
    """

    def __init__(self, error: str, message: str, cause: str | None = None) -> None:
        super().__init__(message)
        self.error, self.message, self.cause = error, message, cause

    def result(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": self.error,
            "message": self.message,
            **({"cause": self.cause} if self.cause else {}),
        }


class Stale(Exception):
    """The accessibility node no longer exists (destroyed, its window closed)."""


class Node(Protocol):
    """One accessibility node of one window, as listed by ``elements`` (task book §6.7).

    ``depth`` is its depth in the platform tree; ``container`` marks a role that only
    groups other nodes, dropped from the listing when it has no name.
    """

    depth: int
    role: str
    name: str
    visible: bool
    container: bool

    def bounds(self) -> tuple[float, float, float, float] | None:
        """``(x, y, width, height)`` in global input coordinates, read now; ``Stale`` if gone."""

    def press(self) -> bool:
        """Its default accessibility action (press, invoke, toggle); False when it has none."""

    def focus(self) -> None:
        """Make it the keyboard focus (foreground); ``Refused`` when it cannot take focus."""


def frame(display: Area, max_dimension: int, max_pixels: int) -> tuple[int, int]:
    """Size of the image of ``display`` under the profile's limits; never upscaled."""
    scale = image_scale(display.pixel_width, display.pixel_height, max_dimension, max_pixels)
    return round(display.pixel_width * scale), round(display.pixel_height * scale)


def to_global(
    display: Area, max_dimension: int, max_pixels: int, x: float, y: float
) -> tuple[float, float]:
    """An image pixel of ``display`` in global input coordinates (may be negative)."""
    scale = image_scale(display.pixel_width, display.pixel_height, max_dimension, max_pixels)
    return (
        display.x + x / scale * display.width / display.pixel_width,
        display.y + y / scale * display.height / display.pixel_height,
    )


def to_image(
    display: Area, max_dimension: int, max_pixels: int, x: float, y: float
) -> tuple[float, float]:
    """A global input point as a pixel of the image of ``display`` (the inverse of to_global)."""
    scale = image_scale(display.pixel_width, display.pixel_height, max_dimension, max_pixels)
    return (
        (x - display.x) * display.pixel_width / display.width * scale,
        (y - display.y) * display.pixel_height / display.height * scale,
    )


def to_virtual_desk(virtual: tuple[int, int, int, int], x: float, y: float) -> tuple[int, int]:
    """Windows ``MOUSEEVENTF_ABSOLUTE|VIRTUALDESK``: 0..65535 across the whole virtual desktop."""
    left, top, width, height = virtual
    return round((x - left) * 65535 / (width - 1)), round((y - top) * 65535 / (height - 1))


class Surface(Protocol):
    """Where input goes: the whole screen (global input) or one window (background).

    Every method answers the route it took (``global_input``, ``window_events`` or
    ``accessibility``); ``modifiers`` are held only while that one action runs and are
    released even when posting fails.
    """

    def move(self, x: float, y: float) -> str: ...

    def click(
        self, x: float, y: float, button: str, count: int, modifiers: tuple[str, ...]
    ) -> str: ...

    def drag(self, x: float, y: float, x2: float, y2: float, modifiers: tuple[str, ...]) -> str: ...

    def scroll(self, x: float, y: float, dx: int, dy: int, modifiers: tuple[str, ...]) -> str: ...

    def key(self, modifiers: tuple[str, ...], key: str) -> str: ...

    def type(self, text: str) -> str: ...


class Target(Surface, Protocol):
    """One window resolved for one call. Input never moves the cursor or raises a window;
    an action without a background route raises ``Refused`` and delivers nothing."""

    @property
    def window(self) -> Window: ...

    @property
    def actions(self) -> frozenset[str]:
        """Inputs this platform can deliver to a window in the background at all, plus
        ``modifiers`` when it can hold modifier keys there; empty outside the owner's Space."""

    def collides(self) -> bool:
        """Whether the owner is using the window's app: it is the frontmost app, or the
        window under the owner's pointer belongs to it."""

    def elevated(self) -> bool:
        """Whether the window's process is above Yuki's integrity level (Windows UIPI)."""

    def capture(
        self,
        width: int,
        height: int,
        path: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> None:
        """Write a JPEG of exactly ``width``x``height`` showing this window alone, now;
        ``region`` is ``(x, y, width, height)`` relative to the window, in input units."""

    def focus(self, x: float, y: float) -> str:
        """Make the input field at the global point the target of ``key``/``type``."""

    def focus_element(self, node: Node) -> None:
        """Make ``node`` the target of ``key``/``type`` without raising the window."""

    def hit(self, x: float, y: float) -> dict[str, str] | None:
        """``{role, name}`` of this window's own node at the global point, before any input."""

    def elements(self) -> Sequence[Node]:
        """This window's accessibility nodes in tree order; ``Refused`` when there are none."""


class Backend(Surface, Protocol):
    """One platform's screen. Every method blocks; the caller runs it off the event loop."""

    def permissions(self) -> tuple[bool, bool]:
        """(screen capture, input posting) as the operating system's preflight reports."""

    def displays(self) -> list[Display]: ...

    def unavailable(self) -> dict[str, Any] | None:
        """A failure result when the screen cannot be seen or used now (lock, secure desktop)."""

    def capture(
        self,
        display: Display,
        width: int,
        height: int,
        path: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> None:
        """Write a JPEG of exactly ``width``x``height`` showing ``display`` now, the real
        pointer drawn in; ``region`` is ``(x, y, width, height)`` relative to the display."""

    def pointer(self) -> tuple[float, float] | None:
        """The pointer in global input coordinates, or None when it cannot be read."""

    def hit(self, x: float, y: float) -> dict[str, str] | None:
        """``{role, name}`` of the topmost node at the global point (system hit test)."""

    def last_input(self) -> tuple[float, float]:
        """Bounds of the moment of the most recent input from any source, Yuki's included.

        In the backend's own clock; only compared with another value of this method, so
        no conversion to another clock (and its rounding) is involved. On macOS only
        deliberate input counts (keys, buttons, drags, scrolling), not pointer movement.
        """

    def idle(self) -> float:
        """Seconds since that same most recent input, as the operating system counts them."""

    def elevated(self, point: tuple[float, float] | None) -> bool:
        """Whether input to the window at ``point`` (or the foreground) would be dropped."""

    def windows(self) -> list[dict[str, str]]:
        """``{id, app, title}`` of the owner's ordinary top-level windows, Yuki's excluded."""

    def window(self, identity: str) -> Target:
        """The window ``identity`` as it is now; ``Refused`` when missing or minimized."""


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
