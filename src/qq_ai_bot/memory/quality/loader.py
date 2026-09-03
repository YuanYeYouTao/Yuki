"""Versioned synthetic dataset loading and hash validation."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qq_ai_bot.memory.quality.models import MemoryQualityCase, QualityManifest


@dataclass(frozen=True, slots=True)
class LoadedQualitySuite:
    root: Path
    manifest: QualityManifest
    cases: tuple[MemoryQualityCase, ...]
    computed_hash: str


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_dataset_hash(cases: tuple[MemoryQualityCase, ...]) -> str:
    payload = [case.model_dump(mode="json", exclude_none=True) for case in cases]
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def load_quality_suite(root: Path) -> LoadedQualitySuite:
    manifest_path = root / "manifest.toml"
    with manifest_path.open("rb") as stream:
        raw = tomllib.load(stream)
    manifest = QualityManifest.model_validate(raw)
    if len(manifest.case_files) != len(set(manifest.case_files)):
        raise ValueError("quality manifest contains duplicate case files")
    cases: list[MemoryQualityCase] = []
    for relative in manifest.case_files:
        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            raise ValueError("quality case path escapes dataset root")
        cases.append(MemoryQualityCase.model_validate_json(path.read_text(encoding="utf-8")))
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("quality case_id values must be globally unique")
    _validate_references(tuple(cases), manifest)
    computed = compute_dataset_hash(tuple(cases))
    if manifest.dataset_hash != computed:
        raise ValueError(
            f"quality dataset hash mismatch: manifest={manifest.dataset_hash} computed={computed}"
        )
    return LoadedQualitySuite(root, manifest, tuple(cases), computed)


def _validate_references(
    cases: tuple[MemoryQualityCase, ...],
    manifest: QualityManifest,
) -> None:
    symbols = set(manifest.symbolic_identities)
    required = {"person_a", "person_b", "person_c", "group_a", "group_b", "bot"}
    if not required <= symbols:
        raise ValueError("quality manifest is missing required symbolic identities")
    categories = {item.category for item in cases}
    required_categories = {
        "identity",
        "extraction",
        "third_party",
        "correction",
        "conflict",
        "temporal",
        "retrieval",
        "context",
        "rebuild",
        "privacy",
        "idempotency",
    }
    if not required_categories <= categories:
        missing = ",".join(sorted(required_categories - categories))
        raise ValueError(f"quality suite categories are incomplete: {missing}")
    secret_pattern = re.compile(r"(?:sk-[A-Za-z0-9_.-]{12,}|api[_-]?key|bearer\s+\S+)", re.I)
    for case in cases:
        for person, group in case.historical_memberships:
            if person not in symbols or group not in symbols:
                raise ValueError(f"{case.case_id}: membership reference is unresolved")
        event_refs = {item.event_ref for item in case.events}
        fact_refs = {item.fact_ref for item in (*case.initial_facts, *case.expected_facts)}
        if len(fact_refs) != len((*case.initial_facts, *case.expected_facts)):
            # The same specification may intentionally appear in initial and expected state.
            initial = {item.fact_ref for item in case.initial_facts}
            expected = {item.fact_ref for item in case.expected_facts}
            if initial != initial & expected:
                raise ValueError(f"{case.case_id}: duplicate fact_ref")
        for event in case.events:
            references = (event.speaker, *(event.mentioned), event.reply_speaker)
            if any(item is not None and item not in symbols for item in references):
                raise ValueError(f"{case.case_id}: event references an unknown identity")
            if event.group is not None and event.group not in symbols:
                raise ValueError(f"{case.case_id}: event references an unknown group")
        for claim in case.fake_model_outputs:
            if claim.event_ref not in event_refs:
                raise ValueError(f"{case.case_id}: fake claim references an unknown event")
        for evidence in case.expected_evidence:
            if evidence.fact_ref not in fact_refs or evidence.event_ref not in event_refs:
                raise ValueError(f"{case.case_id}: evidence reference is unresolved")
            if evidence.speaker not in symbols:
                raise ValueError(f"{case.case_id}: evidence speaker is unresolved")
        for relation in (*case.initial_relations, *case.expected_relations):
            if (
                relation.source_fact_ref not in fact_refs
                or relation.target_fact_ref not in fact_refs
            ):
                raise ValueError(f"{case.case_id}: relation reference is unresolved")
        for query in case.queries:
            if query.requester is not None and query.requester not in symbols:
                raise ValueError(f"{case.case_id}: requester is unresolved")
            if query.subject is not None and query.subject not in symbols:
                raise ValueError(f"{case.case_id}: query subject is unresolved")
            if query.group is not None and query.group not in symbols:
                raise ValueError(f"{case.case_id}: query group is unresolved")
            referenced = set(query.expected_fact_refs) | set(query.forbidden_fact_refs)
            if not referenced <= fact_refs:
                raise ValueError(f"{case.case_id}: query fact reference is unresolved")
            if set(query.expected_fact_refs) & set(query.forbidden_fact_refs):
                raise ValueError(f"{case.case_id}: expected and forbidden retrieval overlap")
        serialized = json.dumps(case.model_dump(mode="json"), ensure_ascii=False)
        if secret_pattern.search(serialized):
            raise ValueError(f"{case.case_id}: fixture resembles a secret")


def write_manifest_hash(root: Path) -> str:
    """Explicit developer action used only by the update-baseline workflow."""

    path = root / "manifest.toml"
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    manifest = QualityManifest.model_validate(raw)
    cases = tuple(
        MemoryQualityCase.model_validate_json((root / item).read_text(encoding="utf-8"))
        for item in manifest.case_files
    )
    digest = compute_dataset_hash(cases)
    lines = path.read_text(encoding="utf-8").splitlines()
    rendered = [
        f'dataset_hash = "{digest}"' if line.startswith("dataset_hash = ") else line
        for line in lines
    ]
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    return digest
