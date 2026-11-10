"""One foreground Supervisor service; all children stay in its process tree."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import urllib.request
from pathlib import Path

CDP_ADDRESS = "http://127.0.0.1:9222"


def browser_ready() -> bool:
    # Chromium's loopback debugger must never go through an outbound proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(CDP_ADDRESS + "/json/version", timeout=0.2) as response:
            version = json.load(response)
        return isinstance(version, dict) and bool(version.get("webSocketDebuggerUrl"))
    except (OSError, ValueError):
        return False


def browser_mode() -> str:
    manifest = Path("/opt/yuki-runtime/manifest.json")
    if manifest.exists():
        return str(json.loads(manifest.read_text()).get("browser_mode", "headed"))
    return "headed"


async def main() -> int:
    children: list[asyncio.subprocess.Process] = []
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    loop.add_signal_handler(signal.SIGINT, stopped.set)
    waiting: list[asyncio.Task[int | bool]] = []
    profile = Path.home() / ".local/share/yuki-computer/chromium"
    profile.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "DISPLAY": ":1"}
    try:
        mode = await asyncio.to_thread(browser_mode)
        if mode == "none":
            return 1
        if mode == "headed":
            children.append(
                await asyncio.create_subprocess_exec(
                    *[
                        "Xvnc",
                        ":1",
                        "-geometry",
                        "1440x900",
                        "-depth",
                        "24",
                        "-rfbport",
                        "5901",
                        "-localhost",
                        "yes",
                        "-SecurityTypes",
                        "None",
                        "-AlwaysShared",
                    ],
                    env=environment,
                )
            )
            # Read actual socket readiness, rather than declaring failure after an age.
            while not stopped.is_set() and children[0].returncode is None:
                try:
                    _, connection = await asyncio.open_connection("127.0.0.1", 5901)
                    connection.close()
                    await connection.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            if stopped.is_set():
                return 0
            if children[0].returncode is not None:
                return 1
            children.append(await asyncio.create_subprocess_exec("openbox", env=environment))
        browser = [
            "chromium",
            "--user-data-dir=" + str(profile),
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            "--restore-last-session",
            "--no-first-run",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]
        if mode == "headless":
            browser.extend(["--headless", "about:blank"])
        children.append(await asyncio.create_subprocess_exec(*browser, env=environment))
        while not stopped.is_set() and all(child.returncode is None for child in children):
            if await asyncio.to_thread(browser_ready):
                break
            await asyncio.sleep(0.1)
        else:
            return 0 if stopped.is_set() else 1
        root = Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer"))
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        # The long-lived driver owns the one CDP connection and the screen input;
        # downloads go to the workspace, never to the small runtime volume.
        workspace = Path(os.environ.get("YUKI_DESKTOP_WORKSPACE", "/workspace"))
        children.append(
            await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(__file__).with_name("yuki_computer.py")),
                "serve",
                "--cdp",
                CDP_ADDRESS,
                "--downloads",
                str(workspace / "downloads"),
                *(("--display", ":1") if mode == "headed" else ()),
            )
        )
        descriptor, temporary = tempfile.mkstemp(dir=root, prefix=".desktop-status-")
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                {
                    "run_id": os.environ["YUKI_JOB_ID"],
                    "display": "running" if mode == "headed" else "unavailable",
                    "browser": "running",
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "desktop-status.json")
        waiting = [asyncio.create_task(child.wait()) for child in children]
        waiting.append(asyncio.create_task(stopped.wait()))
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        return 0 if stopped.is_set() else 1
    finally:
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)
        for child in reversed(children):
            if child.returncode is None:
                child.terminate()
        for child in children:
            try:
                await asyncio.wait_for(child.wait(), 3)
            except TimeoutError:
                child.kill()
                await child.wait()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
