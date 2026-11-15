"""Computer Use on Yuki's own computer: the real Manager, desktop service, Xvnc, Chromium,
driver, AT-SPI and xclip of the headed environment image (CI ``desktop-headed``).

Failure cases were written before the code, from docs/architecture/Computer-Use任务书-2026-11-15.md
§11: C2, C7–C9, X1, X2 (sizes, coordinates, Manager receipts), A1, A3–A5, A8, A10–A13
(actions), Z1–Z4 (zoom), P1 (the pointer in the picture), R1, R6, R8, R9 (echo), E1, E2, E4,
E6, E12, E13, E14 (accessibility nodes of a GTK window), V1, V4, V5, V8 (paste). A local page
records every pointer and key event the real Chromium receives and posts them here.

Artifacts: ``test-results/computer-use/`` — ``steps.json`` (each request, receipt and echo),
each step's picture plus a copy with the echoed point drawn in, ``pointer-lab-events.json``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.integration.test_desktop_session_e2e import Session
from yuki_agent_connector.cdp import image_scale

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "computer-use"
LIMITS = {"max_dimension": 1568, "max_pixels": 1_150_000}
GTK = ROOT / "tests/support/gtk_window.py"

pytestmark = pytest.mark.skipif(
    not all(
        shutil.which(name)
        for name in ("Xvnc", "openbox-session", "chromium", "xdotool", "import", "xclip")
    )
    or not Path("/usr/libexec/at-spi-bus-launcher").exists(),
    reason="Requires the actual headed environment packages (with at-spi2-core and xclip)",
)

LAB = """<!doctype html><meta charset=utf-8><title>pointer-lab</title>
<body style="margin:0;font:16px sans-serif">
<div id=pad style="position:fixed;left:0;top:0;right:0;bottom:130px;background:#dde"></div>
<textarea id=typing aria-label=typing
 style="position:fixed;left:0;right:0;bottom:0;height:120px;width:100%"></textarea>
<script>
const send = record => fetch('/event', {method: 'POST', body: JSON.stringify(record)});
let down = false, moves = 0;
for (const type of ['mousedown', 'mouseup', 'click', 'dblclick', 'auxclick', 'contextmenu'])
  addEventListener(type, e => {
    if (type === 'contextmenu') e.preventDefault();
    if (type === 'mousedown') { down = true; moves = 0; }
    const record = {type, button: e.button, detail: e.detail, shift: e.shiftKey,
      ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey, x: e.screenX, y: e.screenY,
      target: e.target.id};
    if (type === 'mouseup') { record.moves = moves; down = false; }
    send(record);
  }, true);
addEventListener('mousemove', () => { if (down) moves++; }, true);
addEventListener('wheel', e => { if (e.ctrlKey) e.preventDefault();
  send({type: 'wheel', dx: Math.sign(e.deltaX), dy: Math.sign(e.deltaY), shift: e.shiftKey,
        ctrl: e.ctrlKey}); }, {capture: true, passive: false});
addEventListener('keydown', e => send({type: 'keydown', key: e.key, shift: e.shiftKey,
  ctrl: e.ctrlKey}), true);
document.getElementById('typing').addEventListener('input',
  e => send({type: 'value', value: e.target.value}));
</script>"""


class Lab:
    """The local page and the event log it posts to."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        lab = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = LAB.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                lab.events.append(json.loads(self.rfile.read(length)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def since(self, mark: int, kind: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.events[mark:] if kind is None or e["type"] == kind]

    def value(self) -> str:
        values = [e["value"] for e in self.events if e["type"] == "value"]
        return values[-1] if values else ""


def display(*command: str) -> str:
    return subprocess.run(
        command, env={**os.environ, "DISPLAY": ":1"}, capture_output=True, text=True, check=True
    ).stdout


def geometry(window: str) -> tuple[int, int, int, int]:
    fields = {
        key.strip(): value.strip()
        for key, _, value in (
            line.partition(":") for line in display("xwininfo", "-id", window).splitlines()
        )
    }
    return (
        int(fields["Absolute upper-left X"]),
        int(fields["Absolute upper-left Y"]),
        int(fields["Width"]),
        int(fields["Height"]),
    )


