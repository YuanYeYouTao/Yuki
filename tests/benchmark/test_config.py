import json
from pathlib import Path

from benchmarks.config import load_settings, public_settings


def test_explicit_local_key_does_not_import_production_state_or_leak(tmp_path, monkeypatch):
    monkeypatch.delenv("BENCH_TEST_KEY", raising=False)
    (tmp_path / "private.env").write_text(
        "BENCH_TEST_KEY=private-benchmark-key\nDATABASE_URL=production\n"
    )
    config = tmp_path / "bench.toml"
    config.write_text(
        'env_file="private.env"\napi_key_env="BENCH_TEST_KEY"\n[settings]\nllm_model="deepseek-flash"\n'
    )
    settings = load_settings(config, tmp_path / "trial")
    assert settings.llm_api_key == "private-benchmark-key"
    assert "production" not in settings.database_url
    assert settings.code_mode_enabled is False
    public = public_settings(settings)
    assert "private-benchmark-key" not in json.dumps(public)
    assert "private-benchmark-key" not in settings.model_profiles_file.read_text()
    assert public["profiles"]["benchmark"]["model"] == "deepseek-flash"


def test_experiment_identity_ignores_output_paths_and_key_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_TEST_KEY", "first-private-key")
    config = tmp_path / "bench.toml"
    config.write_text('api_key_env="BENCH_TEST_KEY"\n[settings]\nllm_model="deepseek-flash"\n')
    first = public_settings(load_settings(config, tmp_path / "one"))
    monkeypatch.setenv("BENCH_TEST_KEY", "second-private-key")
    second = public_settings(load_settings(config, tmp_path / "two"))
    assert first == second
    assert len(first["fingerprint"]) == 64


def test_missing_key_is_explicit(tmp_path, monkeypatch):
    import pytest

    monkeypatch.delenv("BENCH_ABSENT_KEY", raising=False)
    config = tmp_path / "bench.toml"
    config.write_text('api_key_env="BENCH_ABSENT_KEY"\n[settings]\nllm_model="deepseek-flash"\n')
    with pytest.raises(ValueError, match="BENCH_ABSENT_KEY"):
        load_settings(config, Path(tmp_path / "trial"))


def test_effective_profile_overrides_and_nested_toml_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_TEST_KEY", "private-key")
    config = tmp_path / "bench.toml"
    config.write_text(
        'api_key_env="BENCH_TEST_KEY"\n[settings]\nllm_model="label"\n'
        '[profile]\nmodel="effective"\nheaders={"x-experiment"="offline"}\n'
    )
    settings = load_settings(config, tmp_path / "trial")
    public = public_settings(settings)
    assert settings.llm_model == public["profiles"]["benchmark"]["model"] == "effective"
    assert "offline" not in json.dumps(public)
    assert public["profiles"]["benchmark"]["headers_sha256"]


def test_behavioral_configuration_changes_experiment_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_TEST_KEY", "private-key")
    config = tmp_path / "bench.toml"
    prefix = 'api_key_env="BENCH_TEST_KEY"\n[settings]\nllm_model="deepseek-flash"\n'
    config.write_text(prefix + "tooling_result_item_limit=1\n")
    first = public_settings(load_settings(config, tmp_path / "first"))
    config.write_text(prefix + "tooling_result_item_limit=2\n")
    second = public_settings(load_settings(config, tmp_path / "second"))
    assert first["fingerprint"] != second["fingerprint"]


async def test_provider_ceiling_and_unbounded_timeout_survive_native_configuration(
    tmp_path, monkeypatch
):
    import math

    from yuki.llm.openai_compatible import OpenAICompatibleProvider
    from yuki.model_runtime.profiles import load_model_profile_catalog

    monkeypatch.setenv("BENCH_TEST_KEY", "private-key")
    config = tmp_path / "bench.toml"
    config.write_text(
        'api_key_env="BENCH_TEST_KEY"\n[settings]\nllm_model="deepseek-flash"\n'
        "llm_max_output_tokens=393216\nllm_timeout_seconds=inf\n"
        "context_compact_trigger_tokens=1048576\n"
        "[profile]\ncontext_window_tokens=1048576\n"
    )
    settings = load_settings(config, tmp_path / "trial")
    profile = load_model_profile_catalog(settings.model_profiles_file).profiles["benchmark"]
    assert profile.default_max_output_tokens == settings.llm_max_output_tokens == 393216
    assert profile.context_window_tokens == settings.context_compact_trigger_tokens == 1048576
    assert math.isinf(profile.timeout_seconds)
    provider = OpenAICompatibleProvider(
        base_url=profile.base_url,
        api_key="private-key",
        timeout_seconds=profile.timeout_seconds,
        max_retries=0,
    )
    assert math.isinf(provider._timeout.read)
    await provider.close()
    public = public_settings(settings)
    assert public["benchmark_runtime_limits"] == {"model_requests": None, "tool_calls": None}
    assert public["profiles"]["benchmark"]["timeout_seconds"] is None
