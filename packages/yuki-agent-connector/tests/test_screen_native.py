"""The real screen backend of the platform running the tests (task book §5.7, §8).

GitHub-hosted runners cannot grant macOS screen recording or Accessibility, so on macOS
this checks the fact probes and that a missing grant is reported instead of a black
image (D1). Windows runners have an interactive desktop: the real GDI capture is checked,
and on CI only (never on a developer's computer) a real ``SendInput`` move proves the
image-to-cursor mapping under per-monitor-v2 DPI (M6). Linux has no Connector GUI.
Artifact: test-results/device-computer/native-<platform>.json (and the capture).

The owner's input on macOS (D15), failure cases written before the code:

- Pointer movement alone (``kCGEventMouseMoved``), a key release or a modifier change counts
  as the owner's input and makes Yuki yield.
- A key press, a press of any mouse button, a drag with any button or a scroll wheel is
  missed because only one event type (or ``kCGAnyInputEventType``) is read.
- The most recent of the deliberate events is not the one used (the oldest, or the first read).
- A state other than the combined session state is read, losing Screen Sharing and remote
  control input.
- The real constants or calls fail on an actual Mac (the native test reads them for real).
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
from pathlib import Path
from typing import Any

import pytest
from desk_support import ARTIFACTS

from yuki_agent_connector.desk import Desk
from yuki_agent_connector.screen import frame, native_backend, to_global

LIMITS = {"max_dimension": 1568, "max_pixels": 1_500_000}


def jpeg_size(data: bytes) -> tuple[int, int]:
    index = 2
    while data[index + 1] not in (0xC0, 0xC1, 0xC2):
        index += 2 + struct.unpack(">H", data[index + 2 : index + 4])[0]
    height, width = struct.unpack(">HH", data[index + 5 : index + 9])
    return width, height


async def test_native_backend_facts_and_screenshot(tmp_path: Path) -> None:
    lines: list[dict[str, Any]] = []
    backend = native_backend()
    desk = Desk(tmp_path, backend, lines.append, lambda: None)
    desk.announce()
    record: dict[str, Any] = {"platform": sys.platform, "lines": lines}
    command = {"id": "native-shot", "resource": "computer", "action": "screenshot"}
    shot = await desk.execute({**command, "arguments": LIMITS})
    record["screenshot"] = shot
    try:
        if sys.platform not in {"darwin", "win32"}:
            assert backend is None and desk.facts() is None
            assert shot["error"] == "device_not_supported"
            assert [line["type"] for line in lines] == ["control"]
            return
        assert backend is not None
        facts = desk.facts()
        record["facts"] = facts
        assert facts is not None
        displays = facts["displays"]
        assert displays and [item["primary"] for item in displays].count(True) == 1
        assert all(item["scale"] >= 1 and item["bounds"]["width"] > 0 for item in displays)
        assert lines[-1] == {
            "type": "permissions",
            "screen": facts["screen"],
            "input": facts["input"],
        }
        # The real last-input reads (D15 on macOS: every deliberate event type exists).
        earliest, latest = backend.last_input()
        quiet = backend.idle()
        record["last_input"] = {"earliest": earliest, "latest": latest, "idle": quiet}
        assert earliest <= latest and quiet >= 0
        unavailable = backend.unavailable()
        record["unavailable"] = unavailable
        if sys.platform == "win32":
            assert (facts["screen"], facts["input"]) == (True, True)
        if unavailable is not None:
            assert shot["error"] == unavailable["error"]
            return
        if not facts["screen"]:
            # D1: no grant → a named failure, never a black or wallpaper-only image.
            assert shot["error"] == "screen_permission_required"
            return
        primary = next(display for display in backend.displays() if display.primary)
        size = frame(primary, **LIMITS)
        assert (shot["width"], shot["height"]) == size, shot
        capture = Path(shot["image"]["path"])
        assert jpeg_size(capture.read_bytes()) == size  # noqa: ASYNC240
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        shutil.copy(capture, ARTIFACTS / f"native-{sys.platform}.jpg")
        if sys.platform == "win32" and os.environ.get("CI"):
            import ctypes
            from ctypes import wintypes

            x, y = size[0] // 3, size[1] // 2
            moved = await desk.execute(
                {
                    "id": "native-move",
                    "resource": "computer",
                    "action": "move",
                    "arguments": {**LIMITS, "x": x, "y": y},
                }
            )
            record["move"] = moved
            assert moved["submitted"] is True, moved
            cursor = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
            expected = to_global(primary, **LIMITS, x=x, y=y)
            record["cursor"] = {"actual": [cursor.x, cursor.y], "expected": expected}
            # One virtual-desk unit is a fraction of a pixel; allow its rounding.
            assert abs(cursor.x - expected[0]) <= 1 and abs(cursor.y - expected[1]) <= 1
            assert [line["phase"] for line in lines if line["type"] == "activity"] == [
                "start",
                "end",
            ]
    finally:
        await desk.close()
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"native-{sys.platform}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend's choice of events")
def test_macos_counts_only_deliberate_input(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki_agent_connector import screen_macos
    from yuki_agent_connector.screen_macos import MacScreen, Quartz

    deliberate = {
        "key down": Quartz.kCGEventKeyDown,
        "left button": Quartz.kCGEventLeftMouseDown,
        "right button": Quartz.kCGEventRightMouseDown,
        "other button": Quartz.kCGEventOtherMouseDown,
        "left drag": Quartz.kCGEventLeftMouseDragged,
        "right drag": Quartz.kCGEventRightMouseDragged,
        "other drag": Quartz.kCGEventOtherMouseDragged,
        "scroll": Quartz.kCGEventScrollWheel,
    }
    since: dict[int, float] = {}
    states: set[int] = set()

    def seconds(state: int, kind: int) -> float:
        states.add(state)
        if kind == Quartz.kCGAnyInputEventType:
            return min([1000.0, *since.values()])
        return since.get(kind, 1000.0)

    monkeypatch.setattr(screen_macos.Quartz, "CGEventSourceSecondsSinceLastEventType", seconds)
    screen = MacScreen()
    since.update(
        {
            Quartz.kCGEventMouseMoved: 0.01,
            Quartz.kCGEventKeyUp: 0.01,
            Quartz.kCGEventFlagsChanged: 0.01,
            Quartz.kCGEventLeftMouseUp: 0.01,
        }
    )
    assert screen.idle() == 1000.0
    for name, kind in deliberate.items():
        since[kind] = 0.5
        assert screen.idle() == 0.5, name
        earliest, latest = screen.last_input()
        assert earliest <= latest
        del since[kind]
    # The most recent deliberate event wins.
    since.update({Quartz.kCGEventKeyDown: 3.0, Quartz.kCGEventScrollWheel: 0.2})
    assert screen.idle() == 0.2
    assert states == {Quartz.kCGEventSourceStateCombinedSessionState}
