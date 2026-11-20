"""macOS 14+: ScreenCaptureKit screenshots and CGEventPost input from a private, tagged source.

Permissions belong to the responsible process (the signed Yuki.app when Desktop owns the
Connector). Without Accessibility, ``CGEventPost`` drops events silently, so every input is
preceded by ``CGPreflightPostEventAccess``; nothing here ever requests a permission.

One window (phase 3b, docs/operations/computer-3b-spike-20261110.md): ScreenCaptureKit's
single-window filter captures it however it is covered. Input never uses the global cursor:
the element at the point is found in that window's own accessibility subtree (never a
system or app hit test, which answers for whatever is on top) and pressed, focused or given
its menu through accessibility first; where accessibility has nothing, mouse events are
posted to the window's process "stamped" with its window (the undocumented fields and
``CGEventSetWindowLocation`` Cua uses), because plain ``CGEventPostToPid`` clicks and wheels
are dropped. Keys go to the process with plain ``CGEventPostToPid`` after ``AXMain`` makes
the window the app's key window, and land on that window's focused element.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .screen import EVENT_TAG, Display, Refused, Stale, Window

assert sys.platform == "darwin"

import ApplicationServices as AX  # noqa: E402
import objc  # noqa: E402
import Quartz  # noqa: E402

logger = logging.getLogger(__name__)
# ScreenCaptureKit answers through completion handlers on its own queue.
CAPTURE_SECONDS = 10.0
# US ANSI virtual key codes (Carbon kVK_*); letters follow key positions, as on every Mac.
KEYCODES = {
    **dict(zip("asdfhgzxcv", range(0x00, 0x0A), strict=True)),
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "=": 0x18,
    "9": 0x19,
    "7": 0x1A,
    "-": 0x1B,
    "8": 0x1C,
    "0": 0x1D,
    "]": 0x1E,
    "o": 0x1F,
    "u": 0x20,
    "[": 0x21,
    "i": 0x22,
    "p": 0x23,
    "return": 0x24,
    "l": 0x25,
    "j": 0x26,
    "'": 0x27,
    "k": 0x28,
    ";": 0x29,
    "\\": 0x2A,
    ",": 0x2B,
    "/": 0x2C,
    "n": 0x2D,
    "m": 0x2E,
    ".": 0x2F,
    "tab": 0x30,
    "space": 0x31,
    "`": 0x32,
    "backspace": 0x33,
    "escape": 0x35,
    "cmd": 0x37,
    "shift": 0x38,
    "alt": 0x3A,
    "ctrl": 0x3B,
    "insert": 0x72,  # Help, where a Mac keyboard has Insert
    "home": 0x73,
    "pageup": 0x74,
    "delete": 0x75,
    "end": 0x77,
    "pagedown": 0x79,
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,
    **{
        f"f{number}": code
        for number, code in zip(
            range(1, 21),
            (
                *(0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64, 0x65, 0x6D),
                *(0x67, 0x6F, 0x69, 0x6B, 0x71, 0x6A, 0x40, 0x4F, 0x50, 0x5A),
            ),
            strict=True,
        )
    },
}
FLAGS = {
    "shift": Quartz.kCGEventFlagMaskShift,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "cmd": Quartz.kCGEventFlagMaskCommand,
}
BUTTONS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
    "right": (
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGMouseButtonRight,
    ),
    "middle": (
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
        Quartz.kCGMouseButtonCenter,
    ),
}


DELIBERATE = (
    Quartz.kCGEventKeyDown,
    Quartz.kCGEventLeftMouseDown,
    Quartz.kCGEventRightMouseDown,
    Quartz.kCGEventOtherMouseDown,
    Quartz.kCGEventLeftMouseDragged,
    Quartz.kCGEventRightMouseDragged,
    Quartz.kCGEventOtherMouseDragged,
    Quartz.kCGEventScrollWheel,
)


# Undocumented, used by Cua, Hammerspoon and yabai: the CGWindowID of an AX window, and the
# window-relative location a stamped mouse event carries. Loaded once; a release without
# them fails the window action that needs them, never the whole screen.
PRIVATE: dict[str, Callable[..., Any]] = {}
objc.loadBundleFunctions(
    objc.loadBundle(
        "HIServices",
        {},
        "/System/Library/Frameworks/ApplicationServices.framework/Frameworks/HIServices.framework",
    ),
    PRIVATE,
    [("_AXUIElementGetWindow", b"i@o^I")],
)
objc.loadBundleFunctions(
    objc.loadBundle("CoreGraphics", {}, "/System/Library/Frameworks/CoreGraphics.framework"),
    PRIVATE,
    [("CGEventSetWindowLocation", b"v@{CGPoint=dd}")],
)
# Stamped mouse events (spike Q3, Cua input/mouse.rs): subtype, target pid, window number,
# click group, and the window under the pointer that can handle the event.
FIELD_SUBTYPE, FIELD_PID, FIELD_WINDOW, FIELD_GROUP = 7, 40, 51, 58
FIELDS_UNDER_POINTER = (91, 92)
# macOS 27 reads the stamped location as window-relative; earlier releases as a screen point.
WINDOW_LOCAL = int(platform.mac_ver()[0].split(".")[0]) >= 27
# Cua's pacing: the primer settles before the press, a press is held before its release.
PRIMER_SECONDS = 0.03
PRESS_SECONDS = 0.03
TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"})
# Stamped events carry flags, click states and the other button too (task book §6.11).
WINDOW_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "triple_click",
        "right_click",
        "middle_click",
        "move",
        "drag",
        "type",
        "key",
        "scroll",
        "modifiers",
    }
)
# Roles that only group other nodes; listed only when they have a name.
CONTAINER_ROLES = frozenset(
    {"AXGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea", "AXLayoutItem", "AXUnknown"}
)
# Chromium builds its web accessibility tree after the switch is set (spike R12).
ACCESSIBILITY_SECONDS = 4.0
ACCESSIBLE: set[int] = set()


def wait(start: Callable[[Callable[[Any, Any], None]], Any]) -> Any:
    done = threading.Event()
    box: list[Any] = []

    def finished(value: Any, error: Any) -> None:
        box.extend((value, error))
        done.set()

    start(finished)
    if not done.wait(CAPTURE_SECONDS):
        raise TimeoutError("ScreenCaptureKit did not answer")
    if box[0] is None:
        raise OSError(str(box[1]))
    return box[0]


def shareable() -> Any:
    """Every window, on screen or not (other Spaces, hidden apps, covered), and displays."""
    import ScreenCaptureKit

    return wait(
        lambda finished: (
            ScreenCaptureKit.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                True, False, finished
            )
        )
    )


def write_jpeg(image: Any, path: Path) -> None:
    data = Quartz.CFDataCreateMutable(None, 0)
    destination = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    Quartz.CGImageDestinationAddImage(
        destination, image, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.8}
    )
    if not Quartz.CGImageDestinationFinalize(destination):
        raise OSError("JPEG encoding failed")
    path.write_bytes(bytes(data))


def attribute(element: Any, name: str) -> Any:
    error, value = AX.AXUIElementCopyAttributeValue(element, name, None)
    return value if error == 0 else None


def frame(element: Any) -> tuple[float, float, float, float] | None:
    position, size = attribute(element, "AXPosition"), attribute(element, "AXSize")
    if position is None or size is None:
        return None
    _, point = AX.AXValueGetValue(position, AX.kAXValueCGPointType, None)
    _, extent = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    return point.x, point.y, extent.width, extent.height


def role_name(raw: Any) -> str:
    """``AXTextField`` → ``text field``."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", str(raw or "AXUnknown").removeprefix("AX")).lower()


