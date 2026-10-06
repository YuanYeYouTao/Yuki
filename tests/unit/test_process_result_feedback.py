"""Status reads and archived output retain the actual process failure."""

import json

import pytest
from tests.unit.test_work_effect_results import execute, owned_session

from qq_ai_bot.capabilities.results import ToolResultBudgeter, normalize_legacy_result
from qq_ai_bot.mcp.repository import ToolArtifactRepository
from qq_ai_bot.runtime.effect_outcomes import execution_evidence


@pytest.mark.parametrize("budget,archive", [(24000, False), (600, False), (600, True), (64, True)])
async def test_failed_process_is_visible_in_short_artifact_and_minimal_read_results(
    database, tmp_path, budget, archive
):
    outcome = normalize_legacy_result(
        {
            "run_id": "original-process",
            "status": "failed",
            "exit_code": 127,
            "pending": False,
            "output": "pytest: command not found\n" * (1 if budget == 24000 else 1000),
            "completion": {
                "run_id": "original-process",
                "status": "failed",
                "exit_code": 127,
                "pending": False,
            },
        },
        provider_id="core",
        tool_name="terminal_read",
    )
    store = (
        ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
        if archive
        else None
    )
    rendered = await ToolResultBudgeter(max_characters=budget, artifacts=store).render(outcome)
    payload = json.loads(rendered.text)
    # The read succeeded; the observed test process did not run successfully.
    assert payload["ok"] is True
    assert payload["process"]["succeeded"] is False
    assert payload["process"]["exit_code"] == 127
    assert payload["process"]["run_id"] == "original-process"
    assert execution_evidence(outcome, tool="terminal_read", side_effecting=False)["ok"] is False


@pytest.mark.parametrize("status,pending", [("running", True), ("unknown", False)])
async def test_pending_or_unknown_process_is_not_reported_as_success(status, pending):
    outcome = normalize_legacy_result(
        {"status": status, "pending": pending, "run_id": "original-process", "output": ""},
        provider_id="core",
        tool_name="get_code_run",
    )
    payload = json.loads((await ToolResultBudgeter(max_characters=64).render(outcome)).text)
    assert "succeeded" not in payload["process"]
    assert payload["process"]["status"] == status


async def test_failed_exit_retains_known_partial_mutation_in_original_receipt(database, tmp_path):
    control, session, store = await owned_session(database, tmp_path)
    outcome = normalize_legacy_result(
        {
            "status": "failed",
            "run_id": "original-process",
            "exit_code": 1,
            "pending": False,
            "output": "wrote a file; later command failed",
            "mutation_committed": True,
        },
        provider_id="core",
        tool_name="terminal_exec",
    )
    text = await execute(session, store, "execute-once", outcome)
    assert json.loads(text)["process"]["succeeded"] is False
    await control.refresh_effects()
    fact = next(item for item in control.known_effects if item.get("run_id") == "original-process")
    assert fact["ok"] is False
    assert fact["mutation_committed"] is True
    assert fact["process"]["exit_code"] == 1


def test_background_chat_guidance_uses_declared_send_action():
    from qq_ai_bot.prompting.contracts import CORE_CONTRACT

    assert "可用 answer 回应" not in CORE_CONTRACT
    assert "用 send_message 回应并保留后台任务" in CORE_CONTRACT
    assert "调查和写作用 answer" in CORE_CONTRACT  # output_kind is still legitimate.
