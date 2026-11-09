"""Environment-side computer CLI and driver host. Uses only its runtime mount and stdlib.

``serve`` is the long-lived driver started by ``desktop_service.py``. The Manager
puts requests under ``<runtime>/driver/requests``; one request runs at a time,
is claimed by renaming it into ``taken/`` and answered in ``results/``. A request
claimed by a driver that died is never re-run: its taken file is removed at the
next start and the Manager reports the original call as unknown.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

if TYPE_CHECKING:
    # Only the long-lived driver needs it; help/wait/status stay standard-library only.
    from yuki_agent_connector import cdp


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def wait(root: Path, kind: str, note: str | None) -> dict[str, Any]:
    identity = str(UUID(os.environ["YUKI_JOB_ID"]))
    result = root / "results" / (identity + ".json")
    # A prior registration may already have finished, including before restart
    # or before this process first entered its file wait.
    if not result.exists():
        atomic_json(root / "requests" / (identity + ".json"), {"kind": kind, "note": note})
    while True:
        try:
            return cast(dict[str, Any], json.loads(result.read_text()))
        except FileNotFoundError:
            time.sleep(0.1)


def mark(label: str) -> None:
    if os.environ.get("YUKI_WORK_ID") and os.environ.get("YUKI_JOB_ID"):
        path = (
            Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer")).parent
            / str(UUID(os.environ["YUKI_JOB_ID"]))
            / "state/step-label.json"
        )
        atomic_json(path, {"step_label": label})


def control(root: Path) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], json.loads((root / "control.json").read_text()))
    except FileNotFoundError:
        # The Manager writes this file whenever control changes; none means none.
        return {"holder": "none"}


def visible(current: dict[str, Any]) -> dict[str, Any]:
    return {key: current[key] for key in ("holder", "since") if key in current}


def lease_check(root: Path, lease_id: str | None) -> Callable[[], None]:
    """Run before every input: the request's lease must still be the current agent lease."""

    def check() -> None:
        from yuki_agent_connector import cdp

        current = control(root)
        if current.get("holder") != "agent" or current.get("lease_id") != lease_id:
            raise cdp.Failure(
                {
                    "error": "control_held",
                    "message": "電腦目前不由這次操作控制（可能已被本人接管），沒有繼續輸入。",
                    "control": visible(current),
                }
            )

    return check


def keysym(character: str) -> int | None:
    code = ord(character)
    if code < 0x20 or code == 0x7F:
        # Newline and tab are typed by xdotool as Return and Tab.
        return None
    if code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 + code


class Keymap:
    """Spare X keycodes bound to the characters that are about to be typed.

    xdotool types a character missing from the keymap by rebinding one scratch
    keycode per character. A client re-reads the keymap only when it gets to the
    change notification, by which time the scratch keycode may already mean the
    next character (observed: "中文" arrived as "中中" in Chromium). Here every
    character of a run gets its own keycode before the run is typed, bound on both
    levels so XKB does not case-fold it, and bindings stay in place afterwards so a
    late re-read still finds them. Only when a text needs more characters than
    there are spare keycodes are the least recently used bindings reused, between
    runs.
    """

    def __init__(self, display: str) -> None:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XDisplayKeycodes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        x11.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        x11.XGetKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        x11.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_int,
        ]
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11 = x11
        self.connection = x11.XOpenDisplay(display.encode())
        if not self.connection:
            raise OSError(f"cannot open X display {display}")
        # keycode -> keysym bound here, least recently used first.
        self.bound: dict[int, int] = {}

    def mapping(self) -> dict[int, tuple[int, ...]]:
        low, high, width = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        self.x11.XDisplayKeycodes(self.connection, ctypes.byref(low), ctypes.byref(high))
        count = high.value - low.value + 1
        table = self.x11.XGetKeyboardMapping(self.connection, low.value, count, ctypes.byref(width))
        try:
            return {
                low.value + index: tuple(
                    table[index * width.value + level] for level in range(width.value)
                )
                for index in range(count)
            }
        finally:
            self.x11.XFree(table)

    def runs(self, text: str) -> Iterator[str]:
        """Consecutive parts of ``text``; each is fully on the keymap when yielded."""
        start = 0
        while start < len(text):
            mapping = self.mapping()
            # Someone else (Xvnc for a viewer's keys) may have rebound a keycode.
            self.bound = {
                code: symbol
                for code, symbol in self.bound.items()
                if symbol in mapping.get(code, ())
            }
            present = {
                symbol
                for code, symbols in mapping.items()
                if code not in self.bound
                for symbol in symbols
            }
            spare = [
                code
                for code, symbols in mapping.items()
                if not any(symbols) and code not in self.bound
            ] + list(self.bound)
            wanted: dict[int, None] = {}
            end = start
            while end < len(text):
                symbol = keysym(text[end])
                if symbol is not None and symbol not in present and symbol not in wanted:
                    if len(wanted) == len(spare):
                        break
                    wanted[symbol] = None
                end += 1
            holders = {symbol: code for code, symbol in self.bound.items() if symbol in wanted}
            free = [code for code in spare if self.bound.get(code) not in wanted]
            for symbol in wanted:
                code = holders[symbol] if symbol in holders else free.pop(0)
                if symbol not in holders:
                    pair = (ctypes.c_ulong * 2)(symbol, symbol)
                    self.x11.XChangeKeyboardMapping(self.connection, code, 2, pair, 1)
                self.bound.pop(code, None)
                self.bound[code] = symbol
            self.x11.XSync(self.connection, 0)
            # With no spare keycode at all, xdotool's own scratch binding remains.
            end = max(end, start + 1)
            yield text[start:end]
            start = end


