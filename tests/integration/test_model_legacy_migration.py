"""Offline legacy migration and Main use the real bundle, database, CLI and HTTP adapter."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from tests.integration.test_current_material_main_contract import _catalog, _deployment, _profile
from tests.integration.test_embedding_connection_bundle import embedding_http as embedding_http
from tests.support.model_profiles import write_fake_profiles

from yuki.admin.config_files import ConfigFileService
from yuki.config import Settings
from yuki.domain.messages import ChatMessage, ChatRequest
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.llm.vendor_policy import ChatWireOptions
from yuki.model_runtime.models import ModelProtocol, ModelTask
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import load_model_profile_catalog
from yuki.model_runtime.secrets import model_secrets_path, read_model_secrets
from yuki.persistence.models import RuntimeConfigOverrideModel

ROOT = Path(__file__).resolve().parents[2]


def deployment(database, directory, url):
    directory.mkdir()
    path = write_fake_profiles(directory / "models.toml")
    path.write_text(
        path.read_text()
        .replace("default_temperature = 0.7", "default_temperature = 0.21")
        .replace("default_max_output_tokens = 2048", "default_max_output_tokens = 301")
    )
    values = {
        "DATABASE_URL": database.url,
        "MODEL_PROFILES_FILE": path.as_posix(),
        "MEMORY_EMBEDDING_ENABLED": "true",
        "MEMORY_EMBEDDING_BASE_URL": url,
        "MEMORY_EMBEDDING_API_KEY": "synthetic-legacy-embedding",
        "VISION_ENABLED": "false",
        "VISION_BASE_URL": url,
        "VISION_API_KEY": "synthetic-legacy-vision",
        "VISION_MODEL": "legacy-vision-model",
        "ASR_ENABLED": "true",
        "ASR_BASE_URL": "",
        "ASR_API_KEY": "",
        "ASR_MODEL": "legacy-asr-model",
        "ASR_PROTOCOL": "audio_transcriptions",
        "TAVILY_API_KEY": "synthetic-legacy-tavily",
        "WEB_MODE": "disabled",
        "YUKI_CONTROL_CREDENTIAL": "",
        "SOUL_FILE": str(Path(__file__).resolve().parents[2] / "config/SOUL.md"),
    }
    (directory / ".env").write_text(
        "\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()), encoding="utf-8"
    )
    settings = Settings(_env_file=directory / ".env")
    return path, settings, values


async def migrate(directory, values, revision, *, probe=None):
    environment = {
        **os.environ,
        **values,
        "PYTHONUTF8": "1",
        "PYTHONPATH": str(ROOT / "src"),
    }
    return await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            *(["-c", probe] if probe is not None else ["-m", "yuki.cli"]),
            "admin",
            "model",
            "migrate-legacy",
            "--revision",
            str(revision),
        ],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


async def override(database, key, value, *, scope="global", owner=None, mode="hot"):
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            RuntimeConfigOverrideModel(
                config_key=key,
                scope_type=scope,
                canonical_person_id=owner,
                value_json=json.dumps(value),
                value_type={
                    "llm.temperature": "number",
                    "llm.max_output_tokens": "integer",
                    "llm.thinking_enabled": "boolean",
                }.get(key, "string"),
                apply_mode=mode,
                version=1,
                created_at=now,
                updated_at=now,
                updated_by="synthetic-legacy-operator",
            )
        )


def evidence(name, value):
    path = Path("test-results/model-legacy")
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def test_offline_migration_preserves_effective_sources_and_is_repeatable(
    database, tmp_path, embedding_http
):
    url, wire = embedding_http
    directory = tmp_path / "deployment"
    path, settings, values = deployment(database, directory, url)
    await override(database, "vision.base_url", url + "/activated", mode="restart_required")
    await override(database, "asr.model", "activated-asr-model", mode="restart_required")
    before = await ConfigFileService(settings).read()
    original_profiles = before["document"]["profiles"]
    result = await migrate(directory, values, before["revision"])
    assert result.returncode == 0, result.stderr
    after = await ConfigFileService(settings).read()
    assert after["valid"] is True
    document = after["document"]
    assert document["profiles"] == original_profiles
    assert document["embedding"]["provider"] == "qwen_dashscope"
    assert document["vision"]["enabled"] is False
    assert document["asr"]["base_url"] == url + "/activated"
    assert document["asr"]["model"] == "activated-asr-model"
    assert document["asr"]["protocol"] == "audio_transcriptions"
    assert document["search"]["mode"] == "disabled"
    keys = read_model_secrets(path)[1]
    assert keys[document["embedding"]["api_key_env"]] == values["MEMORY_EMBEDDING_API_KEY"]
    assert keys[document["asr"]["api_key_env"]] == values["VISION_API_KEY"]
    assert keys[document["search"]["tavily_api_key_env"]] == values["TAVILY_API_KEY"]
    for secret in (value for key, value in values.items() if key.endswith("API_KEY") and value):
        assert secret not in path.read_text() and secret not in result.stdout + result.stderr
    written = path.read_bytes(), model_secrets_path(path).read_bytes()
    repeated = await migrate(directory, values, after["revision"])
    assert repeated.returncode == 0, repeated.stderr
    assert written == (path.read_bytes(), model_secrets_path(path).read_bytes())
    assert wire["requests"] == []
    evidence(
        "offline-effective",
        {
            "result": json.loads(result.stdout),
            "repeat": json.loads(repeated.stdout),
            "document": document,
            "upstream_requests": 0,
        },
    )


@pytest.mark.parametrize("mode", ["native", "tavily", "both"])
async def test_offline_migration_preserves_each_enabled_web_mode(
    database, tmp_path, embedding_http, mode
):
    url, wire = embedding_http
    directory = tmp_path / "deployment"
    _, settings, values = deployment(database, directory, url)
    values["WEB_MODE"] = mode
    current = await ConfigFileService(settings).read()
    result = await migrate(directory, values, current["revision"])
    assert result.returncode == 0, result.stderr
    saved = await ConfigFileService(settings).read()
    assert saved["document"]["search"]["mode"] == mode
    assert wire["requests"] == []
    evidence(
        f"search-mode-{mode}",
        {
            "result": json.loads(result.stdout),
            "mode": saved["document"]["search"]["mode"],
            "upstream_requests": wire["requests"],
        },
    )


@pytest.mark.parametrize(
    "source,stored,same_raw_thinking",
    [
        ("dotenv", "absent", False),
        ("process", "absent", False),
        ("dotenv", "global", False),
        ("process", "global", False),
        ("dotenv", "null", False),
        ("process", "null", False),
        ("defaults", "absent", False),
        ("process", "absent", True),
    ],
)
async def test_explicit_environment_migrates_by_original_source_precedence(
    database, tmp_path, embedding_http, monkeypatch, source, stored, same_raw_thinking
):
    url, wire = embedding_http
    directory = tmp_path / "deployment"
    path, _, values = deployment(database, directory, url)
    path.write_text(path.read_text().replace("[routes]", 'thinking_mode = "disabled"\n[routes]'))
    if same_raw_thinking:
        path.write_text(path.read_text().replace("[routes]", "thinking_enabled = true\n[routes]"))
    environment = {
        "LLM_TEMPERATURE": "0.37",
        "LLM_MAX_OUTPUT_TOKENS": "411",
        "LLM_THINKING_ENABLED": "true",
    }
    for name in environment:
        monkeypatch.delenv(name, raising=False)
    if source == "dotenv":
        with (directory / ".env").open("a", encoding="utf-8") as stream:
            stream.write("\n" + "\n".join(f"{key}={value}" for key, value in environment.items()))
    elif source == "process":
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
    settings = Settings(_env_file=directory / ".env")
    fields = {"llm_temperature", "llm_max_output_tokens", "llm_thinking_enabled"}
    assert fields.intersection(settings.model_fields_set) == (
        set() if source == "defaults" else fields
    )
    if stored != "absent":
        for key, value in (
            ("llm.temperature", 0.49),
            ("llm.max_output_tokens", 771),
            ("llm.thinking_enabled", False),
        ):
            await override(database, key, None if stored == "null" else value)
    before = await ConfigFileService(settings).read()
    result = await migrate(directory, values, before["revision"])
    assert result.returncode == 0, result.stderr
    saved = await ConfigFileService(settings).read()
    document = saved["document"]
    catalog = load_model_profile_catalog(path, web_mode=settings.web.mode)
    profile = catalog.profiles[document["routes"]["chat_agent"]]
    expected = (
        (0.21, 301, False)
        if source == "defaults"
        else (0.49, 771, False)
        if stored == "global"
        else (0.37, 411, True)
    )
    assert (
        profile.default_temperature,
        profile.default_max_output_tokens,
        profile.thinking_enabled,
    ) == expected
    assert document["routes"]["chat_agent"] == document["routes"]["plugin_agent_session"]
    assert document["profiles"]["main"] == before["document"]["profiles"]["main"]
    assert document["routes"]["memory_dream"] == "main"
    if source == "defaults":
        assert document["routes"]["chat_agent"] == "main"
        assert len(document["profiles"]) == 1
    else:
        assert document["routes"]["chat_agent"] != "main"
    payload = json.loads(result.stdout)
    assert payload["consumed_env_fields"] == ([] if source == "defaults" else sorted(environment))
    assert wire["requests"] == []
    evidence(
        f"explicit-{source}-{stored}",
        {
            "result": payload,
            "source_fields": sorted(fields.intersection(settings.model_fields_set)),
            "selected": profile.model_dump(mode="json"),
            "upstream_requests": wire["requests"],
        },
    )


async def test_retired_environment_sources_cannot_replay_over_later_profile_edit(
    database, tmp_path, embedding_http, monkeypatch
):
    url, _ = embedding_http
    directory = tmp_path / "deployment"
    path, _, values = deployment(database, directory, url)
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "411")
    await override(database, "llm.max_output_tokens", 771)
    settings = Settings(_env_file=directory / ".env")
    current = await ConfigFileService(settings).read()
    first = await migrate(directory, values, current["revision"])
    assert first.returncode == 0, first.stderr
    saved = await ConfigFileService(settings).read()
    selected = saved["document"]["routes"]["chat_agent"]
    assert saved["document"]["profiles"][selected]["default_max_output_tokens"] == 771
    assert json.loads(first.stdout)["consumed_env_fields"] == ["LLM_MAX_OUTPUT_TOKENS"]
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS")
    files = ConfigFileService(Settings(_env_file=directory / ".env"))
    document = saved["document"]
    document["profiles"][selected]["default_max_output_tokens"] = 888
    revision = await files.save(saved["revision"], {"document": document})
    written = path.read_bytes(), model_secrets_path(path).read_bytes()
    repeated = await migrate(directory, values, revision)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["consumed_env_fields"] == []
    assert written == (path.read_bytes(), model_secrets_path(path).read_bytes())
    evidence(
        "explicit-sources-retired",
        {
            "first": json.loads(first.stdout),
            "repeated": json.loads(repeated.stdout),
            "later_profile_output_tokens": 888,
            "bundle_unchanged_on_repeat": True,
        },
    )


@pytest.mark.parametrize("value", [None, 777])
async def test_only_explicit_global_overrides_migrate_without_changing_other_routes(
    database, tmp_path, embedding_http, value
):
    url, _ = embedding_http
    directory = tmp_path / "deployment"
    path, settings, values = deployment(database, directory, url)
    await override(database, "llm.max_output_tokens", value)
    before = await ConfigFileService(settings).read()
    result = await migrate(directory, values, before["revision"])
    assert result.returncode == 0, result.stderr
    after = await ConfigFileService(settings).read()
    document = after["document"]
    assert document["profiles"]["main"] == before["document"]["profiles"]["main"]
    selected = document["routes"]["chat_agent"]
    assert document["routes"]["plugin_agent_session"] == selected
    if value is None:
        assert selected == "main" and len(document["profiles"]) == 1
    else:
        assert selected != "main" and len(document["profiles"]) == 2
        assert document["profiles"][selected]["default_max_output_tokens"] == value
    assert document["routes"]["memory_dream"] == "main"
    written = path.read_bytes(), model_secrets_path(path).read_bytes()
    repeated = await migrate(directory, values, after["revision"])
    assert repeated.returncode == 0
    assert written == (path.read_bytes(), model_secrets_path(path).read_bytes())
    async with database.read_session() as reader:
        assert (await reader.scalars(select(RuntimeConfigOverrideModel))).first() is None
    evidence(
        f"global-{value}",
        {
            "result": json.loads(result.stdout),
            "document": document,
            "legacy_override_retired": True,
        },
    )


async def test_conflicting_scope_and_stale_revision_never_write_partial_bundle(
    database, tmp_path, embedding_http
):
    url, _ = embedding_http
    directory = tmp_path / "deployment"
    path, settings, values = deployment(database, directory, url)
    current = await ConfigFileService(settings).read()
    original = path.read_bytes()
    stale = await migrate(directory, values, current["revision"] + 1)
    assert stale.returncode != 0 and "version_conflict" in stale.stdout + stale.stderr
    assert path.read_bytes() == original and not model_secrets_path(path).exists()
    person_id, now = str(uuid4()), datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            CanonicalPersonModel(
                id=person_id, display_name="Scope conflict", created_at=now, updated_at=now
            )
        )
    await override(database, "llm.temperature", 0.42, scope="user", owner=person_id)
    result = await migrate(directory, values, current["revision"])
    assert result.returncode != 0 and "scoped_model_overrides" in result.stdout + result.stderr
    assert "0.42" not in result.stdout + result.stderr
    assert path.read_bytes() == original and not model_secrets_path(path).exists()
    evidence(
        "no-partial-write", {"stale_revision_rejected": True, "scoped_override_rejected": True}
    )


async def test_main_uses_profile_defaults_but_explicit_single_call_parameters_survive(
    database, tmp_path, embedding_http
):
    url, wire = embedding_http
    wire["response"] = {
        "choices": [
            {"message": {"role": "assistant", "content": "NO_REPLY"}, "finish_reason": "stop"}
        ]
    }
    options = ChatWireOptions(send_temperature=True, reasoning="enable_thinking")
    async with httpx.AsyncClient(base_url=url) as client:
        provider = OpenAICompatibleProvider(
            base_url=url,
            api_key="synthetic-main",
            timeout_seconds=2,
            max_retries=0,
            options=options,
            client=client,
        )
        app, _, message, sender = await _deployment(database, tmp_path, provider)
        selected = _profile().model_copy(
            update={
                "provider": "openai_compatible",
                "protocol": ModelProtocol.CHAT_COMPLETIONS,
                "default_temperature": 0.23,
                "default_max_output_tokens": 117,
                "thinking_enabled": False,
                "wire_options": options,
            }
        )
        app.models.apply_catalog(
            _catalog(selected), ModelClientPool(injected_profiles={"main": provider})
        )
        try:
            assert (await app.processor.handle(message, sender)).reason == "chat"
            first = wire["requests"][0]["body"]
            assert first["temperature"] == 0.23 and first["max_tokens"] == 117
            assert "enable_thinking" not in first
            await app.models.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(
                    messages=(ChatMessage(role="user", content="explicit one-shot"),),
                    temperature=0.66,
                    max_output_tokens=79,
                    thinking_enabled=True,
                ),
            )
            explicit = wire["requests"][1]["body"]
            assert explicit["temperature"] == 0.66 and explicit["max_tokens"] == 79
            assert explicit["enable_thinking"] is True
            evidence("main-profile-defaults", {"first": first, "explicit": explicit})
        finally:
            assert not await app.lifecycle._close_entries(app.lifecycle._entries)


def test_bundle_embedding_is_not_blocked_by_inactive_legacy_settings(tmp_path):
    path = write_fake_profiles(tmp_path / "models.toml")
    with path.open("a") as stream:
        stream.write(
            '\n[embedding]\nenabled = false\nprovider = "openai_compatible"\ndimensions = 2\n'
        )
    settings = Settings(
        _env_file=None,
        model_profiles_file=path,
        memory_embedding_enabled=False,
        memory_embedding_provider="unused-legacy-provider",
        memory_embedding_dimensions=2,
        memory_embedding_output_type="unused",
        memory_embedding_document_template_version=2,
    )
    assert settings.memory_embedding_dimensions == 2


async def test_existing_segments_and_key_aliases_remain_authoritative(
    database, tmp_path, embedding_http
):
    url, _ = embedding_http
    directory = tmp_path / "deployment"
    path, settings, values = deployment(database, directory, url)
    files = ConfigFileService(settings)
    before = await files.read()
    document = before["document"]
    alias = "YUKI_LEGACY_EMBEDDING_KEY"
    document["profiles"]["main"]["api_key_env"] = alias
    document["vision"] = {
        "enabled": False,
        "base_url": "",
        "model": "",
        "api_key_env": "VISION_KEEP",
    }
    document["asr"] = {"enabled": False, "base_url": "", "model": "", "api_key_env": ""}
    await files.save(
        before["revision"],
        {
            "document": document,
            "api_keys": {
                alias: "synthetic-existing-main-key",
                "VISION_KEEP": "synthetic-existing-vision-key",
            },
        },
    )
    current = await files.read()
    result = await migrate(directory, values, current["revision"])
    assert result.returncode == 0, result.stderr
    after = await files.read()
    assert after["document"]["vision"] == current["document"]["vision"]
    assert after["document"]["asr"] == current["document"]["asr"]
    saved = read_model_secrets(path)[1]
    assert saved[alias] == "synthetic-existing-main-key"
    selected = after["document"]["embedding"]["api_key_env"]
    assert selected != alias and saved[selected] == values["MEMORY_EMBEDDING_API_KEY"]
    evidence("existing-segments", {"document": after["document"], "old_alias_preserved": True})


async def test_missing_credentials_stay_disabled_and_do_not_create_empty_secrets(
    database, tmp_path, embedding_http
):
    url, _ = embedding_http
    directory = tmp_path / "deployment"
    path, _, values = deployment(database, directory, url)
    values = {key: "" if key.endswith("API_KEY") else value for key, value in values.items()}
    (directory / ".env").write_text(
        "\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()), encoding="utf-8"
    )
    settings = Settings(_env_file=directory / ".env")
    before = await ConfigFileService(settings).read()
    result = await migrate(directory, values, before["revision"])
    assert result.returncode == 0, result.stderr
    after = await ConfigFileService(settings).read()
    assert after["document"]["embedding"]["enabled"] is False
    assert after["document"]["asr"]["enabled"] is False
    assert not model_secrets_path(path).exists()
    evidence("no-empty-key", {"document": after["document"], "sidecar_created": False})


async def test_persisted_model_source_is_materialized_before_its_override_is_retired(
    database, tmp_path, embedding_http
):
    url, _ = embedding_http
    directory = tmp_path / "deployment"
    path, settings, values = deployment(database, directory, url)
    path.write_text(path.read_text().replace('model = "fake-model"', 'model_env = "LLM_MODEL"'))
    values["LLM_MODEL"] = "environment-model"
    settings = settings.model_copy(update={"llm_model": "environment-model"})
    await override(database, "llm.model", "persisted-model", mode="restart_required")
    before = await ConfigFileService(settings).read()
    result = await migrate(directory, values, before["revision"])
    assert result.returncode == 0, result.stderr
    after = await ConfigFileService(settings).read()
    assert after["valid"] is True
    assert after["document"]["profiles"]["main"]["model"] == "persisted-model"
    assert "model_env" not in after["document"]["profiles"]["main"]
    evidence(
        "persisted-model-source",
        {"document": after["document"], "result": json.loads(result.stdout)},
    )


async def test_retirement_conflict_reports_partial_and_retry_does_not_reapply_after_user_edit(
    database, tmp_path, embedding_http
):
    url, _ = embedding_http
    directory = tmp_path / "deployment"
    _, settings, values = deployment(database, directory, url)
    await override(database, "llm.max_output_tokens", 777)
    before = await ConfigFileService(settings).read()
    probe = """
