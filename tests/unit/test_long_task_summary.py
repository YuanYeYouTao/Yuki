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
