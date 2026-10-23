"""The desktop uses real setup persistence and does not overwrite an installation."""

import tomllib
from pathlib import Path

import pytest

from yuki.deployment_setup import service
from yuki.deployment_setup.desktop import DesktopConfiguration, configure
from yuki.deployment_setup.service import EnvironmentDocument, SetupPaths


def test_desktop_setup_prepares_app_only_without_platform_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / ".env.example").write_bytes((root / ".env.example").read_bytes())
    (tmp_path / "config").mkdir()
    (tmp_path / "config/SOUL.md").write_bytes((root / "config/SOUL.md").read_bytes())
    monkeypatch.chdir(tmp_path)
    body = DesktopConfiguration(
        protocol="chat_completions",
        base_url="http://host.docker.internal:12345/v1",
        api_key="isolated-local-test",
        model="local-test-model",
    )
    paths = SetupPaths(tmp_path)
    assert configure(paths, body)["configured"] is True
    environment = EnvironmentDocument.load(paths).values()
    assert environment["COMPOSE_PROFILES"] == ""
    assert environment["ONEBOT_ACCESS_TOKEN"] == ""
    assert environment["TELEGRAM_BOT_TOKEN"] == ""
    assert environment["FEISHU_APP_ID"] == ""
    assert environment["LLM_MODEL"] == "local-test-model"
    assert "isolated-local-test" not in paths.model_profiles.read_text("utf-8")
    assert len(environment["YUKI_CONTROL_OPERATOR_TOKEN"]) >= 32
    assert paths.soul.read_bytes() == paths.soul_template.read_bytes()

    before = {path: path.read_bytes() for path in (paths.env, paths.model_profiles, paths.soul)}
    changed = body.model_copy(update={"model": "must-not-replace-existing"})
    assert configure(paths, changed)["preserved"] is True
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("interrupted_file", ["model_profiles.toml", ".env"])
def test_desktop_setup_resumes_interrupted_first_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted_file: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / ".env.example").write_bytes((root / ".env.example").read_bytes())
    (tmp_path / "config").mkdir()
    (tmp_path / "config/SOUL.md").write_bytes((root / "config/SOUL.md").read_bytes())
    monkeypatch.chdir(tmp_path)
    paths = SetupPaths(tmp_path)
    body = DesktopConfiguration(
        protocol="chat_completions", base_url="http://localhost:12345/v1", model="local"
    )
    write = service._atomic_write

    def interrupted(path: Path, content: bytes, *, private: bool) -> None:
        if path.name == interrupted_file:
            raise SystemExit("process interrupted")
        write(path, content, private=private)

    monkeypatch.setattr(service, "_atomic_write", interrupted)
    with pytest.raises(SystemExit, match="process interrupted"):
        configure(paths, body)
    assert not paths.env.exists()
    policy_path = tmp_path / "config/control-operators.toml"
    original_policy = policy_path.read_bytes()
    original_profiles = paths.model_profiles.read_bytes() if paths.model_profiles.exists() else None
    monkeypatch.setattr(service, "_atomic_write", write)
    assert configure(paths, body)["configured"] is True
    assert policy_path.read_bytes() == original_policy
    if original_profiles is not None:
        assert paths.model_profiles.read_bytes() == original_profiles
    environment = EnvironmentDocument.load(paths).values()
    assert len(environment["YUKI_CONTROL_OPERATOR_TOKEN"]) >= 32
    assert tomllib.loads(paths.model_profiles.read_text("utf-8"))["profiles"]
    assert configure(paths, body)["preserved"] is True


def test_interrupted_operator_setup_reuses_supplied_token(tmp_path: Path) -> None:
    paths = SetupPaths(tmp_path)
    environment: dict[str, str] = {}
    policy = service.prepare_operator_configuration(paths, environment)
    assert policy is not None
    policy_path = tmp_path / environment["CONTROL_OPERATORS_FILE"]
    policy_path.parent.mkdir()
    policy_path.write_text(policy, encoding="utf-8")
    token = environment["YUKI_CONTROL_OPERATOR_TOKEN"]
    retry = {"YUKI_CONTROL_OPERATOR_TOKEN": token}
    assert service.prepare_operator_configuration(paths, retry) is None
    assert retry["YUKI_CONTROL_OPERATOR_TOKEN"] == token
    assert policy_path.read_text("utf-8") == policy
