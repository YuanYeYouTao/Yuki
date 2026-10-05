"""T1 code boundary: private snapshot, parent checkpoint and one child intent."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.runtime.protocol_schema import refs
from qq_ai_bot.runtime.protocol_store import CodeSnapshotBinding
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects, work

PARENT = "chain:1:code-call"
ENGINE = {"api_revision": "api-1", "engine_digest": "engine-1", "dump_format": "monty-1"}


async def composed(database, tmp_path):
    _env, owner, _runtime = await active_work(database, tmp_path)
    control = owner.control
    identity = control.current["id"]
    assert await control.repository.prepare_effect(
        control.lease,
        identity,
        PARENT,
        "code_composition",
        composition={"version": 1, "snapshot_revision": 0, "script_id": "s1", **ENGINE},
    )
    async with database.sessions() as reader:
        source = await reader.get(CanonicalConversationModel, control.lease.conversation_id)
    binding = CodeSnapshotBinding(
        work_id=identity,
        operation_id=PARENT,
        source_execution_id="tool-audit",
        conversation_id=control.lease.conversation_id,
        generation=control.lease.generation,
        source_revision=source.prompt_source_revision,
        privacy_generation=0,
        **ENGINE,
    )
    return owner, binding


def child(identity, ordinal=0, engine_call_id="ext-0"):
    return {
        "version": 1,
        "operation_id": f"{PARENT}/child/{ordinal}",
        "owner_execution_id": identity,
        "parent_effect_key": PARENT,
        "child_ordinal": ordinal,
        "feed_index": 0,
        "engine_call_id": engine_call_id,
        "tool_id": "send_message",
        "arguments_digest": "digest",
        "dispatch_started": False,
        "budget_admitted": False,
        "revision": 0,
    }


async def publish(owner, binding, item, *, revision=0, dump=b"vm-state"):
    store = owner.journal.objects
    snapshot_ref = await store.put_code_snapshot(binding, dump)
    await owner.control.repository.publish_code_boundary(
        owner.control.lease,
        binding.work_id,
        PARENT,
        expected_revision=revision,
        composition={"snapshot_ref": snapshot_ref, "feed_index": 0},
        child=item,
        store=store,
        binding=binding,
        side_effecting=True,
    )
    return snapshot_ref


async def rows(database, identity):
    async with database.sessions() as reader:
        found = (
            (await reader.execute(select(effects).where(effects.c.work_id == identity)))
            .mappings()
            .all()
        )
        owned = set(await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == identity)))
        tools = await reader.scalar(select(work.c.tool_calls).where(work.c.id == identity))
    return {row["effect_key"]: row for row in found}, owned, tools


async def test_t1_publishes_snapshot_checkpoint_and_child_intent_without_dispatch(
    database, tmp_path
):
    owner, binding = await composed(database, tmp_path)
    item = child(binding.work_id)
    snapshot_ref = await publish(owner, binding, item)
    found, owned, tools = await rows(database, binding.work_id)
    parent = json.loads(found[PARENT]["receipt_json"])["composition"]
    assert parent["snapshot_revision"] == 1 and parent["snapshot_ref"] == snapshot_ref
    assert snapshot_ref in owned
    leaf = found[item["operation_id"]]
    assert leaf["state"] == "prepared"
    leaf_receipt = json.loads(leaf["receipt_json"])
    assert leaf_receipt["invocation"]["dispatch_started"] is False
    assert leaf_receipt["outcome"]["executed"] is False
    # T1 is registration only: no root budget, no dispatch.
    assert tools == 0
    store = owner.journal.objects
    assert await store.get_code_snapshot(snapshot_ref, binding) == b"vm-state"


async def test_stale_checkpoint_revision_publishes_nothing(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    await publish(owner, binding, child(binding.work_id))
    with pytest.raises(WorkConflict, match="code_checkpoint_conflict"):
        await publish(owner, binding, child(binding.work_id, 1, "ext-1"), revision=0)
    found, _, _ = await rows(database, binding.work_id)
    assert f"{PARENT}/child/1" not in found
    assert json.loads(found[PARENT]["receipt_json"])["composition"]["snapshot_revision"] == 1


async def test_same_engine_call_cannot_become_a_second_child(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    await publish(owner, binding, child(binding.work_id))
    duplicate = {**child(binding.work_id, 1), "operation_id": f"{PARENT}/child/dup"}
    with pytest.raises(WorkConflict, match="code_child_identity_conflict"):
        await publish(owner, binding, duplicate, revision=1)
    found, _, _ = await rows(database, binding.work_id)
    assert f"{PARENT}/child/dup" not in found
    # The failed transaction cannot have advanced the parent checkpoint.
    assert json.loads(found[PARENT]["receipt_json"])["composition"]["snapshot_revision"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"engine_digest": "other-engine"},
        {"dump_format": "monty-2"},
        {"api_revision": "api-2"},
        {"operation_id": "chain:1:another"},
    ],
)
async def test_mismatched_binding_is_rejected_before_publication(database, tmp_path, change):
    owner, binding = await composed(database, tmp_path)
    with pytest.raises(WorkConflict):
        await publish(owner, replace(binding, **change), child(binding.work_id))
    found, owned, _ = await rows(database, binding.work_id)
    assert set(found) == {PARENT} and not owned


async def test_child_metadata_cannot_claim_dispatch_or_foreign_parent(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    for forged in (
        {"dispatch_started": True},
        {"budget_admitted": True},
        {"parent_effect_key": "chain:1:other"},
        {"owner_execution_id": "other-work"},
    ):
        with pytest.raises(WorkConflict, match="code_boundary_identity_conflict"):
            await publish(owner, binding, {**child(binding.work_id), **forged})
    found, _, _ = await rows(database, binding.work_id)
    assert set(found) == {PARENT}


async def test_snapshot_load_rechecks_source_and_privacy(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    snapshot_ref = await publish(owner, binding, child(binding.work_id))
    store = owner.journal.objects
    with pytest.raises(ValueError, match="code_snapshot_binding_mismatch"):
        await store.get_code_snapshot(snapshot_ref, replace(binding, engine_digest="x"))
    with pytest.raises(ValueError, match="code_snapshot_authority_changed"):
        await store.get_code_snapshot(snapshot_ref, replace(binding, privacy_generation=1))
    async with database.sessions() as writer, writer.begin():
        source = await writer.get(CanonicalConversationModel, binding.conversation_id)
        source.prompt_source_revision += 1
    with pytest.raises(ValueError, match="code_snapshot_authority_changed"):
        await store.get_code_snapshot(snapshot_ref, binding)


async def test_unowned_snapshot_bytes_cannot_be_loaded(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    store = owner.journal.objects
    # Prepared but never published under this Work: no owner ref exists.
    digest = await store.put_code_snapshot(binding, b"forged")
    with pytest.raises(ValueError, match="code_snapshot_not_owned"):
        await store.get_code_snapshot(digest, binding)


async def test_composition_parent_is_not_an_unresolved_business_leaf(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    repository = owner.control.repository
    assert not await repository.has_unresolved_effects(owner.control.lease, binding.work_id)
    await publish(owner, binding, child(binding.work_id))
    item = child(binding.work_id)
    assert await repository.admit_dispatch(
        owner.control.lease, binding.work_id, item["operation_id"]
    )
    # A started leaf with no outcome is uncertain; the parent alone never is.
    assert await repository.has_unresolved_effects(owner.control.lease, binding.work_id)


async def test_boundary_cannot_replace_original_media_privacy_version(database, tmp_path):
    owner, binding = await composed(database, tmp_path)
    store = owner.journal.objects
    snapshot_ref = await store.put_code_snapshot(binding, b"vm-state")
    # Legacy parents have no version: a later checkpoint cannot invent one.
    with pytest.raises(WorkConflict, match="code_composition_binding_conflict"):
        await owner.control.repository.publish_code_boundary(
            owner.control.lease,
            binding.work_id,
            PARENT,
            expected_revision=0,
            composition={"snapshot_ref": snapshot_ref, "media_privacy_generation": 1},
            child=child(binding.work_id),
            store=store,
            binding=binding,
            side_effecting=False,
        )
    found, owned, tools = await rows(database, binding.work_id)
    assert set(found) == {PARENT} and not owned and tools == 0
    assert json.loads(found[PARENT]["receipt_json"])["composition"]["snapshot_revision"] == 0
