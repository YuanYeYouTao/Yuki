"""Bounded, source-checked task material and derived execution summaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qq_ai_bot.runtime.work_repository import WorkCapacityError


class SourcedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1)
    refs: list[str] = Field(min_length=1)


class SupersededDirective(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    directive_id: str
    refs: list[str] = Field(min_length=1)


class CompactionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1, le=1)
    task_directives: list[SourcedFact]
    superseded_directives: list[SupersededDirective]
    completed: list[SourcedFact]
    pending: list[SourcedFact]
    failures: list[SourcedFact]
    artifacts: list[SourcedFact]
    next_steps: list[SourcedFact]


def summary_json_text(raw: str) -> str:
    """Accept one complete JSON envelope, preserving strict content validation."""
    text = raw.strip()
    lines = text.splitlines()
    if len(lines) >= 3 and lines[0].strip() in {"```", "```json"} and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text


def directive_id(fact: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"text": fact["text"], "refs": sorted(fact["refs"])}, sort_keys=True).encode()
    ).hexdigest()


def validate_summary(raw: str, source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """References prove supplied provenance, never execution or semantic completeness."""
    try:
        summary = CompactionSummary.model_validate_json(summary_json_text(raw)).model_dump()
    except (ValueError, ValidationError) as exc:
        raise WorkCapacityError("work_compaction_invalid_structure") from exc
    allowed = set(source["source_refs"])
    new_refs = {f"input:{item['input_id']}" for item in source["task_inputs"]}
    for section in (
        "task_directives",
        "superseded_directives",
        "completed",
        "pending",
        "failures",
        "artifacts",
        "next_steps",
    ):
        for fact in summary[section]:
            if "text" in fact and not fact["text"].strip():
                raise WorkCapacityError("work_compaction_invalid_structure")
            if len(set(fact["refs"])) != len(fact["refs"]) or not set(fact["refs"]) <= allowed:
                raise WorkCapacityError("work_compaction_invalid_reference")
    previous = {item["id"]: item for item in source["task_material"].get("directives", [])}
    # References prove a set of sources; their presentation order is not a new
    # instruction. Reuse the stored ID of an exactly equivalent directive so
    # checkpoints created before canonical reference ordering remain resumable.
    previous_ids: dict[str, list[str]] = {}
    for identity, item in previous.items():
        previous_ids.setdefault(directive_id(item), []).append(identity)
    directives = []
    seen_directives: set[str] = set()
    for fact in summary["task_directives"]:
        canonical_id = directive_id(fact)
        original_ids = previous_ids.get(canonical_id, [])
        if original_ids:
            identity = original_ids.pop(0)
        elif canonical_id in seen_directives:
            raise WorkCapacityError("work_compaction_invalid_structure")
        else:
            identity = canonical_id
        seen_directives.add(canonical_id)
        directives.append({**fact, "id": identity})
    retained = {item["id"] for item in directives}
    if len(retained) != len(directives):
        raise WorkCapacityError("work_compaction_invalid_structure")
    directive_refs = {ref for fact in directives for ref in fact["refs"]}
    if any(
        ref != "goal" and ref != source.get("original_request_ref") and not ref.startswith("input:")
        for ref in directive_refs
    ):
        raise WorkCapacityError("work_compaction_invalid_directive_source")
    superseded = {}
    for fact in summary["superseded_directives"]:
        identity = fact["directive_id"]
        if (
            identity not in previous
            or identity in superseded
            or identity in retained
            or not set(fact["refs"]) <= new_refs
            or not set(fact["refs"]) <= directive_refs
        ):
            raise WorkCapacityError("work_compaction_invalid_correction")
        superseded[identity] = fact
    if set(previous) - retained - set(superseded):
        raise WorkCapacityError("work_compaction_missing_directive")
    corrections = [
        *source["task_material"].get("corrections", []),
        *[
            {"previous": previous[identity], "refs": item["refs"]}
            for identity, item in superseded.items()
        ],
    ]
    material = {
        "version": 1,
        "covered_input_id": max(
            [
                source["task_material"].get("covered_input_id", 0),
                *(item["input_id"] for item in source["task_inputs"]),
            ]
        ),
        "directives": directives,
        "corrections": corrections,
        "recent_inputs": source["recent_task_inputs"],
        "raw_inputs_retained": "runtime_work_inputs; original input/event IDs",
        "original_request_ref": source.get("original_request_ref")
        or source["task_material"].get("original_request_ref"),
    }
    # Task requirements appear once, in the locally built material, not again
    # in the derived execution observations.
    summary.pop("task_directives")
    summary.pop("superseded_directives")
    return summary, material
