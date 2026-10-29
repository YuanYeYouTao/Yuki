"""Work metadata stays public; original steps use the original private scope."""

import json

from jsonschema import Draft202012Validator
from tests.support.runtime_work_helpers import _persisted_tool_receipt
from tests.unit.test_work_settlement_writer import _running_work

from yuki.runtime.work_control import work_control_tools
from yuki.runtime.work_queries import WorkQueries


async def test_work_get_uses_original_effect_page_without_dumping_reasoning(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    for index in range(3):
        await _persisted_tool_receipt(
            control,
            f"step-{index}",
            "read_fixture",
            {"ok": True, "data": {"text": f"actual result {index}"}},
            side_effecting=False,
        )
    query = WorkQueries(repository)
    result = await query.get(
        lease, control.source, control.current["id"], include_progress=True, limit=2
    )
    assert result["progress_available"] and result["history_order"] == "desc"
    assert [step["effect_key"] for step in result["steps"]] == ["step-2", "step-1"]
    assert result["steps"][0]["result"]["data"]["text"] == "actual result 2"
    assert result["steps"][0]["updated_at"] <= float(result["updated_at"]) + 1
    older = await query.get(
        lease,
        control.source,
        control.current["id"],
        include_progress=True,
        limit=2,
        cursor=result["next_cursor"],
    )
    assert [step["effect_key"] for step in older["steps"]] == ["step-0"]
    assert older["next_cursor"] is None


async def test_global_metadata_read_does_not_grant_original_private_effects(database, tmp_path):
    repository, lease, control = await _running_work(database, tmp_path)
    await _persisted_tool_receipt(
        control,
        "private-step",
        "read_fixture",
        {"ok": True, "data": {"text": "private original result"}},
        side_effecting=False,
    )
    result = await WorkQueries(repository).get(
        lease,
        {**control.source, "origin": "another-origin"},
        control.current["id"],
        include_progress=True,
    )
    assert result["work_id"] == control.current["id"]
    assert result["progress_available"] is False
    assert result["progress_reason"] == "original_private_source_not_authorized"
    assert "steps" not in result and "context_note" not in result


async def test_original_256_byte_effect_key_cursor_can_be_passed_back_through_tool_contract(
    database, tmp_path
):
    _repository, _lease, control = await _running_work(database, tmp_path)
    original_keys = [letter * 256 for letter in ("a", "b")]
    for key in original_keys:
        await _persisted_tool_receipt(
            control, key, "read_fixture", {"ok": True}, side_effecting=False
        )
    request = {"action": "get", "work_id": control.current["id"], "limit": 1}
    first = json.loads(await control.execute("task_control", request, "first-page"))
    cursor = first["work"]["next_cursor"]
    assert len(cursor) > 256
    following = {**request, "cursor": cursor}
    definition = work_control_tools()[0]
    Draft202012Validator(definition.parameters).validate(following)
    older = json.loads(await control.execute("task_control", following, "older-page"))
    assert older["ok"] and older["work"]["steps"][0]["effect_key"] == original_keys[0]
    assert older["work"]["next_cursor"] is None
