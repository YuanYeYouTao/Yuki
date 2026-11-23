"""End to end: install with install.sh, then use the real Compose Manager and environment.

Run on a Linux Docker host whose images carry the release names, for example in CI after
building ``ghcr.io/yuanyeyoutao/yuki:<version>`` and both ``yuki-environment`` images:

    python scripts/sandbox_compose_e2e.py --version 4.0.0 --bundle dist/yuki-4.0.0-deploy.tar.gz \
        --directory /tmp/yuki-e2e --artifacts test-results/sandbox-compose

Covers task book F4, F7, F8, F9, F14, F15, F17 (success path), F20 and F24 with the real
installer, Docker engine, firewall and environment. Writes one JSON record per step.
The last stage covers the sandbox proxy task book (2026-11-23) P1-P15 against a proxy on
this host; its record is ``proxy.json`` with the proxy's own log ``proxy-log.txt``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import sandbox_snapshot_e2e  # noqa: E402

# The Bot's published port on this host; another deployment may already use 8080.
PORT = os.environ.get("YUKI_PORT", "8080")
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


# A DNS-over-TLS forwarder for hosts whose own resolver hands out fake-ip addresses
# (which the environment's firewall drops as non-public), run as an environment service.
DOT_FORWARDER = """
import socket, ssl, threading
context = ssl.create_default_context()
server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
server.bind(("127.0.0.1", 53))
def exact(stream, size):
    data = b""
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise OSError("closed")
        data += chunk
    return data
def answer(query, address):
    try:
        with socket.create_connection(("1.1.1.1", 853), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="cloudflare-dns.com") as stream:
                stream.sendall(len(query).to_bytes(2, "big") + query)
                reply = exact(stream, int.from_bytes(exact(stream, 2), "big"))
        server.sendto(reply, address)
    except OSError:
        pass
while True:
    query, address = server.recvfrom(4096)
    threading.Thread(target=answer, args=(query, address), daemon=True).start()
"""


# A minimal HTTP CONNECT proxy for the proxy stage: one log line per request, 502 when the
# target cannot be reached, nothing but CONNECT accepted.
CONNECT_PROXY = """
import socket, sys, threading
host, port, log = sys.argv[1], int(sys.argv[2]), sys.argv[3]
server = socket.create_server((host, port))
def note(line):
    with open(log, "a") as stream:
        stream.write(line + "\\n")
def pipe(source, target):
    try:
        while data := source.recv(65536):
            target.sendall(data)
    except OSError:
        pass
    finally:
        for end in (source, target):
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
def serve(client):
    head = b""
    while b"\\r\\n\\r\\n" not in head:
        chunk = client.recv(4096)
        if not chunk:
            client.close()
            return
        head += chunk
    method, target = head.split(b"\\r\\n", 1)[0].decode().split(" ")[:2]
    if method != "CONNECT":
        note(f"{method} {target} 405")
        client.sendall(b"HTTP/1.1 405 Method Not Allowed\\r\\n\\r\\n")
        client.close()
        return
    name, _, number = target.rpartition(":")
    try:
        upstream = socket.create_connection((name.strip("[]"), int(number)), timeout=10)
    except OSError:
        note(f"CONNECT {target} 502")
        client.sendall(b"HTTP/1.1 502 Bad Gateway\\r\\n\\r\\n")
        client.close()
        return
    note(f"CONNECT {target} 200")
    upstream.settimeout(None)
    client.sendall(b"HTTP/1.1 200 Connection established\\r\\n\\r\\n")
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    pipe(upstream, client)
while True:
    connection, _ = server.accept()
    threading.Thread(target=serve, args=(connection,), daemon=True).start()
