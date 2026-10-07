"""Administrative CLI boundaries remain independent of the Agent loop."""

import json

import pytest

from qq_ai_bot import cli


def test_gateway_doctor_is_read_only_without_loading_runtime_settings(monkeypatch, capsys):
    def forbidden_settings():
        pytest.fail("gateway diagnostics must not load private runtime settings")

    monkeypatch.setattr(cli, "Settings", forbidden_settings)
    monkeypatch.setattr("sys.argv", ["yuki", "gateway", "doctor", "--provider", "snowluma"])
    cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload


@pytest.mark.parametrize("command", ["chat", "execute-code", "resume-agent"])
def test_cli_refuses_unregistered_agent_entrypoints(monkeypatch, command):
    monkeypatch.setattr("sys.argv", ["yuki", command])
    with pytest.raises(SystemExit) as refused:
        cli.main()
    assert refused.value.code == 2


def test_help_preserves_administrative_commands_without_starting_an_agent(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["yuki", "--help"])
    with pytest.raises(SystemExit) as help_result:
        cli.main()
    assert help_result.value.code == 0
    help_text = capsys.readouterr().out
    for command in ("init-db", "gateway", "plugin", "runtime", "conversation", "memory"):
        assert command in help_text
