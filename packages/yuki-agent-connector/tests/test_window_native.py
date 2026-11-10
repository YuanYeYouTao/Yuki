"""One window on the real screen backend, against windows this test opens itself (phase 3b).

Failure cases: docs/operations/computer-3b-20261110.md §1 (W1–W5, W8–W12, W14, M6 for a
window). The window is covered by another window of the same test host, so a system or app
hit test, a screen capture or global input would all reach the wrong window.

- macOS: needs Screen Recording and Accessibility for the process running pytest, which
  hosted runners cannot grant; it runs only with ``YUKI_NATIVE_WINDOWS=1`` on a developer's
  Mac (never touching any other app). Without the grants a window screenshot is refused by
  name, which CI checks.
- Windows: the hosted runner has an interactive desktop; on CI (or with
  ``YUKI_NATIVE_WINDOWS=1``) a Win32 test window with standard controls is driven through UI
  Automation.

Artifact: test-results/device-computer/window-native-<platform>.json (and the captures).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from desk_support import ARTIFACTS

from yuki_agent_connector.desk import Desk
from yuki_agent_connector.screen import Window, frame, native_backend

LIMITS = {"max_dimension": 1568, "max_pixels": 1_500_000}
FAKES = Path(__file__).resolve().parent / "fakes"
NATIVE = bool(os.environ.get("YUKI_NATIVE_WINDOWS"))


def host_events(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def until(check: Any, seconds: float = 10.0) -> Any:
    deadline = time.monotonic() + seconds
    while not (value := check()):
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        time.sleep(0.05)
    return value


class Calls:
    def __init__(self, desk: Desk, record: dict[str, Any]) -> None:
        self.desk, self.record, self.count = desk, record, 0

    async def __call__(self, action: str, **arguments: Any) -> dict[str, Any]:
        self.count += 1
        identity = f"window-{self.count}-{action}"
        result = await self.desk.execute(
            {
                "id": identity,
                "resource": "computer",
                "action": action,
                "arguments": {**LIMITS, **arguments},
            }
        )
        self.record.setdefault("calls", []).append(
            {"id": identity, "action": action, "arguments": arguments, "result": result}
        )
        image = result.get("image")
        # Only the test's own window is kept; a whole-screen picture would hold whatever
        # else is on this computer's screen.
        if image and "window" in arguments:
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            shutil.copy(image["path"], ARTIFACTS / f"window-{sys.platform}-{identity}.jpg")
        return result


def start_host(name: str, *arguments: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-B", str(FAKES / name), *arguments])


def image_point(window: Window, x: float, y: float) -> dict[str, int]:
    """A point given in the window's own coordinates (points/pixels from its top-left)."""
    width, _ = frame(window, **LIMITS)
    scale = width / window.width
    return {"x": round(x * scale), "y": round(y * scale)}


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS window backend")
async def test_macos_window_in_the_background(tmp_path: Path) -> None:
    backend = native_backend()
    assert backend is not None
    lines: list[dict[str, Any]] = []
    desk = Desk(tmp_path / "data", backend, lines.append, lambda: None)
    record: dict[str, Any] = {"platform": sys.platform, "lines": lines}
    call = Calls(desk, record)
    screen, accessibility = backend.permissions()
    try:
        if not (screen and accessibility):
            # W5: without both grants a window is refused by name, never a stale picture.
            refused = await call("screenshot", window="1")
            expected = (
                "screen_permission_required" if not screen else "input_permission_required"
            )
            assert refused["error"] == expected, refused
            return
        if not NATIVE:
            pytest.skip("opt in with YUKI_NATIVE_WINDOWS=1: opens test windows on this Mac")
        await _macos_host_session(tmp_path, backend, call, record)
    finally:
        await desk.close()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"window-native-{sys.platform}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )


