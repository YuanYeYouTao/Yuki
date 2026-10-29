"""Actual over-budget progress keeps current actions and authorized full artifacts."""

import json
from dataclasses import replace

from tests.support.runtime_work_helpers import _persisted_tool_receipt
from tests.support.social_identity_cases import social_env
from tests.unit.test_work_settlement_writer import _running_work

from yuki.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture
from yuki.tool_results.access import ArtifactAccess
from yuki.tool_results.artifacts import ToolArtifactRepository


async def test_large_native_turn_does_not_hide_latest_failure_behind_user_prompt(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "originals", retention_seconds=60)
    data = {
        "query_scope": "session",
        "status_scope": "session",
        "status": "idle",
        "history_order": "desc",
        "cursor_parameter": "native_cursor",
        "latest_turn": {
            "id": "original-turn",
            "items_order": "asc",
            "itemsView": "summary",
            "items": [
                {"type": "userMessage", "content": "ORIGINAL_USER_REQUEST" * 50000},
                {
                    "type": "commandExecution",
                    "id": "earlier",
                    "status": "completed",
                    "command": "old check",
                },
                {
                    "type": "commandExecution",
                    "id": "latest",
                    "status": "failed",
                    "exitCode": 2,
                    "command": "latest-test",
                    "aggregatedOutput": "LATEST_REAL_FAILURE " + "details" * 5000,
                },
            ],
        },
    }
    rendered = await ToolResultBudgeter(
        max_characters=1800, artifacts=store, artifact_access=access
    ).render(
        ToolExecutionResult(ok=True, data=data, provider_id="core", tool_name="external_agent")
    )
    short = json.loads(rendered.text)
    assert short["artifact_handle"] == rendered.artifact_id
    progress = short["progress"]
    excerpt = progress["latest_turn"]["content_preview"]
    assert "LATEST_REAL_FAILURE" in excerpt and "latest-test" in excerpt
    assert "ORIGINAL_USER_REQUEST" not in excerpt
    assert progress["latest_turn"]["content_order"] == "desc"
    assert progress["history_order"] == "desc" and progress["cursor_parameter"] == "native_cursor"
    full = await store.read(
        rendered.artifact_id, access=access, limit=2000000, max_characters=2000000
    )
    assert json.loads(full["content"])["data"] == data
    denied = await store.read(rendered.artifact_id, access=replace(access, generation=2))
    assert denied["error_code"] == "artifact_not_authorized"


async def test_large_work_progress_keeps_original_latest_step_and_private_artifact(
    database, tmp_path
):
    _repository, lease, control = await _running_work(database, tmp_path)
    access = ArtifactAccess(lease.conversation_id, 1, control.source["actor_person_id"])
    store = ToolArtifactRepository(database, tmp_path / "originals", retention_seconds=60)
    for key, output in (
        ("earlier-effect", "earlier actual fact"),
        ("original-effect", "LATEST_WORK_FACT " + "actual private body" * 10000),
    ):
        await _persisted_tool_receipt(
            control,
            key,
            "terminal_read",
            {"ok": True, "data": {"status": "failed", "output": output}},
            side_effecting=False,
        )
    capture = ResultCapture(control.current["id"], "original-progress-query")
    token = current_result_capture.set(capture)
    try:
        await control.execute(
            "task_control",
            {"action": "get", "work_id": control.current["id"], "limit": 1},
            "original-progress-query",
        )
    finally:
        current_result_capture.reset(token)
    outcome = capture.outcome
    assert outcome is not None and outcome.provider_id == "work_control"
    data = outcome.data
    rendered = await ToolResultBudgeter(
        max_characters=1800, artifacts=store, artifact_access=access
    ).render(outcome)
    short = json.loads(rendered.text)
    preview = short["progress"]["work"]
    assert "LATEST_WORK_FACT" in preview["content_preview"]
    assert preview["next_cursor"] and preview["history_order"] == "desc"
    full = await store.read(
        rendered.artifact_id, access=access, limit=500000, max_characters=500000
    )
    assert json.loads(full["content"])["data"] == data
    denied = await store.read(rendered.artifact_id, access=replace(access, generation=2))
    assert denied["error_code"] == "artifact_not_authorized"
