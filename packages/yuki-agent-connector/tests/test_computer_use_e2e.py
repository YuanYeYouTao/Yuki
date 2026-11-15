"""Computer Use on the owner's computer: a real Connector process behind the scripted screen.

Failure cases were written before the code, from the task book
docs/architecture/Computer-Use任务书-2026-11-15.md §11:

- coordinates and echo: C7–C9, C16, X1 (journal row first, still ``failed``), R1, R7–R9;
- actions: A1, A3–A6, A8, A9, A13, A14;
- zoom: Z1–Z4; pointer: P3, P5;
- elements: E1–E7, E9, E10, E12–E14; reasons and facts: D3, D5–D9, D11.

Only the operating system's screen is replaced (tests/fakes/screen_connector.py); the
journal, stdin/stdout lines, WebSocket protocol and file transfers are the shipped ones.
Artifacts: test-results/connector-computer/<platform>/<test>.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from desk_support import Cloud, ConnectorProcess
from test_desk_e2e import LEFT, RETINA, desktop_lines, events, jpeg_size, script
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config
from yuki_agent_connector.desk import FEATURES
from yuki_agent_connector.screen import Display, Window, frame, to_global, to_image

ARTIFACTS = Path(__file__).resolve().parents[3] / "test-results" / "connector-computer"
LIMITS = {"max_dimension": 1568, "max_pixels": 1_150_000}
SCREEN = Display(**RETINA)
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
# Accessibility nodes of the editor, in global points (the fake reads them on every call).
NODES = [
    {"key": "root", "depth": 0, "role": "window", "name": "notes.txt", "x": 300, "y": 120,
     "width": 800, "height": 600},
    {"key": "box", "depth": 1, "role": "group", "name": "", "container": True, "x": 300,
     "y": 140, "width": 800, "height": 580},
    {"key": "save", "depth": 2, "role": "button", "name": "保存", "x": 320, "y": 150,
     "width": 80, "height": 30},
    {"key": "field", "depth": 2, "role": "text field", "name": "标题", "x": 420, "y": 150,
     "width": 300, "height": 30},
    {"key": "hidden", "depth": 2, "role": "button", "name": "隐藏", "x": 320, "y": 150,
     "width": 80, "height": 30, "visible": False},
    {"key": "flat", "depth": 2, "role": "button", "name": "零尺寸", "x": 500, "y": 500,
     "width": 0, "height": 0},
]


def write(name: str, value: dict[str, Any]) -> None:
    directory = ARTIFACTS / sys.platform
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def window_geometry(facts: dict[str, Any]) -> Window:
    return Window(**{key: facts[key] for key in Window.__dataclass_fields__})


class Session:
    def __init__(self, cloud: Cloud, state: Path) -> None:
        self.cloud, self.state = cloud, state
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, action: str, identity: str, **arguments: Any) -> dict[str, Any]:
        response = await self.cloud.command(
            "computer", action, identity, {**LIMITS, **arguments}
        )
        result = response.get("result") or response
        self.calls.append({"id": identity, "action": action, "arguments": arguments, **response})
        image = result.get("image") if isinstance(result, dict) else None
        # A retried key returns the original receipt, whose capture was already read.
        if image and identity not in {call["id"] for call in self.calls[:-1]}:
            data = await self.cloud.image(image["path"], identity)
            assert jpeg_size(data) == (image["width"], image["height"])
        return result  # type: ignore[no-any-return]


async def start(tmp_path: Path, **facts: Any) -> tuple[Any, Cloud, ConnectorProcess, Session]:
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir(exist_ok=True)
    script(state, **facts)
    cloud = Cloud()
    server = await serve(cloud.handler, "127.0.0.1", 0).__aenter__()
    port = server.sockets[0].getsockname()[1]
    Config(f"ws://127.0.0.1:{port}/", "original-device", "fixture", full_access=True).save(
        data / "config.json"
    )
    process = ConnectorProcess(state, data)
    await process.start()
    await cloud.connected.wait_for(1)
    return server, cloud, process, Session(cloud, state)


async def test_coordinates_echo_actions_zoom_and_pointer(tmp_path: Path) -> None:
    server, cloud, process, call = await start(
        tmp_path, displays=[RETINA, LEFT], pointer=[756, 491], hit={"role": "button", "name": "OK"}
    )
    state = call.state
    width, height = frame(SCREEN, **LIMITS)
    try:
        # D3: the facts list every new capability this Connector really has.
        facts = cloud.hello[0]["device_capabilities"]["computer"]
        assert facts["features"] == sorted(FEATURES)

        # The screenshot says what its coordinates are; P3: the pointer is on this display.
        shot = await call("screenshot", "shot-1")
        assert (shot["width"], shot["height"]) == (width, height) == (1331, 864)
        assert "scale=1000" in shot["coordinates"]
        assert shot["cursor"] == [665, 432]
        # P5: the pointer on another display: no cursor field for this image.
        script(state, pointer=[-500, 100])
        assert "cursor" not in await call("screenshot", "shot-2")
        script(state, pointer=None)
        assert "cursor" not in await call("screenshot", "shot-3")

        # C7/X1: one pixel outside the image; the journal row exists, the result is failed,
        # nothing is posted and the retry of the key returns the same receipt.
        outside = await call("click", "out-1", x=width, y=10)
        assert outside["error"] == "coordinate_out_of_range" and outside["status"] == "failed"
        assert (outside["width"], outside["height"]) == (width, height)
        assert f"0–{width - 1}" in outside["message"] and "scale=1000" in outside["message"]
        assert await call("click", "out-1", x=width, y=10) == outside
        assert events(state) == []

        # C8/R1: 0..1000 on both axes, 1000 is the last pixel; point echoes the pixel.
        clicked = await call("click", "norm-1", x=1000, y=1000, scale=1000)
        assert clicked["point"] == [width - 1, height - 1] and clicked["scale"] == 1000
        assert clicked["route"] == ["global_input"]
        assert clicked["hit"] == {"role": "button", "name": "OK"}
        last = events(state)[-1]
        assert (last["x"], last["y"]) == pytest.approx(to_global(SCREEN, **LIMITS, x=width - 1,
                                                                 y=height - 1))
        assert last["modifiers"] == []
        # C9 and a scale that is not a declared system.
        assert (await call("click", "norm-2", x=1001, y=0, scale=1000))["error"] == (
            "coordinate_out_of_range"
        )
        assert (await call("click", "norm-3", x=1, y=1, scale=100))["error"] == (
            "invalid_arguments"
        )
        # Pixels without scale: point is the pixel itself, scale is null.
        plain = await call("click", "px-1", x=10, y=20)
        assert plain["point"] == [10, 20] and plain["scale"] is None
        # R7: the hit test answers nothing (timeout, no element): no hit, the input still ran.
        script(state, hit=None)
        quiet = await call("click", "px-2", x=11, y=21)
        assert quiet["submitted"] is True and "hit" not in quiet
        # The activity line names the route once the input ended.
        ends = [line for line in desktop_lines(process) if line.get("phase") == "end"]
        assert ends[-1]["route"] == ["global_input"]
        # R9: the same key returns the original receipt with its original echo.
        assert (await call("click", "norm-1", x=1000, y=1000, scale=1000))["hit"] == {
            "role": "button",
            "name": "OK",
        }

        # A1/A3: modifiers held for this click only; an unknown one posts nothing.
        count = len(events(state))
        await call("click", "mod-1", x=10, y=10, text="ctrl+shift")
        assert events(state)[-1]["modifiers"] == ["ctrl", "shift"]
        assert (await call("click", "mod-2", x=10, y=10, text="hyper"))["error"] == (
            "invalid_arguments"
        )
        assert len(events(state)) == count + 1
        # A4/A5: triple and middle clicks.
        await call("triple_click", "triple-1", x=10, y=10)
        assert events(state)[-1] == {**events(state)[-1], "button": "left", "count": 3}
        await call("middle_click", "middle-1", x=10, y=10)
        assert events(state)[-1]["button"] == "middle"
        # A13/C16: drag and scroll hold modifiers for the whole action; counts stay counts.
        dragged = await call("drag", "drag-1", x=0, y=0, x2=500, y2=500, scale=1000,
                             text="shift")
        assert dragged["point"] == [0, 0] and dragged["point2"] == [666, 432]
        assert events(state)[-1]["modifiers"] == ["shift"]
        await call("scroll", "scroll-1", x=500, y=500, scale=1000, dx=-2, dy=7, text="ctrl")
        assert (events(state)[-1]["dx"], events(state)[-1]["dy"]) == (-2, 7)
        assert events(state)[-1]["modifiers"] == ["ctrl"]
        # A8: repeat sends the key that many times; out of range posts nothing.
        count = len(events(state))
        repeated = await call("key", "repeat-1", text="Down", repeat=5)
        assert repeated["submitted"] is True and "point" not in repeated
        assert len(events(state)) == count + 5
        for bad in (0, 101):
            assert (await call("key", f"repeat-bad-{bad}", text="Down", repeat=bad))["error"] == (
                "invalid_arguments"
            )
        # A14: element and x,y together.
        assert (await call("click", "both-1", x=1, y=1, element="4242.ab12e1"))["error"] == (
            "invalid_arguments"
        )
        assert len(events(state)) == count + 5

        # A9: Desktop 「停止」 during a long repeat: the rest is not sent, unknown + held.
        script(state, key_delay=0.05)
        pending = asyncio.create_task(call("key", "repeat-stop", text="Down", repeat=100))
        await process.wait_for(lambda line: line.get("type") == "activity")
        await asyncio.sleep(0.3)
        process.send({"type": "control", "holder": "owner"})
        stopped = await pending
        assert stopped["status"] == "unknown" and stopped["error"] == "control_held"
        sent = len(events(state)) - count - 5
        assert 0 < sent < 100
        script(state, key_delay=0)

        # Z4: zoom is a read and works while the owner holds the stop.
        zoomed = await call("zoom", "zoom-1", region=[100, 50, 300, 150])
        assert zoomed["region"] == [100, 50, 300, 150]
        assert zoomed["frame"] == {"width": width, "height": height}
        # Z1: native pixels of the region (3024/1330 ≈ 2.27 per image pixel), never upscaled.
        assert (zoomed["width"], zoomed["height"]) == (455, 227)
        assert "后续坐标仍用整张截图" in zoomed["coordinates"]
        captured = json.loads((state / "captures.jsonl").read_text().splitlines()[-1])
        x0, y0 = to_global(SCREEN, **LIMITS, x=100, y=50)
        x1, y1 = to_global(SCREEN, **LIMITS, x=300, y=150)
        assert captured["region"] == pytest.approx([x0, y0, x1 - x0, y1 - y0], abs=0.01)
        # Z3: the region in 0..1000.
        whole = await call("zoom", "zoom-2", region=[0, 0, 1000, 1000], scale=1000)
        assert whole["region"] == [0, 0, width, height]
        # Z2: past the image: nothing is captured.
        captures = len((state / "captures.jsonl").read_text().splitlines())
        assert (await call("zoom", "zoom-3", region=[0, 0, width + 1, 10]))["error"] == (
            "coordinate_out_of_range"
        )
        assert (await call("zoom", "zoom-4", region=[5, 5, 5, 9]))["error"] == "invalid_arguments"
        assert len((state / "captures.jsonl").read_text().splitlines()) == captures
        process.send({"type": "control", "holder": "none"})
        await process.wait_for(
            lambda line: line.get("type") == "control" and line["holder"] == "none"
        )

        # R8: input fails after it was posted: the computed point is still echoed.
        script(state, fail_input=True)
        failed = await call("click", "fail-1", x=30, y=40)
        assert failed["error"] == "input_failed" and failed["point"] == [30, 40]
        script(state, fail_input=False)
    finally:
        exit_code, stderr = await process.stop()
        await server.__aexit__(None, None, None)
        write("coordinates-actions-zoom", {"calls": call.calls, "events": events(state),
                                            "stdout": process.lines})
    assert exit_code == 0, stderr


async def test_window_elements_and_reasons(tmp_path: Path) -> None:
    editor = {**EDITOR, "elements": NODES}
    server, _cloud, process, call = await start(
        tmp_path, displays=[RETINA], windows=[editor], hit=None
    )
    state = call.state
    geometry = window_geometry(EDITOR)
    try:
        # E1: one line per visible node with its id and bounds in the window image's pixels;
        # nameless containers and invisible or zero-sized nodes are left out.
        listed = await call("elements", "list-1", window="4242")
        assert listed["window"] == "4242" and "image" not in listed
        lines = listed["elements"].splitlines()
        assert [line.split(" [")[0].strip() for line in lines] == [
            '- window "notes.txt"',
            '- button "保存"',
            '- text field "标题"',
        ]
        ids = {line.split('"')[1]: line.split("[")[1].split("]")[0] for line in lines}
        assert all(identity.startswith("4242.") for identity in ids.values())
        corner = [round(value) for value in (*to_image(geometry, **LIMITS, x=320, y=150),
                                             *to_image(geometry, **LIMITS, x=400, y=180))]
        assert lines[1].endswith("({},{},{},{})".format(*corner))
        # Children are indented under the nodes that were kept.
        assert lines[1].startswith("  - ")
        # The same bounds in 0..1000 with scale=1000; text keeps matches and their path.
        normalized = await call("elements", "list-norm", window="4242", scale=1000, text="保存")
        assert [line.split(" [")[0].strip() for line in normalized["elements"].splitlines()] == [
            '- window "notes.txt"',
            '- button "保存"',
        ]
        assert normalized["elements"].splitlines()[1].endswith("(25,50,125,100)")
        ids = {
            line.split('"')[1]: line.split("[")[1].split("]")[0]
            for line in normalized["elements"].splitlines()
        }

        # E2: click(element) re-reads the node, presses it through accessibility and echoes it.
        pressed = await call("click", "press-1", window="4242", element=ids["保存"])
        assert pressed["route"] == ["accessibility"] and pressed["element"] == ids["保存"]
        assert pressed["point"] == [round(v) for v in to_image(geometry, **LIMITS, x=360, y=165)]
        assert pressed["hit"] == {"role": "button", "name": "保存"}
        assert events(state)[-1] == {"kind": "press", "window": "4242", "element": "save"}
        # With a modifier no accessibility action can carry it: a pointer click at the centre.
        await call("click", "press-2", window="4242", element=ids["保存"], text="shift")
        last = events(state)[-1]
        assert last["kind"] == "click" and last["modifiers"] == ["shift"]
        assert (last["x"], last["y"]) == (360, 165)
        # E14: type(element) focuses the node first, then types into the window.
        await call("type", "type-1", window="4242", element=ids["保存"], text="abc")
        assert [event["kind"] for event in events(state)[-2:]] == ["focus_element", "type"]
        # E6: the id's window is not the given window.
        assert (await call("click", "other-1", window="77", element=ids["保存"]))["error"] == (
            "invalid_arguments"
        )
        # Without window the node is pressed as well; point is in the display image.
        whole = await call("click", "press-3", element=ids["保存"])
        assert whole["display"] == "1" and whole["route"] == ["accessibility"]
        assert whole["point"] == [round(v) for v in to_image(SCREEN, **LIMITS, x=360, y=165)]

        # E7: the node moved outside the window: not visible, never a coordinate click.
        count = len(events(state))
        moved = [dict(node, x=2000) if node["key"] == "save" else node for node in NODES]
        script(state, windows=[{**EDITOR, "elements": moved}])
        invisible = await call("click", "gone-1", window="4242", element=ids["保存"])
        assert invisible["error"] == "element_not_visible" and invisible["status"] == "failed"
        assert len(invisible["bounds"]) == 4
        # E3: the node was destroyed: stale, with a fresh list, nothing executed.
        script(state, windows=[{**EDITOR, "elements": [n for n in NODES if n["key"] != "save"]}])
        stale = await call("click", "stale-1", window="4242", element=ids["保存"])
        assert stale["error"] == "element_stale" and "标题" in stale["elements"]
        # E4: a new list for the window makes every older id stale, even of a node that
        # exists again.
        script(state, windows=[{**EDITOR, "elements": NODES}])
        await call("elements", "list-2", window="4242")
        fresh = await call("click", "stale-2", window="4242", element=ids["保存"])
        assert fresh["error"] == "element_stale"
        assert len(events(state)) == count
        # The list attached to the refusal is the current one.
        new = {line.split('"')[1]: line.split("[")[1].split("]")[0]
               for line in fresh["elements"].splitlines()}
        assert (await call("click", "fresh-1", window="4242", element=new["标题"]))["route"]

        # E13: elements is a read: it works while the owner holds the stop.
        process.send({"type": "control", "holder": "owner"})
        await process.wait_for(lambda line: line.get("holder") == "owner")
        assert "elements" in await call("elements", "list-held", window="4242")
        process.send({"type": "control", "holder": "none"})
        await process.wait_for(lambda line: line.get("holder") == "none")

        # E9: many nodes: listed whole, never truncated.
        many = NODES[:1] + [
            {"key": f"n{index}", "depth": 1, "role": "cell", "name": f"格{index}",
             "x": 300 + index % 40 * 20, "y": 140 + index // 40 * 10, "width": 20, "height": 10}
            for index in range(2000)
        ]
        script(state, windows=[{**EDITOR, "elements": many}])
        assert len((await call("elements", "list-many", window="4242"))["elements"]
                   .splitlines()) == 2001

        # E12: an app with no accessibility for this window.
        script(state, windows=[EDITOR])
        none = await call("elements", "list-none", window="4242")
        assert none["error"] == "device_not_supported" and none["cause"] == "no_accessibility"

        # D5/E10: a window outside the owner's current Space only reads.
        script(state, windows=[{**EDITOR, "elements": NODES, "actions": []}])
        for action, extra in (("elements", {}), ("click", {"x": 1, "y": 1})):
            refused = await call(action, f"{action}-space", window="4242", **extra)
            assert refused["error"] == "device_not_supported"
            assert refused["cause"] == "off_space" and "桌面空间" in refused["message"]
        assert "image" in await call("screenshot", "shot-space", window="4242")
        # A6/D6: a platform without a background route for these inputs (Windows).
        script(state, windows=[{**EDITOR, "actions": ["click", "scroll", "type"]}])
        for action, extra in (
            ("middle_click", {"x": 1, "y": 1}),
            ("triple_click", {"x": 1, "y": 1}),
            ("click", {"x": 1, "y": 1, "text": "ctrl"}),
            ("key", {"text": "Return"}),
            ("move", {"x": 1, "y": 1}),
            ("drag", {"x": 1, "y": 1, "x2": 2, "y2": 2}),
        ):
            refused = await call(action, f"{action}-unrouted", window="4242", **extra)
            assert refused["error"] == "device_not_supported", (action, refused)
            assert refused["cause"] == "no_background_route", (action, refused)
        # D7/D8: the platform names its own reason while delivering.
        for cause in ("no_control_at_point", "no_focused_field"):
            script(state, refuse="device_not_supported", refuse_cause=cause)
            refused = await call("click", f"click-{cause}", window="4242", x=1, y=1)
            assert refused["cause"] == cause and refused["status"] == "failed"
        script(state, refuse=None, refuse_cause=None)
        assert len(events(state)) == count + 1
    finally:
        exit_code, stderr = await process.stop()
        await server.__aexit__(None, None, None)
        write("window-elements", {"calls": call.calls, "events": events(state),
                                  "stdout": process.lines})
    assert exit_code == 0, stderr


async def test_ids_do_not_survive_a_restart_and_platform_reason(tmp_path: Path) -> None:
    server, cloud, process, call = await start(
        tmp_path, displays=[RETINA], windows=[{**EDITOR, "elements": NODES}]
    )
    try:
        listed = await call("elements", "list-1", window="4242")
        identity = listed["elements"].splitlines()[1].split("[")[1].split("]")[0]
    finally:
        await process.stop()
    # E5: the registry lives in the process; after a restart the old id is stale.
    process = ConnectorProcess(call.state, tmp_path / "data")
    await process.start()
    await cloud.connected.wait_for(2)
    try:
        stale = await call("click", "after-restart", window="4242", element=identity)
        assert stale["error"] == "element_stale"
        # D9/D11: a Connector without a screen it can operate (Linux, macOS 13) says so.
        script(call.state, no_screen=True)
    finally:
        await process.stop()
    process = ConnectorProcess(call.state, tmp_path / "data")
    await process.start()
    await cloud.connected.wait_for(3)
    try:
        assert "computer" not in cloud.hello[-1]["device_capabilities"]
        refused = await call("screenshot", "no-screen")
        assert refused["error"] == "device_not_supported" and refused["cause"] == "platform"
        assert "browser" in refused["message"]
    finally:
        exit_code, stderr = await process.stop()
        await server.__aexit__(None, None, None)
        write("restart-platform", {"calls": call.calls})
    assert exit_code == 0, stderr
