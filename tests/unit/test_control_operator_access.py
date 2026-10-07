"""Only server-configured credentials produce trusted management principals."""

import json
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.control_plane import ControlQueryError, PrincipalSource, ProblemCode
from qq_ai_bot.control_plane.capabilities import is_protocol_capability


def operator_file(
    tmp_path, *, principal=None, capabilities=("control.plugin.read",), enabled=True, person=None
):
    path = tmp_path / "operators.toml"
    identity = principal or str(uuid4())
    path.write_text(
        "[[operators]]\n"
        + f'principal_id = "{identity}"\ntoken_env = "YUKI_TEST_OPERATOR_TOKEN"\n'
        + f'enabled = {str(enabled).lower()}\nroles = ["superuser"]\n'
        + f"capabilities = {json.dumps(capabilities)}\n"
        + (f'person_id = "{person}"\n' if person else ""),
        encoding="utf-8",
    )
    return path, identity


@pytest.mark.asyncio
async def test_credentials_without_qq_create_only_explicit_grants(database, tmp_path, monkeypatch):
    secret = "fixture-operator-credential-" + "a" * 32
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", secret)
    path, identity = operator_file(tmp_path)
    access = ControlOperatorAccess(database, path)
    principal = await access.authenticate(secret, source=PrincipalSource.FUTURE_WEB)
    assert principal.principal_id.text == identity and principal.person_id is None
    assert principal.allows("control.plugin.read")
    assert not principal.allows("control.plugin.mutate")  # role names never grant capabilities
    for invalid in (None, {"principal_id": identity, "roles": ["superuser"]}, "wrong" * 20):
        with pytest.raises(ControlQueryError) as exc:
            await access.authenticate(invalid, source=PrincipalSource.FUTURE_WEB)
        assert exc.value.problem.code is ProblemCode.UNAUTHENTICATED
    with pytest.raises(ValueError):
        await access.authenticate(secret, source=PrincipalSource.QQ)
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", "rotated-" + "b" * 40)
    with pytest.raises(ControlQueryError):
        await access.authenticate(secret, source=PrincipalSource.FUTURE_WEB)
    assert secret not in repr(access) + repr(principal) + path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_unconfigured_disabled_and_unproven_person_fail_closed(
    database, tmp_path, monkeypatch
):
    secret = "s" * 48
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", secret)
    for mode in ("unconfigured", "disabled", "unproven_person"):
        path = None
        if mode == "disabled":
            path = operator_file(tmp_path, enabled=False)[0]
        elif mode == "unproven_person":
            path = operator_file(tmp_path, person=str(uuid4()))[0]
        with pytest.raises(ControlQueryError) as exc:
            await ControlOperatorAccess(database, path).authenticate(
                secret, source=PrincipalSource.CLI
            )
        expected = (
            ProblemCode.PRECONDITION_FAILED if mode == "disabled" else ProblemCode.UNAUTHENTICATED
        )
        assert exc.value.problem.code is expected


@pytest.mark.parametrize(
    "capability",
    [
        "*",
        "plugin.execute",
        "secret.read",
        "database.sql",
        "control.fake.read",
        "control.mcp.read",
        "control.mcp.mutate",
        "mcp.web_search",
    ],
)
def test_server_config_cannot_grant_unreviewed_or_intrinsic_dangerous_capabilities(
    database, tmp_path, capability
):
    path, _ = operator_file(tmp_path, capabilities=(capability,))
    with pytest.raises(ValueError, match="invalid control operator configuration"):
        ControlOperatorAccess(database, path)


@pytest.mark.asyncio
async def test_shipped_operator_template_loads_only_current_explicit_grants(database, monkeypatch):
    path, declarations = _shipped_operator_template()
    access = ControlOperatorAccess(database, path)
    for declaration in declarations:
        capabilities = tuple(declaration["capabilities"])
        assert capabilities and all(is_protocol_capability(item) for item in capabilities)
        assert not any(item.startswith(("control.mcp.", "mcp.")) for item in capabilities)
        credential = "synthetic-template-credential-" + "a" * 32
        monkeypatch.setenv(declaration["token_env"], credential)
        principal = await access.authenticate(credential, source=PrincipalSource.CLI)
        assert principal.principal_id.text == declaration["principal_id"]
        assert principal.granted_capabilities == frozenset(capabilities)
        assert not principal.allows("control.plugin.mutate")


def _shipped_operator_template():
    path = Path(__file__).resolve().parents[2] / "config/control-operators.example.toml"
    return path, tomllib.loads(path.read_text(encoding="utf-8"))["operators"]