"""


class Failure(AssertionError):
    pass


class Deployment:
    def __init__(self, directory: Path, artifacts: Path, version: str) -> None:
        self.directory, self.artifacts, self.version = directory, artifacts, version
        self.steps: list[dict[str, Any]] = []

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> str:
        try:
            result = subprocess.run(
                args,
                cwd=self.directory,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                timeout=1800,
            )
        except subprocess.TimeoutExpired as exc:
            print(self.compose("logs", "--tail", "120", "manager", check=False))
            raise Failure(f"{' '.join(args)} did not answer: {exc}") from exc
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

    def wait_mode(self, mode: str, seconds: float = 1800) -> dict[str, Any]:
        """The mode a profile change reaches once its packages are installed.

        The install runs in the background of a ready Manager, after the environment's own
        services started (U10); the mode is read from the installed manifest afterwards.
        """
        deadline = time.monotonic() + seconds
        status = self.wait_ready(seconds)
        while time.monotonic() < deadline:
            status = self.status()
            if status["browser_mode"] == mode:
                return status
            if status["programs"].get("installed") is False:
                raise Failure(f"installing {mode} failed: {status['programs']}")
            time.sleep(3)
        raise Failure(f"the environment did not reach {mode}: {status}")

    def wait_snapshot(self, seconds: float = 600) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready = [item for item in self.status()["snapshots"] if item["state"] == "ready"]
            if ready:
                return ready[0]  # type: ignore[no-any-return]
            time.sleep(3)
        raise Failure(f"no snapshot became ready: {self.status()['snapshots']}")

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


def configure_resolver(deployment: Deployment, mode: str, runtime: str) -> dict[str, Any]:
    """The documented resolver setting: runsc cannot reach Docker's 127.0.0.11."""
    if mode == "auto":
        mode = "docker" if runtime == "runc" else "nameserver:8.8.8.8"
    if mode == "docker":
        return {"resolver": mode}
    nameserver = "127.0.0.1" if mode == "dot" else mode.split(":", 1)[1]
    image = f"ghcr.io/yuanyeyoutao/yuki:{deployment.version}"
    deployment.run(
        "docker",
        "run",
        "--rm",
        "-i",
        "-v",
        "yuki-sandbox-home:/home",
        "--entrypoint",
        "sh",
        image,
        "-c",
        f"mkdir -p /home/.config && printf 'nameserver {nameserver}\\n' > "
        "/home/.config/resolv.conf && cat > /home/.config/yuki-dns.py && chown -R 10001:10001 "
        "/home/.config",
        stdin=DOT_FORWARDER,
    )
    if mode == "dot":
        for action in ("register", "start"):
            deployment.manager(
                "environment_service",
                {
                    "action": action,
                    "name": "sandbox-dns",
                    "command": "exec python3 /home/yuki/.config/yuki-dns.py",
                    "restart": "always",
                    "cwd": "/home/yuki",
                },
            )
    environment = (deployment.directory / ".env").read_text()
    (deployment.directory / ".env").write_text(
        environment + "YUKI_SANDBOX_RESOLV_CONF=.config/resolv.conf\n"
    )
    deployment.compose("up", "-d", "manager")
    generation = deployment.wait_ready()["generation"]
    # The resolver file is mounted when the environment is created.
    deployment.run("docker", "rm", "-f", "yuki-environment")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = deployment.wait_ready()
        # The forwarder service starts again in the new environment a moment later.
        if status["generation"] != generation and "example.com" in deployment.terminal(
            "getent hosts example.com || true"
        ).get("output", ""):
            return {"resolver": mode, "status": status}
        time.sleep(3)
    raise Failure("the environment was not recreated with a working resolver")