import os
from sqlalchemy import update
from yuki.admin.config_files import ConfigFileService
from yuki.persistence.database import Database
from yuki.persistence.models import RuntimeConfigOverrideModel
original = ConfigFileService.save
async def concurrent(self, *args, **kwargs):
    result = await original(self, *args, **kwargs)
    database = Database(os.environ["DATABASE_URL"])
    try:
        async with database.immediate_session() as writer:
            await writer.execute(update(RuntimeConfigOverrideModel).values(version=2))
    finally:
        await database.close()
    return result
ConfigFileService.save = concurrent
from yuki.cli import main
main()
"""
    partial = await migrate(directory, values, before["revision"], probe=probe)
    report = json.loads(partial.stdout)
    assert partial.returncode != 0 and report["ok"] is False and report["partial"] is True
    assert report["error_category"] == "legacy_changed"
    after = await ConfigFileService(settings).read()
    assert after["revision"] == report["revision"] != before["revision"]
    async with database.read_session() as reader:
        row = (await reader.scalars(select(RuntimeConfigOverrideModel))).one()
        assert row.version == 2
    retry = await migrate(directory, values, after["revision"])
    assert retry.returncode == 0, retry.stderr
    current = await ConfigFileService(settings).read()
    chosen = current["document"]["routes"]["chat_agent"]
    current["document"]["profiles"][chosen]["default_max_output_tokens"] = 888
    await ConfigFileService(settings).save(current["revision"], {"document": current["document"]})
    edited = await ConfigFileService(settings).read()
    repeated = await migrate(directory, values, edited["revision"])
    assert repeated.returncode == 0
    assert (await ConfigFileService(settings).read())["document"]["profiles"][chosen][
        "default_max_output_tokens"
    ] == 888
    evidence(
        "retirement-partial",
        {"partial": report, "retry": json.loads(retry.stdout), "user_edit_preserved": True},
    )
