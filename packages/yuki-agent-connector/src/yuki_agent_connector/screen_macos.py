"""macOS 14+: ScreenCaptureKit screenshots and CGEventPost input from a private, tagged source.

Permissions belong to the responsible process (the signed Yuki.app when Desktop owns the
Connector). Without Accessibility, ``CGEventPost`` drops events silently, so every input is
preceded by ``CGPreflightPostEventAccess``; nothing here ever requests a permission.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .screen import EVENT_TAG, Display

assert sys.platform == "darwin"

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
}


class MacScreen:
    def __init__(self) -> None:
        # A private state source: Yuki's events neither read nor change the owner's
        # modifier state, and each carries EVENT_TAG for anyone observing events.
        self.source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)

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

    def capture(self, display: Display, width: int, height: int, path: Path) -> None:
        try:
            image = self._screenshot(display, width, height)
        except (OSError, StopIteration) as error:
            # Deprecated since the macOS 15 SDK; kept only as the fallback (task book §5.7).
            logger.warning("ScreenCaptureKit capture failed (%s); using CGWindowList", error)
            image = self._window_list(display, width, height)
        data = Quartz.CFDataCreateMutable(None, 0)
        destination = Quartz.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
        Quartz.CGImageDestinationAddImage(
            destination, image, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.8}
        )
        if not Quartz.CGImageDestinationFinalize(destination):
            raise OSError("JPEG encoding failed")
        path.write_bytes(bytes(data))

    def _screenshot(self, display: Display, width: int, height: int) -> Any:
        import ScreenCaptureKit

        def wait(start: Any) -> Any:
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

        content = wait(
            ScreenCaptureKit.SCShareableContent.getShareableContentWithCompletionHandler_
        )
        target = next(item for item in content.displays() if str(item.displayID()) == display.id)
        selection = ScreenCaptureKit.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            target, []
        )
        configuration = ScreenCaptureKit.SCStreamConfiguration.alloc().init()
        # The source produces the final size; nothing rescales it later (§5.2).
        configuration.setWidth_(width)
        configuration.setHeight_(height)
        configuration.setShowsCursor_(True)
        manager = ScreenCaptureKit.SCScreenshotManager
        return wait(
            lambda finished: manager.captureImageWithFilter_configuration_completionHandler_(
                selection, configuration, finished
            )
        )

    @staticmethod
    def _window_list(display: Display, width: int, height: int) -> Any:
        full = Quartz.CGWindowListCreateImage(
            Quartz.CGRectMake(display.x, display.y, display.width, display.height),
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

    def seconds_since_input(self) -> float:
        # Combined session state: hardware, Screen Sharing / remote control and any other
        # process posting into this login session, Yuki's own events included.
        return float(
            Quartz.CGEventSourceSecondsSinceLastEventType(
                Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType
            )
        )

    def elevated(self, point: tuple[float, float] | None) -> bool:
        # macOS has no integrity levels between the owner's own windows.
        return False

    def _post(self, event: Any) -> None:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, EVENT_TAG)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def _mouse(self, kind: int, x: float, y: float, button: int, clicks: int = 1) -> None:
        event = Quartz.CGEventCreateMouseEvent(self.source, kind, (x, y), button)
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
        self._post(event)

    def move(self, x: float, y: float) -> None:
        self._mouse(Quartz.kCGEventMouseMoved, x, y, Quartz.kCGMouseButtonLeft)

    def click(self, x: float, y: float, button: str, count: int) -> None:
        down, up, code = BUTTONS[button]
        self.move(x, y)
        for clicks in range(1, count + 1):
            self._mouse(down, x, y, code, clicks)
            self._mouse(up, x, y, code, clicks)

    def drag(self, x: float, y: float, x2: float, y2: float) -> None:
        self.move(x, y)
        self._mouse(Quartz.kCGEventLeftMouseDown, x, y, Quartz.kCGMouseButtonLeft)
        steps = 12
        for step in range(1, steps + 1):
            # Applications track a drag from intermediate positions, not only its end.
            time.sleep(0.01)
            self._mouse(
                Quartz.kCGEventLeftMouseDragged,
                x + (x2 - x) * step / steps,
                y + (y2 - y) * step / steps,
                Quartz.kCGMouseButtonLeft,
            )
        self._mouse(Quartz.kCGEventLeftMouseUp, x2, y2, Quartz.kCGMouseButtonLeft)

    def scroll(self, x: float, y: float, dx: int, dy: int) -> None:
        self.move(x, y)
        # Wheel 1 is vertical (positive scrolls up), wheel 2 horizontal (positive left).
        self._post(
            Quartz.CGEventCreateScrollWheelEvent2(
                self.source, Quartz.kCGScrollEventUnitLine, 2, -dy, -dx, 0
            )
        )

    def _keyboard(self, code: int, down: bool, flags: int) -> None:
        event = Quartz.CGEventCreateKeyboardEvent(self.source, code, down)
        Quartz.CGEventSetFlags(event, flags)
        self._post(event)

    def key(self, modifiers: tuple[str, ...], key: str) -> None:
        flags = 0
        for name in modifiers:
            flags |= FLAGS[name]
            self._keyboard(KEYCODES[name], True, flags)
        self._keyboard(KEYCODES[key], True, flags)
        self._keyboard(KEYCODES[key], False, flags)
        for name in reversed(modifiers):
            flags &= ~FLAGS[name]
            self._keyboard(KEYCODES[name], False, flags)

    def type(self, text: str) -> None:
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
