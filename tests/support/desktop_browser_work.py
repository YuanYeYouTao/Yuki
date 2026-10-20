"""Controlled real-browser work: observe actual input before handing control back."""

import json
import os
import subprocess
import sys
import threading
import time
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


def main():
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            subprocess.run(
                ["yuki-computer", "browser", "open", url, "--tab", "desktop-integration"],
                check=True,
            )
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
            subprocess.run(
                ["yuki-computer", "screenshot", "--screen", "--out", "/workspace/after-return.png"],
                check=True,
            )
            actual = subprocess.check_output(
                ["yuki-computer", "browser", "snapshot", "--tab", "desktop-integration"],
                text=True,
            )
            Path("/workspace/original-computer.md").write_text(
                "# Original computer artifact\nDelivered after the original return receipt.\n"
                + actual
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
