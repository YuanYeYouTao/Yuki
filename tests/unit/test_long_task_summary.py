"""Keep incomplete outcomes and costs visible in the benchmark comparison."""

from copy import deepcopy

import pytest
from scripts.summarize_long_tasks import markdown, summarize


def report_fixture():
    records = []
    for repeat in range(2):
        for loop in ("old", "new"):
            for mode in ("direct", "code"):
                completed = not (loop == "old" and mode == "direct" and repeat == 0)
                records.append(
                    {
                        "task": "batch_ledger",
                        "repeat": repeat,
                        "loop": loop,
                        "mode": mode,
                        "input_sha256": str(repeat),
                        "success": completed,
                        "completion_fraction": 1 if completed else 0.5,
                        "correct_artifacts": completed,
                        "final_work_state": "completed" if completed else "queued",
                        "stopped_for_stall": not completed,
                        "failure_details": [],
                        "total_seconds": 10 if completed else 1,
                        "first_correct_artifact_seconds": 9 if completed else None,
                        "physical_http": 2,
                        "segments": [{}],
                        "code_used": mode == "code",
                        "duplicate_writes": 0,
                        "duplicate_operation_ids": 0,
                        "wire": [
                            {
                                "estimated_peak_usd": 0.1,
                                "tokens": {"input": 100, "cached": 50, "output": 10},
                                "tools_sha256": "same-tools",
                            }
                        ],
                    }
                )
    return {"records": records, "in_progress": None, "budget": {}}


def test_failed_fast_attempt_is_not_completion_speed_or_free_cost():
    result = summarize(report_fixture())
    group = next(g for g in result["groups"] if (g["loop"], g["mode"]) == ("old", "direct"))
    assert (group["completed"], group["attempted"]) == (1, 2)
    assert group["median_seconds_completed_only"] == 10
    assert group["total_seconds_all_attempts"] == 11
    assert group["cost_per_completed_task_including_failures_usd"] == 0.2
    assert group["tokens_all_attempts"] == {"input": 200, "cached": 100, "output": 20}
    assert len(result["completed_same_input_pairs"]) == 3
    assert "1/2|75%|10.00|2|0.200000" in markdown(result)


def test_unknown_usage_is_retained_separately_from_known_cost():
    report = report_fixture()
    report["records"][0]["wire"].append({"reserved_usd": 0.25, "tools_sha256": "same-tools"})
    result = summarize(report)
    assert result["trials"][0]["known_usage_peak_usd"] == 0.1
    assert result["trials"][0]["unknown_usage_calls"] == 1
    assert result["trials"][0]["unknown_usage_reserved_usd"] == 0.25


def test_tool_free_compaction_is_counted_without_changing_the_main_contract():
    report = report_fixture()
    report["records"][0]["wire"].append(
        {
            "purpose": "work_compaction",
            "tools_count": 0,
            "tools_sha256": "tool-free-separate-chain",
            "estimated_peak_usd": 0.05,
            "tokens": {"input": 200, "cached": 0, "output": 20},
        }
    )
    result = summarize(report)
    assert result["all_groups_same_serialized_tools"]
    assert result["trials"][0]["known_usage_peak_usd"] == pytest.approx(0.15)
    assert result["trials"][0]["tokens"] == {"input": 300, "cached": 50, "output": 30}
    report["records"][0]["wire"][-1]["tools_count"] = 76
    with pytest.raises(ValueError, match="tool-free contract"):
        summarize(report)


def test_default_policy_uses_complete_current_pairs_and_observed_context():
    report = report_fixture()
    report["default_code_policy"] = True
    report["records"] = [r for r in report["records"] if r["loop"] == "new"]
    for row in report["records"]:
        row["logical_models"] = 2
        row["wire"][0].update(message_bytes=300, tool_receipt_characters=40)
    result = summarize(report)
    assert (result["completed"], result["attempted"]) == (4, 4)
    assert len(result["completed_same_input_pairs"]) == 2
    assert result["trials"][0]["logical_requests"] == 2
    assert result["trials"][0]["context"] == {
        "total_message_bytes": 300,
        "peak_message_bytes": 300,
        "total_tool_receipt_characters": 40,
        "measured": True,
    }
    assert "相同当前运行时" in markdown(result)
    report["records"].pop()
    with pytest.raises(ValueError, match="missing a group"):
        summarize(report)


def test_default_policy_cannot_claim_acceptance_without_actual_code_choice():
    report = report_fixture()
    report["default_code_policy"] = True
    report["records"] = [r for r in report["records"] if r["loop"] == "new"]
    report["records"][1]["code_used"] = False
    with pytest.raises(ValueError, match="did not use Code Mode"):
        summarize(report)


def test_goal_completion_and_orchestration_acceptance_remain_separate():
    report = report_fixture()
    report["default_code_policy"] = True
    report["records"] = [r for r in report["records"] if r["loop"] == "new"]
    row = report["records"][1]
    row.update(success=False, code_used=False)
    row.update(all_inputs_read=True, final_report_verified=True, audit_order_verified=True)
    result = summarize(report)
    assert (result["goal_completed"], result["completed"]) == (4, 3)
    assert result["trials"][1]["goal_completed"]
    row["audit_order_verified"] = False
    assert summarize(report)["goal_completed"] == 3


@pytest.mark.parametrize("segment_tools", [None, 5, 32])
def test_markdown_reports_actual_segment_configuration(segment_tools):
    report = report_fixture()
    report.update(segment_tools_override=segment_tools, reasoning_effort="high")
    text = markdown(summarize(report))
    if segment_tools is None:
        assert "恢复任务 5 次，批量与依赖链 80 次" in text
    else:
        assert f"本轮统一分段额度：{segment_tools} 次" in text
        assert "恢复任务 5 次，批量" not in text
    assert "本轮 reasoning_effort：high" in text


@pytest.mark.parametrize("field", ["instruction_sha256", "segment_tools"])
def test_new_report_configuration_must_match_across_groups(field):
    report = report_fixture()
    for row in report["records"]:
        row[field] = "same" if field == "instruction_sha256" else 32
    assert summarize(report)["completed"] == 7
    report["records"][0][field] = "different" if field == "instruction_sha256" else 5
    with pytest.raises(ValueError, match="different task configuration"):
        summarize(report)


@pytest.mark.parametrize("field", ["correct_artifacts", "final_work_state", "stopped_for_stall"])
def test_completed_claim_requires_artifacts_and_durable_completion(field):
    report = report_fixture()
    row = report["records"][1]
    row[field] = {
        "correct_artifacts": False,
        "final_work_state": "queued",
        "stopped_for_stall": True,
    }[field]
    with pytest.raises(ValueError, match="contradicts recorded evidence"):
        summarize(report)


@pytest.mark.parametrize("difference", ["input", "tools", "missing", "duplicate", "unfinished"])
def test_incomparable_or_unfinished_collection_is_rejected(difference):
    report = deepcopy(report_fixture())
    if difference == "input":
        report["records"][0]["input_sha256"] = "different"
    elif difference == "tools":
        report["records"][0]["wire"][0]["tools_sha256"] = "different"
    elif difference == "missing":
        report["records"].pop()
    elif difference == "duplicate":
        report["records"].append(report["records"][0])
    else:
        report["in_progress"] = {"task": "still-running"}
    with pytest.raises(ValueError):
        summarize(report)
