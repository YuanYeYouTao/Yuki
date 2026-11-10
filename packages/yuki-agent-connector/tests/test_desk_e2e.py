"""Real Connector processes: owner stop/resume, the Desktop stdin/stdout contract and
``computer``/``browser`` refusals over the actual WebSocket protocol and journal.

Failure cases: docs/operations/computer-browser-device-3a-20261110.md §1 (D1–D3, D5, D8–D10,
C4, C5, K1–K5, G1, G4, M6 at the process level). Only the operating system's screen is
replaced (tests/fakes/screen_connector.py); everything else is the shipped Connector.
Artifact: test-results/device-computer/<test>-<platform>.json.
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from desk_support import ARTIFACTS, Cloud, ConnectorProcess
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config
from yuki_agent_connector.screen import Display, frame, to_global

RETINA = {
    "id": "1",
    "x": 0,
    "y": 0,
    "width": 1512,
    "height": 982,
    "pixel_width": 3024,
    "pixel_height": 1964,
    "primary": True,
    "scale": 2.0,
}
LEFT = {
    "id": "2",
    "x": -1920,
    "y": -98,
    "width": 1920,
    "height": 1080,
    "pixel_width": 1920,
    "pixel_height": 1080,
    "primary": False,
    "scale": 1.0,
}
LIMITS = {"max_dimension": 1568, "max_pixels": 16_777_216}


def script(state: Path, **facts: Any) -> None:
    current = (
        json.loads((state / "script.json").read_text())
        if (state / "script.json").exists()
        else {"displays": [RETINA, LEFT], "chrome": False}
    )
    (state / "script.json").write_text(json.dumps({**current, **facts}))


def events(state: Path) -> list[dict[str, Any]]:
    path = state / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def jpeg_size(data: bytes) -> tuple[int, int]:
    index = 2
    while data[index + 1] != 0xC0:
        index += 2 + struct.unpack(">H", data[index + 2 : index + 4])[0]
    height, width = struct.unpack(">HH", data[index + 5 : index + 9])
    return width, height


def desktop_lines(process: ConnectorProcess) -> list[dict[str, Any]]:
    return [line for line in process.lines if line.get("type") != "state"]


def write_artifact(name: str, value: dict[str, Any]) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"{name}-{sys.platform}.json").write_text(
        json.dumps({"platform": sys.platform, **value}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def test_desktop_contract_owner_stop_resume_and_refusals(tmp_path: Path) -> None:
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    script(state)
    cloud = Cloud()
    calls: list[dict[str, Any]] = []

    async def call(resource: str, action: str, identity: str, **arguments: Any) -> dict[str, Any]:
        response = await cloud.command(resource, action, identity, {**LIMITS, **arguments})
        calls.append({"id": identity, "resource": resource, "action": action, **response})
        return response

    async with serve(cloud.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        Config(f"ws://127.0.0.1:{port}/", "original-device", "fixture", full_access=True).save(
            data / "config.json"
        )
        process = ConnectorProcess(state, data)
        await process.start()
        try:
            await cloud.connected.wait_for(1)
            # K1/K4: the stop flag and the permission preflight come first.
            await process.wait_for(lambda line: line.get("type") == "permissions")
            assert desktop_lines(process)[:2] == [
                {
                    "type": "control",
                    "holder": "none",
                    "since": desktop_lines(process)[0]["since"],
                    "reason": "start",
                },
                {"type": "permissions", "screen": True, "input": True},
            ]
            # D4: the hello carries the computer facts.
            facts = cloud.hello[0]["device_capabilities"]["computer"]
            assert facts == {
                "screen": True,
                "input": True,
                "displays": [
                    Display(**RETINA).fact(),
                    Display(**LEFT).fact(),
                ],
            }

            # A screenshot is a read: no journal row, no activity line; the image is a
            # FileTransfers source at its final size and is deleted once released (G1).
            shot = (await call("computer", "screenshot", "shot-1"))["result"]
            width, height = frame(Display(**RETINA), **LIMITS)
            assert (shot["width"], shot["height"]) == (width, height) == (1568, 1018)
            assert shot["display"] == "1" and shot["windows"] == []
            assert shot["control"]["holder"] == "none"
            image = await cloud.image(shot["image"]["path"], "shot-1")
            assert jpeg_size(image) == (1568, 1018)
            assert not Path(shot["image"]["path"]).exists()  # noqa: ASYNC240

            # M6: image pixels of the Retina display become global points.
            clicked = (await call("computer", "click", "click-1", x=784, y=509))["result"]
            assert clicked["submitted"] is True and clicked["status"] == "succeeded"
            first = events(state)[-1]
            assert first["kind"] == "click" and first["button"] == "left"
            assert (first["x"], first["y"]) == to_global(Display(**RETINA), **LIMITS, x=784, y=509)
            assert round(first["x"]) == 756 and round(first["y"]) == 491
            # K3: activity lines around the input.
            assert desktop_lines(process)[-2:] == [
                {"type": "activity", "tool": "computer", "action": "click", "phase": "start"},
                {"type": "activity", "tool": "computer", "action": "click", "phase": "end"},
            ]
            # A secondary display left of and above the main one has negative points.
            await call("computer", "move", "move-left", x=0, y=0, display="2")
            assert (events(state)[-1]["x"], events(state)[-1]["y"]) == (-1920, -98)

            # D8: Yuki's own input is not the owner's: a second input runs.
            await call("computer", "double_click", "click-2", x=10, y=10)
            assert events(state)[-1]["count"] == 2

            # D8: the owner touches the computer while the model thinks.
            await asyncio.sleep(0.05)
            (state / "owner_input").write_text(str(time.time()))
            await asyncio.sleep(0.05)
            before = len(events(state))
            held = (await call("computer", "click", "click-3", x=10, y=10))["result"]
            assert held["error"] == "control_held" and held["status"] == "failed"
            assert held["control"]["holder"] == "owner"
            assert len(events(state)) == before
            stop = desktop_lines(process)[-1]
            assert stop["type"] == "control" and stop["holder"] == "owner"
            assert stop["reason"] == "owner_input"

            # D9: every input is refused while the owner holds the computer; reads work.
            browser_input = (await call("browser", "open", "open-held", url="about:blank"))[
                "result"
            ]
            assert browser_input["error"] == "control_held"
            shot = (await call("computer", "screenshot", "shot-held"))["result"]
            assert shot["control"]["holder"] == "owner" and "error" not in shot
            await cloud.image(shot["image"]["path"], "shot-held")
            # A browser read is not refused by the stop (here: no Chrome installed).
            read = (await call("browser", "snapshot", "snapshot-held"))["result"]
            assert read["error"] == "browser_unavailable"
            assert len(events(state)) == before
            assert [line["type"] for line in desktop_lines(process)[-1:]] == ["control"]

            # K2: lines that are not control lines are ignored.
            process.send(b"not json\n\n")
            process.send({"type": "focus"})
            process.send({"type": "control", "holder": "somebody"})
            # Desktop 「繼續」; the owner's own click on it does not stop Yuki again.
            process.send({"type": "control", "holder": "none"})
            resumed = await process.wait_for(
                lambda line: line.get("type") == "control" and line["holder"] == "none"
            )
            assert resumed["reason"] == "desktop"
            await call("computer", "type", "type-1", text="雪の日 Yuki\n")
            assert events(state)[-1] == {"kind": "type", "text": "雪の日 Yuki\n"}

            # Desktop 「停止」, then the model resumes because the owner said so in chat.
            process.send({"type": "control", "holder": "owner"})
            stopped = await process.wait_for(
                lambda line: line.get("type") == "control" and line["holder"] == "owner"
            )
            assert stopped["reason"] == "desktop"
            held = (await call("computer", "key", "key-held", text="ctrl+l"))["result"]
            assert held["error"] == "control_held"
            resume = (await call("computer", "resume", "resume-1"))["result"]
            assert resume["control"]["holder"] == "none" and resume["windows"] == []
            await cloud.image(resume["image"]["path"], "resume-1")
            assert desktop_lines(process)[-1]["reason"] == "resume"
            await call("computer", "key", "key-1", text="ctrl+shift+t")
            assert events(state)[-1] == {"kind": "key", "modifiers": ["ctrl", "shift"], "key": "t"}

            # C4: the same call key returns the original receipt and posts nothing.
            count = len(events(state))
            again = (await call("computer", "key", "key-1", text="ctrl+shift+t"))["result"]
            assert again["status"] == "succeeded" and again["submitted"] is True
            assert len(events(state)) == count

            # Refusals before any input: no events, no activity lines.
            quiet = len(desktop_lines(process))
            unknown_key = (await call("computer", "key", "key-bad", text="ctrl+nonsense"))
            assert unknown_key["result"]["error"] == "invalid_arguments"
            window = (await call("computer", "click", "window-1", x=1, y=1, window="0x1"))
            assert window["result"]["error"] == "device_not_supported"
            missing = (await call("computer", "click", "no-point"))["result"]
            assert missing["error"] == "invalid_arguments"
            # D3: an elevated target window.
            script(state, elevated=True)
            elevated = (await call("computer", "click", "elevated-1", x=1, y=1))["result"]
            assert elevated["error"] == "target_elevated"
            # D2: a locked screen.
            script(state, elevated=False, unavailable={"error": "display_locked", "message": "x"})
            locked = (await call("computer", "screenshot", "locked-1"))["result"]
            assert locked["error"] == "display_locked"
            locked = (await call("computer", "click", "locked-2", x=1, y=1))["result"]
            assert locked["error"] == "display_locked"
            script(state, unavailable=None)
            assert len(events(state)) == count
            assert len(desktop_lines(process)) == quiet

            # D1: Accessibility withdrawn: a new permissions line, refreshed server facts.
            script(state, input=False)
            denied = (await call("computer", "click", "denied-1", x=1, y=1))["result"]
            assert denied["error"] == "input_permission_required"
            assert desktop_lines(process)[-1] == {
                "type": "permissions",
                "screen": True,
                "input": False,
            }
            catalog = await cloud.catalog_where(
                lambda value: value["device_capabilities"]["computer"]["input"] is False
            )
            assert catalog["device_capabilities"]["computer"]["screen"] is True
            script(state, screen=False)
            denied = (await call("computer", "screenshot", "denied-2"))["result"]
            assert denied["error"] == "screen_permission_required"
            script(state, screen=True, input=True)
            assert len(events(state)) == count

            # D5: full access off refuses reads and inputs alike, from the next command.
            Config.update(data / "config.json", lambda value: replace(value, full_access=False))
            closed = await call("computer", "screenshot", "closed-1")
            assert closed["error"]["code"] == "full_access_disabled"
            closed = await call("browser", "snapshot", "closed-2")
            assert closed["error"]["code"] == "full_access_disabled"
        finally:
            exit_code, stderr = await process.stop()
        write_artifact(
            "desktop-contract",
            {
                "stdout_lines": process.lines,
                "calls": calls,
                "events": events(state),
                "hello_device_capabilities": cloud.hello[0]["device_capabilities"],
                "catalogs": cloud.catalogs,
            },
        )
        # K2: EOF still ends the Connector cleanly.
        assert exit_code == 0, stderr
        # G4: the stop flag is in memory only.
        assert "holder" not in (data / "config.json").read_text()


async def test_restart_keeps_the_desktop_stop_and_never_redrives(tmp_path: Path) -> None:
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    script(state)
    cloud = Cloud()
    timeline: list[dict[str, Any]] = []
    async with serve(cloud.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        Config(f"ws://127.0.0.1:{port}/", "original-device", "fixture", full_access=True).save(
            data / "config.json"
        )
        # C5: the Connector dies while an input is executing.
        first = ConnectorProcess(state, data)
        await first.start()
        await cloud.connected.wait_for(1)
        script(state, hang=True)
        pending = asyncio.create_task(
            cloud.command("computer", "click", "click-crash", {**LIMITS, "x": 5, "y": 5})
        )
        await first.wait_for(lambda line: line.get("type") == "activity")
        while not (state / "hanging").exists():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await first.kill()
        pending.cancel()
        timeline.append({"first": first.lines})
        script(state, hang=False)

        # D10: Desktop restarts the child and re-sends its stop right away.
        second = ConnectorProcess(state, data)
        await second.start()
        second.send({"type": "control", "holder": "owner"})
        await cloud.connected.wait_for(2)
        restored = await second.wait_for(
            lambda line: line.get("type") == "control" and line["holder"] == "owner"
        )
        assert restored["reason"] == "desktop"
        assert desktop_lines(second)[0]["reason"] == "start"
        held = await cloud.command("computer", "click", "after-restart", {**LIMITS, "x": 1, "y": 1})
        assert held["result"]["error"] == "control_held"
        # C5: the original call is unknown and is never driven again.
        replay = await cloud.command(
            "computer", "click", "click-crash", {**LIMITS, "x": 5, "y": 5}
        )
        assert replay["result"]["status"] == "unknown"
        assert replay["result"]["reason"] == "original_dispatch_not_confirmed"
        assert events(state) == []
        exit_code, stderr = await second.stop()
        timeline.append({"second": second.lines, "replay": replay, "held": held})
    write_artifact("restart", {"timeline": timeline, "events": events(state)})
    assert exit_code == 0, stderr


async def test_plain_run_prints_no_desktop_lines(tmp_path: Path) -> None:
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    script(state)
    cloud = Cloud()
    async with serve(cloud.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        Config(f"ws://127.0.0.1:{port}/", "original-device", "fixture", full_access=True).save(
            data / "config.json"
        )
        process = ConnectorProcess(state, data, desktop=False)
        await process.start()
        await cloud.connected.wait_for(1)
        # The stop logic still applies; only the Desktop lines are absent (K5).
        clicked = await cloud.command("computer", "click", "plain-1", {**LIMITS, "x": 1, "y": 1})
        assert clicked["result"]["submitted"] is True
        (state / "owner_input").write_text(str(time.time() + 1))
        held = await cloud.command("computer", "click", "plain-2", {**LIMITS, "x": 1, "y": 1})
        assert held["result"]["error"] == "control_held"
        await process.kill()
    write_artifact("plain-run", {"stdout": process.raw})
    assert process.lines == [] and process.raw == []

