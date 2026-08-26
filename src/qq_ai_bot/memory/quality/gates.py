"""Configurable absolute and regression quality gates."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from qq_ai_bot.memory.quality.models import (
    QualityBaseline,
    QualityGateResult,
    QualityMetricValue,
)

_INFORMATIONAL_BASELINE_METRICS = frozenset({"quality_suite_total_ms"})


@dataclass(frozen=True, slots=True)
class GateDefinition:
    metric: str
    operator: str
    threshold: float
    allow_null: bool = False


@dataclass(frozen=True, slots=True)
class GateConfiguration:
    schema_version: str
    gates: tuple[GateDefinition, ...]
    max_absolute_drop: float
    max_latency_ratio: float
    max_model_request_ratio: float
    file_hash: str


def load_gate_configuration(path: Path) -> GateConfiguration:
    payload = path.read_bytes()
    raw = tomllib.loads(payload.decode("utf-8"))
    gates = tuple(
        GateDefinition(
            metric=str(item["metric"]),
            operator=str(item["operator"]),
            threshold=float(item["threshold"]),
            allow_null=bool(item.get("allow_null", False)),
        )
        for item in raw.get("gate", ())
    )
    if len({item.metric for item in gates}) != len(gates):
        raise ValueError("quality gate metrics must be unique")
    regression = raw.get("regression", {})
    return GateConfiguration(
        schema_version=str(raw.get("schema_version", "1")),
        gates=gates,
        max_absolute_drop=float(regression.get("max_absolute_drop", 0.01)),
        max_latency_ratio=float(regression.get("max_latency_ratio", 1.25)),
        max_model_request_ratio=float(regression.get("max_model_request_ratio", 1.10)),
        file_hash=hashlib.sha256(payload).hexdigest(),
    )


def evaluate_gates(
    metrics: dict[str, QualityMetricValue],
    configuration: GateConfiguration,
    *,
    allow_not_applicable: bool = False,
) -> tuple[QualityGateResult, ...]:
    results: list[QualityGateResult] = []
    for gate in configuration.gates:
        metric = metrics.get(gate.metric)
        observed = metric.value if metric is not None else None
        if observed is None:
            passed = gate.allow_null or allow_not_applicable
            detail = (
                "not applicable to selected suite"
                if allow_not_applicable
                else "metric has no denominator"
            )
        elif gate.operator == ">=":
            passed = observed >= gate.threshold
            detail = ""
        elif gate.operator == "<=":
            passed = observed <= gate.threshold
            detail = ""
        else:
            raise ValueError(f"unsupported quality gate operator: {gate.operator}")
        results.append(
            QualityGateResult(
                metric=gate.metric,
                passed=passed,
                observed=observed,
                operator=gate.operator,
                threshold=gate.threshold,
                detail=detail,
            )
        )
    return tuple(results)


def compare_baseline(
    metrics: dict[str, QualityMetricValue],
    baseline: QualityBaseline,
    configuration: GateConfiguration,
) -> tuple[str, ...]:
    regressions: list[str] = []
    for name, prior in baseline.metrics.items():
        if name in _INFORMATIONAL_BASELINE_METRICS:
            # This is a sum of one wall-clock observation from each heterogeneous case.
            # Keep it in reports, but use the per-operation p50 metrics for latency gates.
            continue
        current = metrics.get(name)
        if prior is None or current is None or current.value is None:
            continue
        observed = current.value
        if "latency" in name or name.endswith("_ms"):
            if prior < 1.0 and observed - prior < 1.0:
                # Sub-millisecond scheduler noise is not a meaningful performance regression.
                continue
            if prior > 0 and observed / prior > configuration.max_latency_ratio:
                regressions.append(f"{name}:ratio={observed / prior:.4f}")
        elif "request" in name:
            if prior > 0 and observed / prior > configuration.max_model_request_ratio:
                regressions.append(f"{name}:ratio={observed / prior:.4f}")
        elif name.endswith("_accuracy") or name in {
            "case_pass_rate",
            "precision_at_k",
            "recall_at_k",
            "mean_reciprocal_rank",
            "ndcg_at_k",
            "context_precision",
            "context_recall",
        }:
            if prior - observed > configuration.max_absolute_drop:
                regressions.append(f"{name}:drop={prior - observed:.4f}")
        elif observed - prior > configuration.max_absolute_drop:
            regressions.append(f"{name}:increase={observed - prior:.4f}")
    return tuple(sorted(regressions))
