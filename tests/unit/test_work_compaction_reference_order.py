"""Reference presentation does not alter an unchanged task directive."""

import hashlib
import json

import pytest

from qq_ai_bot.runtime.work_compaction import directive_id, validate_summary
from qq_ai_bot.runtime.work_repository import WorkCapacityError


def _case():
    fact = {"text": "Do not deploy.", "refs": ["input:1", "goal"]}
    # This is the pre-canonical-order ID, as found in existing checkpoints.
    legacy_id = hashlib.sha256(json.dumps(fact, sort_keys=True).encode()).hexdigest()
    source = {
        "source_refs": ["goal", "input:1", "input:2"],
        "task_inputs": [],
        "task_material": {"directives": [{**fact, "id": legacy_id}]},
        "recent_task_inputs": [],
    }
    summary = {
        "version": 1,
        "task_directives": [{**fact, "refs": list(reversed(fact["refs"]))}],
        "superseded_directives": [],
        "input_dispositions": [],
        "completed": [],
        "pending": [],
        "failures": [],
        "artifacts": [],
        "next_steps": [],
    }
    return fact, legacy_id, source, summary


def test_reference_order_preserves_existing_directive_identity():
    fact, legacy_id, source, summary = _case()
    assert directive_id(fact) == directive_id(summary["task_directives"][0])
    assert directive_id(fact) != legacy_id
    _, material = validate_summary(json.dumps(summary), source)
    assert material["directives"] == [{**summary["task_directives"][0], "id": legacy_id}]
    assert material["corrections"] == []
    # A second compaction with the original display order still retains that ID.
    source["task_material"] = material
    summary["task_directives"] = [fact]
    _, again = validate_summary(json.dumps(summary), source)
    assert again["directives"][0]["id"] == legacy_id


@pytest.mark.parametrize("change", ["text", "source", "duplicate"])
def test_order_tolerance_does_not_relax_directive_provenance(change):
    _, _, source, summary = _case()
    directive = summary["task_directives"][0]
    if change == "text":
        directive["text"] = "Deploy now."
    elif change == "source":
        directive["refs"] = ["input:2", "goal"]
    else:
        directive["refs"].append("goal")
    with pytest.raises(WorkCapacityError):
        validate_summary(json.dumps(summary), source)


def test_distinct_legacy_ids_with_same_source_set_are_all_preserved():
    fact, legacy_id, source, summary = _case()
    reversed_fact = {**fact, "refs": list(reversed(fact["refs"]))}
    other_id = hashlib.sha256(json.dumps(reversed_fact, sort_keys=True).encode()).hexdigest()
    assert other_id != legacy_id
    source["task_material"]["directives"].append({**reversed_fact, "id": other_id})
    summary["task_directives"] = [reversed_fact, fact]
    _, material = validate_summary(json.dumps(summary), source)
    assert {item["id"] for item in material["directives"]} == {legacy_id, other_id}
    assert len(material["directives"]) == 2
    source["task_material"] = material
    _, again = validate_summary(json.dumps(summary), source)
    assert {item["id"] for item in again["directives"]} == {legacy_id, other_id}
    summary["task_directives"].pop()
    with pytest.raises(WorkCapacityError, match="work_compaction_missing_directive"):
        validate_summary(json.dumps(summary), source)


@pytest.mark.parametrize("existing", [False, True])
def test_new_duplicate_does_not_acquire_a_second_directive_identity(existing):
    _, _, source, summary = _case()
    if not existing:
        source["task_material"]["directives"] = []
    summary["task_directives"] *= 2
    with pytest.raises(WorkCapacityError, match="work_compaction_invalid_structure"):
        validate_summary(json.dumps(summary), source)
