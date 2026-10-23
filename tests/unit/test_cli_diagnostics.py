"""Only actual offline model/runtime diagnostics remain in the public CLI."""

import json
import sys

import pytest
from tests.conftest import make_settings

from yuki import cli
from yuki.model_runtime.models import ModelProfile, ModelRoute, ModelTask
from yuki.model_runtime.profiles import ModelProfileCatalog


def test_help_and_retired_prompt_rejection_do_not_load_private_settings(monkeypatch, capsys):
    def unexpected_settings():
        raise AssertionError("Help and parser rejection must not load deployment settings")

    monkeypatch.setattr(cli, "Settings", unexpected_settings)
    monkeypatch.setattr(sys, "argv", ["yuki", "--help"])
    with pytest.raises(SystemExit) as help_exit:
        cli.main()
    assert help_exit.value.code == 0
    output = capsys.readouterr().out
    assert "model" in output and "runtime" in output and "prompt" not in output
    for retired in (["prompt", "inspect", "direct-text"], ["prompt", "compare"]):
        monkeypatch.setattr(sys, "argv", ["yuki", *retired])
        with pytest.raises(SystemExit) as rejected:
            cli.main()
        assert rejected.value.code == 2
        assert "invalid choice" in capsys.readouterr().err


def test_model_catalog_diagnostics_remain_offline(monkeypatch, capsys):
    profile = ModelProfile(
        id="offline",
        provider="fake",
        model="offline",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.1,
        default_max_output_tokens=128,
    )
    catalog = ModelProfileCatalog(
        profiles={"offline": profile},
        routes={task: ModelRoute(task=task, profile_id="offline") for task in ModelTask},
    )
    monkeypatch.setattr(cli, "Settings", lambda: make_settings("sqlite+aiosqlite:///:memory:"))
    monkeypatch.setattr(cli, "load_http_environment", lambda: None)
    monkeypatch.setattr(cli, "_model_catalog", lambda settings: catalog)
    for operation in ("profiles", "routes"):
        monkeypatch.setattr(sys, "argv", ["yuki", "model", operation])
        cli.main()
        result = json.loads(capsys.readouterr().out)
        if operation == "profiles":
            assert result["offline"]["provider"] == "fake"
        else:
            assert result["chat_agent"]["profile_id"] == "offline"
