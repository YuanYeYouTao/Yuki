"""Real CLI subprocesses -> TCP operator API -> original SQLite, without Docker."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).parents[2]


async def test_character_bootstrap_preserves_original_person_receipt_over_actual_cli_and_http(
    tmp_path,
):
    spec = importlib.util.spec_from_file_location(
        "character_cli_deployment", ROOT / "deploy/characters/new_character.py"
    )
    product = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(product)
    persona, prompt = tmp_path / "persona.md", tmp_path / "system_prompt.md"
    persona.write_text("Independent integration role.")
    prompt.write_text("Original task instructions {{YUKI_PERSONA_CORE}}")
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        port = endpoint.getsockname()[1]
    base = f"http://127.0.0.1:{port}/role/yuki"
    project = product.prepare(
        tmp_path / "instances",
        "mika",
        name="Mika",
        image="local-verified-bot",
        environment_image="local-verified-environment",
        port=port,
        url=base,
        persona=persona,
        system_prompt=prompt,
        models=ROOT / "config/model_profiles.example.toml",
    )
    values = dotenv_values(project / ".env")
    server_root = tmp_path / "server"
    server_root.mkdir()
    log = (tmp_path / "server.log").open("wb")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.support.desktop_reference_server",
        str(server_root),
        str(port),
        cwd=ROOT,
        stdout=log,
        stderr=log,
        env={
            **os.environ,
            "YUKI_DESKTOP_DEPLOYMENT_ROOT": str(project),
            "YUKI_CONTROL_OPERATOR_TOKEN": values["YUKI_CONTROL_OPERATOR_TOKEN"],
        },
    )
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as http:
            async with asyncio.timeout(30):
                while True:
                    try:
                        if (await http.get(base + "/healthz")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.02)
            original = (await http.get(base + "/client/v1/meta")).json()["data"]["character"]["id"]
            actions = []
            lose = True

            def actual_cli(current, *arguments, environment=None):
                nonlocal lose
                assert current == project
                assert arguments[:4] == ("docker", "compose", "--project-name", "yuki-mika")
                at = arguments.index("yuki")
                command = arguments[at + 1 :]
                actions.append(command)
                child_environment = {
                    **environment,
                    "ADMIN_API_URL": base,
                    "BOT_PERSONA_FILE": str(persona),
                    "SYSTEM_PROMPT_FILE": str(prompt),
                    "MODEL_PROFILES_FILE": str(ROOT / "config/model_profiles.example.toml"),
                    "DATABASE_URL": f"sqlite+aiosqlite:///{server_root / 'test.sqlite3'}",
                    "NO_PROXY": "127.0.0.1,localhost,::1",
                    "no_proxy": "127.0.0.1,localhost,::1",
                }
                result = subprocess.run(
                    [str(Path(sys.executable).with_name("yuki")), *command],
                    cwd=project,
                    env=child_environment,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if command[:3] == ("admin", "person", "create") and lose:
                    lose = False
                    raise subprocess.CalledProcessError(99, command)
                return result.stdout

            product.command = actual_cli
            with pytest.raises(subprocess.CalledProcessError):
                product.bootstrap(project)
            link = product.bootstrap(project)
            state = json.loads((project / "bootstrap.json").read_text())
            assert state["character_id"] == original
            assert state["owner_ready"] is True
            created = [action for action in actions if action[:3] == ("admin", "person", "create")]
            assert len(created) == 2
            assert created[0] == created[1]
            code = parse_qs(urlsplit(link).query)["code"][0]
            paired = (
                await http.post(
                    base + "/client/v1/pair",
                    json={"code": code, "device_label": "actual bootstrap owner"},
                )
            ).json()["data"]
            me = (
                await http.get(
                    base + "/client/v1/me", headers={"Authorization": "Bearer " + paired["token"]}
                )
            ).json()["data"]
            assert me["request_person_id"] == state["owner_person_id"]
            profile = (
                await http.get(
                    base + "/client/v1/character",
                    headers={"Authorization": "Bearer " + paired["token"]},
                )
            ).json()["data"]
            assert profile["id"] == original
            assert profile["display_name"] == "Mika" and profile["slug"] == "mika"
            assert profile["viewer"]["is_owner"] is True
            content = (project / "bootstrap.json").read_text()
            assert code not in content and paired["token"] not in content
            # Setup itself leaves new roles without a default image. Existing-instance
            # upgrades are an explicit operator command, bound to the proven character.
            assert profile["avatar"] is None and profile["default_avatar"] is None
            from PIL import Image

            source, alternate = tmp_path / "original.png", tmp_path / "selected.png"
            Image.new("RGB", (400, 300), "blue").save(source)
            Image.new("RGB", (300, 400), "red").save(alternate)
            state_file = tmp_path / "avatar-upgrade.json"
            initialize = (
                "character",
                "avatar",
                "initialize-existing",
                str(source),
                "--state-file",
                str(state_file),
                "--character-id",
                original,
            )
            assert product.admin(project, *initialize)["status"] == "initialized"
            initialized = product.admin(project, "character", "show")
            assert initialized["id"] == original and initialized["avatar_revision"] == 2
            assert initialized["avatar"]["sha256"] == initialized["default_avatar"]["sha256"]
            selected = product.admin(
                project, "character", "avatar", "set", str(alternate), "--revision", "2"
            )
            assert product.admin(project, *initialize)["status"] == "already_initialized"
            assert product.admin(project, "character", "show")["avatar"] == selected["avatar"]
            assert values["YUKI_CONTROL_OPERATOR_TOKEN"] not in state_file.read_text()

    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), 10)
        log.close()
