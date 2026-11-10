"""B8: the Yuki-dedicated Chrome, driven by real Connector processes (task book §5.8).

Uses the installed Google Chrome (present on every GitHub-hosted runner) with a private
profile in a temporary Connector data directory; the owner's own Chrome and profile are
never touched. Checks: launch flags and profile, ``DevToolsActivePort``, downloads in the
Connector's directory, Chrome outliving a killed Connector, connect-before-launch on the
next Connector, reopening after the owner quits Chrome, and the activity lines.
Artifact: test-results/device-computer/chrome-<platform>.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from desk_support import ARTIFACTS, Cloud, ConnectorProcess, until
from websockets.asyncio.server import serve

from yuki_agent_connector.chrome import FLAGS, find_chrome
from yuki_agent_connector.config import Config

LIMITS = {"max_dimension": 1568, "max_pixels": 1_500_000}
NODE = re.compile(r"\[((?:[0-9a-f]{8}_)?e\d+)\]")
PAGE = (
    b"<!doctype html><title>Yuki B8</title><h1>Dedicated Chrome</h1>"
    b"<a href='/report.bin'>Get report</a>"
)

pytestmark = pytest.mark.skipif(
    find_chrome() is None and not os.environ.get("CI"),
    reason="Google Chrome is not installed on this computer (always required on CI)",
)


class Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PAGE if self.path == "/" else b"report-bytes" * 1000
        self.send_response(200)
        if self.path == "/":
            self.send_header("Content-Type", "text/html")
        else:
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="report.bin"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


def chrome_browsers(profile: Path) -> list[tuple[int, str]]:
    """Browser (not renderer/GPU) processes started with this profile."""
    if sys.platform == "win32":
        output = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }',
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        rows = [line.split("\t", 1) for line in output.splitlines() if "\t" in line]
    else:
        output = subprocess.run(
            ["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True, check=True
        ).stdout
        rows = [line.strip().split(" ", 1) for line in output.splitlines() if line.strip()]
    return [
        (int(pid), arguments)
        for pid, arguments in rows
        if f"--user-data-dir={profile}" in arguments and "--type=" not in arguments
    ]


def kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


async def test_dedicated_chrome_outlives_the_connector_and_reopens(
    tmp_path: Path, site: str
) -> None:
    state, data = tmp_path / "state", tmp_path / "data"
    state.mkdir()
    (state / "script.json").write_text(json.dumps({"displays": [], "screen": False}))
    profile = data / "chrome-profile"
    cloud = Cloud()
    record: dict[str, Any] = {"platform": sys.platform, "chrome": find_chrome()}
    try:
        async with serve(cloud.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            Config(
                f"ws://127.0.0.1:{port}/", "original-device", "fixture", full_access=True
            ).save(data / "config.json")
            first = ConnectorProcess(state, data)
            await first.start()
            await cloud.connected.wait_for(1)

            opened = await cloud.command(
                "browser", "open", "open-1", {**LIMITS, "url": site, "owner": "work-a"}
            )
            result = opened["result"]
            record["open"] = result
            assert result["status"] == "succeeded", result
            assert "Dedicated Chrome" in result["snapshot"]
            tab = result["tab"]["id"]
            assert [line for line in first.lines if line["type"] == "activity"] == [
                {"type": "activity", "tool": "browser", "action": "open", "phase": "start"},
                {"type": "activity", "tool": "browser", "action": "open", "phase": "end"},
            ]
            # A private profile in the Connector's directory, launched with every flag.
            browsers = chrome_browsers(profile)
            record["launched"] = browsers
            assert len(browsers) == 1, browsers
            launched_pid, arguments = browsers[0]
            assert all(flag in arguments for flag in FLAGS), arguments
            assert (profile / "DevToolsActivePort").is_file()

            # Downloads land in the Connector's private directory.
            link = next(line for line in result["snapshot"].splitlines() if "Get report" in line)
            clicked = await cloud.command(
                "browser",
                "click",
                "download-1",
                {**LIMITS, "node": NODE.findall(link)[-1], "tab": tab, "owner": "work-a"},
            )
            download = next(
                event for event in clicked["result"]["events"] if event["type"] == "download"
            )
            record["download"] = download
            assert Path(download["path"]).parent == data / "downloads"
            await until(lambda: Path(download["path"]).is_file())

            # A screenshot comes back through the file transfer loop like the screen's.
            shot = await cloud.command(
                "browser", "screenshot", "shot-1", {**LIMITS, "tab": tab, "owner": "work-a"}
            )
            image = await cloud.image(shot["result"]["image"]["path"], "shot-1")
            assert image[:2] == b"\xff\xd8"

            # The Connector is killed (Desktop restart or crash): Chrome stays open.
            await first.kill()
            await asyncio.sleep(1)
            assert [pid for pid, _ in chrome_browsers(profile)] == [launched_pid]

            # The next Connector connects to that instance instead of launching another.
            second = ConnectorProcess(state, data)
            await second.start()
            await cloud.connected.wait_for(2)
            read = await cloud.command(
                "browser", "snapshot", "snapshot-1", {**LIMITS, "tab": tab, "owner": None}
            )
            record["reconnected"] = read
            assert "Dedicated Chrome" in read["result"]["snapshot"], read
            assert [pid for pid, _ in chrome_browsers(profile)] == [launched_pid]

            # The owner quits the Yuki Chrome; the next action opens it again.
            kill(launched_pid)
            await until(lambda: not chrome_browsers(profile))
            await asyncio.sleep(0.5)
            reopened = await cloud.command(
                "browser", "open", "open-2", {**LIMITS, "url": site, "owner": "work-a"}
            )
            record["reopened"] = reopened
            assert reopened["result"]["status"] == "succeeded", reopened
            relaunched = chrome_browsers(profile)
            assert len(relaunched) == 1 and relaunched[0][0] != launched_pid
            exit_code, stderr = await second.stop()
            assert exit_code == 0, stderr
            record["lines"] = {"first": first.lines, "second": second.lines}
    finally:
        for pid, _ in chrome_browsers(profile):
            kill(pid)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"chrome-{sys.platform}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
