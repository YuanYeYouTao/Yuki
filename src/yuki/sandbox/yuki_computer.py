"""Environment-side computer CLI and driver host. Uses only its runtime mount and stdlib.

``serve`` is the long-lived driver started by ``desktop_service.py``. The Manager
puts requests under ``<runtime>/driver/requests``; one request runs at a time,
is claimed by renaming it into ``taken/`` and answered in ``results/``. A request
claimed by a driver that died is never re-run: its taken file is removed at the
next start and the Manager reports the original call as unknown.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.util
import importlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit
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
    """The sandbox X display: screenshots with ImageMagick, input with xdotool, nodes from
    AT-SPI (task book docs/architecture/Computer-Use任务书-2026-11-15.md)."""

    def __init__(self, display: str) -> None:
        self.display = display
        self.environment = {**os.environ, "DISPLAY": display, "LC_ALL": "C.UTF-8"}
        self.keymap: Keymap | None = None
        self.xfixes: Pointer | None = None
        self.atspi: Any = None
        # The latest ``elements`` list of each window: its tag and nodes, in memory only.
        self.nodes: dict[str, tuple[str, list[Any]]] = {}

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
    ) -> tuple[dict[str, Any], cdp.Image | None]:
        from yuki_agent_connector import actions, cdp

        action = args["action"]
        if action not in actions.READS | actions.INPUTS | {"paste"}:
            raise cdp.Failure({"error": "invalid_arguments", "message": f"未知的動作：{action}"})
        try:
            parsed = actions.validate(action, args)
        except ValueError as error:
            raise cdp.Failure({"error": "invalid_arguments", "message": str(error)}) from None
        # An element names its window: without ``window`` the call is that window's (E16).
        window = args.get("window") or (parsed.element[0] if parsed.element else None)
        result: dict[str, Any] = {}
        inputs = 0
        routes: list[str] = []

        def xdotool(*command: str, checked: bool = True) -> None:
            nonlocal inputs
            if checked:
                before_input()
                inputs += 1
                self.run(deadline, "xdotool", *command)
            else:
                # A release (mouseup, keyup) gets its own short limit: it still goes out when
                # the action used up its time, so nothing stays held down (A16).
                self.run(time.monotonic() + RELEASE_SECONDS, "xdotool", *command)

        def accessible(act: Callable[[], bool]) -> bool:
            """An AT-SPI effect: the lease is checked first, it counts once it acted."""
            nonlocal inputs
            before_input()
            done = act()
            if done:
                inputs += 1
            return done

        try:
            left, top, native_width, native_height = self.geometry(deadline, window)
            scale = cdp.image_scale(native_width, native_height, max_dimension, max_pixels)
            width, height = round(native_width * scale), round(native_height * scale)

            def to_root(point: tuple[int, int]) -> tuple[int, int]:
                return left + round(point[0] / scale), top + round(point[1] / scale)

            def to_image(x: float, y: float) -> tuple[float, float]:
                return (x - left) * scale, (y - top) * scale

            def listing(identity: str, scale_mode: int | None, text: Any) -> str:
                """A window's nodes, bounds in that window's own image."""
                x, y, native_w, native_h = self.geometry(deadline, identity)
                factor = cdp.image_scale(native_w, native_h, max_dimension, max_pixels)
                return self.elements(
                    deadline,
                    identity,
                    (x, y, native_w, native_h),
                    lambda gx, gy: ((gx - x) * factor, (gy - y) * factor),
                    round(native_w * factor),
                    round(native_h * factor),
                    scale_mode,
                    text,
                )

            if action == "elements":
                return {
                    "window": window,
                    "elements": listing(str(window), parsed.scale, args.get("text")),
                    "control": visible(control(root)),
                }, None
            crop: tuple[int, int, int, int] | None = None
            if action == "zoom":
                assert parsed.region is not None
                try:
                    x0, y0, x1, y1 = actions.resolve_region(
                        parsed.region, width, height, parsed.scale
                    )
                except actions.OutOfRange as out:
                    raise cdp.Failure(out.result) from None
                except ValueError as error:
                    raise cdp.Failure(
                        {"error": "invalid_arguments", "message": str(error)}
                    ) from None
                crop = (
                    round(x0 / scale),
                    round(y0 / scale),
                    max(1, round((x1 - x0) / scale)),
                    max(1, round((y1 - y0) / scale)),
                )
                result = {
                    "region": [x0, y0, x1, y1],
                    "frame": {"width": width, "height": height},
                    "coordinates": "zoom 圖只用於查看；後續座標仍用整張截圖"
                    f"（寬 {width}、高 {height}）",
                }
            elif action in actions.INPUTS or action == "paste":
                echo: dict[str, Any] = {}
                aim = aim2 = None
                try:
                    if args.get("x") is not None:
                        point = actions.resolve_point(
                            args["x"], args["y"], width, height, parsed.scale
                        )
                        echo["point"], aim = list(point), to_root(point)
                    if action == "drag":
                        point2 = actions.resolve_point(
                            args["x2"], args["y2"], width, height, parsed.scale
                        )
                        echo["point2"], aim2 = list(point2), to_root(point2)
                except actions.OutOfRange as out:
                    raise cdp.Failure(out.result) from None
                node = None
                if parsed.element is not None:
                    node, centre = self.element(
                        parsed.element, args, to_image, width, height, listing
                    )
                    aim = (round(centre[0]), round(centre[1]))
                    echo["element"] = str(args["element"])
                    echo["point"] = [round(value) for value in to_image(*centre)]
                if action != "paste":
                    echo["scale"] = parsed.scale
                result = echo
                gone = sys.modules["yuki_accessibility"].Gone if node is not None else ()
                try:
                    if node is not None and action in ("type", "key"):
                        if not accessible(node.focus):
                            raise cdp.Failure(
                                {
                                    "error": "device_not_supported",
                                    "cause": "no_focused_field",
                                    "message": "這個節點不能取得輸入焦點（AT-SPI），沒有輸入；"
                                    "換一個輸入框節點，或按截圖座標點擊後再輸入。",
                                }
                            )
                        routes.append("accessibility")
                    pressed = (
                        node is not None
                        and action == "click"
                        and not parsed.modifiers
                        and accessible(node.press)
                    )
                    if pressed:
                        routes.append("accessibility")
                    elif node is not None:
                        # Her own computer: the node's window comes to the front first, so
                        # the pointer or the keys reach it and nothing covering it (E17).
                        xdotool("windowactivate", "--sync", str(window))
                    if aim is not None and (action in actions.POINTED or node is not None):
                        # The real hit test at the point, before the input (R12).
                        hit = self.hit(*aim)
                        if hit is not None:
                            result["hit"] = hit
                    if not pressed:
                        self.deliver(parsed, args, aim, aim2, node, xdotool, before_input, routes)
                except gone:
                    raise cdp.Failure(
                        {
                            "error": "element_stale",
                            "message": f"節點 {args['element']} 在操作時已失效；"
                            "請按下面的新列表重新選擇。",
                            "window": str(window),
                            "elements": listing(str(window), None, None),
                        }
                    ) from None
                finally:
                    result["route"] = list(dict.fromkeys(routes))
                result["submitted"] = True
            # The image always shows the state after the action, at current geometry.
            _, _, native_width, native_height = self.geometry(deadline, window)
            scale = cdp.image_scale(native_width, native_height, max_dimension, max_pixels)
            size = (round(native_width * scale), round(native_height * scale))
            source = (crop[2], crop[3]) if crop is not None else (native_width, native_height)
            if crop is not None:
                zoom = cdp.image_scale(*source, max_dimension, max_pixels)
                size = (round(source[0] * zoom), round(source[1] * zoom))
            pointer = self.cursor() if window is None and crop is None else None
            image = self.capture(deadline, window, crop, size, source, pointer)
            if pointer is not None:
                x, y = (round((pointer[0] - left) * scale), round((pointer[1] - top) * scale))
                if 0 <= x < size[0] and 0 <= y < size[1]:
                    result["cursor"] = [x, y]
            if action in ("screenshot", "resume"):
                result["windows"] = self.windows(deadline)
        except subprocess.TimeoutExpired:
            raise cdp.Failure(
                {
                    "error": "timeout",
                    "message": "桌面操作在等待時間內沒有完成。",
                    "status": "unknown" if inputs else "failed",
                    **({"route": result["route"]} if "route" in result else {}),
                    **{key: result[key] for key in ("point", "point2") if key in result},
                }
            ) from None
        except subprocess.CalledProcessError as error:
            raise cdp.Failure(
                {
                    "error": "input_failed",
                    "message": "桌面指令失敗："
                    + (error.stderr or b"").decode(errors="replace").strip(),
                    # Nothing was input when the failing command came first (V10).
                    "status": "unknown" if inputs else "failed",
                    **{key: result[key] for key in ("point", "point2", "route") if key in result},
                }
            ) from None
        except cdp.Failure as failure:
            # Facts that refused before any input are failed, never unknown (E19).
            failure.result["status"] = (
                "unknown" if inputs else failure.result.get("status", "failed")
            )
            for key in ("point", "point2", "route"):
                if key in result:
                    failure.result.setdefault(key, result[key])
            raise
        return (
            {
                "width": size[0],
                "height": size[1],
                "coordinates": actions.COORDINATES,
                "display": self.display,
                **({"window": window} if window else {}),
                "control": visible(control(root)),
                **result,
            },
            cdp.Image(image, "image/jpeg", size[0], size[1]),
        )

    def deliver(
        self,
        parsed: Any,
        args: dict[str, Any],
        aim: tuple[int, int] | None,
        aim2: tuple[int, int] | None,
        node: Any,
        xdotool: Callable[..., None],
        before_input: Callable[[], None],
        routes: list[str],
    ) -> None:
        """Post one input with xdotool, recording each route taken."""
        from yuki_agent_connector import actions

        action = parsed.action
        move = ["mousemove", str(aim[0]), str(aim[1])] if aim is not None else []
        if node is None and aim is not None and action in ("type", "key"):
            # The field at x,y becomes the focus by a click there, then the keys (A19).
            xdotool(*move, "click", "1")
        held = [MODIFIER_KEYS[name] for name in parsed.modifiers]
        if action in actions.CLICKS:
            button = {"left": "1", "middle": "2", "right": "3"}[parsed.button]
            repeat = ["--repeat", str(parsed.count)] if parsed.count > 1 else []
            # No --sync: it never returns when the pointer is already there, and the X
            # server applies the move before the following click.
            self.holding(xdotool, held, [*move, "click", *repeat, button])
        elif action == "move":
            xdotool(*move)
        elif action == "drag":
            assert aim is not None and aim2 is not None

            def drag() -> None:
                xdotool(*move, "mousedown", "1")
                try:
                    # Applications track a drag from intermediate positions (A12).
                    for step in range(1, DRAG_STEPS + 1):
                        x = aim[0] + (aim2[0] - aim[0]) * step / DRAG_STEPS
                        y = aim[1] + (aim2[1] - aim[1]) * step / DRAG_STEPS
                        xdotool("mousemove", str(round(x)), str(round(y)), "sleep", "0.01")
                finally:
                    xdotool("mouseup", "1", checked=False)

            self.holding(xdotool, held, drag)
        elif action == "scroll":
            # X buttons 4/5 scroll up/down and 6/7 left/right, one notch each.
            steps = [*move]
            for amount, negative, positive in ((parsed.dy, "4", "5"), (parsed.dx, "6", "7")):
                if amount:
                    steps += [
                        "click",
                        "--repeat",
                        str(abs(amount)),
                        positive if amount > 0 else negative,
                    ]
            self.holding(xdotool, held, steps)
        elif action == "key":
            assert parsed.keys is not None
            combo = "+".join(
                [MODIFIER_KEYS[name] for name in parsed.keys[0]] + [keysym_name(parsed.keys[1])]
            )
            # Each repeat checks the lease first: a takeover ends the rest (A9).
            for _ in range(parsed.repeat):
                xdotool("key", "--clearmodifiers", "--", combo)
        elif action == "type":
            if self.keymap is None:
                self.keymap = Keymap(self.display)
            for run in self.keymap.runs(args.get("text") or ""):
                xdotool("type", "--clearmodifiers", "--", run)
        elif action == "paste":
            self.paste(xdotool, before_input, args["text"], routes)
            return
        routes.append("global_input")

    def holding(
        self, xdotool: Callable[..., None], held: list[str], run: list[str] | Callable[[], None]
    ) -> None:
        """Hold ``held`` modifiers only while ``run`` posts; released even when it fails."""
        if held:
            xdotool("keydown", *held)
        try:
            if callable(run):
                run()
            else:
                xdotool(*run)
        finally:
            if held:
                xdotool("keyup", *reversed(held), checked=False)

    def paste(
        self,
        xdotool: Callable[..., None],
        before_input: Callable[[], None],
        text: str,
        routes: list[str],
    ) -> None:
        """Her own clipboard: both selections hold ``text``, then shift+Insert (task book §6.12)."""
        from yuki_agent_connector import cdp

        if shutil.which("xclip", path=self.environment.get("PATH")) is None:
            raise cdp.Failure(
                {
                    "error": "device_not_supported",
                    "cause": "no_clipboard",
                    "message": "這台電腦還沒有剪貼簿工具（xclip），不能 paste；"
                    "改用 type，或等 Manager 下次啟動時補裝。",
                }
            )
        # The lease is checked before her selections change; they are not input, so an
        # xclip failure is a plain failure with nothing typed (V10).
        before_input()
        for selection in ("clipboard", "primary"):
            # xclip stays behind to serve the selection; it must not hold our pipes.
            subprocess.run(
                ["xclip", "-selection", selection, "-in"],
                input=text.encode(),
                env=self.environment,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        routes.append("clipboard")
        xdotool("key", "--clearmodifiers", "shift+Insert")
        routes.append("global_input")

    def cursor(self) -> tuple[int, int, int, int, bytes] | None:
        """Pointer ``(x, y, hot_x, hot_y, PAM image)`` from XFixes; None when unreadable."""
        try:
            if self.xfixes is None:
                self.xfixes = Pointer(self.display)
            return self.xfixes.read()
        except OSError:
            return None

    def capture(
        self,
        deadline: float,
        window: str | None,
        crop: tuple[int, int, int, int] | None,
        size: tuple[int, int],
        source: tuple[int, int],
        pointer: tuple[int, int, int, int, bytes] | None,
    ) -> bytes:
        steps: list[str] = []
        if crop is not None:
            steps += ["-crop", f"{crop[2]}x{crop[3]}+{crop[0]}+{crop[1]}", "+repage"]
        with tempfile.TemporaryDirectory(prefix="yuki-cursor-") as temporary:
            if pointer is not None:
                # The real cursor image at its hotspot, composited before scaling (P1).
                cursor = Path(temporary) / "cursor.pam"
                cursor.write_bytes(pointer[4])
                steps += [
                    str(cursor),
                    "-geometry",
                    f"+{pointer[0] - pointer[2]}+{pointer[1] - pointer[3]}",
                    "-composite",
                ]
            if size != source:
                steps += ["-resize", f"{size[0]}x{size[1]}!"]
            native = self.run(
                deadline, "import", "-display", self.display, "-window", window or "root", "png:-"
            )
            return subprocess.run(
                ["convert", "png:-", *steps, "-quality", "80", "jpeg:-"],
                input=native,
                env=self.environment,
                check=True,
                capture_output=True,
                timeout=max(0.0, deadline - time.monotonic()),
            ).stdout

    # Accessibility (AT-SPI) -----------------------------------------------------------

    def accessibility(self) -> Any:
        from yuki_agent_connector import cdp

        if self.atspi is None:
            try:
                module = importlib.import_module("yuki_accessibility")
                self.atspi = module.Accessibility()
            except Exception as error:  # the library, the bus or the import
                raise cdp.Failure(no_accessibility(str(error))) from None
        return self.atspi

    def pid(self, deadline: float, window: str) -> int:
        from yuki_agent_connector import cdp

        output = self.run(deadline, "xprop", "-id", window, "_NET_WM_PID").decode()
        found = re.search(r"= (\d+)", output)
        if found is None:
            raise cdp.Failure(no_accessibility("the window names no process"))
        return int(found.group(1))

    def elements(
        self,
        deadline: float,
        window: str,
        rect: tuple[int, int, int, int],
        to_image: Callable[[float, float], tuple[float, float]],
        width: int,
        height: int,
        scale: int | None,
        text: Any,
    ) -> str:
        from yuki_agent_connector import actions, cdp

        atspi = self.accessibility()
        module = sys.modules["yuki_accessibility"]
        try:
            nodes = atspi.nodes(self.pid(deadline, window), rect)
        except module.Unavailable as error:
            raise cdp.Failure(no_accessibility(str(error))) from None
        entries = []
        for node in nodes:
            try:
                bounds = node.bounds()
            except module.Gone:
                bounds = None
            entries.append(
                actions.Entry(
                    node.depth,
                    node.role,
                    node.name,
                    node.visible,
                    node.container,
                    actions.image_box(bounds, to_image, width, height),
                )
            )
        tag = secrets.token_hex(2)
        lines, kept = actions.render_elements(
            entries, window, tag, str(text) if text else None, scale, width, height
        )
        keep = {id(nodes[index]) for index in kept}
        for node in nodes:
            if id(node) not in keep:
                node.release()
        for node in self.nodes.pop(window, ("", []))[1]:
            node.release()
        self.nodes[window] = (tag, [nodes[index] for index in kept])
        return lines

    def element(
        self,
        element: tuple[str, str, int],
        args: dict[str, Any],
        to_image: Callable[[float, float], tuple[float, float]],
        width: int,
        height: int,
        listing: Callable[[str, int | None, Any], str],
    ) -> tuple[Any, tuple[float, float]]:
        """The listed node and its centre in root pixels now, or a failure before any input."""
        from yuki_agent_connector import cdp

        window, tag, number = element
        listed = self.nodes.get(window)
        node = None
        if listed is not None and listed[0] == tag and 0 < number <= len(listed[1]):
            node = listed[1][number - 1]
        try:
            bounds = node.bounds() if node is not None else None
        except sys.modules["yuki_accessibility"].Gone:
            node = None
        if node is None:
            fresh = listing(window, None, None)
            raise cdp.Failure(
                {
                    "error": "element_stale",
                    "message": f"節點 {args['element']} 已失效（視窗重新列出過節點、節點已銷毀"
                    "或驅動重啟過）；沒有執行。請按下面的新列表重新選擇。",
                    "window": window,
                    "elements": fresh,
                }
            )
        if bounds is not None and bounds[2] > 0 and bounds[3] > 0:
            centre = (bounds[0] + bounds[2] / 2, bounds[1] + bounds[3] / 2)
            x, y = to_image(*centre)
            if 0 <= x < width and 0 <= y < height:
                return node, centre
        box = None
        if bounds is not None:
            x0, y0 = to_image(bounds[0], bounds[1])
            x1, y1 = to_image(bounds[0] + bounds[2], bounds[1] + bounds[3])
            box = [round(x0), round(y0), round(x1), round(y1)]
        raise cdp.Failure(
            {
                "error": "element_not_visible",
                "message": "這個節點現在不在畫面內（已移出、被收起或尺寸為零），沒有執行，"
                "也不會改按座標點擊；先 screenshot 或 elements 重新看。",
                "bounds": box,
            }
        )

    def hit(self, x: int, y: int) -> dict[str, str] | None:
        """The node under a root point before any input; None when unknown (never a failure).

        It has its own short limit, apart from the action's: describing the point never uses
        up the time the input needs (R10).
        """
        deadline = time.monotonic() + HIT_SECONDS
        try:
            atspi = self.accessibility()
            window = self.window_at(deadline, x, y)
            if window is None:
                return None
            identity, rect = window
            return cast(
                dict[str, str], atspi.at(self.pid(deadline, identity), rect, x, y, deadline)
            )
        except Exception:  # no AT-SPI, no process, a hung app: the input still runs (R6, R7)
            return None

    def window_at(
        self, deadline: float, x: int, y: int
    ) -> tuple[str, tuple[int, int, int, int]] | None:
        """The topmost managed window containing the root point."""
        listed = self.run(deadline, "xprop", "-root", "_NET_CLIENT_LIST_STACKING").decode()
        for identity in reversed(re.findall(r"0x[0-9a-f]+", listed.partition("#")[2])):
            try:
                rect = self.geometry(deadline, identity)
            except Exception:
                continue
            if rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]:
                return identity, rect
        return None


MODIFIER_KEYS = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "cmd": "super"}
KEYSYMS = {
    "return": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    "insert": "Insert",
    "`": "grave",
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
    **MODIFIER_KEYS,
}
DRAG_STEPS = 12
# A release has its own limit; a hit test has its own limit (A16, R10).
RELEASE_SECONDS = 2.0
HIT_SECONDS = 1.0


def keysym_name(key: str) -> str:
    """A key of the shared grammar (``actions.parse_keys``) as the xdotool keysym name."""
    if key in KEYSYMS:
        return KEYSYMS[key]
    return key.upper() if key.startswith("f") and key[1:].isdigit() else key


def no_accessibility(detail: str) -> dict[str, Any]:
    return {
        "error": "device_not_supported",
        "cause": "no_accessibility",
        "message": "這個視窗沒有可讀的無障礙資訊（AT-SPI 不可用或應用沒有註冊），"
        f"不能列出或按節點操作；請按截圖座標操作。（{detail}）",
    }


class Pointer:
    """The X cursor through XFixes: its position, hotspot and ARGB image (P0 item 4)."""

    class Image(ctypes.Structure):
        _fields_ = [
            ("x", ctypes.c_short),
            ("y", ctypes.c_short),
            ("width", ctypes.c_ushort),
            ("height", ctypes.c_ushort),
            ("xhot", ctypes.c_ushort),
            ("yhot", ctypes.c_ushort),
            ("cursor_serial", ctypes.c_ulong),
            ("pixels", ctypes.POINTER(ctypes.c_ulong)),
            ("atom", ctypes.c_ulong),
            ("name", ctypes.c_char_p),
        ]

    def __init__(self, display: str) -> None:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
        name = ctypes.util.find_library("Xfixes")
        if name is None:
            raise OSError("libXfixes is missing")
        xfixes = ctypes.CDLL(name)
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XFree.argtypes = [ctypes.c_void_p]
        xfixes.XFixesGetCursorImage.restype = ctypes.POINTER(Pointer.Image)
        xfixes.XFixesGetCursorImage.argtypes = [ctypes.c_void_p]
        self.x11, self.xfixes = x11, xfixes
        self.connection = x11.XOpenDisplay(display.encode())
        if not self.connection:
            raise OSError(f"cannot open X display {display}")

    def read(self) -> tuple[int, int, int, int, bytes]:
        image = self.xfixes.XFixesGetCursorImage(self.connection)
        if not image:
            raise OSError("XFixesGetCursorImage returned nothing")
        try:
            cursor = image.contents
            pixels = bytearray()
            for index in range(cursor.width * cursor.height):
                argb = cursor.pixels[index] & 0xFFFFFFFF
                pixels += bytes(((argb >> 16) & 0xFF, (argb >> 8) & 0xFF, argb & 0xFF, argb >> 24))
            header = (
                f"P7\nWIDTH {cursor.width}\nHEIGHT {cursor.height}\nDEPTH 4\nMAXVAL 255\n"
                "TUPLTYPE RGB_ALPHA\nENDHDR\n"
            ).encode()
            return cursor.x, cursor.y, cursor.xhot, cursor.yhot, header + bytes(pixels)
        finally:
            self.x11.XFree(image)


def _property(output: str, name: str) -> str:
    for line in output.splitlines():
        if line.startswith(name + "("):
            return line.partition("=")[2]
    return ""


READS = {
    "computer": ("screenshot", "resume", "zoom", "elements"),
    "browser": ("snapshot", "screenshot"),
}


class BrowserHost:
    """The driver's optional CDP connection and the browser it launches on open."""

    def __init__(self, endpoint: str, downloads: str, display: str | None) -> None:
        self.endpoint, self.downloads, self.display = endpoint, downloads, display
        self.browser: cdp.Browser | None = None
        self.process: subprocess.Popen[bytes] | None = None

    @staticmethod
    def executable() -> str | None:
        return next(
            (
                path
                for name in (
                    "chromium",
                    "google-chrome",
                    "google-chrome-stable",
                    "chromium-browser",
                )
                if (path := shutil.which(name))
            ),
            None,
        )

    def get(self, args: dict[str, Any], before_input: Callable[[], None]) -> cdp.Browser:
        from yuki_agent_connector import cdp

        self.pump(0)
        if self.browser is not None:
            return self.browser
        try:
            self.browser = cdp.Browser(self.endpoint, downloads=self.downloads, timeout=10)
            return self.browser
        except OSError:
            pass
        if args["action"] != "open" or args.get("tab"):
            raise cdp.Failure(
                {
                    "error": "browser_closed",
                    "message": "浏览器未运行；用 browser 的 open 动作打开。",
                }
            )
        executable = self.executable()
        if executable is None:
            raise cdp.Failure(
                {
                    "error": "browser_unavailable",
                    "message": "没有安装 Chrome/Chromium；可通过终端安装。",
                }
            )
        before_input()
        profile = Path.home() / ".local/share/yuki-computer/chromium"
        self.process = subprocess.Popen(
            [
                executable,
                f"--user-data-dir={profile}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={urlsplit(self.endpoint).port}",
                "--restore-last-session",
                "--no-first-run",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                *(["--headless"] if self.display is None else []),
                "about:blank",
            ]
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                self.browser = cdp.Browser(self.endpoint, downloads=self.downloads, timeout=10)
                return self.browser
            except OSError as error:
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    raise cdp.Failure(
                        {"error": "browser_start_failed", "message": str(error)}
                    ) from error
                time.sleep(0.1)

    def pump(self, timeout: float = 0.05) -> None:
        if self.browser is not None:
            try:
                self.browser.pump(timeout)
            except (OSError, ValueError):
                self.disconnect()
        else:
            time.sleep(timeout)
        if self.process is not None:
            self.process.poll()  # Reap a browser the person has closed.

    def disconnect(self) -> None:
        if self.browser is not None:
            with contextlib.suppress(OSError):
                self.browser.close()
            self.browser = None

    def close(self) -> None:
        self.disconnect()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def handle(
    request: dict[str, Any], root: Path, browser: BrowserHost, screen: Screen | None
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
            return browser.get(args, before_input).execute(
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
    except Exception as error:
        # One request's unforeseen failure answers that request and is logged;
        # the driver keeps serving instead of taking the whole desktop down.
        traceback.print_exc()
        return {
            "error": "driver_failed",
            "message": f"驅動處理這個請求時出錯：{type(error).__name__}: {error}",
            # Not knowing how many inputs ran, an input reports an unknown effect.
            **({} if args["action"] in READS[request["tool"]] else {"status": "unknown"}),
        }, None


def unchecked() -> None:
    """Read actions never consult the control lease."""


def serve(root: Path, endpoint: str, downloads: str, display: str | None) -> None:
    driver = root / "driver"
    requests, taken, results = driver / "requests", driver / "taken", driver / "results"
    for directory in (requests, taken, results):
        directory.mkdir(parents=True, exist_ok=True)
    for leftover in taken.glob("*.json"):
        leftover.unlink()
    browser = BrowserHost(endpoint, downloads, display)
    screen = Screen(display) if display else None
    written: dict[str, Any] | None = None

    def publish() -> None:
        nonlocal written
        tabs = (
            [
                {"target_id": tab["id"], "title": tab["title"], "url": tab["url"]}
                | ({"work_id": tab["work_id"]} if "work_id" in tab else {})
                for tab in browser.browser.tabs()
            ]
            if browser.browser is not None
            else []
        )
        value = {
            "run_id": os.environ.get("YUKI_JOB_ID"),
            "browser": "running"
            if browser.browser is not None
            else ("stopped" if browser.executable() else "unavailable"),
            "tabs": tabs,
        }
        if value != written:
            atomic_json(driver / "tabs.json", value)
            written = value

    stopped = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopped
        stopped = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        while not stopped:
            publish()
            pending = sorted(requests.glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
            if not pending:
                browser.pump()
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
            publish()
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
    finally:
        browser.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
