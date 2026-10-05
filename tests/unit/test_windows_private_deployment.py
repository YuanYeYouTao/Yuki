"""Deployment regressions: real Settings/routes, original files and secret boundaries."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import tomllib
from pathlib import Path

import pytest

from qq_ai_bot.application.control_access import ControlOperatorAccess
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import ReasoningEffort
from qq_ai_bot.model_runtime.models import ModelCapability, ModelTask
from qq_ai_bot.model_runtime.profiles import (
    load_model_profile_catalog,
    model_profile_environment,
)
from qq_ai_bot.persistence.database import Database

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "windows_deployment", ROOT / "deploy/windows/deployment.py"
)
assert spec and spec.loader
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


@pytest.fixture
def configuration_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    bundle = tmp_path / "bundle"
    (bundle / "private").mkdir(parents=True)
    deployment.private_write(
        bundle / "private/provider.json",
        json.dumps(
            {
                "base_url": "https://provider.example/v1",
                "api_key": "test-key-private",
                "model": "test-claude",
            }
        ),
    )
    deployment.private_write(bundle / "private/persona.md", "完整角色提示词，不混入 Yuki 人格。")
    (bundle / "manifest.json").write_text(json.dumps({"bot_qq": "876543210"}))
    artifacts = tmp_path / "artifacts.json"
    artifacts.write_text(
        json.dumps({"worker": {"sha256": "a" * 64}, "launcher": {"sha256": "b" * 64}})
    )
    app = tmp_path / "app"
    return bundle, app, artifacts


def test_generated_configuration_uses_real_runtime_contract(configuration_inputs, monkeypatch):
    bundle, app, artifacts = configuration_inputs
    deployment.configure(bundle, app, "123456789", artifacts)
    monkeypatch.chdir(app)
    settings = Settings(_env_file=app / ".env")
    assert settings.superusers == frozenset({"123456789"})
    assert settings.bot_display_name == "波奇"
    assert (
        settings.system_prompt.strip()
        == settings.bot_persona
        == "完整角色提示词，不混入 Yuki 人格。"
    )
    assert settings.code_mode_worker_sha256 == "a" * 64
    assert settings.code_mode_launcher_sha256 == "b" * 64
    assert (
        settings.runtime_work_enabled and settings.automation_enabled and settings.subagents_enabled
    )
    catalog = load_model_profile_catalog(
        settings.model_profiles_file,
        legacy_provider=settings.llm_provider,
        legacy_base_url=settings.llm_base_url,
        legacy_model=settings.llm_model,
        legacy_timeout_seconds=120,
        legacy_max_retries=0,
        legacy_temperature=0.7,
        legacy_max_output_tokens=8192,
        legacy_thinking_enabled=True,
        environment=model_profile_environment(settings),
    )
    assert set(catalog.routes) == set(ModelTask)
    assert ModelCapability.IMAGE_INPUT in catalog.profiles["main"].capabilities
    assert catalog.profiles["main"].reasoning_effort is ReasoningEffort.HIGH
    assert catalog.profiles["background"].reasoning_effort is ReasoningEffort.LOW
    assert catalog.profiles["main"].wire_options.reasoning == "budget"
    assert "test-key-private" not in settings.model_profiles_file.read_text()
    policy = tomllib.loads(settings.control_operators_file.read_text())
    assert len(policy["operators"]) == 1
    database = Database("sqlite+aiosqlite:///:memory:")
    ControlOperatorAccess(database, settings.control_operators_file)
    for file in (app / ".env", app / "management.txt", app / "gateway/.env"):
        assert file.stat().st_mode & 0o777 == 0o600


def test_repeated_configuration_preserves_keys_and_operator_edits(configuration_inputs):
    bundle, app, artifacts = configuration_inputs
    deployment.configure(bundle, app, "123456789", artifacts)
    persona = app / "webui-config/persona.md"
    persona.write_text("用户自己的后续修改")
    before = {file: file.read_bytes() for file in app.rglob("*") if file.is_file()}
    deployment.configure(bundle, app, "987654321", artifacts)
    assert all(file.read_bytes() == data for file, data in before.items())


@pytest.mark.parametrize("admin", ["", "bad", "876543210", "1", "12\n34"])
def test_bot_is_never_implicitly_its_own_superuser(configuration_inputs, admin):
    bundle, app, artifacts = configuration_inputs
    with pytest.raises(ValueError):
        deployment.configure(bundle, app, admin, artifacts)
    assert not app.exists()


def test_credential_markdown_is_data_and_native_endpoint_is_explicit(tmp_path):
    path = tmp_path / "provider.md"
    path.write_text(
        "| base_url (Anthropic) | `https://provider.example` |\n"
        "| api_key | sk-test-secret |\n| model | test-claude |\n"
    )
    assert deployment.read_credentials(path) == {
        "base_url": "https://provider.example/v1",
        "api_key": "sk-test-secret",
        "model": "test-claude",
    }
    path.write_text(
        "| base_url (Anthropic) | https://user:secret@provider.example |\n"
        "| api_key | secret |\n| model | test |"
    )
    with pytest.raises(ValueError, match="credentials"):
        deployment.read_credentials(path)


def test_bundle_tampering_and_foreign_application_content_are_refused(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = bundle / "source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        content = b"packaged source"
        item = tarfile.TarInfo("source.txt")
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))
    manifest = {
        "source_revision": "c" * 40,
        "files": {"source.tar.gz": deployment.hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    app = tmp_path / "app"
    deployment.extract_source(bundle, app)
    (app / "source.txt").write_text("later user edit")
    deployment.extract_source(bundle, app)
    assert (app / "source.txt").read_text() == "later user edit"
    marker = app / ".yuki-source-revision"
    marker.write_text("d" * 40)
    with pytest.raises(ValueError, match="different revision"):
        deployment.extract_source(bundle, app)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "original.txt").write_text("keep")
    with pytest.raises(ValueError, match="ownership"):
        deployment.extract_source(bundle, foreign)
    assert (foreign / "original.txt").read_text() == "keep"
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        deployment.verify_bundle(bundle)


def test_bundle_manifest_cannot_read_outside_package(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"files": {"../outside": "a" * 64}}))
    with pytest.raises(ValueError, match="manifest"):
        deployment.verify_bundle(bundle)


def test_configuration_failure_is_atomic_and_can_retry(configuration_inputs, monkeypatch):
    bundle, app, artifacts = configuration_inputs
    original = deployment.private_write

    def fail(path, text):
        if path.name == "model_profiles.toml":
            raise OSError("simulated interrupted write")
        return original(path, text)

    monkeypatch.setattr(deployment, "private_write", fail)
    with pytest.raises(OSError):
        deployment.configure(bundle, app, "123456789", artifacts)
    assert not (app / ".env").exists() and not (app / "webui-config").exists()
    assert not list(app.glob(".yuki-config-*"))
    monkeypatch.setattr(deployment, "private_write", original)
    deployment.configure(bundle, app, "123456789", artifacts)
    assert (app / ".env").is_file()


def test_configuration_retry_repairs_missing_links(configuration_inputs):
    bundle, app, artifacts = configuration_inputs
    deployment.configure(bundle, app, "123456789", artifacts)
    original = (app / ".env").read_bytes()
    (app / ".env").unlink()
    (app / "gateway/.env").unlink()
    deployment.configure(bundle, app, "987654321", artifacts)
    assert (app / ".env").read_bytes() == original
    assert (app / "gateway/.env").is_file()


def test_unsafe_archive_leaves_no_partial_installation(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = bundle / "source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for name in ("first.txt", "../../escape.txt"):
            content = b"partial extract"
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
    manifest = {
        "source_revision": "c" * 40,
        "files": {"source.tar.gz": deployment.hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    app = tmp_path / "app"
    with pytest.raises(tarfile.OutsideDestinationError):
        deployment.extract_source(bundle, app)
    assert not app.exists() and not list(tmp_path.glob(".yuki-source-*"))
    assert not (tmp_path / "escape.txt").exists()


def test_builder_uses_committed_source_and_keeps_secrets_private(tmp_path, monkeypatch):
    import gzip
    import shutil
    import subprocess
    import zipfile

    builder_spec = importlib.util.spec_from_file_location(
        "windows_bundle_builder", ROOT / "scripts/build_windows_private_bundle.py"
    )
    builder = importlib.util.module_from_spec(builder_spec)
    builder_spec.loader.exec_module(builder)
    repository = tmp_path / "repository"
    shutil.copytree(ROOT / "deploy/windows", repository / "deploy/windows")
    (repository / "src").mkdir()
    source = repository / "src/version.txt"
    source.write_text("committed source")
    subprocess.run(["git", "init", "-q", repository], check=True)
    subprocess.run(["git", "-C", repository, "add", "src"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repository,
            "-c",
            "user.name=Deployment test",
            "-c",
            "user.email=test@invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    source.write_text("uncommitted source must stay local")
    monkeypatch.setattr(builder, "ROOT", repository)
    monkeypatch.setattr(builder, "SOURCE_PATHS", ("src",))
    provider = tmp_path / "provider.md"
    provider.write_text(
        "| base_url (Anthropic) | https://provider.example |\n"
        "| api_key | sk-test-private |\n| model | test-model |\n"
    )
    persona = tmp_path / "persona.md"
    persona.write_text("完整角色提示词")
    output = tmp_path / "private-bundle"
    archive = builder.build(provider, persona, output, "HEAD", "876543210")
    assert archive.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(archive) as package:
        prefix = output.name + "/"
        manifest = json.loads(package.read(prefix + "manifest.json"))
        assert manifest["bot_qq"] == "876543210"
        assert (
            json.loads(package.read(prefix + "private/provider.json"))["api_key"]
            == "sk-test-private"
        )
        assert package.read(prefix + "private/persona.md").decode() == persona.read_text()
        command = package.read(prefix + "Deploy.cmd")
        assert b"\r\n" in command and b"\n" not in command.replace(b"\r\n", b"")
        with tarfile.open(
            fileobj=io.BytesIO(gzip.decompress(package.read(prefix + "source.tar.gz")))
        ) as tar:
            assert tar.extractfile("src/version.txt").read() == b"committed source"
    deployment.verify_bundle(output)
    assert source.read_text() == "uncommitted source must stay local"
