"""Real subprocess and loopback readiness; Xvnc/Openbox/Chromium are child stand-ins."""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def debugger():
    available = threading.Event()
    observed = threading.Event()
    replies = [(503, b"warming"), (200, b"invalid json"), (200, b"{}")]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if available.is_set():
                status, body = 200, b'{"webSocketDebuggerUrl":"ws://127.0.0.1/devtools/browser"}'
            else:
                status, body = replies.pop(0) if replies else (200, b"{}")
                if not replies:
                    observed.set()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_address, available, observed
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def launch_service(tmp_path, address, *, crash_browser=False):
    program = f"""
import socket, subprocess, sys
from pathlib import Path
from yuki.sandbox import desktop_service
real_process = subprocess.Popen
real_connect = socket.create_connection
def child(command, **kwargs):
    code = (
        'raise SystemExit(1)' if {crash_browser!r} and command[0] == 'chromium'
        else 'import time; time.sleep(60)'
    )
    return real_process([sys.executable, '-c', code], **kwargs)
def connect(address, *args, **kwargs):
    return real_connect({address!r} if address == ('127.0.0.1', 5901) else address, *args, **kwargs)
desktop_service.subprocess.Popen = child
desktop_service.socket.create_connection = connect
desktop_service.Path.home = classmethod(lambda cls: Path({str(tmp_path / "home")!r}))
desktop_service.CDP_VERSION_URL = 'http://{address[0]}:{address[1]}/json/version'
raise SystemExit(desktop_service.main())
"""
    return subprocess.Popen(
        [sys.executable, "-c", program],
        env={
            **os.environ,
            "YUKI_JOB_ID": "original-desktop-run",
            "YUKI_COMPUTER_RUNTIME": str(tmp_path / "runtime"),
            "HTTP_PROXY": "http://127.0.0.1:1",
            "http_proxy": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "no_proxy": "",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_running_is_published_only_after_actual_debugger_ready(tmp_path, debugger):
    address, available, observed = debugger
    process = launch_service(tmp_path, address)
    status = tmp_path / "runtime/desktop-status.json"
    try:
        assert observed.wait(5), "Service did not reach the actual loopback debugger"
        assert process.poll() is None
        assert not status.exists(), "503, malformed JSON and missing debugger cannot mean running"
        available.set()
        deadline = time.monotonic() + 5
        while not status.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert json.loads(status.read_text()) == {
            "run_id": "original-desktop-run",
            "display": "running",
        }
    finally:
        if process.poll() is None:
            process.terminate()
        _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr.decode()


def test_browser_exit_before_ready_never_publishes_running(tmp_path, debugger):
    address, _available, _observed = debugger
    process = launch_service(tmp_path, address, crash_browser=True)
    try:
        _stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 1, stderr.decode()
        assert not (tmp_path / "runtime/desktop-status.json").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
