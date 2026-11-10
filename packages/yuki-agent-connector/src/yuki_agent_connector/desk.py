"""``computer`` / ``browser`` on the owner's computer, yielding to the owner (task book §5.5).

The owner keeps using the computer while Yuki works. Yuki's global input (the whole screen,
the real cursor) first compares two facts: when the last input of any source happened, and
when Yuki last looked or acted (its previous screenshot or ``computer`` input). Any input
after that is not Yuki's (the owner, Screen Sharing, a remote-control tool), so Yuki yields:
it waits until the owner has been quiet for a moment, then refuses this input without
running it (``owner_active``) because the screen may have changed; a fresh screenshot moves
the baseline and the retried input runs. The owner's input never stops Yuki.

The stop is explicit: Desktop 「停止」 (stdin) sets the holder to ``owner``, and every
``computer`` and ``browser`` input is refused with ``control_held`` while screenshots and
snapshots still read. Only Desktop 「繼續」 or ``computer(action=resume)`` clears it. The
flag lives in this process only: Desktop re-sends it after restarting the child.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chrome import BrowserHost, capture_name
from .config import private_path
from .screen import Backend, Display, frame, parse_keys, to_global

READS = frozenset({"screenshot", "resume"})
INPUTS = frozenset(
    {"click", "double_click", "right_click", "move", "drag", "type", "key", "scroll"}
)
POINTED = frozenset({"click", "double_click", "right_click", "move", "drag", "scroll"})
# Typed text is posted in runs so that a Desktop stop arriving meanwhile ends it.
TYPE_RUN = 32
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
            )
        if args.get("window") is not None:
            return failure(
                "device_not_supported",
                "本人电脑暂不支持按窗口截图和操作；省略 window 即使用整个屏幕。",
            )
        if action not in READS | INPUTS:
            return failure("invalid_arguments", f"未知的动作：{action}")
        keys = None
        if action == "key":
            try:
                keys = parse_keys(args.get("text") or "")
            except ValueError as error:
                return failure("invalid_arguments", str(error))
        if action in POINTED and (args.get("x") is None or args.get("y") is None):
            return failure("invalid_arguments", f"{action} 需要 x 与 y。")
        if action == "drag" and (args.get("x2") is None or args.get("y2") is None):
            return failure("invalid_arguments", "drag 需要 x2 与 y2。")
        with self.lock:
            return self._locked(command["id"], args, action, keys)

    def _locked(
        self,
        identity: str,
        args: dict[str, Any],
        action: str,
        keys: tuple[tuple[str, ...], str] | None,
    ) -> dict[str, Any]:
        assert self.screen is not None
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
        display = self._display(args.get("display"))
        if display is None:
            return failure(
                "display_unavailable",
                f"找不到显示器 {args.get('display')}；可用的显示器见 get_environment。",
            )
        max_dimension, max_pixels = args["max_dimension"], args["max_pixels"]
        result: dict[str, Any] = {}
        if action in INPUTS:
            if not input_granted:
                return failure(
                    "input_permission_required",
                    "这台 Mac 没有给 Yuki 辅助功能权限，输入会被系统丢弃；请本人在“系统设置 › "
                    "隐私与安全性 › 辅助功能”中允许 Yuki。",
                )
            if self.control.holder == "owner":
                return {"status": "failed", **self.control.held()}
            if (
                self.observed is not None
                # Even the earliest possible moment of the last input is after the baseline.
                and self.screen.last_input()[0] > self.observed
            ):
                return self._yield()

            def point(x_name: str, y_name: str) -> tuple[float, float]:
                return to_global(display, max_dimension, max_pixels, args[x_name], args[y_name])

            target = point("x", "y") if action in POINTED else None
            if self.screen.elevated(target):
                return failure(
                    "target_elevated",
                    "目标窗口以管理员权限运行，系统不允许普通程序向它输入；输入没有执行。",
                )
            submitted = self._input(action, args, keys, target, point)
            if "error" in submitted:
                return submitted
            result["submitted"] = True
        width, height = frame(display, max_dimension, max_pixels)
        path = self.captures / f"{capture_name(identity)}.jpg"
        try:
            self.screen.capture(display, width, height, path)
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
        # After the picture: Yuki's events have been processed by now, and the model will
        # act on what it shows, so only input after this is someone else's.
        self.observed = self._baseline()
        if action in READS:
            # Per-window capture and delivery arrive with phase 3b.
            result["windows"] = []
        return {
            "status": "succeeded",
            "width": width,
            "height": height,
            "display": display.id,
            "control": self.control.visible(),
            **result,
        }

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
        action: str,
        args: dict[str, Any],
        keys: tuple[tuple[str, ...], str] | None,
        target: tuple[float, float] | None,
        point: Callable[[str, str], tuple[float, float]],
    ) -> dict[str, Any]:
        assert self.screen is not None
        screen = self.screen
        posted = 0
        self.report({"type": "activity", "tool": "computer", "action": action, "phase": "start"})
        try:
            if action in {"click", "double_click", "right_click"}:
                assert target is not None
                posted += 1
                screen.click(
                    *target,
                    "right" if action == "right_click" else "left",
                    2 if action == "double_click" else 1,
                )
            elif action == "move":
                assert target is not None
                posted += 1
                screen.move(*target)
            elif action == "drag":
                assert target is not None
                posted += 1
                screen.drag(*target, *point("x2", "y2"))
            elif action == "scroll":
                assert target is not None
                posted += 1
                screen.scroll(*target, args.get("dx") or 0, args.get("dy") or 0)
            elif action == "key":
                assert keys is not None
                posted += 1
                screen.key(*keys)
            else:
                text = args.get("text") or ""
                for start in range(0, len(text), TYPE_RUN):
                    if self.control.holder == "owner":
                        # Desktop 「停止」 arrived while typing: the rest is not typed.
                        return {"status": "unknown", **self.control.held()}
                    posted += 1
                    screen.type(text[start : start + TYPE_RUN])
        except OSError as error:
            return {
                "status": "unknown" if posted else "failed",
                "error": "input_failed",
                "message": f"输入失败：{error}",
            }
        finally:
            if posted:
                # Yuki's own events are now the latest input; only later input is someone else's.
                self.observed = self._baseline()
            self.report({"type": "activity", "tool": "computer", "action": action, "phase": "end"})
        return {}
