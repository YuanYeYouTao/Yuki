"""``computer`` / ``browser`` on the owner's computer, yielding to the owner (task book §5.5).

The owner keeps using the computer while Yuki works. Yuki's global input (the whole screen,
the real cursor) first compares two facts: when the last input of any source happened, and
when Yuki last looked or acted (its previous screenshot or ``computer`` input). Any input
after that is not Yuki's (the owner, Screen Sharing, a remote-control tool), so Yuki yields:
it waits until the owner has been quiet for a moment, then refuses this input without
running it (``owner_active``) because the screen may have changed; a fresh screenshot moves
the baseline and the retried input runs. The owner's input never stops Yuki.

With ``window`` (phase 3b) the picture is that window alone and input goes to it in the
background, without the cursor or the frontmost app. It yields only on a real collision:
the window's app is the owner's frontmost app, or the window under the owner's pointer is
its, and the owner used the computer since Yuki last looked. Input that has no background
route is refused; it never falls back to global input.

The stop is explicit: Desktop 「停止」 (stdin) sets the holder to ``owner``, and every
``computer`` and ``browser`` input is refused with ``control_held`` while screenshots and
snapshots still read. Only Desktop 「繼續」 or ``computer(action=resume)`` clears it. The
flag lives in this process only: Desktop re-sends it after restarting the child.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import actions
from .actions import COORDINATES, INPUTS, POINTED, READS, OutOfRange, Parsed
from .cdp import image_scale
from .chrome import BrowserHost, capture_name
from .config import private_path
from .screen import (
    Area,
    Backend,
    Display,
    Node,
    Refused,
    Stale,
    Surface,
    Target,
    frame,
    to_global,
    to_image,
)

# Published in the computer facts; the server refuses calls that need a missing one
# before they reach this process (task book §6.10).
FEATURES = frozenset(
    {
        "scale",
        "zoom",
        "elements",
        "modifiers",
        "triple_click",
        "middle_click",
        "key_repeat",
        "route",
        "cursor",
    }
)
# Typed text is posted in runs so that a Desktop stop arriving meanwhile ends it.
TYPE_RUN = 32
# A hit test only describes the point; it never holds up the input (R10).
HIT_SECONDS = 0.5
# Reads the platform's permission preflight so Desktop and the server see a grant soon.
PERMISSION_SECONDS = 5.0
# Yielding to the owner: their input has paused for this long, or Yuki gives up waiting.
QUIET_SECONDS = 1.5
YIELD_SECONDS = 10.0


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Control:
    """The owner's stop flag, in Connector memory only (no config, journal, CLI or server row)."""

    def __init__(self, report: Callable[[dict[str, Any]], None]) -> None:
        self.report = report
        self.lock = threading.Lock()
        self.holder, self.since, self.reason = "none", now(), "start"

    def line(self) -> dict[str, Any]:
        return {
            "type": "control",
            "holder": self.holder,
            "since": self.since,
            "reason": self.reason,
        }

    def set(self, holder: str, reason: str) -> bool:
        with self.lock:
            if holder == self.holder:
                return False
            self.holder, self.since, self.reason = holder, now(), reason
            line = self.line()
        self.report(line)
        return True

    def visible(self) -> dict[str, str]:
        return {"holder": self.holder, "since": self.since}

    def held(self) -> dict[str, Any]:
        return {
            "error": "control_held",
            "message": "本人已叫停 Yuki 操作这台电脑，输入没有执行。截图和读取页面仍可用；"
            "本人明确让你继续时再调用 computer(action=resume)。",
            "control": self.visible(),
        }


def bounded(call: Callable[[], Any], seconds: float) -> Any:
    """``call()`` in its own thread, or None when it fails or has not answered in time.

    The platform's accessibility call may block on an app that does not answer; the thread
    is left to finish on its own and its answer is dropped.
    """
    answer: list[Any] = []

    def run() -> None:
        try:
            answer.append(call())
        except Exception:  # a hit test that fails only leaves the field out (R7)
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return answer[0] if answer else None


