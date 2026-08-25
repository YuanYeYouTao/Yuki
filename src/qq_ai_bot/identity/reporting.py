"""Sanitized JSON/text rendering for identity backfill reports.

Imported only by CLI and tests, never by the application service.
"""

from __future__ import annotations

import json
from typing import Any

from qq_ai_bot.identity.backfill_types import BackfillReport
from qq_ai_bot.identity.sanitize import looks_like_secret_or_path


def report_to_dict(report: BackfillReport) -> dict[str, Any]:
    payload = {
        "mode": report.mode,
        "status": report.status,
        "business_diff": report.business_diff,
        "source_fingerprint": report.source_fingerprint,
        "inventory_version": report.inventory_version,
        "platform": report.platform,
        "run_recorded": report.run_recorded,
        "error_category": report.error_category,
        "counts": {
            "processed": report.counts.processed,
            "persons": report.counts.persons,
            "identity_bindings": report.counts.identity_bindings,
            "spaces": report.counts.spaces,
            "space_bindings": report.counts.space_bindings,
            "presences": report.counts.presences,
            "conflicts": report.counts.conflicts,
            "skipped": report.counts.skipped,
            "shadows_filled": report.counts.shadows_filled,
        },
        "classifications": {
            "person": report.counts.person_class,
            "yuki_presence": report.counts.yuki_presence_class,
            "external_bot": report.counts.external_bot_class,
            "space": report.counts.space_class,
        },
        "conflicts": [
            {
                "subject_kind": item.subject_kind,
                "conflict_kind": item.conflict_kind,
                "error_category": item.error_category,
                "fingerprint": item.fingerprint,
            }
            for item in report.conflicts
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if looks_like_secret_or_path(encoded):
        raise RuntimeError("identity backfill report leaked a path or secret")
    return payload


def render_report(report: BackfillReport, fmt: str) -> str:
    payload = report_to_dict(report)
    if fmt == "text":
        lines = [
            f"mode={payload['mode']}",
            f"status={payload['status']}",
            f"business_diff={payload['business_diff']}",
            f"source_fingerprint={payload['source_fingerprint']}",
            f"persons={payload['counts']['persons']}",
            f"spaces={payload['counts']['spaces']}",
            f"presences={payload['counts']['presences']}",
            f"external_bots={payload['classifications']['external_bot']}",
            f"conflicts={payload['counts']['conflicts']}",
        ]
        for item in payload["conflicts"]:
            lines.append(
                "conflict "
                f"{item['subject_kind']}/{item['conflict_kind']}/"
                f"{item['error_category']}/{item['fingerprint']}"
            )
        text = "\n".join(lines)
        if looks_like_secret_or_path(text):
            raise RuntimeError("identity backfill report leaked a path or secret")
        return text
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
