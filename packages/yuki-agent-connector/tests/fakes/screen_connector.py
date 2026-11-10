"""Run the actual Connector CLI with only the OS screen boundary replaced by a recorder.

Usage: python screen_connector.py <state-dir> <connector CLI arguments...>

GitHub-hosted runners cannot grant macOS screen recording or Accessibility, and posting
real input on a developer's computer is not acceptable, so the end-to-end tests run the
real process, journal, stdin/stdout contract, file transfers and stop/resume logic with
this backend. ``<state-dir>`` holds:

- ``script.json``: facts the operating system would report (read on every call, so a test
  can change them while the Connector runs): ``screen``, ``input``, ``unavailable``,
  ``elevated``, ``displays``, ``hang`` (the next input blocks forever, to kill the
  process mid-execution), ``chrome`` (false: no Chrome installed), ``windows`` (each a
  ``screen.Window`` plus ``minimized``, ``front`` — the owner's frontmost app or the window
  under the owner's pointer is this window's — ``elevated`` and ``actions``, the inputs this
  platform can deliver to a window in the background; omitted means all).
- ``owner_last_input``: a wall-clock timestamp of the owner's latest deliberate input.
- ``events.jsonl``: every input the Connector posted, in global input coordinates; input
  delivered to a window in the background carries ``window``.
"""

from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path
from typing import Any

from yuki_agent_connector import chrome, cli, screen


