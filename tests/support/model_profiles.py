"""Explicit complete fake profile files for assembly tests."""

from pathlib import Path

from qq_ai_bot.model_runtime.models import ModelTask


def write_fake_profiles(path: Path, *, model: str = "fake-model") -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'schema_version = 3\n[profiles.main]\nprovider = "fake"\n'
        'protocol = "chat_completions"\nmodel = ' + json.dumps(model) + "\n"
        'base_url = "https://test.invalid/v1"\napi_key_env = ""\n'
        "timeout_seconds = 60\nmax_retries = 1\n"
        "default_temperature = 0.7\ndefault_max_output_tokens = 2048\n"
        'capabilities = ["tools", "structured_output", "reasoning"]\n'
        "[routes]\n" + "".join(f'{task.value} = "main"\n' for task in ModelTask),
        encoding="utf-8",
    )
    return path
