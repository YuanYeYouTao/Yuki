"""Summarize observed completion, cost and latency; never issues paid requests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from typing import Any


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    records = report["records"]
    loops = ("new",) if report.get("default_code_policy") else ("old", "new")
    keys = {(r["task"], r["repeat"], r["loop"], r["mode"]) for r in records}
    if not records or len(keys) != len(records) or report.get("in_progress"):
        raise ValueError("duplicate or unfinished benchmark collection")
    for task in {r["task"] for r in records}:
        for repeat in {r["repeat"] for r in records if r["task"] == task}:
            selected = [r for r in records if r["task"] == task and r["repeat"] == repeat]
            if {(r["loop"], r["mode"]) for r in selected} != {
                (loop, mode) for loop in loops for mode in ("direct", "code")
            }:
                raise ValueError("comparison dataset is missing a group")
            hashes = {
                r["input_sha256"] for r in records if r["task"] == task and r["repeat"] == repeat
            }
            if len(hashes) != 1:
                raise ValueError("comparison groups did not receive identical data")
            # New reports also expose the task wording and segment allowance.
            # Preserve old evidence compatibility, but never mix new configs.
            for field in ("instruction_sha256", "segment_tools"):
                if any(field in row for row in selected) and (
                    not all(field in row for row in selected)
                    or len({row[field] for row in selected}) != 1
                ):
                    raise ValueError("comparison groups have different task configuration")
    # Work compaction is a separately paid, tool-free chain; it does not change
    # the main chain's declarations. Retain its cost/time in every total below.
    if any(
        w.get("purpose") == "work_compaction" and w.get("tools_count") != 0
        for r in records
        for w in r["wire"]
    ):
        raise ValueError("compaction request changed the tool-free contract")
    hashes = {
        w["tools_sha256"] for r in records for w in r["wire"] if w.get("purpose", "main") == "main"
    }
    if len(hashes) > 1:
        raise ValueError("serialized tool contract changed across groups")
    groups, trials = [], []
    for row in records:
        if row["success"] and (
            not row["correct_artifacts"]
            or row["final_work_state"] != "completed"
            or row["stopped_for_stall"]
            or row.get("audit_order_verified") is False
        ):
            raise ValueError("completion claim contradicts recorded evidence")
        if (
            report.get("default_code_policy")
            and row["mode"] == "code"
            and row["success"]
            and not row["code_used"]
        ):
            raise ValueError("default-policy acceptance did not use Code Mode")
        known_cost = sum(w.get("estimated_peak_usd", 0) for w in row["wire"])
        unknown = [w for w in row["wire"] if "estimated_peak_usd" not in w]
        tokens = {
            key: sum(w.get("tokens", {}).get(key, 0) for w in row["wire"])
            for key in ("input", "cached", "output")
        }
        goal_completed = row.get("goal_completed")
        if goal_completed is None:
            # Older evidence bundled tool-mode compliance into success. Infer
            # completion only when every independent acceptance fact is present;
            # otherwise keep the older, conservative recorded conclusion.
            facts = ("all_inputs_read", "final_report_verified", "audit_order_verified")
            goal_completed = (
                row["correct_artifacts"]
                and row["final_work_state"] == "completed"
                and not row["stopped_for_stall"]
                and not row["duplicate_writes"]
                and row.get("error_category") is None
                and all(row[field] for field in facts)
                if all(field in row for field in facts)
                else row["success"]
            )
        trials.append(
            {
                "task": row["task"],
                "repeat": row["repeat"],
                "loop": row["loop"],
                "mode": row["mode"],
                "completed": row["success"],
                "goal_completed": goal_completed,
                "completion_fraction": row["completion_fraction"],
                "correct_artifacts": row["correct_artifacts"],
                "work_state": row["final_work_state"],
                "stopped_for_stall": row["stopped_for_stall"],
                "failures": row["failure_details"],
                "seconds_spent": row["total_seconds"],
                "first_correct_artifact_seconds": row["first_correct_artifact_seconds"],
                "physical_requests": row["physical_http"],
                "logical_requests": row.get("logical_models"),
                "context": {
                    "total_message_bytes": sum(w.get("message_bytes", 0) for w in row["wire"]),
                    "peak_message_bytes": max(
                        (w.get("message_bytes", 0) for w in row["wire"]), default=0
                    ),
                    "total_tool_receipt_characters": sum(
                        w.get("tool_receipt_characters", 0) for w in row["wire"]
                    ),
                    "measured": all("message_bytes" in w for w in row["wire"]),
                },
                "tokens": tokens,
                "known_usage_peak_usd": known_cost,
                "unknown_usage_calls": len(unknown),
                "unknown_usage_reserved_usd": sum(w["reserved_usd"] for w in unknown),
                "segments": len(row["segments"]),
                "code_used": row["code_used"],
                "segment_tools": row.get("segment_tools"),
                "repeated_committed_path_writes": row["duplicate_writes"],
                "duplicate_operation_ids": row["duplicate_operation_ids"],
                "audit_order_verified": row.get("audit_order_verified"),
            }
        )
    for task in sorted({r["task"] for r in trials}):
        for loop in loops:
            for mode in ("direct", "code"):
                selected = [
                    r for r in trials if (r["task"], r["loop"], r["mode"]) == (task, loop, mode)
                ]
                completed = [r for r in selected if r["completed"]]
                if not selected:
                    continue
                cost = sum(r["known_usage_peak_usd"] for r in selected)
                groups.append(
                    {
                        "task": task,
                        "loop": loop,
                        "mode": mode,
                        "completed": len(completed),
                        "attempted": len(selected),
                        "mean_completion_fraction": sum(r["completion_fraction"] for r in selected)
                        / len(selected),
                        "median_seconds_completed_only": median(
                            r["seconds_spent"] for r in completed
                        )
                        if completed
                        else None,
                        "median_requests_completed_only": median(
                            r["physical_requests"] for r in completed
                        )
                        if completed
                        else None,
                        "total_seconds_all_attempts": sum(r["seconds_spent"] for r in selected),
                        "total_known_peak_usd": cost,
                        "cost_per_completed_task_including_failures_usd": cost / len(completed)
                        if completed
                        else None,
                        "unknown_usage_calls": sum(r["unknown_usage_calls"] for r in selected),
                        "physical_requests_all_attempts": sum(
                            r["physical_requests"] for r in selected
                        ),
                        "tokens_all_attempts": {
                            key: sum(r["tokens"][key] for r in selected)
                            for key in ("input", "cached", "output")
                        },
                    }
                )
    paired = []
    for trial in trials:
        if trial["loop"] != "new" or trial["mode"] != "code" or not trial["completed"]:
            continue
        for loop in loops:
            base = next(
                (
                    r
                    for r in trials
                    if (r["task"], r["repeat"], r["loop"], r["mode"])
                    == (trial["task"], trial["repeat"], loop, "direct")
                ),
                None,
            )
            if base is None or not base["completed"]:
                continue
            paired.append(
                {
                    "task": trial["task"],
                    "repeat": trial["repeat"],
                    "baseline_loop": loop,
                    "direct_seconds": base["seconds_spent"],
                    "code_seconds": trial["seconds_spent"],
                    "time_ratio_code_over_direct": trial["seconds_spent"] / base["seconds_spent"],
                    "direct_requests": base["physical_requests"],
                    "code_requests": trial["physical_requests"],
                    "direct_tokens": base["tokens"],
                    "code_tokens": trial["tokens"],
                    "direct_context": base["context"],
                    "code_context": trial["context"],
                    "direct_peak_usd": base["known_usage_peak_usd"],
                    "code_peak_usd": trial["known_usage_peak_usd"],
                }
            )
    return {
        "attempted": len(trials),
        "completed": sum(r["completed"] for r in trials),
        "goal_completed": sum(r["goal_completed"] for r in trials),
        "all_groups_same_input": True,
        "all_groups_same_serialized_tools": True,
        "groups": groups,
        "trials": trials,
        "completed_same_input_pairs": paired,
        "budget": report["budget"],
        "run_contract": {
            key: report.get(key)
            for key in (
                "model",
                "baseline_commit",
                "baseline_scope",
                "max_output_tokens",
                "reasoning_effort",
                "completion_definition",
                "acceptance_definition",
                "stop_policy",
                "business_scope",
                "recovery_scope",
                "harness_sha256",
                "read_identity",
                "receipt_errors_scope",
                "runtime_source_sha256",
                "segment_tools_override",
                "default_code_policy",
                "model_tool_exposure",
                "cost_estimate_basis",
                "context_measurement",
            )
        },
        "cost_basis": "public peak rates times observed wire usage; "
        "off-peak half these rates; not invoice. Unknown usage is separate.",
        "time_basis": "completion medians exclude failures; failure time still appears in "
        "all-attempt totals. Observed sample counts appear in each group.",
    }


def markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# 长任务隔离对照实测",
        "",
        "主指标是独立答案正确、所需文件完整且 Work 已结算完成。"
        "失败的耗时不算完成时间。各组尝试次数见完成栏分母。",
        "",
        "|任务|循环|工具模式|完成|平均内容正确率|完成耗时中位数（秒）|完成请求数中位数|全部尝试峰值用量估算（美元）|",
        "|---|---|---|---|---|---|---|---|",
    ]
    for g in summary["groups"]:
        seconds = g["median_seconds_completed_only"]
        requests = g["median_requests_completed_only"]
        seconds_text = f"{seconds:.2f}" if seconds is not None else "—"
        lines.append(
            f"|{g['task']}|{g['loop']}|{g['mode']}|{g['completed']}/{g['attempted']}|"
            f"{g['mean_completion_fraction']:.0%}|{seconds_text}"
            f"|{requests if requests is not None else '—'}|{g['total_known_peak_usd']:.6f}|"
        )
    lines.extend(
        [
            "",
            "代码组允许混用直接工具；direct 组允许批量直接调用。"
            + (
                "本轮只比较相同当前运行时的直接调用对照与共享默认编排指引；模型自行规划，"
                "未提供程序。"
                if summary["run_contract"].get("default_code_policy")
                else "旧循环使用固定历史主迭代及共同的新 Invocation/Code Mode 内核，仅作为"
                "隔离装配；不是原封不动的旧版部署。"
            )
            + "工具被限制为临时工作区读、列举、写和生命周期控制，没有终端、"
            "生产数据库、真实发送。恢复是同一进程中的新激活，不是进程崩溃。",
            "",
            "费用按 [DeepSeek 官方峰值费率](https://api-docs.deepseek.com/quick_start/pricing/)"
            "和 HTTP 返回用量估算，实际账单未核验。运行的预算、输出上限和停止条件"
            "保留在 JSON 的 budget 和 run_contract 中。",
            "",
            "24 份 CSV 共 480 条记录需跨文件去重汇总；依赖链需要正确走完 18 层分支；"
            "恢复任务包含 12 层分支、12 份审计文件；分段额度见以下配置。",
            "",
        ]
    )
    contract = summary["run_contract"]
    segment_tools = contract.get("segment_tools_override")
    lines.append(
        f"本轮统一分段额度：{segment_tools} 次业务调用。"
        if segment_tools is not None
        else "沿用任务默认额度：恢复任务 5 次，批量与依赖链 80 次业务调用。"
    )
    if contract.get("reasoning_effort") is not None:
        lines.append(f"本轮 reasoning_effort：{contract['reasoning_effort']}。")
    lines.append("")
    if contract.get("default_code_policy"):
        lines.extend(
            [
                f"目标完成：{summary['goal_completed']}/{summary['attempted']}；"
                f"目标及工具编排联合验收：{summary['completed']}/{summary['attempted']}。",
                "代码组联合验收还要求模型自行选择 execute_code；文件正确与编排合规分开统计。",
                "",
            ]
        )
        lines.extend(
            [
                "|任务/重复|模式|物理/逻辑请求|输入/缓存 token|输出 token|"
                "消息累计/峰值字节|工具回执累计字符|",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for trial in summary["trials"]:
            context, tokens = trial["context"], trial["tokens"]
            lines.append(
                f"|{trial['task']}/{trial['repeat']}|{trial['mode']}|"
                f"{trial['physical_requests']}/{trial['logical_requests']}|"
                f"{tokens['input']}/{tokens['cached']}|{tokens['output']}|"
                f"{context['total_message_bytes']}/{context['peak_message_bytes']}|"
                f"{context['total_tool_receipt_characters']}|"
            )
        lines.extend(
            [
                "",
                "消息字节是每次 HTTP 请求的 messages 序列化大小，不含固定工具声明；"
                "累计值包含重复发送的历史。token 来自 Provider 用量，字节不等于 token。"
                "工具回执字符只统计 role=tool 的正文；跨段 Host 注入证据计入消息字节。",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(json.loads(args.report.read_text()))
    args.json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    args.markdown_output.write_text(markdown(summary))


if __name__ == "__main__":
    main()