async def _macos_host_session(
    tmp_path: Path, backend: Any, call: Calls, record: dict[str, Any]
) -> None:
    import Quartz

    from yuki_agent_connector.screen_macos import attribute, ax_window, front_pid

    log = tmp_path / "host.jsonl"
    host = start_host("mac_window_host.py", "w3b", "200", "300", str(log))

    def pointer() -> tuple[float, float]:
        location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return location.x, location.y

    def front() -> int | None:
        return front_pid()

    try:
        until(lambda: [e for e in host_events(log) if e["ev"] == "window"][1:])
        a, b = (
            next(e["win"] for e in host_events(log) if e.get("title", "").endswith(side))
            for side in (" A", " B")
        )
        before = {"pointer": pointer(), "front": front()}
        record["before"] = before
        assert before["front"] != host.pid, "the test host must not be the frontmost app"

        # W2: real windows, ids usable as window; the Connector's own process is not listed.
        listed = await call("screenshot")
        ids = {item["id"]: item for item in listed["windows"]}
        assert str(a) in ids and str(b) in ids, listed["windows"]
        assert ids[str(a)]["title"] == "Yuki test w3b A"

        # W1/W3/W6/M6: A alone, at the size the profile allows, answered with window not display.
        shot = await call("screenshot", window=str(a))
        target = backend.window(str(a))
        window = target.window
        assert (shot["width"], shot["height"]) == frame(window, **LIMITS)
        assert shot["window"] == str(a) and "display" not in shot
        assert not target.collides()
        # Content coordinates are AppKit's (bottom-up); the window image is top-down.
        def content(x: float, y: float) -> dict[str, int]:
            return image_point(window, x, window.height - y)

        # W8: A's button is partly covered by B; the press reaches A's own button.
        await call("click", window=str(a), **content(150, 286))
        until(lambda: [e for e in host_events(log) if e["ev"] == "buttonAction"])
        pressed = [e for e in host_events(log) if e["ev"] == "buttonAction"]
        assert [e["win"] for e in pressed] == [a]

        # W11: click focuses A's field (AX), then keys reach it, not B's field.
        await call("click", window=str(a), **content(290, 286))
        typed = await call("type", window=str(a), text="abc")
        assert typed["submitted"] is True

        def field(number: int) -> str:
            element = ax_window(host.pid, number)
            fields = [
                node
                for node in _walk(element, attribute)
                if attribute(node, "AXRole") == "AXTextField"
            ]
            return str(attribute(fields[0], "AXValue") or "")

        until(lambda: field(a) == "abc")
        await call("type", window=str(b), text="xyz", **image_point(
            backend.window(str(b)).window, 290, backend.window(str(b)).window.height - 286
        ))
        until(lambda: field(b) == "xyz")
        await call("key", window=str(a), text="backspace", **content(290, 286))
        until(lambda: field(a) == "ab")
        assert field(b) == "xyz"

        # W10: the custom pad has no AX element; stamped events reach covered window A.
        await call("click", window=str(a), **content(100, 50))
        await call("right_click", window=str(a), **content(110, 50))
        await call("double_click", window=str(a), **content(120, 50))
        await call("scroll", window=str(a), dy=3, **content(130, 50))
        await call("drag", window=str(a), **content(140, 50), x2=content(200, 60)["x"],
                   y2=content(200, 60)["y"])
        await call("move", window=str(a), **content(150, 50))
        await asyncio.sleep(0.5)
        pad = [e for e in host_events(log) if e.get("x") is not None]
        record["pad"] = pad
        kinds = {e["ev"] for e in pad}
        assert all(e["win"] == a for e in pad), pad
        assert {"mouseDown", "rightMouseDown", "scroll", "mouseDragged", "mouseUp"} <= kinds, kinds
        first = next(e for e in pad if e["ev"] == "mouseDown")
        assert abs(first["x"] - 100) <= 2 and abs(first["y"] - 50) <= 2, first
        assert any(e["ev"] == "mouseDown" and e["clicks"] == 2 for e in pad)
        record["moved"] = "mouseMoved" in kinds

        # W9: nothing moved the owner's pointer, changed the frontmost app or activated A.
        after = {"pointer": pointer(), "front": front()}
        record["after"] = after
        assert after == before
        assert not any(e["active"] for e in host_events(log))

        # W4: minimized → window_unavailable; closed → window_not_found.
        log.with_suffix(".cmd").write_text("mini A")
        until(lambda: [e for e in host_events(log) if e["ev"] == "cmd"])
        await asyncio.sleep(0.5)
        assert (await call("screenshot", window=str(a)))["error"] == "window_unavailable"
        assert (await call("click", window=str(a), x=1, y=1))["error"] == "window_unavailable"
    finally:
        host.kill()
        host.wait()
    await asyncio.sleep(0.5)
    gone = await call("screenshot", window=str(a))
    assert gone["error"] == "window_not_found"
    assert (await call("screenshot", window="not-a-window"))["error"] == "window_not_found"


