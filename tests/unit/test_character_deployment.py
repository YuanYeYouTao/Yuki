"""Isolated character generation and original CLI request recovery; no deployment."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from dotenv import dotenv_values

from yuki.admin.authority.operators import ControlOperatorAccess
from yuki.admin.authority.principal import PrincipalSource
from yuki.admin.authority.problems import ControlQueryError
from yuki.persistence.database import Database

ROOT = Path(__file__).parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "character_deployment", ROOT / "deploy/characters/new_character.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def prepared(tmp_path, slug="mika", port=18082):
    source = tmp_path / f"inputs-{slug}"
    source.mkdir()
    persona = source / "persona.md"
    persona.write_text(f"独立角色 {slug} 的人格。")
    prompt = source / "system_prompt.md"
    prompt.write_bytes(b"generic instructions\r\n{{YUKI_PERSONA_CORE}}\r\n")
    model = ROOT / "config/model_profiles.example.toml"
    product = module()
    root = product.prepare(
        tmp_path / "instances",
        slug,
        name=slug,
        image="local-bot:verified",
        environment_image="local-environment:verified",
        port=port,
        url=f"https://example.test/roles/{slug}",
        persona=persona,
        system_prompt=prompt,
        models=model,
    )
    return product, root


async def test_prepared_roles_have_independent_private_data_and_real_operator_credentials(
    tmp_path, monkeypatch
):
    product, first = prepared(tmp_path, "yuki", 18081)
    _, second = prepared(tmp_path, "mika", 18082)
    first_values, second_values = dotenv_values(first / ".env"), dotenv_values(second / ".env")
    assert (
        first_values["YUKI_CONTROL_OPERATOR_TOKEN"] != second_values["YUKI_CONTROL_OPERATOR_TOKEN"]
    )
    assert (first / ".env").stat().st_mode & 0o777 == 0o600
    assert (second / "character/system_prompt.md").read_bytes().endswith(b"\r\n")
    assert str(first / "sandbox/home") in (first / "manager/deployment.env").read_text()
    assert str(second / "sandbox/home") in (second / "manager/deployment.env").read_text()
    assert (first / "bootstrap.json").read_text() == (second / "bootstrap.json").read_text()
    assert not list(first.glob("**/*.db"))
    (first / "data/original.db").write_bytes(b"keep old instance")
    with pytest.raises(ValueError, match="already exists"):
        product.prepare(
            tmp_path / "instances",
            "yuki",
            name="other",
            image="other",
            environment_image="other",
            port=18083,
            url="https://example.test/another",
            persona=first / "character/persona.md",
            system_prompt=first / "character/system_prompt.md",
            models=first / "config/model_profiles.toml",
        )
    assert (first / "data/original.db").read_bytes() == b"keep old instance"
    database = Database("sqlite+aiosqlite:///:memory:")
    first_access = ControlOperatorAccess(database, first / "config/control-operators.toml")
    second_access = ControlOperatorAccess(database, second / "config/control-operators.toml")
    try:
        monkeypatch.setenv(
            "YUKI_CONTROL_OPERATOR_TOKEN", first_values["YUKI_CONTROL_OPERATOR_TOKEN"]
        )
        principal = await first_access.authenticate(
            first_values["YUKI_CONTROL_OPERATOR_TOKEN"], source=PrincipalSource.CLI
        )
        assert principal.authenticated
        # Each Bot has a separate environment; select the second one's secret.
        monkeypatch.setenv(
            "YUKI_CONTROL_OPERATOR_TOKEN", second_values["YUKI_CONTROL_OPERATOR_TOKEN"]
        )
        with pytest.raises(ControlQueryError):
            await second_access.authenticate(
                first_values["YUKI_CONTROL_OPERATOR_TOKEN"], source=PrincipalSource.CLI
            )
        assert (
            await second_access.authenticate(
                second_values["YUKI_CONTROL_OPERATOR_TOKEN"], source=PrincipalSource.CLI
            )
        ).authenticated
    finally:
        await database.close()


def test_bootstrap_cold_retry_preserves_original_person_and_never_stores_pair_plaintext(tmp_path):
    product, root = prepared(tmp_path)
    receipts, calls = {}, []
    lose = {"person", "pair"}
    code = "AbC-_" + "x" * 27

    def operator(current, *arguments):
        assert current == root
        if arguments == ("character", "show"):
            return {"revision": 1}
        nonce = arguments[arguments.index("--request-id") + 1]
        state = json.loads((root / "bootstrap.json").read_text())
        assert nonce in state.values()
        calls.append((arguments, nonce))
        if arguments[:2] == ("character", "update"):
            return {"id": "original-character", "revision": 2}
        if arguments[:2] == ("person", "create"):
            original = receipts.setdefault(nonce, {"success": True, "resource_id": str(uuid4())})
            if "person" in lose:
                lose.remove("person")
                raise ConnectionError("lost after original person commit")
            return original
        if arguments[:3] == ("character", "owner", "grant"):
            return {"granted": True}
        if arguments[:2] == ("client", "pair"):
            replay = nonce in receipts
            result = receipts.setdefault(
                nonce, {"code_id": str(uuid4()), "expires_at": "2026-10-20T00:10:00Z"}
            )
            if "pair" in lose:
                lose.remove("pair")
                raise ConnectionError("lost after code issue")
            return {**result, "code": None if replay else code}
        raise AssertionError(arguments)

    product.admin = operator
    product.read_meta = lambda _base: {"character": {"id": "original-character"}}
    with pytest.raises(ConnectionError, match="person commit"):
        product.bootstrap(root)
    cold = module()
    cold.admin = operator
    cold.read_meta = lambda _base: {"character": {"id": "original-character"}}
    with pytest.raises(ConnectionError, match="code issue"):
        cold.bootstrap(root)
    again = module()
    again.admin = operator
    again.read_meta = lambda _base: {"character": {"id": "original-character"}}
    assert again.bootstrap(root) is None
    person_calls = [nonce for arguments, nonce in calls if arguments[:2] == ("person", "create")]
    assert len(person_calls) == 2 and len(set(person_calls)) == 1
    assert (
        len([1 for arguments, _ in calls if arguments[:3] == ("character", "owner", "grant")]) == 1
    )
    link = again.bootstrap(root, issue_pair=True)
    assert parse_qs(urlsplit(link).query)["code"] == [code]
    assert parse_qs(urlsplit(link).query)["server"] == ["https://example.test/roles/mika"]
    content = (root / "bootstrap.json").read_text()
    assert code not in content
    assert dotenv_values(root / ".env")["YUKI_CONTROL_OPERATOR_TOKEN"] not in content


def test_compose_context_overrides_an_unrelated_host_project_without_exposing_secret(
    tmp_path, monkeypatch
):
    product, root = prepared(tmp_path)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "original-yuki")
    monkeypatch.setenv("COMPOSE_FILE", "/do-not-use/original.yaml")
    monkeypatch.setenv("YUKI_CHARACTER_SLUG", "original-yuki")
    monkeypatch.setenv("YUKI_BOT_IMAGE", "unrelated:old")
    actual = []

    def execute(current, *arguments, environment=None):
        actual.append((current, arguments, environment))
        return "{}"

    product.command = execute
    product.admin(root, "character", "show")
    current, arguments, environment = actual[0]
    assert current == root
    assert arguments[:4] == ("docker", "compose", "--project-name", "yuki-mika")
    assert str(root / "compose.yaml") in arguments
    assert str(root / ".env") in arguments
    assert "/do-not-use/original.yaml" not in arguments
    secret = dotenv_values(root / ".env")["YUKI_CONTROL_OPERATOR_TOKEN"]
    assert secret not in " ".join(arguments)
    assert environment["YUKI_CONTROL_CREDENTIAL"] == secret
    assert environment["YUKI_CHARACTER_SLUG"] == "mika"
    assert environment["YUKI_BOT_IMAGE"] == "local-bot:verified"
