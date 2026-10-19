"""Read-only registration of an existing deployment using an actual HTTP C1 exchange."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).parents[2]


def test_adoption_references_original_files_and_compose_chain_without_reinitializing(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(ROOT / "deploy/characters"))
    import new_character

    spec = importlib.util.spec_from_file_location(
        "adopt_existing", ROOT / "deploy/characters/adopt_existing.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = tmp_path / "original"
    original.mkdir()
    (original / "data").mkdir()
    database = original / "data/original.db"
    database.write_bytes(b"old identity, history, memory and receipts")
    compose = original / "compose.yaml"
    compose.write_text("services: {}\n")
    override = original / "production.yaml"
    override.write_text("services: {}\n")
    env = original / ".env"
    secret = "operator-secret-" + "x" * 32
    env.write_text(f"YUKI_CONTROL_OPERATOR_TOKEN='{secret}'\n")
    manager = tmp_path / "original-manager"
    home = manager / "home"
    home.mkdir(parents=True)
    (home / "installed-software.txt").write_text("preserve me")
    identity, calls = str(uuid4()), []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            value = {
                "data": {
                    "character": {"id": identity, "display_name": "Original Yuki"},
                    "server_version": "test",
                    "api_revision": 5,
                }
            }
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        root = module.adopt(
            tmp_path / "registry",
            "yuki",
            project_root=original,
            compose_project="original-project",
            compose_files=[compose, override],
            env_file=env,
            manager_root=manager,
            home=home,
            environment_name="yuki-environment",
            socket=manager / "manager.sock",
            upstream=base + "/private",
            url=base + "/public",
        )
        value = json.loads((root / "instance.json").read_text())
        assert value["mode"] == "existing"
        assert value["character_id"] == identity
        assert value["compose_files"] == [str(compose), str(override)]
        assert value["manager_home"] == str(home)
        assert not (root / "data").exists()
        assert not (root / ".env").exists()
        assert secret not in (root / "instance.json").read_text()
        commands = []

        def execute(current, *arguments, environment=None):
            commands.append((current, arguments, environment))
            return "{}"

        monkeypatch.setattr(new_character, "command", execute)
        new_character.admin(root, "character", "show")
        cwd, arguments, environment = commands[0]
        assert cwd == original
        assert arguments[:4] == ("docker", "compose", "--project-name", "original-project")
        assert str(compose) in arguments and str(override) in arguments
        assert environment["YUKI_CONTROL_CREDENTIAL"] == secret
        monkeypatch.setattr(new_character.os, "geteuid", lambda: 0)
        with pytest.raises(ValueError, match="adopted"):
            new_character.start(root)
        assert len(commands) == 1
        assert database.read_bytes() == b"old identity, history, memory and receipts"
        assert (home / "installed-software.txt").read_text() == "preserve me"
        assert calls == [("/public/client/v1/meta", None), ("/private/client/v1/meta", None)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
