"""End to end: install with install.sh, then use the real Compose Manager and environment.

Run on a Linux Docker host whose images carry the release names, for example in CI after
building ``ghcr.io/yuanyeyoutao/yuki:<version>`` and both ``yuki-environment`` images:

    python scripts/sandbox_compose_e2e.py --version 4.0.0 --bundle dist/yuki-4.0.0-deploy.tar.gz \
        --directory /tmp/yuki-e2e --artifacts test-results/sandbox-compose

Covers task book F4, F7, F8, F9, F14, F15, F17 (success path), F20 and F24 with the real
installer, Docker engine, firewall and environment. Writes one JSON record per step.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CALL = """
import json, socket, sys
request = sys.stdin.read().encode()
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect("/run/yuki-sandbox/manager.sock")
    connection.sendall(request + b"\\n")
    reply = b""
    while not reply.endswith(b"\\n"):
        chunk = connection.recv(65536)
        if not chunk:
            break
        reply += chunk
print(reply.decode())
"""


class Failure(AssertionError):
    pass


class Deployment:
    def __init__(self, directory: Path, artifacts: Path) -> None:
        self.directory, self.artifacts = directory, artifacts
        self.steps: list[dict[str, Any]] = []

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> str:
        result = subprocess.run(
            args, cwd=self.directory, input=stdin, capture_output=True, text=True, check=False
        )
        if check and result.returncode:
            raise Failure(f"{' '.join(args)} failed: {result.stdout}{result.stderr}")
        return result.stdout

    def compose(self, *args: str, check: bool = True) -> str:
        return self.run("docker", "compose", *args, check=check)

    def manager(self, method: str, args: dict[str, Any], request_id: str = "") -> dict[str, Any]:
        # As the Bot's own user: the socket must be usable through the Bot's group (F14).
        request = {"method": method, "args": args, "request_id": request_id or str(uuid4())}
        output = self.run(
            "docker",
            "compose",
            "exec",
            "-T",
            "--user",
            "10001:10001",
            "bot",
            "python",
            "-c",
            CALL,
            stdin=json.dumps(request),
        )
        return json.loads(output)  # type: ignore[no-any-return]

    def status(self) -> dict[str, Any]:
        return self.manager("environment_status", {})

    def wait_ready(self, seconds: float = 900) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                status = self.status()
                if status.get("ready"):
                    return status
            except (Failure, ValueError):
                pass
            time.sleep(3)
        print(self.compose("logs", "--tail", "60", "manager", check=False))
        raise Failure("the Manager did not become ready")

    def terminal(self, command: str) -> dict[str, Any]:
        started = self.manager("terminal_exec", {"command": command})
        run = started["run_id"]
        for _ in range(60):
            result = self.manager("terminal_read", {"run_id": run, "wait_seconds": 4})
            if result.get("status") in {"succeeded", "failed", "cancelled"}:
                return result
        raise Failure(f"terminal run {run} did not finish")

    def record(self, name: str, **facts: Any) -> None:
        self.steps.append({"step": name, **facts})
        self.artifacts.mkdir(parents=True, exist_ok=True)
        (self.artifacts / "steps.json").write_text(
            json.dumps(self.steps, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"ok  {name}")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True)
    deployment = Deployment(args.directory, args.artifacts.resolve())
    answers = args.directory.parent / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "protocol": "chat_completions",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "not-called-by-this-test",
            }
        )
    )

    # F24: no terminal and no --yes is refused before anything is written.
    refused = subprocess.run(
        ["sh", str(ROOT / "install.sh"), "--dir", str(args.directory)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    expect(
        refused.returncode != 0 and "--yes" in refused.stderr, "a promptless run was not refused"
    )
    expect(not any(args.directory.iterdir()), "a refused install wrote files")
    deployment.record("refuses_without_terminal", stderr=refused.stderr.strip())

    installed = subprocess.run(
        [
            "sh",
            str(ROOT / "install.sh"),
            "--dir",
            str(args.directory),
            "--version",
            args.version,
            "--bundle",
            str(args.bundle.resolve()),
            "--yes",
            "--profile",
            "lite",
            "--answers",
            str(answers),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    expect(installed.returncode == 0, f"install failed:\n{installed.stdout}{installed.stderr}")
    status = deployment.wait_ready()
    expect(status["profile"] == "lite" and status["browser_mode"] == "none", "not the lite profile")
    expect(status["runtime"] in {"runc", "runsc"}, "runtime not reported")
    deployment.record("install_lite", stdout=installed.stdout, status=status)

    # F14/F15: the Bot's user talks to the Manager and the environment runs commands.
    result = deployment.terminal(
        "id -u && echo yuki > /workspace/keep.txt && cat /workspace/keep.txt"
    )
    expect(result["status"] == "succeeded" and "yuki" in result["output"], f"terminal: {result}")
    first_run = result["run_id"]
    deployment.record("terminal", result=result)

    # F7: public egress works; the host (bridge gateway) and private ranges do not.
    gateway = deployment.run(
        "docker",
        "network",
        "inspect",
        "yuki-sandbox-egress",
        "--format",
        "{{(index .IPAM.Config 0).Gateway}}",
    ).strip()
    probes = {}
    for name, url in {
        "public": "https://example.com",
        "host": f"http://{gateway}:22",
        "private": "http://10.255.255.1",
    }.items():
        probe = deployment.terminal(f"curl -sS -o /dev/null -m 5 {url}; echo exit=$?")
        probes[name] = probe["output"].strip().splitlines()[-1]
    expect(probes["public"] == "exit=0", f"public egress failed: {probes}")
    # A drop times out (28); an unfiltered host port would refuse at once (7).
    expect(probes["host"] == "exit=28", f"the host was reachable: {probes}")
    expect(probes["private"] == "exit=28", f"a private range was reachable: {probes}")
    deployment.record("network_isolation", gateway=gateway, probes=probes)

    # The lite profile declares the browser and answers with the real reason.
    browser = deployment.manager("browser_action", {"action": "snapshot"})
    expect(browser.get("error") == "browser_unavailable", f"lite browser: {browser}")
    deployment.record("lite_browser_unavailable", result=browser)

    # F8/F15: restarting the Manager keeps the environment and the original run.
    generation = status["generation"]
    deployment.compose("restart", "manager")
    restarted = deployment.wait_ready()
    expect(restarted["generation"] == generation, "a Manager restart replaced the environment")
    again = deployment.manager("terminal_read", {"run_id": first_run})
    expect(again.get("status") == "succeeded", f"the original run was lost: {again}")
    deployment.record("manager_restart", generation=generation, run=again)

    # F9: down -v keeps the external volumes, so files and receipts survive.
    deployment.compose("down", "-v")
    volumes = deployment.run("docker", "volume", "ls", "--format", "{{.Name}}").split()
    expect({"yuki-sandbox-home", "yuki-sandbox-state"} <= set(volumes), "down -v removed data")
    deployment.compose("up", "-d")
    deployment.wait_ready()
    kept = deployment.terminal("cat /workspace/keep.txt")
    expect("yuki" in kept["output"], "a workspace file was lost by down -v")
    deployment.record("down_volumes_keeps_data", volumes=volumes, file=kept["output"])

    # F17/F20: moving to headless installs the browser in place; no desktop is claimed.
    environment = (args.directory / ".env").read_text()
    (args.directory / ".env").write_text(
        environment.replace("YUKI_SANDBOX_PROFILE=lite", "YUKI_SANDBOX_PROFILE=headless")
    )
    deployment.compose("up", "-d")
    status = deployment.wait_ready(1800)
    expect(status["profile"] == "headless", "the profile did not change")
    expect(status["browser_mode"] == "headless" and not status["desktop"], f"headless: {status}")
    opened = deployment.manager(
        "browser_action", {"action": "open", "url": "data:text/html,<h1>yuki-e2e</h1>"}
    )
    expect(not opened.get("error"), f"browser open: {opened}")
    snapshot = deployment.manager("browser_action", {"action": "snapshot"})
    expect("yuki-e2e" in json.dumps(snapshot, ensure_ascii=False), f"snapshot: {snapshot}")
    screen = deployment.manager("computer_action", {"action": "screenshot"})
    expect(screen.get("error") == "display_unavailable", f"headless computer: {screen}")
    still = deployment.terminal("cat /workspace/keep.txt")
    expect("yuki" in still["output"], "the profile change lost files")
    deployment.record(
        "headless_in_place",
        programs=status["programs"],
        snapshot_has_page=True,
        computer=screen,
    )
    print(f"All steps passed; record in {args.artifacts / 'steps.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