class Screen:
    """The sandbox X display: screenshots with ImageMagick, input with xdotool."""

    def __init__(self, display: str) -> None:
        self.display = display
        self.environment = {**os.environ, "DISPLAY": display, "LC_ALL": "C.UTF-8"}
        self.keymap: Keymap | None = None

    def run(self, deadline: float, *command: str) -> bytes:
        return subprocess.run(
            command,
            env=self.environment,
            check=True,
            capture_output=True,
            timeout=max(0.0, deadline - time.monotonic()),
        ).stdout

    def geometry(self, deadline: float, window: str | None) -> tuple[int, int, int, int]:
        """Origin and size of the root window or of one top-level window, read now."""
        from yuki_agent_connector import cdp

        if window is None:
            output = self.run(deadline, "xwininfo", "-root")
        else:
            try:
                # xwininfo reads an unparsable or zero id as "let the user click
                # a window" and would wait for a pointer click.
                if not int(window, 0):
                    raise ValueError(window)
                output = self.run(deadline, "xwininfo", "-id", window)
            except (ValueError, subprocess.CalledProcessError):
                raise cdp.Failure(
                    {"error": "window_not_found", "message": f"找不到視窗：{window}"}
                ) from None
        fields = {
            key.strip(): value.strip()
            for key, _, value in (line.partition(":") for line in output.decode().splitlines())
        }
        if window and fields["Map State"] != "IsViewable":
            raise cdp.Failure(
                {
                    "error": "window_unavailable",
                    "message": f"視窗 {window} 目前沒有顯示（可能已最小化）。",
                }
            )
        return (
            int(fields["Absolute upper-left X"]),
            int(fields["Absolute upper-left Y"]),
            int(fields["Width"]),
            int(fields["Height"]),
        )

    def windows(self, deadline: float) -> list[dict[str, str]]:
        """Visible managed top-level windows; ``app`` is the WM_CLASS class name."""
        listed = self.run(deadline, "xprop", "-root", "_NET_CLIENT_LIST").decode()
        result = []
        for identity in re.findall(r"0x[0-9a-f]+", listed.partition("#")[2]):
            properties = self.run(
                deadline, "xprop", "-id", identity, "WM_CLASS", "_NET_WM_NAME", "_NET_WM_STATE"
            ).decode()
            if "_NET_WM_STATE_HIDDEN" in properties:
                continue
            classes = re.findall(r'"((?:[^"\\]|\\.)*)"', _property(properties, "WM_CLASS"))
            title = re.findall(r'"((?:[^"\\]|\\.)*)"', _property(properties, "_NET_WM_NAME"))
            result.append(
                {
                    "id": identity,
                    "app": classes[-1] if classes else "",
                    "title": title[0].replace('\\"', '"') if title else "",
                }
            )
        return result

    def execute(
        self,
        args: dict[str, Any],
        root: Path,
        before_input: Callable[[], None],
        max_dimension: int,
        max_pixels: int,
        deadline: float,
    ) -> tuple[dict[str, Any], cdp.Image]:
        from yuki_agent_connector import cdp

        action, window = args["action"], args.get("window")
        result: dict[str, Any] = {}
        inputs = 0

        def xdotool(*command: str) -> None:
            nonlocal inputs
            before_input()
            inputs += 1
            self.run(deadline, "xdotool", *command)

        try:
            if action not in ("screenshot", "resume"):
                left, top, width, height = self.geometry(deadline, window)
                scale = cdp.image_scale(width, height, max_dimension, max_pixels)

                def at(x_name: str, y_name: str) -> list[str]:
                    # Coordinates are in the image this geometry and scale produce.
                    if args.get(x_name) is None or args.get(y_name) is None:
                        return []
                    return [
                        "mousemove",
                        "--sync",
                        str(left + round(args[x_name] / scale)),
                        str(top + round(args[y_name] / scale)),
                    ]

                if action == "click":
                    xdotool(*at("x", "y"), "click", "1")
                elif action == "double_click":
                    xdotool(*at("x", "y"), "click", "--repeat", "2", "1")
                elif action == "right_click":
                    xdotool(*at("x", "y"), "click", "3")
                elif action == "move":
                    xdotool(*at("x", "y"))
                elif action == "drag":
                    xdotool(*at("x", "y"), "mousedown", "1")
                    xdotool(*at("x2", "y2"))
                    xdotool("mouseup", "1")
                elif action == "type":
                    if self.keymap is None:
                        self.keymap = Keymap(self.display)
                    for run in self.keymap.runs(args.get("text") or ""):
                        xdotool("type", "--clearmodifiers", "--", run)
                elif action == "key":
                    xdotool("key", "--clearmodifiers", "--", args.get("text") or "")
                elif action == "scroll":
                    # X buttons 4/5 scroll up/down and 6/7 left/right, one notch each.
                    steps = [*at("x", "y")]
                    for amount, negative, positive in (
                        (args.get("dy") or 0, "4", "5"),
                        (args.get("dx") or 0, "6", "7"),
                    ):
                        if amount:
                            steps += [
                                "click",
                                "--repeat",
                                str(abs(amount)),
                                positive if amount > 0 else negative,
                            ]
                    xdotool(*steps)
                else:
                    raise cdp.Failure(
                        {"error": "invalid_arguments", "message": f"未知的動作：{action}"}
                    )
                result["submitted"] = True
            # The image always shows the state after the action, at current geometry.
            _, _, width, height = self.geometry(deadline, window)
            scale = cdp.image_scale(width, height, max_dimension, max_pixels)
            size = (round(width * scale), round(height * scale))
            image = self.run(
                deadline,
                "import",
                "-display",
                self.display,
                "-window",
                window or "root",
                *(("-resize", f"{size[0]}x{size[1]}!") if scale < 1 else ()),
                "-quality",
                "80",
                "jpeg:-",
            )
            if action in ("screenshot", "resume"):
                result["windows"] = self.windows(deadline)
        except subprocess.TimeoutExpired:
            raise cdp.Failure(
                {
                    "error": "timeout",
                    "message": "桌面操作在等待時間內沒有完成。",
                    **({"status": "unknown"} if inputs else {}),
                }
            ) from None
        except subprocess.CalledProcessError as error:
            raise cdp.Failure(
                {
                    "error": "input_failed",
                    "message": f"桌面指令失敗：{error.stderr.decode(errors='replace').strip()}",
                    **({"status": "unknown"} if inputs else {}),
                }
            ) from None
        except cdp.Failure as failure:
            if inputs:
                failure.result["status"] = "unknown"
            raise
        return (
            {
                "width": size[0],
                "height": size[1],
                "display": self.display,
                **({"window": window} if window else {}),
                "control": visible(control(root)),
                **result,
            },
            cdp.Image(image, "image/jpeg", size[0], size[1]),
        )