def jpeg(width: int, height: int) -> bytes:
    """A valid baseline JPEG of uniform grey, any size, without an image library."""
    blocks = -(-width // 8) * -(-height // 8)

    def segment(marker: int, payload: bytes) -> bytes:
        return struct.pack(">HH", marker, len(payload) + 2) + payload

    # Level-shifted 128 is 0: every block is DC difference 0 then EOB, two "0" bits.
    one_symbol = bytes([1] + [0] * 15) + b"\x00"
    data = bytes((2 * blocks + 7) // 8)
    padding = (8 - (2 * blocks) % 8) % 8
    if padding:
        data = data[:-1] + bytes([(1 << padding) - 1])
    return (
        b"\xff\xd8"
        + segment(0xFFDB, b"\x00" + b"\x01" * 64)
        + segment(0xFFC0, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00")
        + segment(0xFFC4, b"\x00" + one_symbol)
        + segment(0xFFC4, b"\x10" + one_symbol)
        + segment(0xFFDA, b"\x01\x01\x00\x00\x3f\x00")
        + data
        + b"\xff\xd9"
    )


class FakeScreen:
    def __init__(self, state: Path) -> None:
        self.state = state
        self.own = time.time()

    def script(self) -> dict[str, Any]:
        while True:
            try:
                return json.loads((self.state / "script.json").read_text())  # type: ignore[no-any-return]
            except (ValueError, PermissionError):
                # The test rewrites the file while the Connector reads it.
                time.sleep(0.01)

    def permissions(self) -> tuple[bool, bool]:
        script = self.script()
        return script.get("screen", True), script.get("input", True)

    def displays(self) -> list[screen.Display]:
        return [screen.Display(**item) for item in self.script()["displays"]]

    def unavailable(self) -> dict[str, Any] | None:
        return self.script().get("unavailable")  # type: ignore[no-any-return]

    def capture(self, display: screen.Display, width: int, height: int, path: Path) -> None:
        path.write_bytes(jpeg(width, height))

    def _latest(self) -> float:
        owner = self.state / "owner_last_input"
        while True:
            try:
                return max(self.own, float(owner.read_text()) if owner.exists() else 0.0)
            except (ValueError, PermissionError):
                # The test rewrites the file while the Connector reads it.
                time.sleep(0.01)

    def last_input(self) -> tuple[float, float]:
        latest = self._latest()
        return latest, latest

    def idle(self) -> float:
        return time.time() - self._latest()

    def elevated(self, point: tuple[float, float] | None) -> bool:
        return bool(self.script().get("elevated"))

    def _record(self, kind: str, **facts: Any) -> None:
        if self.script().get("hang"):
            (self.state / "hanging").write_text(kind)
            while True:
                time.sleep(1)
        with (self.state / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **facts}, ensure_ascii=False) + "\n")

    def _post(self, kind: str, **facts: Any) -> None:
        self._record(kind, **facts)
        # Global input is the session's latest input; background input is not (spike Q4).
        self.own = time.time()

    def move(self, x: float, y: float) -> None:
        self._post("move", x=x, y=y)

    def click(self, x: float, y: float, button: str, count: int) -> None:
        self._post("click", x=x, y=y, button=button, count=count)

    def drag(self, x: float, y: float, x2: float, y2: float) -> None:
        self._post("drag", x=x, y=y, x2=x2, y2=y2)

    def scroll(self, x: float, y: float, dx: int, dy: int) -> None:
        self._post("scroll", x=x, y=y, dx=dx, dy=dy)

    def key(self, modifiers: tuple[str, ...], key: str) -> None:
        self._post("key", modifiers=list(modifiers), key=key)

    def type(self, text: str) -> None:
        self._post("type", text=text)

    def _windows(self) -> list[dict[str, Any]]:
        return list(self.script().get("windows", []))

    def windows(self) -> list[dict[str, str]]:
        return [
            {"id": item["id"], "app": item["app"], "title": item["title"]}
            for item in self._windows()
        ]

    def window(self, identity: str) -> FakeTarget:
        facts = next((item for item in self._windows() if item["id"] == identity), None)
        if facts is None:
            raise screen.Refused("window_not_found", f"找不到窗口 {identity}。")
        if facts.get("minimized"):
            raise screen.Refused("window_unavailable", "这个窗口已最小化。")
        geometry = {key: facts[key] for key in screen.Window.__dataclass_fields__}
        return FakeTarget(self, screen.Window(**geometry), facts)


class FakeTarget:
    """One window: input to it is recorded with its id and never as global input."""

    def __init__(self, owner: FakeScreen, window: screen.Window, facts: dict[str, Any]) -> None:
        self.owner, self.window, self.facts = owner, window, facts
        self.actions = frozenset(
            facts.get(
                "actions",
                ["click", "double_click", "right_click", "move", "drag", "type", "key", "scroll"],
            )
        )

    def collides(self) -> bool:
        return bool(self.facts.get("front"))

    def elevated(self) -> bool:
        return bool(self.facts.get("elevated"))

    def capture(self, width: int, height: int, path: Path) -> None:
        path.write_bytes(jpeg(width, height))

    def _post(self, kind: str, **facts: Any) -> None:
        refused = self.owner.script().get("refuse")
        if refused:
            # No background route for this input (e.g. no UI Automation pattern there).
            raise screen.Refused(refused, "该处没有可在后台操作的控件。")
        self.owner._record(kind, window=self.window.id, **facts)

    def focus(self, x: float, y: float) -> None:
        self._post("focus", x=x, y=y)

    def move(self, x: float, y: float) -> None:
        self._post("move", x=x, y=y)

    def click(self, x: float, y: float, button: str, count: int) -> None:
        self._post("click", x=x, y=y, button=button, count=count)

    def drag(self, x: float, y: float, x2: float, y2: float) -> None:
        self._post("drag", x=x, y=y, x2=x2, y2=y2)

    def scroll(self, x: float, y: float, dx: int, dy: int) -> None:
        self._post("scroll", x=x, y=y, dx=dx, dy=dy)

    def key(self, modifiers: tuple[str, ...], key: str) -> None:
        self._post("key", modifiers=list(modifiers), key=key)

    def type(self, text: str) -> None:
        self._post("type", text=text)


if __name__ == "__main__":
    state = Path(sys.argv[1])
    screen.native_backend = lambda: FakeScreen(state)  # type: ignore[assignment,return-value]
    if json.loads((state / "script.json").read_text()).get("chrome") is False:
        chrome.find_chrome = lambda: None
    raise SystemExit(cli.main(sys.argv[2:]))