def _walk(root: Any, attribute: Any) -> list[Any]:
    found, stack = [], [root]
    while stack:
        node = stack.pop()
        found.append(node)
        stack.extend(attribute(node, "AXChildren") or [])
    return found


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows window backend")
async def test_windows_window_through_ui_automation(tmp_path: Path) -> None:
    if not (NATIVE or os.environ.get("CI")):
        pytest.skip("opt in with YUKI_NATIVE_WINDOWS=1: opens test windows on this computer")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    backend = native_backend()
    assert backend is not None
    lines: list[dict[str, Any]] = []
    desk = Desk(tmp_path / "data", backend, lines.append, lambda: None)
    record: dict[str, Any] = {"platform": sys.platform, "lines": lines}
    call = Calls(desk, record)
    log = tmp_path / "host.jsonl"
    host = start_host("win_window_host.py", str(log))

    def cursor() -> tuple[int, int]:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def text(handle: int) -> str:
        # WM_GETTEXT: GetWindowText does not read another process's control text.
        buffer = ctypes.create_unicode_buffer(4096)
        user32.SendMessageW(handle, 0x000D, len(buffer), ctypes.addressof(buffer))
        return buffer.value

    def center(handle: int, window: Window) -> dict[str, int]:
        rect = wintypes.RECT()
        user32.GetWindowRect(handle, ctypes.byref(rect))
        return image_point(
            window,
            (rect.left + rect.right) / 2 - window.x,
            (rect.top + rect.bottom) / 2 - window.y,
        )

    main = 0
    try:
        ready = until(lambda: next((e for e in host_events(log) if e["ev"] == "ready"), None))
        main = ready["main"]
        record["host"] = ready
        before = {"cursor": cursor(), "foreground": user32.GetForegroundWindow()}
        record["before"] = before

        # W2: the host is listed and its id is what window takes.
        listed = await call("screenshot")
        ids = {item["id"]: item for item in listed["windows"]}
        assert ids[str(main)]["title"] == "Yuki test host", listed["windows"]
        assert ids[str(main)]["app"].lower().startswith("python")

        # W3/W6/M6: PrintWindow of the covered window at the size the profile allows.
        shot = await call("screenshot", window=str(main))
        window = backend.window(str(main)).window
        assert (shot["width"], shot["height"]) == frame(window, **LIMITS)
        assert shot["window"] == str(main) and "display" not in shot

        # W9 after each pattern: the cursor never moves and no third window comes to the
        # front. Whether the host itself is activated is the UI Automation provider's
        # behaviour, recorded rather than frozen: the classic Win32 proxies measured on CI
        # (2026-11-10) activated it for Value and Invoke.
        effects: dict[str, Any] = record.setdefault("foreground_after", {})

        def observe(step: str) -> None:
            now = {"cursor": cursor(), "foreground": user32.GetForegroundWindow()}
            effects[step] = {**now, "host_active": now["foreground"] == main}
            assert now["cursor"] == before["cursor"], step
            assert now["foreground"] in {before["foreground"], main}, step

        # The scroll pattern of the multi-line edit.
        first_line = 0x00CE  # EM_GETFIRSTVISIBLELINE
        assert user32.SendMessageW(ready["lines"], first_line, 0, 0) == 0
        scrolled = await call("scroll", window=str(main), dy=5, **center(ready["lines"], window))
        assert scrolled["submitted"] is True, scrolled
        until(lambda: user32.SendMessageW(ready["lines"], first_line, 0, 0) > 0)
        observe("scroll")

        # W11: type at x,y writes the edit's value (appended), without keyboard input.
        at_edit = center(ready["edit"], window)
        typed = await call("type", window=str(main), text="abc", **at_edit)
        assert typed["submitted"] is True, typed
        until(lambda: text(ready["edit"]) == "abc")
        await call("type", window=str(main), text="de", **at_edit)
        until(lambda: text(ready["edit"]) == "abcde")
        observe("value")

        # W8: the button is under the topmost cover; Invoke reaches the host's button.
        pressed = await call("click", window=str(main), **center(ready["button"], window))
        assert pressed["submitted"] is True, pressed
        until(lambda: [e for e in host_events(log) if e["ev"] == "clicked"])
        observe("invoke")

        # W12: no background route → refused by name, nothing delivered.
        unaimed = await call("type", window=str(main), text="zz")
        assert unaimed["error"] == "device_not_supported" and unaimed["status"] == "failed"
        for action in ("key", "double_click", "right_click", "move", "drag"):
            refused = await call(action, window=str(main), text="Return", x=1, y=1, x2=2, y2=2)
            assert refused["error"] == "device_not_supported", (action, refused)
        assert text(ready["edit"]) == "abcde"
        record["host_events"] = host_events(log)

        # W4: minimized → window_unavailable.
        log.with_suffix(".cmd").write_text("mini")
        until(lambda: [e for e in host_events(log) if e["ev"] == "cmd"])
        assert (await call("screenshot", window=str(main)))["error"] == "window_unavailable"
    finally:
        host.kill()
        host.wait()
        await desk.close()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"window-native-{sys.platform}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    # W4: closed → window_not_found.
    await asyncio.sleep(0.5)
    desk = Desk(tmp_path / "data-2", backend, lines.append, lambda: None)
    try:
        gone = await Calls(desk, record)("screenshot", window=str(main))
        assert gone["error"] == "window_not_found"
    finally:
        await desk.close()
