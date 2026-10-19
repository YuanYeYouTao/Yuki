"""Two real isolated backends and real CLI processes; Docker filesystem transport is adapted.

This does not assert Nginx, gVisor, the kernel firewall or native systemd behavior.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).parents[2]


def test_actual_peer_cli_preserves_ordinary_person_and_recovers_lost_reply(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "deploy/characters"))
    product = importlib.import_module("new_character")
    collaboration = importlib.import_module("configure_collaboration")
    persona, prompt = tmp_path / "persona.md", tmp_path / "system_prompt.md"
    persona.write_text("Independent role integration fixture.")
    prompt.write_text("Original instructions {{YUKI_PERSONA_CORE}}")
    registry = tmp_path / "instances"
    projects, metadata, links, children, logs = {}, {}, {}, [], []
    model = ROOT / "config/model_profiles.example.toml"
    try:
        with ExitStack() as stack:
            http = stack.enter_context(httpx.Client(trust_env=False, timeout=30))
            for slug, name in (("yuki", "Yuki"), ("mika", "Mika")):
                with socket.socket() as endpoint:
                    endpoint.bind(("127.0.0.1", 0))
                    port = endpoint.getsockname()[1]
                base = f"http://127.0.0.1:{port}/{slug}"
                project = product.prepare(
                    registry,
                    slug,
                    name=name,
                    image="local-bot",
                    environment_image="local-env",
                    port=port,
                    url=base,
                    persona=persona,
                    system_prompt=prompt,
                    models=model,
                )
                projects[slug] = project
                metadata[slug] = json.loads((project / "instance.json").read_text())
                metadata[slug]["upstream"] = base
                (project / "instance.json").write_text(json.dumps(metadata[slug]))
                root = tmp_path / (slug + "-server")
                root.mkdir()
                log = (root / "server.log").open("wb")
                logs.append(log)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tests.support.desktop_reference_server",
                        str(root),
                        str(port),
                    ],
                    cwd=ROOT,
                    stdout=log,
                    stderr=log,
                    env={
                        **os.environ,
                        "YUKI_DESKTOP_DEPLOYMENT_ROOT": str(project),
                        "YUKI_DESKTOP_BASE_PATH": "/" + slug,
                        "YUKI_CONTROL_OPERATOR_TOKEN": dotenv_values(project / ".env")[
                            "YUKI_CONTROL_OPERATOR_TOKEN"
                        ],
                    },
                )
                children.append(child)
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if http.get(base + "/healthz").is_success:
                            break
                    except httpx.TransportError:
                        pass
                    if child.poll() is not None or time.monotonic() > deadline:
                        pytest.fail("isolated real backend did not start; inspect server.log")
                    time.sleep(0.02)

            actions = []
            lose_person = False

            def actual_admin(current, *arguments, environment=None):
                nonlocal lose_person
                slug = current.name
                at = arguments.index("yuki")
                command = arguments[at + 1 :]
                actions.append((slug, command))
                result = subprocess.run(
                    [str(Path(sys.executable).with_name("yuki")), *command],
                    cwd=current,
                    env={
                        **environment,
                        "ADMIN_API_URL": metadata[slug]["public_url"],
                        "BOT_PERSONA_FILE": str(persona),
                        "SYSTEM_PROMPT_FILE": str(prompt),
                        "MODEL_PROFILES_FILE": str(model),
                        "DATABASE_URL": "sqlite+aiosqlite:///"
                        + str(tmp_path / (slug + "-server") / "test.sqlite3"),
                    },
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if lose_person and command[:3] == ("admin", "person", "create"):
                    lose_person = False
                    raise subprocess.CalledProcessError(99, command)
                return result.stdout

            monkeypatch.setattr(product, "command", actual_admin)
            for slug, project in projects.items():
                links[slug] = product.bootstrap(project)
            assert (
                json.loads((projects["yuki"] / "bootstrap.json").read_text())["character_id"]
                != json.loads((projects["mika"] / "bootstrap.json").read_text())["character_id"]
            )

            homes = {slug: tmp_path / (slug + "-home") for slug in projects}
            for home in homes.values():
                home.mkdir()

            def actual_client(caller, *arguments, environment=None, input_text=None):
                # Keep real CLI/HTTP; adapt the container installation and HOME paths.
                home = homes[caller["slug"]]
                args = [
                    argument.replace("/home/yuki", str(home))
                    .replace("/opt/yuki-client/bin/python", sys.executable)
                    .replace(
                        "/opt/yuki-client/bin/yuki", str(Path(sys.executable).with_name("yuki"))
                    )
                    for argument in arguments
                ]
                return subprocess.run(
                    args,
                    env=environment,
                    input=input_text,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout

            monkeypatch.setattr(collaboration, "execute", actual_client)
            caller, callee = metadata["yuki"], metadata["mika"]
            port = callee["http_port"]
            lose_person = True
            with pytest.raises(subprocess.CalledProcessError):
                collaboration.connect_peer(registry, caller, callee, "127.0.0.1", port)
            person_requests = [
                command
                for slug, command in actions
                if slug == "mika" and command[:3] == ("admin", "person", "create")
            ]
            original_peer_request = person_requests[-1]
            original_store = collaboration.store_token

            def lose_after_store(*args, **kwargs):
                original_store(*args, **kwargs)
                raise subprocess.CalledProcessError(98, ["lost-after-token-publication"])

            monkeypatch.setattr(collaboration, "store_token", lose_after_store)
            with pytest.raises(subprocess.CalledProcessError):
                collaboration.connect_peer(registry, caller, callee, "127.0.0.1", port)
            monkeypatch.setattr(collaboration, "store_token", original_store)
            state = collaboration.connect_peer(registry, caller, callee, "127.0.0.1", port)
            assert state["connected"] is True
            assert [
                command
                for slug, command in actions
                if slug == "mika" and command[:3] == ("admin", "person", "create")
            ][-1] == original_peer_request
            owner = json.loads((projects["mika"] / "bootstrap.json").read_text())
            assert state["person_id"] != owner["owner_person_id"]
            token_path = homes["yuki"] / ".config/yuki-peers/mika/token"
            assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
            token = token_path.read_text()
            me = http.get(
                callee["public_url"] + "/client/v1/me", headers={"Authorization": "Bearer " + token}
            ).json()["data"]
            assert me["request_person_id"] == state["person_id"]
            profile = http.get(
                callee["public_url"] + "/client/v1/character",
                headers={"Authorization": "Bearer " + token},
            ).json()["data"]
            assert profile["viewer"]["is_owner"] is False
            before = len(actions)
            assert collaboration.connect_peer(registry, caller, callee, "127.0.0.1", port) == state
            assert len(actions) == before
            token_path.unlink()
            with pytest.raises(ValueError, match="credential missing"):
                collaboration.connect_peer(registry, caller, callee, "127.0.0.1", port)
            repaired = collaboration.connect_peer(
                registry, caller, callee, "127.0.0.1", port, replace_credential=True
            )
            assert repaired["person_id"] == state["person_id"]
            assert repaired["device_id"] != state["device_id"]
            assert (
                http.get(
                    callee["public_url"] + "/client/v1/me",
                    headers={"Authorization": "Bearer " + token},
                ).status_code
                == 401
            )
            assert token_path.read_text() != token
            peer_state = (projects["yuki"] / "manager/peers/mika.json").read_text()
            assert token not in peer_state and token_path.read_text() not in peer_state
            assert not (homes["mika"] / ".config/yuki-peers/yuki/token").exists()
            code = parse_qs(urlsplit(links["mika"]).query)["code"][0]
            owner_device = http.post(
                callee["public_url"] + "/client/v1/pair",
                json={"code": code, "device_label": "owner"},
            ).json()["data"]
            owner_me = http.get(
                callee["public_url"] + "/client/v1/me",
                headers={"Authorization": "Bearer " + owner_device["token"]},
            ).json()["data"]
            assert owner_me["conversation_id"] != me["conversation_id"]
            forbidden = http.get(
                callee["public_url"]
                + "/client/v1/conversations/"
                + owner_me["conversation_id"]
                + "/events",
                headers={"Authorization": "Bearer " + token_path.read_text()},
            )
            assert forbidden.status_code in {403, 404}
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.wait(timeout=10)
        for log in logs:
            log.close()
