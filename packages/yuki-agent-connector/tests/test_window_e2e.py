"""A real Connector process operating one window in the background (phase 3b).

Failure cases: docs/operations/computer-3b-20261110.md §1 — the window list and identity
(W2, W4, W5, W6), window coordinates at the current geometry (M6), background-only delivery
and refusals without a route (W9–W12, W14, W15), yielding only on a real collision
(W16–W19), the journal (W20), the facts (W21) and the activity line (W23). Only the
operating system's screen is replaced (tests/fakes/screen_connector.py); the native
backends are exercised by tests/test_window_native.py.
Artifact: test-results/device-computer/window-<platform>.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from desk_support import ARTIFACTS, Cloud, ConnectorProcess
from test_desk_e2e import RETINA, desktop_lines, events, script
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config
from yuki_agent_connector.screen import Window, frame, to_global

LIMITS = {"max_dimension": 1568, "max_pixels": 16_777_216}
# A Retina window: 800x600 points, 1600x1200 pixels, its origin inside the main display.
EDITOR = {
    "id": "4242",
    "app": "TextEdit",
    "title": "notes.txt",
    "x": 300,
    "y": 120,
    "width": 800,
    "height": 600,
    "pixel_width": 1600,
    "pixel_height": 1200,
}
CHAT = {**EDITOR, "id": "77", "app": "Telegram", "title": "Telegram", "x": 40, "y": 60}
BACKGROUND = {"type": "activity", "tool": "computer", "mode": "background"}


def geometry(facts: dict[str, Any]) -> Window:
    return Window(**{key: facts[key] for key in Window.__dataclass_fields__})


async def test_window_in_the_background(tmp_path: Path) -> None:
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    script(state, displays=[RETINA], windows=[EDITOR, {**CHAT, "minimized": True}])
    cloud = Cloud()
    calls: list[dict[str, Any]] = []

    async def call(action: str, identity: str, **arguments: Any) -> dict[str, Any]:
        response = await cloud.command("computer", action, identity, {**LIMITS, **arguments})
        calls.append({"id": identity, "action": action, "arguments": arguments, **response})
        return response.get("result") or response

    def window_events() -> list[dict[str, Any]]:
        return [event for event in events(state) if "window" in event]

    async with serve(cloud.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        Config(f"ws://127.0.0.1:{port}/", "original-device", "fixture", full_access=True).save(
            data / "config.json"
        )
        process = ConnectorProcess(state, data)
        await process.start()
        try:
            await cloud.connected.wait_for(1)
            # W21: the facts say windows are delivered in the background.
            assert cloud.hello[0]["device_capabilities"]["computer"]["windows"] is True

            # W2: every screenshot lists the real windows; their ids are what window takes.
            whole = await call("screenshot", "list-1")
            assert whole["display"] == "1"
            assert whole["windows"] == [
                {"id": "4242", "app": "TextEdit", "title": "notes.txt"},
                {"id": "77", "app": "Telegram", "title": "Telegram"},
            ]
            await cloud.image(whole["image"]["path"], "list-1")

            # W6/M6: the window alone, at the size its pixels allow, answered as a window.
            shot = await call("screenshot", "shot-1", window="4242")
            size = frame(geometry(EDITOR), **LIMITS)
            assert (shot["width"], shot["height"]) == size == (1568, 1176)
            assert shot["window"] == "4242" and "display" not in shot
            assert shot["windows"][0]["id"] == "4242"
            await cloud.image(shot["image"]["path"], "shot-1")

            # W9/W14/W23: a click goes to the window only, as submitted, with a new picture
            # of that window and a background activity line; nothing global is posted.
            clicked = await call("click", "click-1", window="4242", x=784, y=588)
            assert clicked["status"] == "succeeded" and clicked["submitted"] is True
            assert clicked["window"] == "4242" and "image" in clicked
            await cloud.image(clicked["image"]["path"], "click-1")
            (first,) = events(state)
            assert first["window"] == "4242" and first["kind"] == "click"
            # M6: window image pixels → global points through the window's origin and scale.
            expected = to_global(geometry(EDITOR), **LIMITS, x=784, y=588)
            assert (first["x"], first["y"]) == pytest.approx(expected)
            assert round(first["x"]) == 700 and round(first["y"]) == 420
            assert desktop_lines(process)[-2:] == [
                {**BACKGROUND, "action": "click", "phase": "start"},
                {**BACKGROUND, "action": "click", "phase": "end"},
            ]

            # M6: the owner moved the window after the picture; the click follows it now.
            script(state, windows=[{**EDITOR, "x": 0, "y": 0}, {**CHAT, "minimized": True}])
            await call("click", "click-moved", window="4242", x=784, y=588)
            assert round(events(state)[-1]["x"]) == 400 and round(events(state)[-1]["y"]) == 300
            script(state, windows=[EDITOR, {**CHAT, "minimized": True}])

            # W11: type at x,y focuses that field first, then the keys go to the window.
            await call("type", "type-1", window="4242", x=100, y=100, text="雪の日")
            assert [event["kind"] for event in events(state)[-2:]] == ["focus", "type"]
            assert events(state)[-1] == {"kind": "type", "window": "4242", "text": "雪の日"}
            # Without x,y the keys go to the window's own focus: no focus step.
            await call("key", "key-1", window="4242", text="cmd+s")
            assert events(state)[-1] == {
                "kind": "key",
                "window": "4242",
                "modifiers": ["cmd"],
                "key": "s",
            }

            # W20: the same call key returns the original receipt and delivers nothing.
            count = len(events(state))
            again = await call("click", "click-1", window="4242", x=784, y=588)
            assert again["submitted"] is True and len(events(state)) == count

            # W16: the owner types in another app; background input runs at once.
            (state / "owner_last_input").write_text(str(time.time()))
            await asyncio.sleep(0.05)
            started = time.monotonic()
            ran = await call("scroll", "scroll-1", window="4242", x=10, y=10, dy=3)
            assert ran["submitted"] is True and time.monotonic() - started < 1.3
            assert events(state)[-1]["kind"] == "scroll" and events(state)[-1]["dy"] == 3

            # W17: the window's app is the owner's frontmost app and the owner used the
            # computer since Yuki last looked: Yuki waits for quiet, then refuses unexecuted.
            script(state, windows=[{**EDITOR, "front": True}, {**CHAT, "minimized": True}])
            (state / "owner_last_input").write_text(str(time.time()))
            await asyncio.sleep(0.05)
            count, lines = len(events(state)), len(desktop_lines(process))
            started = time.monotonic()
            yielded = await call("type", "type-collide", window="4242", text="x")
            assert yielded["error"] == "owner_active" and yielded["status"] == "failed"
            assert 1.3 < time.monotonic() - started < 10
            assert len(events(state)) == count and len(desktop_lines(process)) == lines
            # W17/W19: after a fresh look the same app in front does not stop Yuki.
            look = await call("screenshot", "shot-front", window="4242")
            await cloud.image(look["image"]["path"], "shot-front")
            retried = await call("type", "type-front", window="4242", text="x")
            assert retried["submitted"] is True and len(events(state)) == count + 1
            # W19: background events are not the owner's input; the next one runs too.
            again = await call("type", "type-front-2", window="4242", text="y")
            assert again["submitted"] is True
            script(state, windows=[EDITOR, {**CHAT, "minimized": True}])

            # W18: Desktop 「停止」 refuses window input; the window still reads.
            process.send({"type": "control", "holder": "owner"})
            await process.wait_for(
                lambda line: line.get("type") == "control" and line["holder"] == "owner"
            )
            held = await call("click", "click-held", window="4242", x=1, y=1)
            assert held["error"] == "control_held"
            read = await call("screenshot", "shot-held", window="4242")
            assert read["window"] == "4242" and "error" not in read
            await cloud.image(read["image"]["path"], "shot-held")
            process.send({"type": "control", "holder": "none"})
            await process.wait_for(
                lambda line: line.get("type") == "control" and line["holder"] == "none"
            )

            # Refusals before any input: no events, no activity lines.
            count = len(events(state))

            def activity() -> int:
                return sum(line["type"] == "activity" for line in desktop_lines(process))

            lines = activity()
            # W4: minimized and missing windows, for reads and inputs alike.
            for action, extra in (("screenshot", {}), ("click", {"x": 1, "y": 1})):
                minimized = await call(action, f"{action}-min", window="77", **extra)
                assert minimized["error"] == "window_unavailable"
                gone = await call(action, f"{action}-gone", window="9", **extra)
                assert gone["error"] == "window_not_found"
            # W12: a platform without a background route for an action refuses it by name
            # before posting anything, and never falls back to global input.
            script(
                state,
                windows=[{**EDITOR, "actions": ["click", "scroll", "type"]}],
            )
            unrouted = await call("key", "key-unrouted", window="4242", text="Return")
            assert unrouted["error"] == "device_not_supported"
            assert "省略 window" in unrouted["message"]
            # W24: a window outside the owner's current Space takes no input at all, yet
            # its picture still reads.
            script(state, windows=[{**EDITOR, "actions": []}])
            elsewhere = await call("screenshot", "shot-elsewhere", window="4242")
            assert elsewhere["window"] == "4242" and "image" in elsewhere
            for action, extra in (("click", {"x": 1, "y": 1}), ("key", {"text": "cmd+n"})):
                refused = await call(action, f"{action}-elsewhere", window="4242", **extra)
                assert refused["error"] == "device_not_supported"
                assert "桌面空间" in refused["message"]
            # D3 for a window: the window's process is elevated.
            script(state, windows=[{**EDITOR, "elevated": True}])
            elevated = await call("click", "click-elevated", window="4242", x=1, y=1)
            assert elevated["error"] == "target_elevated"
            # W5: a window needs Accessibility as well, even only to look at it.
            script(state, windows=[EDITOR], input=False)
            denied = await call("screenshot", "shot-denied", window="4242")
            assert denied["error"] == "input_permission_required"
            script(state, input=True)
            assert len(events(state)) == count
            assert activity() == lines

            # W12/W15: no route found while delivering (no pattern at that point): refused,
            # nothing delivered, nothing global posted.
            script(state, refuse="device_not_supported")
            unrouted = await call("click", "click-unrouted", window="4242", x=1, y=1)
            assert unrouted["error"] == "device_not_supported"
            assert unrouted["status"] == "failed"
            script(state, refuse=None)
            assert len(events(state)) == count
            # No event in this whole session was global input.
            assert all("window" in event for event in events(state))
        finally:
            exit_code, stderr = await process.stop()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"window-{sys.platform}.json").write_text(
        json.dumps(
            {"calls": calls, "events": events(state), "stdout_lines": process.lines},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert exit_code == 0, stderr