def _property(output: str, name: str) -> str:
    for line in output.splitlines():
        if line.startswith(name + "("):
            return line.partition("=")[2]
    return ""


READS = {"computer": ("screenshot", "resume"), "browser": ("snapshot", "screenshot")}


def handle(
    request: dict[str, Any], root: Path, browser: cdp.Browser, screen: Screen | None
) -> tuple[dict[str, Any], cdp.Image | None]:
    from yuki_agent_connector import cdp

    args = request["args"]
    before_input = (
        unchecked
        if args["action"] in READS[request["tool"]]
        else lease_check(root, request["lease_id"])
    )
    try:
        if request["tool"] == "browser":
            return browser.execute(
                args,
                owner=request["owner"],
                before_input=before_input,
                max_dimension=request["max_dimension"],
                max_pixels=request["max_pixels"],
                wait_seconds=request["wait_seconds"],
            )
        if screen is None or args.get("display") not in (None, screen.display):
            return {
                "error": "display_unavailable",
                "message": "Yuki 環境沒有可操作的桌面顯示（無頭模式只能使用 browser）。",
            }, None
        # In the sandbox nobody else's input competes for this display: a human
        # takes over through Desktop and returns control there, so ``resume``
        # has no stop to clear and reads exactly like ``screenshot``.
        return screen.execute(
            args,
            root,
            before_input,
            request["max_dimension"],
            request["max_pixels"],
            time.monotonic() + request["wait_seconds"],
        )
    except cdp.Failure as failure:
        return failure.result, None


