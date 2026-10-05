"""Content-free sensitivity model; measured chat traffic is not a long-Work benchmark."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

WINDOWS = (64_000, 96_000, 128_000, 160_000, 192_000, 256_000)
TRIGGERS = (0.75, 0.80, 0.85, 0.90)
TARGETS = (0.40, 0.50, 0.60, 0.65)
ESTIMATE_RATIOS = (1.0, 1.25, 1.5, 1.8, 2.0)


def percentile(values: list[int], fraction: float) -> int:
    return sorted(values)[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)]


def stats(rows: list[dict]) -> dict:
    chat = [r for r in rows if r["task"] == "chat_agent" and r["model"] == "gemini-3.8-flash"]
    known = [r for r in chat if r["success"] and r["prompt_tokens"]]
    cached = [r for r in known if r["cached_prompt_tokens"] is not None]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in known:
        if row["runtime_turn_id"]:
            grouped[row["runtime_turn_id"]].append(row)
    growth = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda row: row["created_at"])
        growth.extend(
            b["prompt_tokens"] - a["prompt_tokens"]
            for a, b in pairwise(ordered)
            if b["prompt_tokens"] > a["prompt_tokens"]
        )
    inputs = [r["prompt_tokens"] for r in known]
    cache_sum = sum(r["cached_prompt_tokens"] for r in cached)
    measured_input = sum(r["prompt_tokens"] for r in cached)
    summary = [r for r in rows if r["task"] == "conversation_compaction"]
    return {
        "chat_rows": len(chat),
        "known_success_inputs": len(known),
        "cache_missing": sum(r["cached_prompt_tokens"] is None for r in chat),
        "cache_explicit_zero": sum(r["cached_prompt_tokens"] == 0 for r in chat),
        "input_min": min(inputs),
        "input_median": statistics.median(inputs),
        "input_p95": percentile(inputs, 0.95),
        "input_max": max(inputs),
        "known_cache_ratio": cache_sum / measured_input,
        "unknown_as_miss_cache_ratio_lower_bound": cache_sum / sum(inputs),
        "positive_growth_count": len(growth),
        "growth_median": statistics.median(growth),
        "growth_p95": percentile(growth, 0.95),
        "growth_max": max(growth),
        "multi_request_turns": sum(len(group) > 1 for group in grouped.values()),
        "max_requests_in_observed_turn": max(map(len, grouped.values())),
        "observed_rollup_calls": len(summary),
        "observed_rollup_input_sum": sum(r["prompt_tokens"] or 0 for r in summary),
        "observed_rollup_output_sum": sum(r["completion_tokens"] or 0 for r in summary),
        "growth": growth,
        "inputs": inputs,
    }


def simulate(
    growth: list[int],
    *,
    window: int,
    trigger: float,
    target: float,
    cache_ratio: float,
    cache_price: float,
    fixed_prefix: int,
    retention_floor: int,
    summary_output: int,
    summary_cache_ratio: float,
    requests: int = 800,
    prefix_survives: bool = True,
    estimate_ratio: float = 1.0,
    input_margin_tokens: int = 0,
    model_input_limit: int | None = None,
    model_context_limit: int | None = None,
    output_reserve_tokens: int = 4096,
) -> dict:
    if estimate_ratio <= 0 or input_margin_tokens < 0 or output_reserve_tokens < 0:
        raise ValueError("invalid_capacity_assumption")
    capacity = {
        "window": window,
        "trigger": trigger,
        "target": target,
        "estimate_ratio": estimate_ratio,
        "window_unit": "estimated_input_tokens",
    }
    # Measured increments and costs stay in provider usage units. Runtime policy
    # windows use conservative estimates, which need not equal provider usage.
    if target * window / estimate_ratio < retention_floor:
        return {
            **capacity,
            "feasible": False,
            "reason": "below_assumed_information_retention_floor",
        }
    context = retention_floor
    fresh = False
    input_tokens = cached_tokens = summary_input = summary_cached = compactions = 0
    total_output = 0
    peak = 0
    for index in range(requests):
        delta = growth[(index * 37 + 17) % len(growth)]
        if (context + delta) * estimate_ratio >= window * trigger:
            summary_input += context
            summary_cached += int(context * summary_cache_ratio)
            compactions += 1
            context = int(target * window / estimate_ratio)
            fresh = True
        context += delta
        # An independent input budget never subtracts output again. Only an
        # explicitly supplied joint model context limit includes output reserve.
        estimated = math.ceil(context * estimate_ratio)
        reason = None
        if estimated + input_margin_tokens > window:
            reason = "predicted_estimated_input_exceeds_policy_budget"
        elif model_input_limit is not None and context > model_input_limit:
            reason = "predicted_usage_input_exceeds_model_input_limit"
        elif (
            model_context_limit is not None
            and context + output_reserve_tokens > model_context_limit
        ):
            reason = "predicted_usage_input_and_output_exceed_joint_model_context"
        if reason is not None:
            return {
                **capacity,
                "feasible": False,
                "reason": reason,
            }
        peak = max(peak, context)
        input_tokens += context
        cached_tokens += (
            int(min(context, fixed_prefix) * cache_ratio)
            if fresh and prefix_survives
            else 0
            if fresh
            else int(context * cache_ratio)
        )
        fresh = False
        # Execution output remains constant across policies and does not determine the winner.
        total_output += 46
    total_output += compactions * summary_output
    uncached = input_tokens - cached_tokens + summary_input - summary_cached
    cached = cached_tokens + summary_cached
    cost = (uncached + cached * cache_price + total_output * 5) / 1_000_000
    return {
        **capacity,
        "feasible": True,
        "compactions": compactions,
        "requests": requests,
        "peak_input": peak,
        "peak_estimated_input": math.ceil(peak * estimate_ratio),
        "execution_input": input_tokens,
        "execution_cached": cached_tokens,
        "summary_input": summary_input,
        "summary_cached": summary_cached,
        "summary_output": compactions * summary_output,
        "relative_input_price_millions": cost,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yuki", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--estimate-ratios", nargs="+", type=float, default=ESTIMATE_RATIOS)
    parser.add_argument("--input-margin-tokens", type=int, default=0)
    parser.add_argument("--model-input-limit", type=int)
    parser.add_argument("--model-context-limit", type=int)
    parser.add_argument("--output-reserve-tokens", type=int, default=4096)
    args = parser.parse_args()
    yuki = json.loads(args.yuki.read_text(encoding="utf-8-sig"))
    summary = stats(yuki["rows"])
    # Policy assumptions, not measurements of fixed-prefix or semantic quality.
    prefix = 12_000
    retention_floor = prefix + 4000 + 8 * summary["growth_p95"]
    scenarios = {}
    capacity_options = {
        "input_margin_tokens": args.input_margin_tokens,
        "model_input_limit": args.model_input_limit,
        "model_context_limit": args.model_context_limit,
        "output_reserve_tokens": args.output_reserve_tokens,
    }
    for price in (0.1, 0.25, 0.5, 1.0):
        for hit in (summary["known_cache_ratio"], 0.5, 0.0):
            for summary_hit in (0.0, 0.5):
                for ratio in args.estimate_ratios:
                    key = (
                        f"cacheprice={price}:hit={hit:.4f}:summaryhit={summary_hit}"
                        f":estimate_ratio={ratio}"
                    )
                    scenarios[key] = [
                        simulate(
                            summary["growth"],
                            window=window,
                            trigger=trigger,
                            target=target,
                            cache_ratio=hit,
                            cache_price=price,
                            fixed_prefix=prefix,
                            retention_floor=retention_floor,
                            summary_output=2000,
                            summary_cache_ratio=summary_hit,
                            estimate_ratio=ratio,
                            **capacity_options,
                        )
                        for window in WINDOWS
                        for trigger in TRIGGERS
                        for target in TARGETS
                    ]
    chat_thresholds = [
        {
            "window": window,
            "trigger": trigger,
            "estimate_ratio": ratio,
            "observed_inputs_above_trigger": sum(
                v * ratio >= window * trigger for v in summary["inputs"]
            ),
        }
        for window in WINDOWS
        for trigger in TRIGGERS
        for ratio in args.estimate_ratios
    ]
    retention_sensitivity = []
    for floor in (retention_floor, 48_000, 64_000):
        for summary_tokens in (500, 2000, 8000):
            for prefix_survives, ratio in (
                (survives, ratio) for survives in (True, False) for ratio in args.estimate_ratios
            ):
                grid = [
                    simulate(
                        summary["growth"],
                        window=window,
                        trigger=trigger,
                        target=target,
                        cache_ratio=summary["known_cache_ratio"],
                        cache_price=0.1,
                        fixed_prefix=prefix,
                        retention_floor=floor,
                        summary_output=summary_tokens,
                        summary_cache_ratio=0.0,
                        prefix_survives=prefix_survives,
                        estimate_ratio=ratio,
                        **capacity_options,
                    )
                    for window in WINDOWS
                    for trigger in TRIGGERS
                    for target in TARGETS
                ]
                winner = min(
                    (row for row in grid if row["feasible"]),
                    key=lambda row: row["relative_input_price_millions"],
                    default=None,
                )
                retention_sensitivity.append(
                    {
                        "floor": floor,
                        "summary_output": summary_tokens,
                        "prefix_survives": prefix_survives,
                        "estimate_ratio": ratio,
                        "winner": winner,
                    }
                )
    result = {
        "collected_at": yuki["collected_at"],
        "measurement": {
            key: value for key, value in summary.items() if key not in {"growth", "inputs"}
        },
        "assumptions": {
            "fixed_prefix_tokens": prefix,
            "task_facts_tokens": 4000,
            "recent_rounds": 8,
            "retention_floor": retention_floor,
            "window_unit": "estimated_input_tokens",
            "measured_increment_and_cost_unit": "provider_usage_tokens",
            "model_capacity_limit_unit": "provider_usage_tokens_if_certified",
            "output_reserve_applies_only_to_joint_model_context": True,
            "estimate_ratios": args.estimate_ratios,
            **capacity_options,
            "summary_output_tokens": 2000,
            "execution_output_tokens": 46,
            "long_requests": 800,
            "output_input_price_ratio": 5,
            "simulation_type": "measured_increment_driven_synthetic_long_task",
        },
        "chat_observed_thresholds": chat_thresholds,
        "long_scenarios": scenarios,
        "retention_sensitivity": retention_sensitivity,
        "work_initial_policy_sensitivity": [
            simulate(
                summary["growth"],
                window=128_000,
                trigger=0.90,
                target=0.50,
                cache_ratio=summary["known_cache_ratio"],
                cache_price=0.1,
                fixed_prefix=prefix,
                retention_floor=retention_floor,
                summary_output=8192,
                summary_cache_ratio=0.0,
                estimate_ratio=ratio,
                **capacity_options,
            )
            for ratio in args.estimate_ratios
        ],
        "work_previous_policy_sensitivity": [
            simulate(
                summary["growth"],
                window=128_000,
                trigger=0.85,
                target=0.50,
                cache_ratio=summary["known_cache_ratio"],
                cache_price=0.1,
                fixed_prefix=prefix,
                retention_floor=retention_floor,
                summary_output=8192,
                summary_cache_ratio=0.0,
                estimate_ratio=ratio,
                **capacity_options,
            )
            for ratio in args.estimate_ratios
        ],
        "chat_initial_policy_retention_sensitivity": [
            simulate(
                summary["growth"],
                window=96_000,
                trigger=0.90,
                target=0.60,
                cache_ratio=summary["known_cache_ratio"],
                cache_price=0.1,
                fixed_prefix=prefix,
                retention_floor=retention_floor,
                summary_output=8192,
                summary_cache_ratio=0.0,
                estimate_ratio=ratio,
                **capacity_options,
            )
            for ratio in args.estimate_ratios
        ],
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    primary = next(iter(scenarios.values()))
    for window in WINDOWS:
        valid = [row for row in primary if row["feasible"] and row["window"] == window]
        print(
            json.dumps(
                min(valid, key=lambda row: row["relative_input_price_millions"], default=None)
            )
        )


if __name__ == "__main__":
    main()
