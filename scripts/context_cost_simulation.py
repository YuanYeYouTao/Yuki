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
) -> dict:
    if target * window < retention_floor:
        return {
            "feasible": False,
            "window": window,
            "trigger": trigger,
            "target": target,
            "reason": "below_assumed_information_retention_floor",
        }
    context = retention_floor
    fresh = False
    input_tokens = cached_tokens = summary_input = summary_cached = compactions = 0
    total_output = 0
    peak = 0
    for index in range(requests):
        delta = growth[(index * 37 + 17) % len(growth)]
        if context + delta >= window * trigger:
            summary_input += context
            summary_cached += int(context * summary_cache_ratio)
            compactions += 1
            context = int(target * window)
            fresh = True
        context += delta
        # A watermark isn't the model's hard limit. Reserve 4096 output + 2048 error tokens.
        if context + 6144 > window:
            return {
                "feasible": False,
                "window": window,
                "trigger": trigger,
                "target": target,
                "reason": "predicted_request_exceeds_reserved_window",
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
        "feasible": True,
        "window": window,
        "trigger": trigger,
        "target": target,
        "compactions": compactions,
        "requests": requests,
        "peak_input": peak,
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
    parser.add_argument("--agm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    yuki = json.loads(args.yuki.read_text(encoding="utf-8-sig"))
    agm = json.loads(args.agm.read_text(encoding="utf-8-sig"))
    summary = stats(yuki["rows"])
    # Policy assumptions, not measurements of fixed-prefix or semantic quality.
    prefix = 12_000
    retention_floor = prefix + 4000 + 8 * summary["growth_p95"]
    scenarios = {}
    for price in (0.1, 0.25, 0.5, 1.0):
        for hit in (summary["known_cache_ratio"], 0.5, 0.0):
            for summary_hit in (0.0, 0.5):
                key = f"cacheprice={price}:hit={hit:.4f}:summaryhit={summary_hit}"
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
                    )
                    for window in WINDOWS
                    for trigger in TRIGGERS
                    for target in TARGETS
                ]
    chat_thresholds = [
        {
            "window": window,
            "trigger": trigger,
            "observed_inputs_above_trigger": sum(v >= window * trigger for v in summary["inputs"]),
        }
        for window in WINDOWS
        for trigger in TRIGGERS
    ]
    retention_sensitivity = []
    for floor in (retention_floor, 48_000, 64_000):
        for summary_tokens in (500, 2000, 8000):
            for prefix_survives in (True, False):
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
                    )
                    for window in WINDOWS
                    for trigger in TRIGGERS
                    for target in TARGETS
                ]
                winner = min(
                    (row for row in grid if row["feasible"]),
                    key=lambda row: row["relative_input_price_millions"],
                )
                retention_sensitivity.append(
                    {
                        "floor": floor,
                        "summary_output": summary_tokens,
                        "prefix_survives": prefix_survives,
                        "winner": winner,
                    }
                )
    result = {
        "collected_at": yuki["collected_at"],
        "measurement": {
            key: value for key, value in summary.items() if key not in {"growth", "inputs"}
        },
        "agm": {
            "request_rows": len(agm["requests"]),
            "request_cache_missing": sum(r["cached_tokens"] is None for r in agm["requests"]),
            "request_cache_explicit_zero": sum(r["cached_tokens"] == 0 for r in agm["requests"]),
            "token_stats_cache_zero": sum(r["cached_tokens"] == 0 for r in agm["usage"]),
        },
        "assumptions": {
            "fixed_prefix_tokens": prefix,
            "task_facts_tokens": 4000,
            "recent_rounds": 8,
            "retention_floor": retention_floor,
            "output_and_error_reserve": 6144,
            "summary_output_tokens": 2000,
            "execution_output_tokens": 46,
            "long_requests": 800,
            "output_input_price_ratio": 5,
            "simulation_type": "measured_increment_driven_synthetic_long_task",
        },
        "chat_observed_thresholds": chat_thresholds,
        "long_scenarios": scenarios,
        "retention_sensitivity": retention_sensitivity,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    primary = next(iter(scenarios.values()))
    for window in WINDOWS:
        valid = [row for row in primary if row["feasible"] and row["window"] == window]
        print(json.dumps(min(valid, key=lambda row: row["relative_input_price_millions"])))


if __name__ == "__main__":
    main()
