"""Operator-owned client relay and ACP wiring for independent character instances."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from new_character import (
    locked,
    public_url,
    valid_slug,
)

NETWORK = "yuki-sandbox-egress"
BRIDGE = "yuki-egress0"
CHAIN = "YUKI-COLLAB"


def instance(registry: Path, slug: str) -> dict:
    value = json.loads((registry / valid_slug(slug) / "instance.json").read_text())
    if value["slug"] != slug:
        raise ValueError("registry identity mismatch")
    return value


def relay_config(instances: list[dict], gateway: str, port: int) -> str:
    gateway = str(ipaddress.IPv4Address(gateway))
    if not 1 <= port <= 65535:
        raise ValueError("invalid relay port")
    lines = [
        "# Operator-owned Yuki collaboration inlet; client API only.",
        "server {",
        f"    listen {gateway}:{port};",
        "    server_name _;",
        "    location / { return 404; }",
    ]
    for value in sorted(instances, key=lambda item: item["slug"]):
        slug = valid_slug(value["slug"])
        base = urlsplit(public_url(value["upstream"]))
        if base.scheme != "http":
            raise ValueError("the same-host relay requires a private HTTP upstream")
        path = quote(base.path.rstrip("/") + "/client/v1/", safe="/%~.-_")
        upstream = urlunsplit((base.scheme, base.netloc, path, "", ""))
        lines.extend(
            [
                f"    location ^~ /{slug}/client/v1/ {{",
                f"        proxy_pass {json.dumps(upstream)};",
                "        proxy_http_version 1.1;",
                "        proxy_buffering off;",
                "        proxy_set_header Upgrade $http_upgrade;",
                '        proxy_set_header Connection "upgrade";',
                "    }",
            ]
        )
    return "\n".join([*lines, "}", ""])


def peer_program(callee: dict, gateway: str, port: int) -> dict:
    slug = valid_slug(callee["slug"])
    gateway = str(ipaddress.IPv4Address(gateway))
    if not 1 <= port <= 65535:
        raise ValueError("invalid relay port")
    base = f"http://{gateway}:{port}/{slug}"
    token = f"/home/yuki/.config/yuki-peers/{slug}/token"
    state = f"/home/yuki/.yuki-peers/{slug}"
    no_proxy = f"{gateway},localhost,127.0.0.1,::1"
    launch = (
        f'YUKI_CLIENT_TOKEN="$(cat {token})" '
        f"exec /opt/yuki-client/bin/yuki acp --url {base} --state-dir {state}/acp"
    )
    description = (
        f"{callee['display_name']}：独立角色与私人会话。成果保留远端 ResourceLink；"
        f"可在原环境用 /opt/yuki-client/bin/yuki client --url {base} "
        f"--state {state}/outbox.sqlite3 artifact <id> --version <sha> --output <path> "
        f"核对下载；YUKI_CLIENT_TOKEN 从 {token} 读取。"
    )
    return {
        "command": "/bin/sh",
        "args": ["-c", launch],
        "cwd": "/workspace",
        "description": description,
        "env": {"NO_PROXY": no_proxy, "no_proxy": no_proxy},
        "permission_mode": "allow",
    }


def program_text(slug: str, program: dict) -> str:
    lines = [f"[agents.{valid_slug(slug)}]"]
    for key in ["command", "args", "cwd", "description", "permission_mode"]:
        lines.append(f"{key}={json.dumps(program[key], ensure_ascii=False)}")
    environment = ", ".join(f"{name}={json.dumps(value)}" for name, value in program["env"].items())
    lines.append(f"env={{ {environment} }}")
    return "\n".join(lines) + "\n"


def append_program(path: Path, slug: str, program: dict) -> None:
    with locked(path.parent / ".acp-programs.lock"):
        _append_program(path, slug, program)


def _append_program(path: Path, slug: str, program: dict) -> None:
    if path.is_symlink():
        raise ValueError("preserve and review the operator program file symlink")
    content = path.read_bytes().decode("utf-8") if path.exists() else ""
    previous = tomllib.loads(content).get("agents", {}).get(slug)
    if previous is not None:
        if previous != program:
            raise ValueError("existing ACP program differs; preserve and review it explicitly")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".peer-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content + ("\n" if content and not content.endswith("\n") else ""))
            stream.write(program_text(slug, program))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, capture_output=True, text=True, check=check)


def source_addresses(configuration: dict) -> list[tuple[str, str]]:
    selected = []
    for caller in configuration["callers"]:
        found = process("docker", "inspect", caller["environment_name"], check=False)
        if found.returncode:
            # A stopped/deleted environment grants no source. Other Docker failures
            # must not flush the last known configuration and pretend to succeed.
            if "No such" in found.stderr:
                continue
            raise RuntimeError("cannot inspect original caller environment")
        value = json.loads(found.stdout)[0]
        if value.get("Config", {}).get("Labels", {}).get("io.yuki.sandbox") != "persistent-v1":
            raise ValueError("caller environment label mismatch")
        homes = [
            item for item in value.get("Mounts", []) if item.get("Destination") == "/home/yuki"
        ]
        if (
            len(homes) != 1
            or Path(homes[0]["Source"]).resolve() != Path(caller["manager_home"]).resolve()
        ):
            raise ValueError("caller environment points to a different HOME")
        if not value.get("State", {}).get("Running"):
            continue
        network = value.get("NetworkSettings", {}).get("Networks", {}).get(NETWORK)
        if not network or not network.get("IPAddress"):
            raise ValueError("caller is not attached to the configured egress network")
        source = str(ipaddress.IPv4Address(network["IPAddress"]))
        gateway = str(ipaddress.IPv4Address(network["Gateway"]))
        if gateway != configuration["gateway"]:
            raise ValueError("egress gateway changed; regenerate the listener configuration")
        selected.append((source, gateway))
    return sorted(set(selected))


def refresh_firewall(configuration: dict | Path) -> None:
    if os.geteuid() != 0:
        raise ValueError("the host firewall requires root")
    with locked(Path("/run/yuki-sandbox/collaboration-firewall.lock")):
        current = (
            json.loads(configuration.read_text())
            if isinstance(configuration, Path)
            else configuration
        )
        _refresh_firewall(current)


def _refresh_firewall(configuration: dict) -> None:
    selected = source_addresses(configuration)
    created = process("iptables", "-w", "10", "-N", CHAIN, check=False)
    if created.returncode:
        process("iptables", "-w", "10", "-L", CHAIN)
    process("iptables", "-w", "10", "-F", CHAIN)
    for source, gateway in selected:
        process(
            "iptables",
            "-w",
            "10",
            "-A",
            CHAIN,
            "-s",
            source,
            "-d",
            gateway,
            "-p",
            "tcp",
            "--dport",
            str(configuration["port"]),
            "-j",
            "ACCEPT",
        )
    process("iptables", "-w", "10", "-A", CHAIN, "-j", "RETURN")
    existing = process(
        "iptables", "-w", "10", "-C", "INPUT", "-i", BRIDGE, "-j", CHAIN, check=False
    )
    if not existing.returncode:
        process("iptables", "-w", "10", "-D", "INPUT", "-i", BRIDGE, "-j", CHAIN)
    process("iptables", "-w", "10", "-I", "INPUT", "1", "-i", BRIDGE, "-j", CHAIN)


def watch(configuration_path: Path) -> None:
    since = str(time.time())
    refresh_firewall(configuration_path)
    with subprocess.Popen(
        [
            "docker",
            "events",
            "--since",
            since,
            "--filter",
            "type=container",
            "--filter",
            "event=start",
            "--filter",
            "event=die",
            "--filter",
            "event=destroy",
            "--filter",
            "label=io.yuki.sandbox=persistent-v1",
            "--format",
            "{{json .}}",
        ],
        stdout=subprocess.PIPE,
        text=True,
    ) as events:
        assert events.stdout is not None
        for line in events.stdout:
            value = json.loads(line)
            current = json.loads(configuration_path.read_text())
            names = {item["environment_name"] for item in current["callers"]}
            if value.get("Actor", {}).get("Attributes", {}).get("name") in names:
                refresh_firewall(configuration_path)
        raise RuntimeError("Docker event stream ended; restart the firewall observer")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firewall", type=Path)
    parser.add_argument("--watch", type=Path)
    args = parser.parse_args()
    if args.firewall:
        refresh_firewall(args.firewall)
    elif args.watch:
        watch(args.watch)
    else:
        parser.error("select --firewall or --watch")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as failure:
        raise SystemExit(str(failure)) from None
