"""Run the actual Connector CLI with only the OS screen boundary replaced by a recorder.

Usage: python screen_connector.py <state-dir> <connector CLI arguments...>

GitHub-hosted runners cannot grant macOS screen recording or Accessibility, and posting
real input on a developer's computer is not acceptable, so the end-to-end tests run the
real process, journal, stdin/stdout contract, file transfers and stop/resume logic with
this backend. ``<state-dir>`` holds:

- ``script.json``: facts the operating system would report (read on every call, so a test
  can change them while the Connector runs): ``screen``, ``input``, ``unavailable``,
  ``elevated``, ``displays``, ``hang`` (the next input blocks forever, to kill the
  process mid-execution), ``chrome`` (false: no Chrome installed).
- ``owner_input``: a wall-clock timestamp of the owner's latest input.
- ``events.jsonl``: every input the Connector posted, in global input coordinates.
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

    def last_input(self) -> tuple[float, float]:
        owner = self.state / "owner_input"
        latest = max(self.own, float(owner.read_text()) if owner.exists() else 0.0)
        return latest, latest

    def elevated(self, point: tuple[float, float] | None) -> bool:
        return bool(self.script().get("elevated"))

    def _post(self, kind: str, **facts: Any) -> None:
        if self.script().get("hang"):
            (self.state / "hanging").write_text(kind)
            while True:
                time.sleep(1)
        with (self.state / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **facts}, ensure_ascii=False) + "\n")
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


if __name__ == "__main__":
    state = Path(sys.argv[1])
    screen.native_backend = lambda: FakeScreen(state)  # type: ignore[assignment,return-value]
    if json.loads((state / "script.json").read_text()).get("chrome") is False:
        chrome.find_chrome = lambda: None
    raise SystemExit(cli.main(sys.argv[2:]))
