"""Exercise installer functions and HTTP stalls without changing a Windows host."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b"download fixture"


class FixtureServer(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        try:
            if self.path == "/stall-header":
                time.sleep(3)
            payload = b"wrong bytes" if self.path == "/wrong" else PAYLOAD
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.path == "/stall-body":
                self.wfile.write(payload[:1])
                self.wfile.flush()
                time.sleep(3)
                self.wfile.write(payload[1:])
            elif self.path == "/slow":
                for char in payload:
                    self.wfile.write(bytes([char]))
                    self.wfile.flush()
                    time.sleep(0.3)
            else:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--powershell", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureServer)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                str(args.powershell),
                "-NoProfile",
                "-File",
                str(ROOT / "tests/support/windows_installer_checks.ps1"),
                "-Source",
                str(ROOT / "deploy/windows/Deploy-Yuki.ps1"),
                "-DownloadOrigin",
                f"http://127.0.0.1:{server.server_port}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(result.stdout)
        print(result.stderr)
        print(
            json.dumps(
                {"exit_code": result.returncode, "seconds": round(time.monotonic() - started, 3)}
            )
        )
        raise SystemExit(result.returncode)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
