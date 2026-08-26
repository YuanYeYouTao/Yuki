"""Stable regression semantics for Memory Quality performance signals."""

from __future__ import annotations

from datetime import UTC, datetime

from qq_ai_bot.memory.quality.gates import GateConfiguration, compare_baseline
from qq_ai_bot.memory.quality.models import QualityBaseline, QualityMetricValue


def _baseline() -> QualityBaseline:
    return QualityBaseline(
        suite_version="memory-v2-quality-v1",
        commit="baseline",
        python_version="3.12",
        sqlite_version="3",
        generated_at=datetime.now(UTC),
        dataset_hash="dataset",
        gate_config_hash="gates",
        fake_model_id="fake-model",
        fake_embedding_id="fake-embedding",
        case_count=18,
        metrics={
            "quality_suite_total_ms": 100.0,
            "extraction_latency_p50_ms": 10.0,
        },
    )


def _metric(value: float) -> QualityMetricValue:
    return QualityMetricValue(value=value, numerator=value, denominator=1, unit="milliseconds")


def _configuration() -> GateConfiguration:
    return GateConfiguration(
        schema_version="1",
        gates=(),
        max_absolute_drop=0.01,
        max_latency_ratio=1.25,
        max_model_request_ratio=1.1,
        file_hash="gates",
    )


def test_single_sample_suite_total_is_report_only() -> None:
    regressions = compare_baseline(
        {
            "quality_suite_total_ms": _metric(1000.0),
            "extraction_latency_p50_ms": _metric(10.0),
        },
        _baseline(),
        _configuration(),
    )

    assert regressions == ()


def test_per_operation_latency_regression_remains_blocking() -> None:
    regressions = compare_baseline(
        {
            "quality_suite_total_ms": _metric(100.0),
            "extraction_latency_p50_ms": _metric(13.0),
        },
        _baseline(),
        _configuration(),
    )

    assert regressions == ("extraction_latency_p50_ms:ratio=1.3000",)
