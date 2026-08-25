"""Sanitized JSON/text rendering for identity cutover reports."""

from __future__ import annotations

import json
from typing import Any

from qq_ai_bot.identity.cutover_types import CutoverReport
from qq_ai_bot.identity.sanitize import looks_like_secret_or_path


def report_to_dict(report: CutoverReport) -> dict[str, Any]:
    payload = {
        "mode": report.mode,
        "status": report.status,
        "source_fingerprint": report.source_fingerprint,
        "inventory_version": report.inventory_version,
        "platform": report.platform,
        "git_revision": report.git_revision,
        "run_recorded": report.run_recorded,
        "error_category": report.error_category,
        "business_diff": report.business_diff,
        "counts": {
            "conversations": report.counts.conversations,
            "aliases": report.counts.aliases,
            "routes": report.counts.routes,
            "mapped_events": report.counts.mapped_events,
            "suppressed_events": report.counts.suppressed_events,
            "baselines": report.counts.baselines,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if looks_like_secret_or_path(encoded):
        raise RuntimeError("identity cutover report leaked a path or secret")
    return payload


def render_cutover_report(report: CutoverReport, fmt: str) -> str:
    payload = report_to_dict(report)
    if fmt == "text":
        lines = [
            f"mode={payload['mode']}",
            f"status={payload['status']}",
            f"source_fingerprint={payload['source_fingerprint']}",
            f"conversations={payload['counts']['conversations']}",
            f"mapped_events={payload['counts']['mapped_events']}",
            f"suppressed_events={payload['counts']['suppressed_events']}",
        ]
        if payload["error_category"]:
            lines.append(f"error_category={payload['error_category']}")
        text = "\n".join(lines)
        if looks_like_secret_or_path(text):
            raise RuntimeError("identity cutover report leaked a path or secret")
        return text
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