def computer_use(deployment: Deployment, status: dict[str, Any]) -> None:
    """Computer Use in the installed desktop (task book Computer-Use-2026-11-15 §12).

    The Bot's own computer limit (1568 / 1.15 MP) produces 1356x848 from the 1440x900
    display; at-spi2-core and xclip are installed by the profile, the accessibility bus is
    published on the display, and its processes' memory is recorded for runc and runsc.
    """
    limits = {"max_dimension": 1568, "max_pixels": 1_150_000}
    shot = deployment.manager("computer_action", {"action": "screenshot", **limits})
    expect((shot.get("width"), shot.get("height")) == (1356, 848), f"computer size: {shot}")
    expect("cursor" in shot and "scale=1000" in shot.get("coordinates", ""), f"echo: {shot}")
    zoom = deployment.manager(
        "computer_action", {"action": "zoom", "region": [0, 0, 678, 424], **limits}
    )
    expect(zoom.get("frame") == {"width": 1356, "height": 848}, f"zoom: {zoom}")
    # The profile's packages are installed after the environment's services started, in the
    # background of a ready Manager (Computer Use task book §11 U10).
    deadline = time.monotonic() + 1800
    probe = "dpkg-query -W -f='${Status}' xclip 2>/dev/null | grep -q 'ok installed' && echo ready"
    while "ready" not in str(deployment.terminal(probe)["output"]):
        expect(time.monotonic() < deadline, f"packages not installed: {deployment.status()}")
        time.sleep(10)
    facts = deployment.terminal(
        "dpkg-query -W -f='${Package} ${Version}\\n' at-spi2-core xclip; "
        "DISPLAY=:1 xprop -root AT_SPI_BUS | cut -c1-40; "
        "ps -eo rss=,comm= | awk '/at-spi|dbus-daemon/ {print}'"
    )
    output = str(facts["output"])
    expect("at-spi2-core" in output and "xclip" in output, f"packages: {output}")
    expect("AT_SPI_BUS(STRING)" in output, f"accessibility bus: {output}")
    rss = [line.split() for line in output.splitlines() if line.strip()[:1].isdigit()]
    record = {
        "runtime": status.get("runtime"),
        "screenshot": {key: shot.get(key) for key in ("width", "height", "cursor")},
        "zoom": {key: zoom.get(key) for key in ("region", "frame", "width", "height")},
        "packages": [line for line in output.splitlines() if line.startswith(("at-spi", "xclip"))],
        "accessibility_memory_kib": {name: int(size) for size, name in rss},
        "programs": status.get("programs"),
    }
    (deployment.artifacts / "computer-use.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    deployment.record("computer_use", **record)


def proxy_stage(deployment: Deployment, gateway: str) -> None:
    """Sandbox proxy task book (2026-11-23) §6: a proxy on this host, reached from inside.

    The proxy listens on the host's address on the egress bridge, which the firewall drops
    until it is configured (INPUT path). Credentials in the URL are fake and must never
    appear in a status or check result (P14).
    """
    secret = "not-a-real-proxy-secret"
    port = 3128
    log = deployment.artifacts / "proxy-log.txt"
    log.write_text("")
    stage = sandbox_snapshot_e2e.Computer(deployment, "proxy")
    records: list[dict[str, Any]] = []

    def record(name: str, **facts: Any) -> None:
        records.append({"step": name, **facts})
        (deployment.artifacts / "proxy.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        deployment.record(f"proxy_{name}")

    def start_proxy() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(
            [sys.executable, "-c", CONNECT_PROXY, gateway, str(port), str(log)]
        )
        time.sleep(1)
        expect(process.poll() is None, "the test proxy did not start")
        return process

    def blocked(target: str) -> str:
        probe = deployment.terminal(
            f"curl --noproxy '*' -sS -o /dev/null -m 5 http://{target}; echo exit=$?"
        )
        return str(probe["output"]).strip().splitlines()[-1]

    def check(url: str = "") -> dict[str, Any]:
        result = deployment.manager("environment_network_check", {"url": url} if url else {})
        expect(secret not in json.dumps(result), f"credentials in a check: {result}")
        return result

    def proxy_status() -> dict[str, Any]:
        status = deployment.status()
        expect(secret not in json.dumps(status), "credentials in environment_status")
        return status

    def logged(target: str, seconds: float = 60) -> list[str]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            lines = [line for line in log.read_text().splitlines() if f" {target} " in line]
            if lines:
                return lines
            time.sleep(2)
        raise Failure(f"the proxy never saw {target}: {log.read_text()}")

    # The desktop stage ends without Chromium; put it back directly, before any proxy,
    # so the snapshot that applies the proxy carries it.
    installed = deployment.terminal(
        "apt-get update -qq && apt-get install -y -qq --no-install-recommends chromium "
        ">/dev/null; echo exit=$?"
    )
    expect("exit=0" in str(installed["output"]), f"reinstalling Chromium: {installed}")

    proxy = start_proxy()
    try:
        # P1: not configured, the proxy port on the host is dropped like any other.
        status = proxy_status()
        expect(status["proxy"]["configured"] is None, f"P1 status: {status['proxy']}")
        expect(blocked(f"{gateway}:{port}") == "exit=28", "P1: the host proxy port was open")
        direct = check()
        expect(direct["proxy"] is None, f"P1 check: {direct}")
        record("p1_not_configured", proxy=status["proxy"], check=direct)

        # P5: saved, not yet in the running environment.
        url = f"http://e2e:{secret}@{gateway}:{port}"
        stage.settings(YUKI_SANDBOX_PROXY=url)
        status = proxy_status()
        shown = f"http://{gateway}:{port}"
        expect(status["proxy"]["configured"] == shown, f"P5: {status['proxy']}")
        expect(status["proxy"]["environment"] is None, f"P5: {status['proxy']}")
        expect(status["proxy"]["allowed"] == [f"{gateway}:{port}"], f"P2: {status['proxy']}")
        expect("proxy" in status["recreate_required"], f"P5: {status['recreate_required']}")
        before = check()
        expect(before["proxy"] is None, f"P5 check uses the environment's variables: {before}")
        record("p5_saved_not_applied", proxy=status["proxy"], check=before)

        # P6: applied the documented way, a snapshot and its restore; files are kept.
        deployment.terminal("echo proxied > /workspace/proxy-keep.txt")
        taken = stage.snapshot("apply the proxy")
        stage.restore(taken["id"])
        status = proxy_status()
        expect(status["proxy"]["environment"] == shown, f"P6: {status['proxy']}")
        expect("proxy" not in status["recreate_required"], f"P6: {status['recreate_required']}")
        kept = deployment.terminal("cat /workspace/proxy-keep.txt")
        expect("proxied" in str(kept["output"]), "P6: the restore lost a file")
        record("p6_applied", snapshot=taken["id"], proxy=status["proxy"])

        # Through the proxy, from inside: the check, terminal, package manager, a
        # background service and the browser, each seen in the proxy's own log.
        through = check()
        expect(through["stage"] == "ok" and through["proxy"] == shown, f"check: {through}")
        seen = {"check": logged("deb.debian.org:443")}
        terminal = deployment.terminal(
            "curl -sS -o /dev/null -w '%{http_code}' https://example.com/; echo; "
            "env | grep -c '^HTTPS_PROXY='"
        )
        expect(terminal["status"] == "succeeded", f"terminal through the proxy: {terminal}")
        seen["terminal"] = logged("example.com:443")
        package = deployment.terminal(
            "python3 -m pip download --no-deps --quiet --dest /tmp/proxy-pip six; echo exit=$?"
        )
        expect("exit=0" in str(package["output"]), f"package download: {package}")
        seen["package"] = logged("pypi.org:443")
        for action in ("register", "start"):
            deployment.manager(
                "environment_service",
                {
                    "action": action,
                    "name": "proxy-probe",
                    "command": "curl -sS -o /dev/null https://example.org/",
                    "restart": "never",
                    "cwd": "/home/yuki",
                },
            )
        seen["service"] = logged("example.org:443")
        opened = deployment.manager(
            "browser_action", {"action": "open", "url": "https://www.iana.org/"}
        )
        expect(not opened.get("error"), f"browser through the proxy: {opened}")
        seen["browser"] = logged("www.iana.org:443")
        record("through_the_proxy", check=through, seen=seen, terminal=terminal["output"])

        # P2: only that endpoint; the same host's other ports stay dropped.
        expect(blocked(f"{gateway}:22") == "exit=28", "P2: another host port was open")
        # P11: the proxy cannot reach the target.
        upstream = check("https://yuki-e2e.invalid/")
        expect(upstream["stage"] == "proxy_upstream_failed", f"P11: {upstream}")
        record("p2_p11", upstream=upstream)

        # P8: nothing listens on the allowed endpoint any more.
        proxy.terminate()
        proxy.wait()
        refused = check()
        expect(refused["stage"] == "proxy_unreachable", f"P8: {refused}")
        expect(proxy_status()["proxy"]["check"] == refused, "the last check was not kept")
        record("p8_proxy_down", check=refused)

        # P13 and P9: removing the setting closes the endpoint again while the proxy runs;
        # the environment keeps its variables until recreated and now meets the drop.
        proxy = start_proxy()
        stage.settings(YUKI_SANDBOX_PROXY="")
        status = proxy_status()
        expect(status["proxy"]["configured"] is None, f"P13: {status['proxy']}")
        expect(status["proxy"]["allowed"] == [], f"P13: {status['proxy']}")
        expect("proxy" in status["recreate_required"], f"P13: {status['recreate_required']}")
        expect(blocked(f"{gateway}:{port}") == "exit=28", "P13: the endpoint stayed open")
        dropped = check()
        expect(dropped["stage"] == "proxy_unreachable", f"P9: {dropped}")
        expect(dropped["exit_code"] == 28, f"P9: {dropped}")
        record("p13_p9_removed", proxy=status["proxy"], check=dropped)

        # The operator's entry: the same check through the Bot's CLI.
        cli = deployment.run(
            "docker",
            "compose",
            "exec",
            "-T",
            "--user",
            "10001:10001",
            "bot",
            "yuki",
            "setup",
            "environment-check",
            "--network",
            check=False,
        )
        answer = json.loads(cli.strip().splitlines()[-1])
        expect(answer.get("stage") == "proxy_unreachable", f"CLI: {cli}")
        record("operator_cli", check=answer)
    finally:
        proxy.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--desktop", action="store_true", help="add the desktop stage")
    parser.add_argument(
        "--resolver",
        default="auto",
        help="auto (runsc: nameserver:8.8.8.8), docker, nameserver:<ip> or dot",
    )
    args = parser.parse_args()
    args.directory.mkdir(parents=True)
    deployment = Deployment(args.directory, args.artifacts.resolve(), args.version)
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
    deployment.record(
        "install_lite", stdout=installed.stdout, stderr=installed.stderr, status=status
    )
    # Task book §9.3: without any snapshot the first daily one is taken once ready.
    first = deployment.wait_snapshot()
    deployment.record("first_daily_snapshot", snapshot=first)
    deployment.record(
        "resolver", **configure_resolver(deployment, args.resolver, status["runtime"])
    )

    # The owner's first Person, administrator grant and Yuki Desktop code, ready to use.
    code = installed.stdout.split("Pairing code: ", 1)[-1].split()[0]
    expect("Pairing code: " in installed.stdout and len(code) >= 6, "no pairing code printed")
    environment = (args.directory / ".env").read_text()
    expect("YUKI_OWNER_PERSON_ID=" in environment, "the owner was not recorded")
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/livez", timeout=10) as response:
        expect(response.status == 200, "the Bot is not reachable on 127.0.0.1:8080")
    again = deployment.run("sh", "deploy/pair.sh")
    expect(len(again.strip()) >= 6, f"pair.sh gave no new code: {again}")
    deployment.record("owner_pairing", code_length=len(code), reissued=bool(again.strip()))

    # F14/F15: the Bot's user talks to the Manager and the environment runs commands.
    result = deployment.terminal(
        "id -u && echo yuki > /workspace/keep.txt && cat /workspace/keep.txt"
    )
    expect(result["status"] == "succeeded" and "yuki" in result["output"], f"terminal: {result}")
    first_run = result["run_id"]
    deployment.record("terminal", result=result)

    # F7: public egress works; the host (bridge gateway) and private ranges do not.
    # The environment's default gateway is the host's address on the egress bridge.
    route = deployment.terminal(
        "python3 -c \"import socket,struct; print([socket.inet_ntoa(struct.pack('<L', "
        "int(f[2], 16))) for f in (l.split() for l in open('/proc/net/route')) "
        "if f[1] == '00000000'][0])\""
    )
    gateway = route["output"].strip().splitlines()[-1]
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

    # Her computer on lite: autostart, snapshots, restore, rotation, rebuild, daily.
    earlier = sandbox_snapshot_e2e.lite(deployment)
    deployment.record("lite_computer", snapshot=earlier["lite_snapshot"]["id"])

    # F8/F15: restarting the Manager keeps the environment and the original run.
    generation = deployment.status()["generation"]
    deployment.compose("restart", "manager")
    restarted = deployment.wait_ready()
    expect(restarted["generation"] == generation, "a Manager restart replaced the environment")
    again = deployment.manager("terminal_read", {"run_id": first_run})
    expect(again.get("status") == "succeeded", f"the original run was lost: {again}")
    deployment.record("manager_restart", generation=generation, run=again)

    # F9: down -v keeps the external volumes, so files and receipts survive.
    ready = [item["id"] for item in deployment.status()["snapshots"] if item["state"] == "ready"]
    deployment.compose("down", "-v")
    volumes = deployment.run("docker", "volume", "ls", "--format", "{{.Name}}").split()
    expect(
        {"yuki-sandbox-home", "yuki-sandbox-state", "yuki-sandbox-snapshots"} <= set(volumes),
        "down -v removed data",
    )
    deployment.compose("up", "-d")
    after = deployment.wait_ready()
    kept = deployment.terminal("cat /workspace/keep.txt")
    expect("yuki" in kept["output"], "a workspace file was lost by down -v")
    # S24: the external snapshot volume and its records survive down -v.
    survived = [item["id"] for item in after["snapshots"] if item["state"] == "ready"]
    expect(survived == ready, f"down -v changed the snapshots: {ready} -> {survived}")
    deployment.record(
        "down_volumes_keeps_data", volumes=volumes, file=kept["output"], snapshots=survived
    )

    # F17/F20: moving to headless installs the browser in place; no desktop is claimed.
    environment = (args.directory / ".env").read_text()
    (args.directory / ".env").write_text(
        environment.replace("YUKI_SANDBOX_PROFILE=lite", "YUKI_SANDBOX_PROFILE=headless")
    )
    deployment.compose("up", "-d")
    status = deployment.wait_mode("headless")
    expect(status["profile"] == "headless", "the profile did not change")
    expect(not status["desktop"], f"headless: {status}")
    expect(status["programs"].get("installed") is True, f"headless install: {status}")
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
    sandbox_snapshot_e2e.headless(deployment, earlier)
    deployment.record("headless_computer")

    if args.desktop:
        environment = (args.directory / ".env").read_text()
        (args.directory / ".env").write_text(
            environment.replace("YUKI_SANDBOX_PROFILE=headless", "YUKI_SANDBOX_PROFILE=desktop")
        )
        deployment.compose("up", "-d")
        status = deployment.wait_mode("headed")
        expect(status["desktop"] and status["profile"] == "desktop", f"desktop: {status}")
        sandbox_snapshot_e2e.desktop(deployment)
        deployment.record("desktop_computer", limits=status["limits"])
        computer_use(deployment, status)
    proxy_stage(deployment, gateway)
    print(f"All steps passed; record in {args.artifacts / 'steps.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