def describe(element: Any) -> dict[str, str]:
    name = attribute(element, "AXTitle") or attribute(element, "AXDescription") or ""
    return {"role": role_name(attribute(element, "AXRole")), "name": str(name)}


def flags_of(modifiers: tuple[str, ...]) -> int:
    flags = 0
    for name in modifiers:
        flags |= FLAGS[name]
    return flags


def action_names(element: Any) -> list[str]:
    error, names = AX.AXUIElementCopyActionNames(element, None)
    return list(names or []) if error == 0 else []


def check(error: int, what: str) -> None:
    # A non-zero answer may still have been acted on (an app busy past the AX timeout):
    # it is an input failure, reported as unknown once anything was posted.
    if error != 0:
        raise OSError(f"{what}: accessibility error {error}")


def subtree(root: Any) -> Iterator[Any]:
    stack = [root]
    while stack:
        element = stack.pop()
        yield element
        stack.extend(attribute(element, "AXChildren") or [])


def front_pid() -> int | None:
    """The owner's frontmost app, as Launch Services knows it now.

    ``NSWorkspace`` only refreshes it from a run loop this process does not have, and the
    system-wide AX ``AXFocusedApplication`` times out (-25204) whenever that app is slow
    to answer accessibility (measured with Finder in front).
    """
    front = subprocess.run(
        ["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, check=True
    ).stdout.strip()
    info = subprocess.run(
        ["/usr/bin/lsappinfo", "info", "-only", "pid", front],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = re.search(r'"?pid"?\s*=\s*(\d+)', info)
    return int(found.group(1)) if found else None


def ax_window(pid: int, number: int) -> Any | None:
    for element in attribute(AX.AXUIElementCreateApplication(pid), "AXWindows") or []:
        error, found = PRIVATE["_AXUIElementGetWindow"](element, None)
        if error == 0 and found == number:
            return element
    return None


@dataclass
class MacNode:
    """One AX element of a window, as listed by ``elements``."""

    element: Any
    depth: int
    role: str
    name: str
    container: bool
    visible: bool = True

    def bounds(self) -> tuple[float, float, float, float] | None:
        error, position = AX.AXUIElementCopyAttributeValue(self.element, "AXPosition", None)
        if error == AX.kAXErrorInvalidUIElement:
            raise Stale(self.name)
        size = attribute(self.element, "AXSize")
        if error != 0 or position is None or size is None:
            return None
        _, point = AX.AXValueGetValue(position, AX.kAXValueCGPointType, None)
        _, extent = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
        return point.x, point.y, extent.width, extent.height

    def press(self) -> bool:
        if "AXPress" not in action_names(self.element):
            return False
        live(AX.AXUIElementPerformAction(self.element, "AXPress"), "AXPress", self.name)
        return True

    def focus(self) -> None:
        live(
            AX.AXUIElementSetAttributeValue(self.element, "AXFocused", True),
            "AXFocused",
            self.name,
        )


def live(error: int, what: str, name: str) -> None:
    """``check`` for a listed node: a destroyed element is stale, not an input failure."""
    if error == AX.kAXErrorInvalidUIElement:
        raise Stale(name)
    check(error, what)


def nodes(root: Any) -> list[MacNode]:
    """The subtree of ``root`` in tree order; an element is visited once even in a cycle."""
    found: list[MacNode] = []
    seen: set[Any] = set()
    stack = [(root, 0)]
    while stack:
        element, depth = stack.pop()
        if element in seen:
            continue
        seen.add(element)
        raw = attribute(element, "AXRole")
        node = describe(element)
        found.append(MacNode(element, depth, node["role"], node["name"], raw in CONTAINER_ROLES))
        children = attribute(element, "AXChildren") or []
        stack.extend((child, depth + 1) for child in reversed(children))
    return found


class Keys:
    """Key presses and typed text from Yuki's private, tagged source; ``_post`` routes them."""

    source: Any
    route = "global_input"

    def _post(self, event: Any) -> None:
        raise NotImplementedError

    def _keyboard(self, code: int, down: bool, flags: int) -> None:
        event = Quartz.CGEventCreateKeyboardEvent(self.source, code, down)
        Quartz.CGEventSetFlags(event, flags)
        self._post(event)

    def key(self, modifiers: tuple[str, ...], key: str) -> str:
        flags = 0
        pressed: list[str] = []
        try:
            for name in modifiers:
                flags |= FLAGS[name]
                self._keyboard(KEYCODES[name], True, flags)
                pressed.append(name)
            self._keyboard(KEYCODES[key], True, flags)
            self._keyboard(KEYCODES[key], False, flags)
        finally:
            for name in reversed(pressed):
                flags &= ~FLAGS[name]
                self._keyboard(KEYCODES[name], False, flags)
        return self.route

    def type(self, text: str) -> str:
        for character in text:
            if character in "\r\n\t":
                if character != "\r":
                    self.key((), "return" if character == "\n" else "tab")
                continue
            # The Unicode string replaces the key's character, whatever the keyboard layout.
            units = len(character.encode("utf-16-le")) // 2
            for down in (True, False):
                event = Quartz.CGEventCreateKeyboardEvent(self.source, 0, down)
                Quartz.CGEventKeyboardSetUnicodeString(event, units, character)
                self._post(event)
        return self.route


class MacScreen(Keys):
    def __init__(self) -> None:
        # CoreGraphics must be initialised before ScreenCaptureKit builds a window filter,
        # or the process aborts in CGS_REQUIRE_INIT (spike R1), which nothing can catch.
        Quartz.CGMainDisplayID()
        # A private state source: Yuki's events neither read nor change the owner's
        # modifier state, and each carries EVENT_TAG for anyone observing events.
        self.source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
        # Hit tests run under the desk's own time limit (desk.bounded): the system-wide
        # messaging timeout would change every accessibility call, AXPress included.
        self.system = AX.AXUIElementCreateSystemWide()

    def permissions(self) -> tuple[bool, bool]:
        return bool(Quartz.CGPreflightScreenCaptureAccess()), bool(
            Quartz.CGPreflightPostEventAccess()
        )

    def displays(self) -> list[Display]:
        _, identities, count = Quartz.CGGetActiveDisplayList(32, None, None)
        main = Quartz.CGMainDisplayID()
        result = []
        for identity in identities[:count]:
            bounds = Quartz.CGDisplayBounds(identity)
            mode = Quartz.CGDisplayCopyDisplayMode(identity)
            pixels = (
                Quartz.CGDisplayModeGetPixelWidth(mode),
                Quartz.CGDisplayModeGetPixelHeight(mode),
            )
            result.append(
                Display(
                    str(identity),
                    bounds.origin.x,
                    bounds.origin.y,
                    bounds.size.width,
                    bounds.size.height,
                    pixels[0],
                    pixels[1],
                    identity == main,
                    pixels[0] / bounds.size.width,
                )
            )
        return result

    def unavailable(self) -> dict[str, Any] | None:
        session = Quartz.CGSessionCopyCurrentDictionary() or {}
        if not session.get("kCGSSessionOnConsoleKey"):
            return {
                "error": "desktop_unavailable",
                "message": "这台 Mac 当前显示的是其他用户或登录窗口，Yuki 不能看或操作屏幕。",
            }
        if session.get("CGSSessionScreenIsLocked"):
            return {
                "error": "display_locked",
                "message": "这台 Mac 已锁屏；本人解锁后才能看和操作屏幕。",
            }
        if Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()):
            return {
                "error": "display_locked",
                "message": "这台 Mac 的显示器已休眠；本人唤醒后才能看和操作屏幕。",
            }
        return None

    def capture(
        self,
        display: Display,
        width: int,
        height: int,
        path: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> None:
        try:
            image = self._screenshot(display, width, height, region)
        except (OSError, StopIteration) as error:
            # Deprecated since the macOS 15 SDK; kept only as the fallback (task book §5.7).
            logger.warning("ScreenCaptureKit capture failed (%s); using CGWindowList", error)
            image = self._window_list(display, width, height, region)
        write_jpeg(image, path)

    def pointer(self) -> tuple[float, float] | None:
        location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return float(location.x), float(location.y)

    def hit(self, x: float, y: float) -> dict[str, str] | None:
        error, element = AX.AXUIElementCopyElementAtPosition(self.system, x, y, None)
        return describe(element) if error == 0 and element is not None else None

    def _screenshot(
        self,
        display: Display,
        width: int,
        height: int,
        region: tuple[float, float, float, float] | None,
    ) -> Any:
        import ScreenCaptureKit

        content = shareable()
        target = next(item for item in content.displays() if str(item.displayID()) == display.id)
        selection = ScreenCaptureKit.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            target, []
        )
        configuration = ScreenCaptureKit.SCStreamConfiguration.alloc().init()
        # The source produces the final size; nothing rescales it later (§5.2).
        configuration.setWidth_(width)
        configuration.setHeight_(height)
        configuration.setShowsCursor_(True)
        if region is not None:
            # Points from the display's top-left; the output is never larger than the
            # region's own pixels, which ScreenCaptureKit would pad instead (P0 item 8).
            configuration.setSourceRect_(Quartz.CGRectMake(*region))
        manager = ScreenCaptureKit.SCScreenshotManager
        return wait(
            lambda finished: manager.captureImageWithFilter_configuration_completionHandler_(
                selection, configuration, finished
            )
        )

    @staticmethod
    def _window_list(
        display: Display,
        width: int,
        height: int,
        region: tuple[float, float, float, float] | None,
    ) -> Any:
        left, top, extent_x, extent_y = region or (0, 0, display.width, display.height)
        full = Quartz.CGWindowListCreateImage(
            Quartz.CGRectMake(display.x + left, display.y + top, extent_x, extent_y),
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if full is None:
            raise OSError("CGWindowListCreateImage returned no image")
        context = Quartz.CGBitmapContextCreate(
            None,
            width,
            height,
            8,
            0,
            Quartz.CGColorSpaceCreateDeviceRGB(),
            Quartz.kCGImageAlphaNoneSkipLast,
        )
        Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
        Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), full)
        return Quartz.CGBitmapContextCreateImage(context)

    def last_input(self) -> tuple[float, float]:
        # The system measures "seconds since" at some instant between the two clock reads.
        before = time.monotonic()
        since = self.idle()
        after = time.monotonic()
        return before - since, after - since

    def idle(self) -> float:
        # Combined session state: hardware, Screen Sharing / remote control and any other
        # process posting into this login session, Yuki's own events included. Only
        # deliberate input: the owner merely moving the pointer is not using the computer.
        return min(
            float(
                Quartz.CGEventSourceSecondsSinceLastEventType(
                    Quartz.kCGEventSourceStateCombinedSessionState, kind
                )
            )
            for kind in DELIBERATE
        )

    def elevated(self, point: tuple[float, float] | None) -> bool:
        # macOS has no integrity levels between the owner's own windows.
        return False

    def _post(self, event: Any) -> None:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, EVENT_TAG)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _mouse(
        self, kind: int, x: float, y: float, button: int, clicks: int = 1, flags: int = 0
    ) -> None:
        event = Quartz.CGEventCreateMouseEvent(self.source, kind, (x, y), button)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
        # Modifiers ride on the events themselves: a private source holds no key state.
        Quartz.CGEventSetFlags(event, flags)
        self._post(event)

    def move(self, x: float, y: float) -> str:
        self._mouse(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft)
        return self.route

    def click(self, x: float, y: float, button: str, count: int, modifiers: tuple[str, ...]) -> str:
        down, up, code = BUTTONS[button]
        flags = flags_of(modifiers)
        self.move(x, y)
        for clicks in range(1, count + 1):
            self._mouse(down, x, y, code, clicks, flags)
            self._mouse(up, x, y, code, clicks, flags)
        return self.route

    def drag(self, x: float, y: float, x2: float, y2: float, modifiers: tuple[str, ...]) -> str:
        flags = flags_of(modifiers)
        self.move(x, y)
        self._mouse(Quartz.kCGEventLeftMouseDown, x, y, Quartz.kCGMouseButtonLeft, 1, flags)
        try:
            steps = 12
            for step in range(1, steps + 1):
                # Applications track a drag from intermediate positions, not only its end.
                time.sleep(0.01)
                self._mouse(
                    Quartz.kCGEventLeftMouseDragged,
                    x + (x2 - x) * step / steps,
                    y + (y2 - y) * step / steps,
                    Quartz.kCGMouseButtonLeft,
                    1,
                    flags,
                )
        finally:
            self._mouse(Quartz.kCGEventLeftMouseUp, x2, y2, Quartz.kCGMouseButtonLeft, 1, flags)
        return self.route

    def scroll(self, x: float, y: float, dx: int, dy: int, modifiers: tuple[str, ...]) -> str:
        self.move(x, y)
        # Wheel 1 is vertical (positive scrolls up), wheel 2 horizontal (positive left).
        event = Quartz.CGEventCreateScrollWheelEvent2(
            self.source, Quartz.kCGScrollEventUnitLine, 2, -dy, -dx, 0
        )
        Quartz.CGEventSetFlags(event, flags_of(modifiers))
        self._post(event)
        return self.route

    # One window (phase 3b)

    def windows(self) -> list[dict[str, str]]:
        own = os.getpid()
        return [
            {
                "id": str(item.windowID()),
                "app": str(item.owningApplication().applicationName()),
                "title": str(item.title()),
            }
            for item in shareable().windows()
            # Layer 0 is ordinary app windows: no menu bar, Dock, status items or overlays.
            if item.windowLayer() == 0
            and item.title()
            and item.owningApplication() is not None
            and item.owningApplication().processID() != own
        ]

    def window(self, identity: str) -> MacTarget:
        import ScreenCaptureKit

        missing = Refused(
            "window_not_found",
            f"找不到窗口 {identity}（可能已关闭）；"
            "先 computer(action=screenshot) 看 windows 列表。",
        )
        if not identity.isdigit():
            raise missing
        item = next(
            (item for item in shareable().windows() if item.windowID() == int(identity)), None
        )
        if item is None or item.owningApplication() is None:
            raise missing
        pid = int(item.owningApplication().processID())
        element = ax_window(pid, int(identity))
        # The minimized state is read, never guessed from pixels: ScreenCaptureKit keeps
        # returning a minimized Chrome window's last frame (spike R4).
        if element is not None and attribute(element, "AXMinimized"):
            raise Refused(
                "window_unavailable",
                "这个窗口已最小化；Yuki 不还原它（还原会抢走前台）。本人还原后再试，"
                "或省略 window 在前台操作。",
            )
        selection = ScreenCaptureKit.SCContentFilter.alloc().initWithDesktopIndependentWindow_(item)
        scale = float(selection.pointPixelScale())
        bounds = item.frame()
        return MacTarget(
            Window(
                identity,
                str(item.owningApplication().applicationName()),
                str(item.title() or ""),
                bounds.origin.x,
                bounds.origin.y,
                bounds.size.width,
                bounds.size.height,
                round(bounds.size.width * scale),
                round(bounds.size.height * scale),
            ),
            pid,
            selection,
            element,
            self.source,
            # Outside the owner's current Space no app-level AX sees the window and stamped
            # events miss it; a key there makes the app open a window here and come to the
            # front (measured). Such a window is only looked at.
            WINDOW_ACTIONS if item.isOnScreen() else frozenset(),
        )


