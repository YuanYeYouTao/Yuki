"""Bounded, source-checked task material and derived execution summaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from qq_ai_bot.runtime.work_repository import WorkCapacityError


class SourcedFact(BaseModel):
    text: str
    refs: list[str] = Field(min_length=1)


class SupersededDirective(BaseModel):
    directive_id: str
    refs: list[str] = Field(min_length=1)


class CompactionSummary(BaseModel):
    version: int = 1
    task_directives: list[SourcedFact] = Field(default_factory=list)
    superseded_directives: list[SupersededDirective] = Field(default_factory=list)
    completed: list[SourcedFact] = Field(default_factory=list)
    pending: list[SourcedFact] = Field(default_factory=list)
    failures: list[SourcedFact] = Field(default_factory=list)
    artifacts: list[SourcedFact] = Field(default_factory=list)
    next_steps: list[SourcedFact] = Field(default_factory=list)


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
        parsed = CompactionSummary.model_validate_json(summary_json_text(raw))
        summary = parsed.model_dump()
    except (ValueError, ValidationError) as exc:
        raise WorkCapacityError("work_compaction_invalid_structure") from exc
    allowed = set(source["source_refs"])
    observations = source.get(
        "derived_observations", source["task_material"].get("paid_observations", {})
    )
    for section in ("completed", "pending", "failures", "artifacts", "next_steps"):
        if section not in parsed.model_fields_set:
            summary[section] = observations.get(section, [])
    referenced: set[str] = set()
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
            if not set(fact["refs"]) <= allowed:
                raise WorkCapacityError("work_compaction_invalid_reference")
            referenced.update(fact["refs"])
    previous = {item["id"]: item for item in source["task_material"].get("directives", [])}
    # References prove a set of sources; their presentation order is not a new
    # instruction. Reuse the stored ID of an exactly equivalent directive so
    # checkpoints created before canonical reference ordering remain resumable.
    previous_ids: dict[str, list[str]] = {}
    for identity, item in previous.items():
        previous_ids.setdefault(directive_id(item), []).append(identity)
    directives = []
    for fact in summary["task_directives"]:
        canonical_id = directive_id(fact)
        original_ids = previous_ids.get(canonical_id, [])
        identity = original_ids.pop(0) if original_ids else canonical_id
        directives.append({**fact, "id": identity})
    superseded = {}
    for fact in summary["superseded_directives"]:
        identity = fact["directive_id"]
        if identity not in previous or not set(fact["refs"]) <= new_refs:
            raise WorkCapacityError("work_compaction_invalid_correction")
        superseded[identity] = fact
    retained = {item["id"] for item in directives}
    directives.extend(
        item for identity, item in previous.items() if identity not in retained | superseded.keys()
    )
    corrections = [
        *source["task_material"].get("corrections", []),
        *[
            {"previous": previous[identity], "refs": item["refs"]}
            for identity, item in superseded.items()
        ],
    ]
    covered = source["task_material"].get("covered_input_id", 0)
    inputs = {
        item["input_id"]: item
        for item in [
            *source["task_material"].get("recent_inputs", []),
            *source["task_inputs"],
        ]
        if item["input_id"] > covered
    }
    for identity in sorted(inputs):
        item = inputs[identity]
        if f"input:{item['input_id']}" not in referenced:
            break
        covered = max(covered, item["input_id"])
    original_ref = source["task_material"].get("original_request_ref")
    if source.get("original_request_ref") in referenced:
        original_ref = source["original_request_ref"]
    material = {
        "version": 1,
        "covered_input_id": covered,
        "directives": directives,
        "corrections": corrections,
        "recent_inputs": list(
            {
                **{identity: item for identity, item in inputs.items() if identity > covered},
                **{item["input_id"]: item for item in source["recent_task_inputs"]},
            }.values()
        ),
        "raw_inputs_retained": "runtime_work_inputs; original input/event IDs",
        "original_request_ref": original_ref,
    }
    if "paid_observations" in source["task_material"]:
        material["paid_observations"] = source["task_material"]["paid_observations"]
    # Task requirements appear once, in the locally built material, not again
    # in the derived execution observations.
    summary.pop("task_directives")
    summary.pop("superseded_directives")
    return summary, material