def selection(name: str) -> str:
    found = subprocess.run(
        ["xclip", "-selection", name, "-out"],
        env={**os.environ, "DISPLAY": ":1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    return found.stdout


class Steps:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.steps: list[dict[str, Any]] = []

    async def __call__(self, action: str, key: str | None = None, **args: Any) -> dict[str, Any]:
        key = key or f"{action}-{uuid4().hex[:8]}"
        result = await self.session.manager.desktop.action(
            "computer_action", {"action": action, **LIMITS, **args}, key
        )
        entry: dict[str, Any] = {"key": key, "action": action, "arguments": args}
        image = result.get("image")
        echo = {k: v for k, v in result.items() if k != "image"}
        entry["result"] = echo
        if isinstance(image, dict):
            index = len(self.steps)
            path = ARTIFACT / f"{index:02d}-{action}.jpg"
            path.write_bytes(base64.b64decode(image["base64"]))
            entry["picture"] = path.name
            point = echo.get("point") or echo.get("cursor")
            if point:
                marked = path.with_name(path.stem + "-point.png")
                await asyncio.to_thread(
                    subprocess.run,
                    [
                        "convert",
                        str(path),
                        "-fill",
                        "none",
                        "-stroke",
                        "red",
                        "-strokewidth",
                        "2",
                        "-draw",
                        f"circle {point[0]},{point[1]} {point[0] + 9},{point[1]}",
                        str(marked),
                    ],
                    check=True,
                )
                entry["marked"] = marked.name
        self.steps.append(entry)
        (ARTIFACT / "steps.json").write_text(
            json.dumps(self.steps, ensure_ascii=False, indent=2, default=str)
        )
        return result


def pixel_patch(jpeg: bytes, x: int, y: int) -> bytes:
    """Raw RGB of a 10x14 patch at x,y of a JPEG (ImageMagick of the image)."""
    return subprocess.run(
        ["convert", "jpeg:-", "-crop", f"10x14+{x}+{y}", "+repage", "rgb:-"],
        input=jpeg,
        capture_output=True,
        check=True,
    ).stdout


async def until(check: Any, seconds: float = 20, message: str = "") -> Any:
    deadline = time.monotonic() + seconds
    while not (value := check()):
        assert time.monotonic() < deadline, message or "condition not reached"
        await asyncio.sleep(0.1)
    return value


async def test_own_computer_computer_use(tmp_path: Path) -> None:
    shutil.rmtree(ARTIFACT, ignore_errors=True)
    ARTIFACT.mkdir(parents=True)
    (tmp_path / "manager").mkdir()
    session = Session(tmp_path / "manager")
    manager = session.manager
    lab = Lab()
    gtk: subprocess.Popen[bytes] | None = None
    await session.start()
    call = Steps(session)
    try:
        woken = await session.computer("wake", "wake-1")
        assert woken["display"] == "running" and woken["browser"] == "running", woken
        opened = await manager.desktop.action(
            "browser_action", {"action": "open", "url": lab.url, **LIMITS}, "open-lab"
        )
        assert not opened.get("error"), opened

        # C2: 1440x900 under the computer screenshot limit is 1356x848; P1: the pointer.
        display("xdotool", "mousemove", "700", "400")
        shot = await call("screenshot", "shot-1")
        assert (shot["width"], shot["height"]) == (1356, 848), shot
        assert "scale=1000" in shot["coordinates"]
        scale = 1356 / 1440
        assert shot["cursor"] == [round(700 * scale), round(400 * scale)]
        chromium = next(w["id"] for w in shot["windows"] if w["app"].lower() == "chromium")
        left, top, width, height = geometry(chromium)
        centre = (left + width // 2, top + height // 3)

        # P1: the pointer is drawn in: the patch at the cursor changes when it moves away.
        here = base64.b64decode(shot["image"]["base64"])
        display("xdotool", "mousemove", "100", "100")
        away = base64.b64decode((await call("screenshot", "shot-2"))["image"]["base64"])
        x, y = shot["cursor"]
        assert pixel_patch(here, x, y) != pixel_patch(away, x, y)

        # C8/R1: 0..1000 coordinates; point echoes the image pixel; the click lands there.
        mark = len(lab.events)
        normalized = (round(centre[0] * scale * 1000 / 1356), round(centre[1] * scale * 1000 / 848))
        clicked = await call("click", "click-norm", x=normalized[0], y=normalized[1], scale=1000)
        assert clicked["submitted"] is True and clicked["scale"] == 1000, clicked
        assert clicked["route"] == ["global_input"]
        point = clicked["point"]
        down = (await until(lambda: lab.since(mark, "mousedown")))[0]
        assert abs(down["x"] - round(point[0] / scale)) <= 1, (down, point)
        assert abs(down["y"] - round(point[1] / scale)) <= 1, (down, point)
        assert not (down["shift"] or down["ctrl"] or down["alt"])
        # R6: Chromium's page is not exposed to AT-SPI: no hit, and the input still ran.
        assert "hit" not in clicked

        # C7/X1: outside the image; the Manager's pending row ends as failed, never unknown;
        # X2: the same key returns that receipt.
        mark = len(lab.events)
        outside = await call("click", "click-out", x=1356, y=10)
        assert outside["error"] == "coordinate_out_of_range" and outside["status"] == "failed"
        receipt = json.loads(
            manager.db.execute(
                "SELECT result FROM environment_mutations WHERE request_id='click-out'"
            ).fetchone()[0]
        )
        assert receipt["status"] == "failed" and receipt["error"] == "coordinate_out_of_range"
        assert (await call("click", "click-out", x=1356, y=10))["error"] == outside["error"]
        # C9 and an undeclared system.
        assert (await call("click", x=1001, y=5, scale=1000))["error"] == "coordinate_out_of_range"
        assert (await call("click", x=5, y=5, scale=100))["error"] == "invalid_arguments"
        await asyncio.sleep(0.5)
        assert lab.since(mark, "mousedown") == []

        # A1/A3: modifiers only for this click; an unknown modifier posts nothing.
        pixel = [round(centre[0] * scale), round(centre[1] * scale)]
        mark = len(lab.events)
        await call("click", "click-mod", x=pixel[0], y=pixel[1], text="ctrl+shift")
        held = (await until(lambda: lab.since(mark, "mousedown")))[0]
        assert held["shift"] and held["ctrl"] and not held["alt"], held
        await asyncio.sleep(0.6)
        mark = len(lab.events)
        await call("click", x=pixel[0], y=pixel[1])
        plain = (await until(lambda: lab.since(mark, "mousedown")))[0]
        assert not (plain["shift"] or plain["ctrl"]), plain
        assert (await call("click", x=pixel[0], y=pixel[1], text="hyper"))["error"] == (
            "invalid_arguments"
        )
        # A4/A5: a triple click arrives with detail 1, 2, 3; the middle button is button 1.
        await asyncio.sleep(0.6)
        mark = len(lab.events)
        await call("triple_click", x=pixel[0], y=pixel[1])
        await until(lambda: len(lab.since(mark, "mousedown")) == 3)
        assert [e["detail"] for e in lab.since(mark, "mousedown")] == [1, 2, 3]
        await asyncio.sleep(0.6)
        mark = len(lab.events)
        await call("middle_click", x=pixel[0], y=pixel[1])
        assert (await until(lambda: lab.since(mark, "mousedown")))[0]["button"] == 1
        # A12/A13: a drag with shift held posts intermediate moves.
        mark = len(lab.events)
        dragged = await call(
            "drag",
            x=pixel[0] - 100,
            y=pixel[1],
            x2=pixel[0] + 100,
            y2=pixel[1] + 20,
            text="shift",
        )
        assert dragged["point2"] == [pixel[0] + 100, pixel[1] + 20]
        up = (await until(lambda: lab.since(mark, "mouseup")))[0]
        assert up["moves"] > 1 and up["shift"], up
        # Scroll with ctrl held; counts are not scaled.
        mark = len(lab.events)
        await call(
            "scroll",
            x=round(pixel[0] * 1000 / 1356),
            y=round(pixel[1] * 1000 / 848),
            scale=1000,
            dy=2,
            text="ctrl",
        )
        wheels = await until(lambda: lab.since(mark, "wheel"))
        assert wheels[0]["ctrl"] and wheels[0]["dy"] == 1

        # A8/A10/A11: keys through the shared grammar; repeat; unknown keys refused.
        textarea = [pixel[0], round((top + height - 60) * scale)]
        await call("click", x=textarea[0], y=textarea[1])
        await call("type", text="abcd")
        await until(lambda: lab.value() == "abcd", message=lab.value())
        await call("key", text="BackSpace", repeat=2)
        await until(lambda: lab.value() == "ab", message=lab.value())
        await call("key", text="KP_Enter")
        await call("key", text="Return")
        await until(lambda: lab.value() == "ab\n\n", message=repr(lab.value()))
        assert (await call("key", text="ctrl+nonsense"))["error"] == "invalid_arguments"
        assert (await call("key", text="Return", repeat=0))["error"] == "invalid_arguments"
        await call("key", text="ctrl+a")
        await call("key", text="Delete")
        await until(lambda: lab.value() == "", message=repr(lab.value()))

        # V1: paste long mixed text through her own clipboard (CLIPBOARD and PRIMARY).
        long = "\n".join(f"第{index}行 Yuki 粘贴 ✓ line {index}" for index in range(200))
        pasted = await call("paste", "paste-1", text=long)
        assert pasted["route"] == ["clipboard", "global_input"] and "point" not in pasted
        await until(lambda: lab.value() == long, seconds=30, message=lab.value()[-80:])
        assert selection("clipboard") == long and selection("primary") == long
        # V8: the same key returns the original receipt; nothing is pasted again.
        assert (await call("paste", "paste-1", text=long))["route"] == pasted["route"]
        await asyncio.sleep(1)
        assert lab.value() == long
        # V4: nothing to paste, or a point.
        assert (await call("paste", text=""))["error"] == "invalid_arguments"
        assert (await call("paste", text="x", x=1, y=1))["error"] == "invalid_arguments"

        # Z1–Z3: zoom reads native pixels of a region; the next click still uses the full
        # screenshot's coordinates.
        zoomed = await call("zoom", "zoom-1", region=[100, 100, 400, 250])
        assert zoomed["region"] == [100, 100, 400, 250]
        assert zoomed["frame"] == {"width": 1356, "height": 848}
        exact = image_scale(1440, 900, **LIMITS)
        assert (zoomed["width"], zoomed["height"]) == (round(300 / exact), round(150 / exact))
        whole = await call("zoom", region=[0, 0, 1000, 1000], scale=1000)
        # The whole screen as a region obeys the same limit: never larger than a screenshot.
        assert whole["region"] == [0, 0, 1356, 848] and whole["width"] == 1356
        assert (await call("zoom", region=[0, 0, 1357, 10]))["error"] == "coordinate_out_of_range"

        # E1/E2/E14/R6: a GTK window's nodes; press and type through AT-SPI.
        log = tmp_path / "gtk.log"
        gtk = subprocess.Popen(  # noqa: ASYNC220 - a window that lives for the test
            [sys.executable, str(GTK), "Yuki GTK lab", str(log)],
            env={
                key: value
                for key, value in {**os.environ, "DISPLAY": ":1"}.items()
                # Like an app she starts from a terminal: no session bus address.
                if key != "DBUS_SESSION_BUS_ADDRESS"
            },
        )
        await until(log.exists)
        windows = await until(
            lambda: [
                w
                for w in display(
                    "xdotool", "search", "--onlyvisible", "--name", "Yuki GTK lab"
                ).split()
            ]
        )
        window = hex(int(windows[0]))
        listed = await call("elements", "elements-1", window=window)
        lines = listed["elements"].splitlines()
        button = next(line for line in lines if 'push button "Press me"' in line)
        entry = next(line for line in lines if line.lstrip().startswith("- text "))
        button_id = button.split("[")[1].split("]")[0]
        entry_id = entry.split("[")[1].split("]")[0]
        pressed = await call("click", "press-1", window=window, element=button_id)
        assert pressed["route"] == ["accessibility"] and pressed["element"] == button_id
        assert pressed["hit"] == {"role": "push button", "name": "Press me"}
        await until(lambda: "clicked" in log.read_text())
        typed = await call("type", window=window, element=entry_id, text="雪 abc")
        assert typed["route"] == ["accessibility", "global_input"], typed
        await until(
            lambda: (
                log.with_suffix(".entry").exists()
                and log.with_suffix(".entry").read_text() == "雪 abc"
            )
        )
        # R6: a coordinate click on the button names it before the input.
        box = [int(v) for v in button.rsplit("(", 1)[1].rstrip(")").split(",")]
        middle = [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]
        by_point = await call("click", window=window, x=middle[0], y=middle[1])
        assert by_point.get("hit", {}).get("role") == "push button", by_point
        # E4/E6: a new list makes older ids stale; an id of another window is refused.
        await call("elements", window=window)
        stale = await call("click", window=window, element=button_id)
        assert stale["error"] == "element_stale" and "Press me" in stale["elements"]
        assert (await call("click", window=chromium, element=button_id))["error"] == (
            "invalid_arguments"
        )
        # E12: Chromium does not register with AT-SPI here.
        none = await call("elements", window=chromium)
        assert none["error"] == "device_not_supported" and none["cause"] == "no_accessibility"

        # Z4/E13/V5: while the person holds the computer, reads work and paste is held.
        taken = manager.computer.set(
            str(uuid4()),
            {
                "action": "take",
                "actor": {
                    "person_id": str(uuid4()),
                    "device_id": str(uuid4()),
                    "device_label": "desk",
                },
            },
        )
        assert "region" in await call("zoom", region=[0, 0, 100, 100])
        assert "elements" in await call("elements", window=window)
        before = selection("clipboard")
        held_paste = await call("paste", text="never")
        assert held_paste["error"] == "control_held", held_paste
        assert selection("clipboard") == before
        manager.computer.set(
            str(uuid4()),
            {
                "action": "return",
                "lease_id": taken["control"]["lease_id"],
                "actor": taken["control"] | {"system": False},
            },
        )
    finally:
        (ARTIFACT / "pointer-lab-events.json").write_text(
            json.dumps(lab.events, ensure_ascii=False, indent=2)
        )
        if gtk is not None:
            gtk.terminate()
            gtk.wait(timeout=10)
        lab.server.shutdown()
        await session.close()