@dataclass
class MacTarget(Keys):
    """One window: its own capture, its own AX subtree, its own process."""

    window: Window
    pid: int
    selection: Any
    element: Any | None
    source: Any
    actions: frozenset[str] = field(default=WINDOW_ACTIONS)
    group: int = field(default_factory=lambda: time.time_ns() & 0x7FFFFFFF)
    route = "window_events"

    def collides(self) -> bool:
        if front_pid() == self.pid:
            return True
        pointer = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        listed = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        # Front to back: the first ordinary window containing the pointer is under it.
        for info in listed or []:
            bounds = info.get("kCGWindowBounds") or {}
            if info.get("kCGWindowLayer") == 0 and (
                bounds["X"] <= pointer.x < bounds["X"] + bounds["Width"]
                and bounds["Y"] <= pointer.y < bounds["Y"] + bounds["Height"]
            ):
                return int(info["kCGWindowOwnerPID"]) == self.pid
        return False

    def elevated(self) -> bool:
        return False

    def capture(
        self,
        width: int,
        height: int,
        path: Path,
        region: tuple[float, float, float, float] | None = None,
    ) -> None:
        import ScreenCaptureKit

        configuration = ScreenCaptureKit.SCStreamConfiguration.alloc().init()
        configuration.setWidth_(width)
        configuration.setHeight_(height)
        configuration.setShowsCursor_(False)
        configuration.setIgnoreShadowsSingleWindow_(True)
        if region is not None:
            # Points from the window's top-left (P0 item 8).
            configuration.setSourceRect_(Quartz.CGRectMake(*region))
        manager = ScreenCaptureKit.SCScreenshotManager
        write_jpeg(
            wait(
                lambda finished: manager.captureImageWithFilter_configuration_completionHandler_(
                    self.selection, configuration, finished
                )
            ),
            path,
        )

    # Accessibility first

    def _accessible(self) -> None:
        """Chromium and Electron expose their web content only once asked (decision 4)."""
        if self.pid in ACCESSIBLE or self.element is None:
            return
        ACCESSIBLE.add(self.pid)
        application = AX.AXUIElementCreateApplication(self.pid)
        if AX.AXUIElementSetAttributeValue(application, "AXManualAccessibility", True) != 0:
            # Not Chromium: native apps answer accessibility without being asked.
            return
        deadline = time.monotonic() + ACCESSIBILITY_SECONDS
        while time.monotonic() < deadline and not any(
            attribute(element, "AXRole") == "AXWebArea" for element in subtree(self.element)
        ):
            time.sleep(0.25)

    def _element_at(self, x: float, y: float) -> Any | None:
        """The smallest element of this window's own subtree containing the point."""
        if self.element is None:
            return None
        best, area = None, None
        stack = [self.element]
        while stack:
            element = stack.pop()
            bounds = frame(element)
            inside = bounds is not None and (
                bounds[0] <= x < bounds[0] + bounds[2] and bounds[1] <= y < bounds[1] + bounds[3]
            )
            if inside:
                assert bounds is not None
                if area is None or bounds[2] * bounds[3] <= area:
                    best, area = element, bounds[2] * bounds[3]
            # Elements outside the point hold nothing at it, except frameless groups.
            if inside or bounds is None or bounds[2] * bounds[3] == 0:
                stack.extend(attribute(element, "AXChildren") or [])
        return best

    def _ancestors(self, x: float, y: float) -> Iterator[Any]:
        node = self._element_at(x, y)
        while node is not None and node != self.element:
            yield node
            node = attribute(node, "AXParent")

    def _field(self, x: float, y: float) -> Any | None:
        return next(
            (node for node in self._ancestors(x, y) if attribute(node, "AXRole") in TEXT_ROLES),
            None,
        )

    def hit(self, x: float, y: float) -> dict[str, str] | None:
        self._accessible()
        node = next(self._ancestors(x, y), None)
        return describe(node) if node is not None else None

    def elements(self) -> list[MacNode]:
        if self.element is None:
            raise Refused(
                "device_not_supported",
                "这个窗口没有可读的无障碍信息（辅助功能找不到它），不能列出节点；"
                "请按截图坐标操作。",
                cause="no_accessibility",
            )
        self._accessible()
        return nodes(self.element)

    def focus_element(self, node: Any) -> None:
        bounds = node.bounds()
        if bounds is None:
            raise Refused("element_not_visible", "这个节点现在没有位置，不能聚焦；没有执行。")
        self._focus(node.element, bounds[0] + bounds[2] / 2, bounds[1] + bounds[3] / 2)

    def key(self, modifiers: tuple[str, ...], key: str) -> str:
        self._key_window()
        return super().key(modifiers, key)

    def type(self, text: str) -> str:
        self._key_window()
        return super().type(text)

    def _key_window(self) -> None:
        # Keys reach the app's key window, which can be another of its windows; with a
        # separate note open, Notes took them there and cmd+n even activated the app
        # (measured). AXMain moves the key window without activating the app.
        if self.element is not None:
            check(AX.AXUIElementSetAttributeValue(self.element, "AXMain", True), "AXMain")

    def focus(self, x: float, y: float) -> str:
        self._accessible()
        editable = self._field(x, y)
        if editable is None:
            # Nothing accessibility calls a text field (a terminal, a canvas editor): a
            # stamped click there makes it the app's focus, as the owner's click would.
            self._click(x, y, "left", 1, 0)
            return "window_events"
        return self._focus(editable, x, y)

    def _focus(self, editable: Any, x: float, y: float) -> str:
        check(AX.AXUIElementSetAttributeValue(editable, "AXFocused", True), "AXFocused")
        if not attribute(editable, "AXFocused"):
            # An app that was never active has no key window, and AppKit then ignores the
            # request (measured); a stamped click on the field focuses it in the background.
            self._click(x, y, "left", 1, 0)
            return "window_events"
        return "accessibility"

    def click(self, x: float, y: float, button: str, count: int, modifiers: tuple[str, ...]) -> str:
        # Accessibility actions carry no modifiers, click states or other buttons (A7).
        if count == 1 and not modifiers and button in {"left", "right"}:
            self._accessible()
            wanted = "AXShowMenu" if button == "right" else "AXPress"
            for node in self._ancestors(x, y):
                if button == "left" and attribute(node, "AXRole") in TEXT_ROLES:
                    return self._focus(node, x, y)
                if wanted in action_names(node):
                    check(AX.AXUIElementPerformAction(node, wanted), wanted)
                    return "accessibility"
        self._click(x, y, button, count, flags_of(modifiers))
        return "window_events"

    def _click(self, x: float, y: float, button: str, count: int, flags: int) -> None:
        down, up, code = BUTTONS[button]
        self._prime(x, y)
        for clicks in range(1, count + 1):
            self._stamped(down, x, y, code, clicks, flags)
            try:
                time.sleep(PRESS_SECONDS)
            finally:
                # A press is always released (A17).
                self._stamped(up, x, y, code, clicks, flags)

    # Stamped events where accessibility has nothing

    def _post(self, event: Any) -> None:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, EVENT_TAG)
        Quartz.CGEventPostToPid(self.pid, event)

    def _stamp(self, event: Any, x: float, y: float) -> None:
        number = int(self.window.id)
        Quartz.CGEventSetIntegerValueField(event, FIELD_SUBTYPE, 3)
        Quartz.CGEventSetIntegerValueField(event, FIELD_PID, self.pid)
        Quartz.CGEventSetIntegerValueField(event, FIELD_WINDOW, number)
        Quartz.CGEventSetIntegerValueField(event, FIELD_GROUP, self.group)
        for name in FIELDS_UNDER_POINTER:
            Quartz.CGEventSetIntegerValueField(event, name, number)
        PRIVATE["CGEventSetWindowLocation"](
            event, (x - self.window.x, y - self.window.y) if WINDOW_LOCAL else (x, y)
        )
        self._post(event)

    def _stamped(
        self, kind: int, x: float, y: float, button: int, clicks: int = 1, flags: int = 0
    ) -> None:
        event = Quartz.CGEventCreateMouseEvent(self.source, kind, (x, y), button)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
        Quartz.CGEventSetFlags(event, flags)
        self._stamp(event, x, y)

    def _prime(self, x: float, y: float) -> None:
        # Without a moved event first, background AppKit controls ignore the press.
        self._stamped(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft, 0)
        time.sleep(PRIMER_SECONDS)

    def move(self, x: float, y: float) -> str:
        self._stamped(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft, 0)
        return self.route

    def drag(self, x: float, y: float, x2: float, y2: float, modifiers: tuple[str, ...]) -> str:
        flags = flags_of(modifiers)
        self._prime(x, y)
        self._stamped(Quartz.kCGEventLeftMouseDown, x, y, Quartz.kCGMouseButtonLeft, 1, flags)
        try:
            steps = 12
            for step in range(1, steps + 1):
                time.sleep(0.01)
                self._stamped(
                    Quartz.kCGEventLeftMouseDragged,
                    x + (x2 - x) * step / steps,
                    y + (y2 - y) * step / steps,
                    Quartz.kCGMouseButtonLeft,
                    1,
                    flags,
                )
        finally:
            self._stamped(Quartz.kCGEventLeftMouseUp, x2, y2, Quartz.kCGMouseButtonLeft, 1, flags)
        return self.route

    def scroll(self, x: float, y: float, dx: int, dy: int, modifiers: tuple[str, ...]) -> str:
        self._prime(x, y)
        event = Quartz.CGEventCreateScrollWheelEvent2(
            self.source, Quartz.kCGScrollEventUnitLine, 2, -dy, -dx, 0
        )
        Quartz.CGEventSetLocation(event, (x, y))
        Quartz.CGEventSetFlags(event, flags_of(modifiers))
        self._stamp(event, x, y)
        return self.route
