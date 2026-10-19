"""Root configuration and real HTTPX proxy selection; no kernel or Nginx claims."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from yuki.sandbox.acp_config import load_agents

ROOT = Path(__file__).parents[2]


def product(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "deploy/characters"))
    spec = importlib.util.spec_from_file_location(
        "character_collaboration", ROOT / "deploy/characters/collaboration.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_peer_program_uses_original_cli_and_keeps_other_operator_programs(tmp_path, monkeypatch):
    module = product(monkeypatch)
    program = module.peer_program({"slug": "mika", "display_name": "Mika"}, "172.30.252.1", 18780)
    path = tmp_path / "acp-agents.toml"
    original = b'[agents.original]\r\ncommand="/usr/bin/other"\r\n# preserve operator comment\r\n'
    path.write_bytes(original)
    module.append_program(path, "mika", program)
    assert path.read_bytes().startswith(original)
    actual = load_agents(path)
    assert actual["original"].command == "/usr/bin/other"
    assert actual["mika"].args == tuple(program["args"])
    assert actual["mika"].env["NO_PROXY"] == actual["mika"].env["no_proxy"]
    assert (
        "/opt/yuki-client/bin/yuki acp --url http://172.30.252.1:18780/mika"
        in actual["mika"].args[1]
    )
    before = path.read_bytes()
    module.append_program(path, "mika", program)
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="existing ACP program differs"):
        module.append_program(path, "mika", {**program, "command": "/different"})
    assert path.read_bytes() == before
    assert tomllib.loads(before.decode())["agents"]["mika"] == program


def test_both_no_proxy_cases_reach_private_inlet_and_preserve_actual_external_proxy(monkeypatch):
    module = product(monkeypatch)
    observed = []

    def handler(label):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((label, self.path))
                body = label.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        return Handler

    inlet = ThreadingHTTPServer(("127.0.0.1", 0), handler("inlet"))
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), handler("proxy"))
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (inlet, proxy)
    ]
    for thread in threads:
        thread.start()
    try:
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("http_proxy", proxy_url)
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("https_proxy", proxy_url)
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("all_proxy", proxy_url)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        monkeypatch.setenv("no_proxy", "localhost")
        private = f"http://127.0.0.1:{inlet.server_port}/mika/client/v1/meta"
        with httpx.Client(trust_env=True) as client:
            assert client.get(private).text == "proxy"
        environment = module.peer_program(
            {"slug": "mika", "display_name": "Mika"}, "127.0.0.1", 18780
        )["env"]
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        with httpx.Client(trust_env=True) as client:
            assert client.get(private).text == "inlet"
            assert client.get("http://external.invalid/retained-proxy").text == "proxy"
        assert observed[-2:] == [
            ("inlet", "/mika/client/v1/meta"),
            ("proxy", "http://external.invalid/retained-proxy"),
        ]
        assert "NO_PROXY" in environment and "no_proxy" in environment
    finally:
        for server in (inlet, proxy):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


def test_firewall_refresh_uses_current_home_and_source_and_never_opens_other_host_ports(
    tmp_path, monkeypatch
):
    module = product(monkeypatch)
    home = tmp_path / "original-home"
    home.mkdir()
    configuration = {
        "gateway": "172.30.252.1",
        "port": 18780,
        "callers": [{"environment_name": "yuki-environment", "manager_home": str(home)}],
    }
    source = "172.30.252.2"
    rules = []
    commands = []

    def execute(*arguments, check=True):
        commands.append(arguments)
        if arguments[:2] == ("docker", "inspect"):
            value = {
                "Config": {"Labels": {"io.yuki.sandbox": "persistent-v1"}},
                "State": {"Running": True},
                "Mounts": [{"Source": str(home), "Destination": "/home/yuki"}],
                "NetworkSettings": {
                    "Networks": {
                        "yuki-sandbox-egress": {
                            "IPAddress": source,
                            "Gateway": configuration["gateway"],
                        }
                    }
                },
            }
            return subprocess.CompletedProcess(arguments, 0, json.dumps([value]), "")
        if "-F" in arguments:
            rules.clear()
        elif "-A" in arguments:
            rules.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(module, "process", execute)
    module._refresh_firewall(configuration)
    assert any("172.30.252.2" in rule and "18780" in rule and "ACCEPT" in rule for rule in rules)
    source = "172.30.252.9"
    module._refresh_firewall(configuration)
    assert not any("172.30.252.2" in rule for rule in rules)
    assert any("172.30.252.9" in rule for rule in rules)
    assert rules[-1][-2:] == ("-j", "RETURN")
    assert not any("DOCKER-USER" in command or "YUKI-EGRESS" in command for command in commands)
    assert any(command[3:6] == ("-I", "INPUT", "1") for command in commands)
    before = list(rules)
    configuration["callers"][0]["manager_home"] = str(tmp_path / "other-home")
    with pytest.raises(ValueError, match="different HOME"):
        module._refresh_firewall(configuration)
    assert rules == before