def failure(error: str, message: str, **facts: Any) -> dict[str, Any]:
    return {"status": "failed", "error": error, "message": message, **facts}


class Desk:
    def __init__(
        self,
        directory: Path,
        screen: Backend | None,
        report: Callable[[dict[str, Any]], None],
        facts_changed: Callable[[], None],
    ) -> None:
        self.screen, self.report, self.facts_changed = screen, report, facts_changed
        self.control = Control(report)
        self.captures = directory / "captures"
        # A capture is released (deleted) once the server has read it; anything left
        # belongs to a call the previous process never answered.
        shutil.rmtree(self.captures, ignore_errors=True)
        self.captures.mkdir(parents=True)
        private_path(self.captures, directory=True)
        self.browser = BrowserHost(
            directory, self.captures, lambda: self.control.holder, self.control.held, report
        )
        self.lock = threading.Lock()
        # The latest possible moment of the last input as read right after Yuki's previous
        # screenshot or computer input (or resume): the yield baseline, in the backend's clock.
        self.observed: float | None = None
        self.permissions: tuple[bool, bool] | None = None
        self.permission_lock = threading.Lock()
        self.dirty = False
        # The latest ``elements`` list of each window: its tag and nodes, in memory only.
        self.nodes: dict[str, tuple[str, list[Node]]] = {}

    # Facts and Desktop lines

    def check_permissions(self) -> tuple[bool, bool]:
        assert self.screen is not None
        with self.permission_lock:
            current = self.screen.permissions()
            if current != self.permissions:
                announced = self.permissions is not None
                self.permissions = current
                self.report({"type": "permissions", "screen": current[0], "input": current[1]})
                self.dirty = announced
        return current

    def facts(self) -> dict[str, Any] | None:
        if self.screen is None:
            return None
        screen, granted = self.check_permissions()
        return {
            "screen": screen,
            "input": granted,
            "displays": [display.fact() for display in self.screen.displays()],
            # Phase 3b: ``window`` is captured alone and its input delivered in the background.
            "windows": True,
            "features": sorted(FEATURES),
        }

    def announce(self) -> None:
        """Startup lines for the owning Desktop: the stop flag, then the permissions."""
        self.report(self.control.line())
        if self.screen is not None:
            self.check_permissions()

    def desktop(self, holder: str) -> None:
        """Desktop 「停止」 / 「繼續」 from stdin."""
        if holder not in {"owner", "none"}:
            return
        if self.control.set(holder, "desktop") and holder == "none":
            # The click on 「繼續」 itself is the owner's input; Yuki need not yield to it.
            self.observed = self._baseline()

    def _baseline(self) -> float | None:
        return self.screen.last_input()[1] if self.screen is not None else None

    def _publish(self) -> None:
        if self.dirty:
            self.dirty = False
            self.facts_changed()

    async def watch(self) -> None:
        if self.screen is None:
            return
        while True:
            await asyncio.sleep(PERMISSION_SECONDS)
            await asyncio.to_thread(self.check_permissions)
            self._publish()

    async def close(self) -> None:
        await self.browser.close()

    # Commands

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        if command["resource"] == "browser":
            return await self.browser.execute(command)
        result = await asyncio.to_thread(self._computer, command)
        self._publish()
        return result

    def _computer(self, command: dict[str, Any]) -> dict[str, Any]:
        args = command["arguments"]
        action = command["action"]
        if self.screen is None:
            return failure(
                "device_not_supported",
                "这台电脑的连接器不能操作屏幕（只支持 macOS 14 及以上与 Windows）；"
                "网页可用 browser。",
                cause="platform",
            )
        if action not in READS | INPUTS:
            return failure("invalid_arguments", f"未知的动作：{action}")
        try:
            parsed = actions.validate(action, args)
        except ValueError as error:
            return failure("invalid_arguments", str(error))
        with self.lock:
            return self._locked(command["id"], args, parsed)

    def _locked(self, identity: str, args: dict[str, Any], parsed: Parsed) -> dict[str, Any]:
        assert self.screen is not None
        action = parsed.action
        screen_granted, input_granted = self.check_permissions()
        if action == "resume":
            # The owner said so in chat; the server checked the device belongs to them.
            self.control.set("none", "resume")
            self.observed = self._baseline()
        unavailable = self.screen.unavailable()
        if unavailable is not None:
            return {"status": "failed", **unavailable}
        if not screen_granted:
            return failure(
                "screen_permission_required",
                "这台 Mac 没有给 Yuki 屏幕录制权限；请本人在“系统设置 › 隐私与安全性 › "
                "录屏与系统录音”中允许 Yuki。",
            )
        target: Target | None = None
        display: Display | None = None
        area: Area
        # An element names its window: without ``window`` the call is that window's, with
        # its picture and its own routes, never the owner's foreground (E16).
        window = args.get("window") or (parsed.element[0] if parsed.element else None)
        if window is not None:
            if not input_granted:
                # A window's state (minimized) and every background route need Accessibility.
                return failure(
                    "input_permission_required",
                    "按窗口截图和操作需要辅助功能权限；请本人在“系统设置 › 隐私与安全性 › "
                    "辅助功能”中允许 Yuki，或省略 window 截整个屏幕。",
                )
            try:
                target = self.screen.window(str(window))
            except Refused as refused:
                return refused.result()
            area = target.window
        else:
            display = self._display(args.get("display"))
            if display is None:
                return failure(
                    "display_unavailable",
                    f"找不到显示器 {args.get('display')}；可用的显示器见 get_environment。",
                )
            area = display
        limits = args["max_dimension"], args["max_pixels"]
        width, height = frame(area, *limits)
        if action == "elements":
            assert target is not None
            if not target.actions:
                return self._off_space()
            try:
                return {
                    "status": "succeeded",
                    "window": target.window.id,
                    "elements": self._list(target, limits, parsed.scale, args.get("text")),
                    "control": self.control.visible(),
                }
            except Refused as refused:
                return refused.result()
        result: dict[str, Any] = {}
        region: tuple[float, float, float, float] | None = None
        if action == "zoom":
            assert parsed.region is not None
            try:
                x0, y0, x1, y1 = actions.resolve_region(parsed.region, width, height, parsed.scale)
            except OutOfRange as out:
                return out.result
            except ValueError as error:
                return failure("invalid_arguments", str(error))
            left, top = to_global(area, *limits, x0, y0)
            right, bottom = to_global(area, *limits, x1, y1)
            region = (left - area.x, top - area.y, right - left, bottom - top)
            pixels = image_scale(area.pixel_width, area.pixel_height, *limits)
            native = (round((x1 - x0) / pixels), round((y1 - y0) / pixels))
            zoom = image_scale(*native, *limits)
            result = {
                "region": [x0, y0, x1, y1],
                "frame": {"width": width, "height": height},
                "coordinates": "zoom 图只用于查看；后续坐标仍用整张截图"
                f"（宽 {width}、高 {height}）",
            }
            width, height = round(native[0] * zoom), round(native[1] * zoom)
        elif action in INPUTS:
            echo: dict[str, Any] = {}
            denied = self._refuse_input(parsed, target)
            if denied is not None:
                return denied
            if (
                self.observed is not None
                # Even the earliest possible moment of the last input is after the baseline.
                and self.screen.last_input()[0] > self.observed
                # Background input collides with the owner only in the owner's own app.
                and (target is None or target.collides())
            ):
                return self._yield()
            aim: tuple[float, float] | None = None
            aim2: tuple[float, float] | None = None
            node: Node | None = None
            try:
                if args.get("x") is not None:
                    point = actions.resolve_point(args["x"], args["y"], width, height, parsed.scale)
                    echo["point"] = list(point)
                    aim = to_global(area, *limits, *point)
                if action == "drag":
                    point2 = actions.resolve_point(
                        args["x2"], args["y2"], width, height, parsed.scale
                    )
                    echo["point2"] = list(point2)
                    aim2 = to_global(area, *limits, *point2)
            except OutOfRange as out:
                return out.result
            if parsed.element is not None:
                found = self._node(parsed.element, limits, parsed.scale)
                if isinstance(found, dict):
                    return found
                node = found
                try:
                    bounds = node.bounds()
                except Stale:
                    return self._stale(parsed.element, limits, parsed.scale)
                visible = self._visible(area, limits, bounds, width, height)
                if isinstance(visible, dict):
                    return visible
                aim = visible
                echo["element"] = str(args["element"])
                echo["point"] = [round(value) for value in to_image(area, *limits, *aim)]
            echo["scale"] = parsed.scale
            if target.elevated() if target is not None else self.screen.elevated(aim):
                return failure(
                    "target_elevated",
                    "目标窗口以管理员权限运行，系统不允许普通程序向它输入；输入没有执行。",
                )
            if aim is not None and (action in POINTED or node is not None):
                # The real hit test at the point, before any input, with its own short limit:
                # whatever is on top there, never the node's own description (R10, R12).
                surface = target if target is not None else self.screen
                at = aim
                hit = bounded(lambda: surface.hit(*at), HIT_SECONDS)
                if hit is not None:
                    echo["hit"] = hit
            submitted = self._input(parsed, args, aim, aim2, target, node)
            if submitted.get("error") == "element_stale" and parsed.element is not None:
                # The node went away after its bounds were read (E18).
                return {
                    **echo,
                    **self._stale(parsed.element, limits, parsed.scale),
                    "status": submitted["status"],
                }
            if "error" in submitted:
                return {**submitted, **echo}
            result = {"submitted": True, **echo, **submitted}
        path = self.captures / f"{capture_name(identity)}.jpg"
        try:
            if target is not None:
                target.capture(width, height, path, region)
            else:
                assert display is not None
                self.screen.capture(display, width, height, path, region)
                pointer = self.screen.pointer() if region is None else None
                if pointer is not None:
                    x, y = (round(value) for value in to_image(display, *limits, *pointer))
                    if 0 <= x < width and 0 <= y < height:
                        result["cursor"] = [x, y]
            if action in READS and action != "zoom":
                result["windows"] = self.screen.windows()
        except OSError as error:
            if action in INPUTS:
                # The input was posted; only its picture is missing.
                result["capture_error"] = f"{type(error).__name__}: {error}"
            else:
                return failure("capture_failed", f"截图失败：{error}")
        else:
            result["image"] = {
                "path": str(path),
                "media_type": "image/jpeg",
                "width": width,
                "height": height,
            }
        if action != "zoom":
            # After the picture: Yuki's events have been processed by now, and the model will
            # act on what it shows, so only input after this is someone else's.
            self.observed = self._baseline()
        return {
            "status": "succeeded",
            "width": width,
            "height": height,
            "coordinates": COORDINATES,
            **({"window": target.window.id} if target is not None else {}),
            **({"display": display.id} if display is not None else {}),
            "control": self.control.visible(),
            **result,
        }

    def _refuse_input(self, parsed: Parsed, target: Target | None) -> dict[str, Any] | None:
        """Facts that refuse an input before anything is posted: grants, the stop, routes."""
        assert self.screen is not None
        if not self.check_permissions()[1]:
            return failure(
                "input_permission_required",
                "这台 Mac 没有给 Yuki 辅助功能权限，输入会被系统丢弃；请本人在“系统设置 › "
                "隐私与安全性 › 辅助功能”中允许 Yuki。",
            )
        if self.control.holder == "owner":
            return {"status": "failed", **self.control.held()}
        if target is None:
            return None
        if not target.actions:
            return self._off_space()
        needed = {parsed.action, *(("modifiers",) if parsed.modifiers else ())}
        if not needed <= target.actions:
            return failure(
                "device_not_supported",
                f"这台电脑的平台没有把 {parsed.action}"
                + ("（按住修饰键）" if parsed.modifiers else "")
                + " 投递给后台窗口的途径，输入没有执行；需要时省略 window，在前台操作整个屏幕。",
                cause="no_background_route",
            )
        return None

    @staticmethod
    def _off_space() -> dict[str, Any]:
        return failure(
            "device_not_supported",
            "这个窗口不在本人当前的桌面空间，只能看不能操作，输入没有执行；"
            "请本人切到它所在的桌面空间，或省略 window 在前台操作。",
            cause="off_space",
        )

    # Accessibility nodes (task book §6.7)

    def _list(
        self, target: Target, limits: tuple[int, int], scale: int | None, text: Any
    ) -> str:
        """Register this window's nodes under a fresh tag and render them like a snapshot."""
        area = target.window
        width, height = frame(area, *limits)
        nodes = list(target.elements())
        entries = []
        for node in nodes:
            try:
                bounds = node.bounds()
            except Stale:
                bounds = None
            box = actions.image_box(
                bounds, lambda x, y: to_image(area, *limits, x, y), width, height
            )
            entries.append(
                actions.Entry(node.depth, node.role, node.name, node.visible, node.container, box)
            )
        tag = secrets.token_hex(2)
        lines, kept = actions.render_elements(
            entries, area.id, tag, str(text) if text else None, scale, width, height
        )
        self.nodes[area.id] = (tag, [nodes[index] for index in kept])
        return lines

    def _node(
        self, element: tuple[str, str, int], limits: tuple[int, int], scale: int | None
    ) -> Node | dict[str, Any]:
        window, tag, number = element
        listed = self.nodes.get(window)
        if listed is None or listed[0] != tag or not 0 < number <= len(listed[1]):
            return self._stale(element, limits, scale)
        return listed[1][number - 1]

    def _stale(
        self, element: tuple[str, str, int], limits: tuple[int, int], scale: int | None
    ) -> dict[str, Any]:
        """The id no longer names a node: answer with the window's fresh list, run nothing."""
        assert self.screen is not None
        stale = failure(
            "element_stale",
            f"节点 {element[0]}.{element[1]}e{element[2]} 已失效（窗口重新列出过节点、节点已销毁"
            "或连接器重启过）；没有执行。请按下面的新列表重新选择。",
            window=element[0],
        )
        try:
            target = self.screen.window(element[0])
            stale["elements"] = self._list(target, limits, scale, None)
        except Refused as refused:
            stale["elements_error"] = refused.message
        return stale

    def _visible(
        self,
        area: Area,
        limits: tuple[int, int],
        bounds: tuple[float, float, float, float] | None,
        width: int,
        height: int,
    ) -> tuple[float, float] | dict[str, Any]:
        """The centre of a node now, in global input coordinates, when it is inside ``area``."""
        if bounds is not None and bounds[2] > 0 and bounds[3] > 0:
            centre = (bounds[0] + bounds[2] / 2, bounds[1] + bounds[3] / 2)
            x, y = to_image(area, *limits, *centre)
            if 0 <= x < width and 0 <= y < height:
                return centre
        box = None
        if bounds is not None:
            x0, y0 = to_image(area, *limits, bounds[0], bounds[1])
            x1, y1 = to_image(area, *limits, bounds[0] + bounds[2], bounds[1] + bounds[3])
            box = [round(x0), round(y0), round(x1), round(y1)]
        return failure(
            "element_not_visible",
            "这个节点现在不在画面内（已移出、被收起或尺寸为零），没有执行，也不会改按坐标点击；"
            "先 screenshot 或 elements 重新看。",
            bounds=box,
        )

    def _yield(self) -> dict[str, Any]:
        """Someone used the computer since Yuki last looked: let them finish, run nothing."""
        assert self.screen is not None
        deadline = time.monotonic() + YIELD_SECONDS
        while (left := deadline - time.monotonic()) > 0 and (
            idle := self.screen.idle()
        ) < QUIET_SECONDS:
            time.sleep(min(QUIET_SECONDS - idle, left))
        return failure(
            "owner_active",
            "本人（或屏幕共享、远程控制）刚在用这台电脑，Yuki 让出了这次输入，没有执行；"
            "画面可能已经变了。先 computer(action=screenshot) 重新截图，再按新截图重试。",
        )

    def _display(self, identity: str | None) -> Display | None:
        assert self.screen is not None
        displays = self.screen.displays()
        return next(
            (
                display
                for display in displays
                if (display.id == identity if identity is not None else display.primary)
            ),
            None,
        )

    def _input(
        self,
        parsed: Parsed,
        args: dict[str, Any],
        aim: tuple[float, float] | None,
        aim2: tuple[float, float] | None,
        window: Target | None,
        node: Node | None,
    ) -> dict[str, Any]:
        assert self.screen is not None
        action = parsed.action
        screen: Surface = window if window is not None else self.screen
        posted = 0
        routes: list[str] = []
        activity = {
            "type": "activity",
            "tool": "computer",
            "action": action,
            "mode": "background" if window is not None else "foreground",
        }
        self.report({**activity, "phase": "start"})
        try:
            if node is not None and action in {"key", "type"}:
                posted += 1
                if window is not None:
                    window.focus_element(node)
                else:
                    node.focus()
                routes.append("accessibility")
            elif aim is not None and action in {"key", "type"}:
                # Keys go to the focus: first make the field at x,y that focus (A19).
                posted += 1
                if window is not None:
                    routes.append(window.focus(*aim))
                else:
                    routes.append(self.screen.click(*aim, "left", 1, ()))
            if action in actions.CLICKS:
                assert aim is not None
                posted += 1
                if (
                    node is not None
                    and action == "click"
                    and not parsed.modifiers
                    and node.press()
                ):
                    routes.append("accessibility")
                else:
                    routes.append(
                        screen.click(*aim, parsed.button, parsed.count, parsed.modifiers)
                    )
            elif action == "move":
                assert aim is not None
                posted += 1
                routes.append(screen.move(*aim))
            elif action == "drag":
                assert aim is not None and aim2 is not None
                posted += 1
                routes.append(screen.drag(*aim, *aim2, parsed.modifiers))
            elif action == "scroll":
                assert aim is not None
                posted += 1
                routes.append(screen.scroll(*aim, parsed.dx, parsed.dy, parsed.modifiers))
            elif action == "key":
                assert parsed.keys is not None
                for _ in range(parsed.repeat):
                    if self.control.holder == "owner":
                        # Desktop 「停止」 arrived while repeating: the rest is not sent.
                        return {
                            "status": "unknown" if posted else "failed",
                            **self.control.held(),
                            "route": list(dict.fromkeys(routes)),
                        }
                    posted += 1
                    routes.append(screen.key(*parsed.keys))
            elif action == "type":
                text = args.get("text") or ""
                for start in range(0, len(text), TYPE_RUN):
                    if self.control.holder == "owner":
                        # Desktop 「停止」 arrived while typing: the rest is not typed.
                        return {
                            "status": "unknown" if posted else "failed",
                            **self.control.held(),
                            "route": list(dict.fromkeys(routes)),
                        }
                    posted += 1
                    routes.append(screen.type(text[start : start + TYPE_RUN]))
        except Refused as refused:
            # That call delivered nothing; whatever was posted before it was delivered.
            posted -= 1
            return {
                **refused.result(),
                "status": "unknown" if posted else "failed",
                "route": list(dict.fromkeys(routes)),
            }
        except Stale:
            # The node was gone when it was pressed or focused: that call delivered nothing.
            posted -= 1
            return {
                "status": "unknown" if posted else "failed",
                "error": "element_stale",
                "route": list(dict.fromkeys(routes)),
            }
        except OSError as error:
            return {
                "status": "unknown" if posted else "failed",
                "error": "input_failed",
                "message": f"输入失败：{error}",
                "route": list(dict.fromkeys(routes)),
            }
        finally:
            if posted:
                # Yuki's own events are now the latest input; only later input is someone else's.
                self.observed = self._baseline()
            self.report({**activity, "phase": "end", "route": list(dict.fromkeys(routes))})
        return {"route": list(dict.fromkeys(routes))}