def unchecked() -> None:
    """Read actions never consult the control lease."""


def serve(root: Path, endpoint: str, downloads: str, display: str | None) -> None:
    from yuki_agent_connector import cdp

    driver = root / "driver"
    requests, taken, results = driver / "requests", driver / "taken", driver / "results"
    for directory in (requests, taken, results):
        directory.mkdir(parents=True, exist_ok=True)
    for leftover in taken.glob("*.json"):
        leftover.unlink()
    # Bounds the one-time loopback handshake; desktop_service has already seen
    # the debugger answer, so this is not a readiness gate.
    browser = cdp.Browser(endpoint, downloads=downloads, timeout=10)
    screen = Screen(display) if display else None
    written: list[dict[str, Any]] | None = None
    while True:
        tabs = [
            {"target_id": tab["id"], "title": tab["title"], "url": tab["url"]}
            | ({"work_id": tab["work_id"]} if "work_id" in tab else {})
            for tab in browser.tabs()
        ]
        if tabs != written:
            atomic_json(driver / "tabs.json", {"tabs": tabs})
            written = tabs
        pending = sorted(requests.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
        if not pending:
            # Idle: wait for browser events instead of sleeping blind.
            browser.pump(0.05)
            continue
        claimed = taken / pending[0].name
        try:
            os.rename(pending[0], claimed)
        except FileNotFoundError:
            # The Manager withdrew the request before it was claimed.
            continue
        request = json.loads(claimed.read_text())
        identity = str(UUID(request["id"]))
        result, image = handle(request, root, browser, screen)
        response: dict[str, Any] = {"result": result}
        if image is not None:
            path = results / (identity + ".jpg")
            path.write_bytes(image.data)
            response["image"] = {
                "file": path.name,
                "media_type": image.media_type,
                "width": image.width,
                "height": image.height,
            }
        atomic_json(results / (identity + ".json"), response)
        claimed.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(prog="yuki-computer")
    commands = parser.add_subparsers(dest="command", required=True)
    help_request = commands.add_parser("help-request")
    help_request.add_argument("--note", required=True)
    commands.add_parser("wait-control")
    commands.add_parser("status").add_argument("--json", action="store_true")
    driver = commands.add_parser("serve")
    driver.add_argument("--cdp", required=True)
    driver.add_argument("--downloads", required=True)
    driver.add_argument("--display")
    args = parser.parse_args()
    root = Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer"))
    if args.command == "serve":
        serve(root, args.cdp, args.downloads, args.display)
        return 0
    if args.command == "status":
        try:
            tabs = json.loads((root / "driver/tabs.json").read_text())["tabs"]
        except FileNotFoundError:
            tabs = []
        value = {"control": control(root), "tabs": tabs}
    else:
        mark("computer")
        value = wait(
            root,
            "help" if args.command == "help-request" else "wait_control",
            getattr(args, "note", None),
        )
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
