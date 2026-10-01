"""Bounded, source-checked task material and derived execution summaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qq_ai_bot.runtime.work_repository import WorkCapacityError


class SourcedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=1024)
    refs: list[str] = Field(min_length=1, max_length=8)


class SupersededDirective(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    directive_id: str
    refs: list[str] = Field(min_length=1, max_length=8)


class InputDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_ref: str
    kind: Literal["directive", "correction", "context"]
    reason: str = Field(min_length=1, max_length=512)


class CompactionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1, le=1)
    task_directives: list[SourcedFact] = Field(max_length=32)
    superseded_directives: list[SupersededDirective] = Field(max_length=32)
    input_dispositions: list[InputDisposition] = Field(max_length=16)
    completed: list[SourcedFact] = Field(max_length=32)
    pending: list[SourcedFact] = Field(max_length=32)
    failures: list[SourcedFact] = Field(max_length=32)
    artifacts: list[SourcedFact] = Field(max_length=32)
    next_steps: list[SourcedFact] = Field(max_length=16)


def directive_id(fact: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"text": fact["text"], "refs": fact["refs"]}, sort_keys=True).encode()
    ).hexdigest()


def validate_summary(raw: str, source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """References prove supplied provenance, never execution or semantic completeness."""
    try:
        if len(raw.encode()) > 65536:
            raise ValueError("oversized summary")
        summary = CompactionSummary.model_validate_json(raw).model_dump()
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
    directives = [{**fact, "id": directive_id(fact)} for fact in summary["task_directives"]]
    retained = {item["id"] for item in directives}
    if len(retained) != len(directives):
        raise WorkCapacityError("work_compaction_invalid_structure")
    directive_refs = {ref for fact in directives for ref in fact["refs"]}
    if any(ref != "goal" and not ref.startswith("input:") for ref in directive_refs):
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
    classified = set()
    for item in summary["input_dispositions"]:
        ref = item["input_ref"]
        if ref not in new_refs or ref in classified or not item["reason"].strip():
            raise WorkCapacityError("work_compaction_invalid_input_disposition")
        if item["kind"] != "context" and ref not in directive_refs:
            raise WorkCapacityError("work_compaction_missing_input")
        classified.add(ref)
    if classified != new_refs:
        raise WorkCapacityError("work_compaction_missing_input")
    corrections = [
        *source["task_material"].get("corrections", []),
        *[
            {"previous": previous[identity], "refs": item["refs"]}
            for identity, item in superseded.items()
        ],
    ][-16:]
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
    }
    if len(json.dumps(material, ensure_ascii=False).encode()) > 65536:
        raise WorkCapacityError("work_task_material_capacity")
    # Task requirements appear once, in the locally built material, not again
    # in the derived execution observations.
    summary.pop("task_directives")
    summary.pop("superseded_directives")
    return summary, material
