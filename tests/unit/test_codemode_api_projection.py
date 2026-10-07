"""The script API is a deterministic projection of the frozen manifest."""

import json
from pathlib import Path

import pytest

from qq_ai_bot.codemode.api_projection import (
    decode_wrapper_name,
    encode_wrapper_name,
    project,
    receipt_view,
)
from qq_ai_bot.codemode.contract import EXECUTE_CODE_TOOL
from qq_ai_bot.domain.messages import ChatTool

INVENTORY = json.loads(
    (
        Path(__file__).parents[2] / "docs/architecture/pi-codemode-capability-inventory.json"
    ).read_text(encoding="utf-8")
)
FROZEN = tuple(ChatTool(**tool) for tool in INVENTORY["frozen_definitions"])


@pytest.mark.parametrize("row", INVENTORY["tools"], ids=lambda row: row["model_name"])
def test_every_inventory_tool_has_one_exact_wrapper(row):
    api = project(FROZEN, INVENTORY["manifest_revision"])
    name = row["model_name"]
    if name in {"execute_code", "lookup_tools"}:
        assert all(tool != name for tool in api.wrappers.values())  # Host-only entrypoints.
        return
    wrapper = encode_wrapper_name(name)
    assert api.tool_for(wrapper) == name
    # The original declared schema object, unchanged: one business contract.
    assert api.schemas[name] == row["input_schema"]


def test_projection_adds_no_aliases_or_unknown_routes():
    api = project(FROZEN, INVENTORY["manifest_revision"])
    assert set(api.wrappers.values()) == {t.name for t in FROZEN} - {"execute_code", "lookup_tools"}
    for probe in ("yuki_", "__yuki_invoke", "yuki_db.execute", "send_message", "yuki_x00"):
        assert api.tool_for(probe) is None


@pytest.mark.parametrize("name", ["send_message", "a.b", "工具", "x__y", "Upper"])
def test_wrapper_name_encoding_is_reversible(name):
    assert decode_wrapper_name(encode_wrapper_name(name)) == name


def test_projection_digest_follows_manifest_and_api_revision():
    first = project(FROZEN, "rev-1")
    assert first.digest() == project(FROZEN, "rev-1").digest()
    assert first.digest() != project(FROZEN, "rev-2").digest()
    assert first.digest() != project(FROZEN[:-1], "rev-1").digest()


def test_execute_code_declaration_is_fixed_and_strict():
    schema = EXECUTE_CODE_TOOL.parameters
    assert schema["required"] == ["code"] and schema["additionalProperties"] is False
    assert EXECUTE_CODE_TOOL.result_cacheable is False


@pytest.mark.parametrize(
    ("raw", "executed", "status"),
    [
        ('{"ok":true,"data":{"x":1}}', True, "succeeded"),
        ('{"ok":true,"data":{"pending":true,"run_id":"r"}}', True, "pending"),
        ('{"ok":false,"uncertain":true,"error":"timeout"}', True, "unknown"),
        ('{"ok":false,"executed":false,"error":"capability_not_allowed"}', False, "not_executed"),
        ('{"ok":false,"error":"not_found"}', True, "failed"),
    ],
)
def test_receipt_view_keeps_status_fields_separate(raw, executed, status):
    evidence = {
        "ok": status in {"succeeded", "pending"},
        "executed": executed,
        "pending": status == "pending",
        "uncertain": status == "unknown",
        "error_code": "test_failure" if status not in {"succeeded", "pending"} else None,
    }
    view = receipt_view(raw, evidence=evidence, operation_id="op", executed=executed)
    assert view.status == status
    assert view.operation_id == "op"
    if status != "succeeded" and status != "pending":
        assert view.error is not None and view.ok is False


def test_receipt_view_marks_truncated_results_incomplete():
    view = receipt_view(
        '{"ok":true,"truncated":true,"artifact_handle":"a1"}',
        evidence={"ok": True},
        operation_id="op",
        executed=True,
    )
    assert view.complete is False and view.result_ref == "a1"


@pytest.mark.parametrize("uncertain", [True, False])
def test_receipt_display_cannot_override_typed_execution_state(uncertain):
    view = receipt_view(
        json.dumps({"ok": uncertain, "uncertain": not uncertain, "executed": False}),
        evidence={"ok": not uncertain, "uncertain": uncertain, "executed": True},
        operation_id="original",
        executed=True,
    )
    assert view.status == ("unknown" if uncertain else "succeeded")
    assert view.executed is True and view.uncertain is uncertain
