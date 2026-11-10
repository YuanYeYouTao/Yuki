"""Controlled real-browser work: serve a page, wait for the human, then record the result.

The page is opened by the model's ``browser`` tool (the scenario reads the URL
printed here); this terminal program only serves it and waits on the original
help/control request through ``yuki-computer``.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML = (
    b"<!doctype html><meta charset=utf-8><title>Desktop integration</title>"
    b"<body style='margin:48px;font:24px sans-serif;background:#faf7f2'>"
    b"<h1>Real Yuki computer</h1><label>Remote input<br>"
    b"<textarea autofocus style='width:640px;height:180px;font:24px sans-serif' "
    b'oninput=\'document.getElementById("echo").textContent=this.value;'
    b'fetch("/observed",{method:"POST",body:this.value})\'></textarea>'
    b"</label><p id=echo></p>"
)
# Chromium's loopback debugger in the same environment, never through a proxy.
TABS = "http://127.0.0.1:9222/json/list"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)

    def do_POST(self):
        actual = self.rfile.read(int(self.headers["Content-Length"])).decode()
        print("observed-input:" + actual, flush=True)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args):
        pass


def page(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(TABS, timeout=5) as response:
        return next((tab for tab in json.load(response) if tab.get("url") == url), None)


def main():
    workspace = Path(os.environ.get("YUKI_DESKTOP_WORKSPACE", "/workspace"))
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            print("computer-work-url:" + url, flush=True)
            while page(url) is None:
                time.sleep(0.1)
            print("computer-work-ready", flush=True)
            control_wait = "wait-control" in sys.argv[1:]
            if control_wait:
                control = (
                    Path(os.environ.get("YUKI_COMPUTER_RUNTIME", "/var/lib/yuki-runtime/computer"))
                    / "control.json"
                )
                while json.loads(control.read_text()).get("holder") != "human":
                    time.sleep(0.1)
            subprocess.run(
                ["yuki-computer", "wait-control"]
                if control_wait
                else ["yuki-computer", "help-request", "--note", "請協助原工作的畫面步驟"],
                check=True,
            )
            (workspace / "original-computer.md").write_text(
                "# Original computer artifact\nDelivered after the original return receipt.\n"
                + json.dumps(page(url), ensure_ascii=False)
                + "\n"
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
