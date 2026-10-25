"""Result display truncation preserves original paid evidence and control facts."""

import hashlib
import json
from types import SimpleNamespace

from yuki.codemode.driver import CodeModeDriver


def driver(limit, archive=None):
    result = object.__new__(CodeModeDriver)
    result.host = SimpleNamespace(result_limit=limit, archive=archive)
    return result


async def test_operation_prefix_is_maximal_and_original_composition_is_archived():
    archived = []

    async def archive(value):
        archived.append(value)
        return "original-handle"

    original = {
        "ok": True,
        "operations": [{"id": str(index), "text": "中\n" * 10} for index in range(100)],
        "control": {"state": "waiting_external", "wait_id": "original-wait"},
        "stop_reason": "control",
    }
    result = await driver(800, archive)._bounded_result(original, "original-parent")
    page = json.loads(result)
    assert len(result) <= 800
    assert len(json.loads(archived[0])["operations"]) == 100
    assert page["summary_ref"] == "original-handle"
    assert page["control"] == {"state": "waiting_external", "wait_id": "original-wait"}
    assert (
        page["operations_ref"]["parent_key_sha256"]
        == hashlib.sha256(b"original-parent").hexdigest()
    )
    assert page["operations"]
    next_operation = json.loads(archived[0])["operations"][len(page["operations"])]
    assert (
        len(
            json.dumps(
                {**page, "operations": [*page["operations"], next_operation]},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        > 800
    )


async def test_stdout_truncated_flag_is_changed_after_original_prefix_selection():
    original = {
        "operations": [],
        "stdout": "中\n" * 100,
        "stdout_truncated": False,
        "stop_reason": "compact",
    }
    # The original false flag occupies one more character than true. It is
    # changed after prefix selection; the new byte is not used to extend stdout.
    empty = {**original, "stdout": ""}
    fixed = len(json.dumps(empty, ensure_ascii=False, separators=(",", ":")))
    result = json.loads(await driver(fixed + 3)._bounded_result(original, "p"))
    assert result["stdout"] == "中\n"
    assert result["stdout_truncated"] is True
    assert result["stop_reason"] == "compact"


async def test_oversized_control_digest_points_to_the_original_value():
    control = {"state": "waiting_external", "wait_id": "original-wait", "detail": "x" * 2000}
    original = {"operations": [], "control": control, "stop_reason": "control"}
    result = json.loads(await driver(200)._bounded_result(original, "p"))
    assert "control" not in result
    assert (
        result["control_ref_sha256"]
        == hashlib.sha256(json.dumps(control, ensure_ascii=False).encode()).hexdigest()
    )
    assert result["stop_reason"] == "control"
