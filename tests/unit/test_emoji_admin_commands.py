"""The retired pool has no current command or SDK; original receipts remain readable."""

from yuki.admin.authority.command_types import CommandOperation, project_replayed_result


def test_original_emoji_control_receipt_keeps_original_change():
    action = "adopt"
    state = {
        "resource": "original-asset",
        "revision": 1,
        "status": "applied",
        "change": {"action": action, "adopted": action == "adopt"},
    }
    result = project_replayed_result(
        operation=CommandOperation.EMOJI_MUTATE,
        resource_id="original-asset",
        revision=1,
        audit_id="original-audit",
        raw_state=state,
        semantic_target_id="original-asset",
        material={"action": action},
    )
    assert result.success and result.resource_id == "original-asset"
    assert result.effective_state["change"] == state["change"]
