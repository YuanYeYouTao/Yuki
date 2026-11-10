"""The Yuki-dedicated Chrome on the owner's computer, driven through the shared CDP driver.

The Connector finds an installed Chrome (it never downloads one) and runs it with its own
profile in the Connector's data directory, never the owner's. It connects to the instance
named by that profile's ``DevToolsActivePort`` first and launches only when that fails: a
second launch on a running profile is handed to the running instance, whose arguments win.
Chrome is started outside the Connector's process group / job, so a Connector restart
leaves the window open. ``--remote-debugging-port`` is reachable by every local process;
the profile holds none of the owner's sign-ins (task book §5.8).
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import cdp

logger = logging.getLogger(__name__)
FLAGS = (
    "--remote-debugging-port=0",
    "--no-first-run",
    "--no-default-browser-check",
    # An occluded or background window keeps rendering, so captures never freeze.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
)
# Bounds a cold Chrome start until its DevTools port file appears; not a success gate.
LAUNCH_SECONDS = 30.0
# The driver's own settle wait per action (navigation, dialog, new tab, download).
SETTLE_SECONDS = 15.0
READS = frozenset({"snapshot", "screenshot"})


def find_chrome() -> str | None:
    """An installed Google Chrome executable, or None."""
    if sys.platform == "darwin":
        candidates = [
            Path(root) / "Google Chrome.app/Contents/MacOS/Google Chrome"
            for root in ("/Applications", Path.home() / "Applications")
        ]
    elif sys.platform == "win32":
        import winreg

        candidates = []
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                ) as key:
                    candidates.append(Path(winreg.QueryValue(key, None)))
            except OSError:
                pass
        candidates += [
            Path(os.environ[name]) / "Google/Chrome/Application/chrome.exe"
            for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
            if os.environ.get(name)
        ]
    else:
        candidates = [
            Path(found)
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
            if (found := shutil.which(name))
        ]
    return next((str(path) for path in candidates if path.is_file()), None)


class Unavailable(Exception):
    pass


class BrowserHost:
    """One thread owns the CDP connection; requests queue to it, idle time pumps events."""

    def __init__(
        self,
        directory: Path,
        captures: Path,
        holder: Callable[[], str],
        held: Callable[[], dict[str, Any]],
        report: Callable[[dict[str, Any]], None],
    ) -> None:
        self.profile = directory / "chrome-profile"
        self.downloads = directory / "downloads"
        self.captures = captures
        self.holder, self.held, self.report = holder, held, report
        self.requests: queue.Queue[tuple[dict[str, Any], Callable[[dict[str, Any]], None]] | None]
        self.requests = queue.Queue()
        self.thread: threading.Thread | None = None
        self.browser: cdp.Browser | None = None

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[dict[str, Any]] = loop.create_future()

        def settle(result: dict[str, Any]) -> None:
            # The caller may have been cancelled (Connector shutdown) meanwhile.
            if not answer.done():
                answer.set_result(result)

        def deliver(result: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(settle, result)

        if self.thread is None:
            self.thread = threading.Thread(target=self._serve, name="yuki-browser", daemon=True)
            self.thread.start()
        self.requests.put((command, deliver))
        return await answer

    async def close(self) -> None:
        if self.thread is not None:
            self.requests.put(None)
            await asyncio.to_thread(self.thread.join)
            self.thread = None

    def _serve(self) -> None:
        while True:
            try:
                item = self.requests.get(timeout=0.05 if self.browser else None)
            except queue.Empty:
                # Idle: dialogs, downloads and new tabs are observed as they happen.
                try:
                    assert self.browser is not None
                    self.browser.pump(0)
                except (OSError, ValueError):
                    # Chrome quit or the owner closed it; the next action reopens it.
                    self._drop()
                continue
            if item is None:
                self._drop()
                return
            command, deliver = item
            try:
                result = self._handle(command)
            except Exception as error:
                logger.exception("Browser request failed")
                result = {
                    "status": "failed" if command["action"] in READS else "unknown",
                    "error": "driver_failed",
                    "message": f"浏览器驱动处理这个请求时出错：{type(error).__name__}: {error}",
                }
            deliver(result)

    def _drop(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def _check(self) -> None:
        """Runs before every input the driver sends (task book §5.5 point 4)."""
        if self.holder() == "owner":
            raise cdp.Failure(self.held())

    def _handle(self, command: dict[str, Any]) -> dict[str, Any]:
        args = command["arguments"]
        action = command["action"]
        read = action in READS
        if not read and self.holder() == "owner":
            return {"status": "failed", **self.held()}
        try:
            browser = self._ensure()
        except Unavailable as error:
            return {"status": "failed", "error": "browser_unavailable", "message": str(error)}
        if not read:
            self.report({"type": "activity", "tool": "browser", "action": action, "phase": "start"})
        try:
            result, image = browser.execute(
                {
                    **{
                        name: value
                        for name, value in args.items()
                        if name not in {"owner", "max_dimension", "max_pixels"}
                    },
                    "action": action,
                },
                owner=args.get("owner"),
                before_input=self._check,
                max_dimension=args["max_dimension"],
                max_pixels=args["max_pixels"],
                wait_seconds=SETTLE_SECONDS,
            )
        except cdp.Failure as failure:
            result, image = failure.result, None
            if failure.result["error"] == "browser_unavailable":
                self._drop()
        finally:
            if not read:
                self.report(
                    {"type": "activity", "tool": "browser", "action": action, "phase": "end"}
                )
        if image is not None:
            path = self.captures / f"{capture_name(command['id'])}.jpg"
            path.write_bytes(image.data)
            result["image"] = {
                "path": str(path),
                "media_type": image.media_type,
                "width": image.width,
                "height": image.height,
            }
        return {
            **result,
            "status": result.get("status") or ("failed" if "error" in result else "succeeded"),
        }

    def _connect(self) -> cdp.Browser:
        lines = (self.profile / "DevToolsActivePort").read_text().splitlines()
        return cdp.Browser(
            f"http://127.0.0.1:{int(lines[0])}", downloads=str(self.downloads), timeout=10
        )

    def _ensure(self) -> cdp.Browser:
        if self.browser is not None:
            return self.browser
        try:
            self.browser = self._connect()
            logger.info("Connected to the running Yuki Chrome")
            return self.browser
        except (OSError, ValueError, IndexError) as error:
            logger.info("No running Yuki Chrome to connect to (%s)", type(error).__name__)
        executable = find_chrome()
        if executable is None:
            raise Unavailable("这台电脑没有安装 Google Chrome；本人安装后才能使用浏览器。")
        self.profile.mkdir(parents=True, exist_ok=True)
        # A port file left by an instance that is gone names a dead port.
        (self.profile / "DevToolsActivePort").unlink(missing_ok=True)
        launch([executable, f"--user-data-dir={self.profile}", *FLAGS, "about:blank"])
        logger.info("Launched the Yuki Chrome: %s", executable)
        deadline = time.monotonic() + LAUNCH_SECONDS
        failure: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.browser = self._connect()
                return self.browser
            except (OSError, ValueError, IndexError) as error:
                failure = error
                time.sleep(0.1)
        logger.warning("The Yuki Chrome opened no DevTools port: %r", failure)
        raise Unavailable(
            "Chrome 没有在等待时间内打开调试端口；可让本人退出 Yuki 专用 Chrome 后重试。"
        )


def launch(arguments: list[str]) -> None:
    """Start Chrome so that it outlives the Connector."""
    quiet: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        # A new session: a signal to the Connector's process group does not reach Chrome.
        subprocess.Popen(arguments, start_new_session=True, **quiet)
        return
    detached = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            arguments, creationflags=detached | subprocess.CREATE_BREAKAWAY_FROM_JOB, **quiet
        )
    except PermissionError:
        # The owning job forbids breakaway; Chrome then shares the Connector's job.
        logger.warning("Chrome could not leave the Connector's job object")
        subprocess.Popen(arguments, creationflags=detached, **quiet)


def capture_name(identity: str) -> str:
    import hashlib

    return hashlib.sha256(identity.encode()).hexdigest()[:32]
