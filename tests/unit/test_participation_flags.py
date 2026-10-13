"""Product flags constrain new admission in the actual SQLite writer."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from tests.conftest import MemorySender, make_settings
from tests.support.autonomy_repository_helpers import _accept, _enable, _scene
from tests.unit.test_auto_reply_merge import _admissions, _open_group_scene, _wait_for_worker

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.domain.memory_config import MemoryConfigScope
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel


async def override(database, space, key, value, version=1):
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            RuntimeConfigOverrideModel(
                config_key=key,
                scope_type="group" if space else "global",
                canonical_space_id=space,
                value_json=json.dumps(value),
                value_type="boolean",
                apply_mode="hot",
                version=version,
                created_at=now,
                updated_at=now,
                updated_by="synthetic-test",
            )
        )


@pytest.mark.parametrize(
    "auto,proactive", [(True, True), (True, False), (False, True), (False, False)]
)
async def test_four_flags_new_ordinary_admission_and_explicit_call(
    database, tmp_path, auto, proactive
):
    provider = FakeLLMProvider()
    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, provider, debounce=0
    )
    await override(database, env.space, "conversation.auto_reply_enabled", auto)
    await override(database, env.space, "conversation.proactive_wakeup_enabled", proactive)
    result = await harness.processor.handle(message(1, "implicit ordinary"), MemorySender())
    assert result.reason == "ordinary_deferred"
    observed = next(iter(harness.processor._observed_human_turns.values()))
    await _wait_for_worker(worker, observed.snapshot.scope_key)
    assert len(await _admissions(database)) == int(auto)
    direct = await harness.processor.handle(
        message(2, "explicit call", mention=True), MemorySender()
    )
    assert direct.reason == "chat"
    assert len(await _admissions(database)) == int(auto) + 1
    await worker.close()


async def test_canonical_config_queries_use_exact_keys_aliases_and_owner(database):
    runtime = RuntimeConfigService(settings=make_settings(database.url), database=database)
    _, space = await runtime._owner_match(user_id=None, group_id="2001")
    _, other = await runtime._owner_match(user_id=None, group_id="2002")
    await override(database, None, "conversation.auto_reply_enabled", False, version=5)
    await override(database, other, "conversation.auto_reply_enabled", True, version=9)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(CanonicalSpaceModel).where(CanonicalSpaceModel.id == space).values(enabled=False)
        )
        values = await runtime.inspect_configs(
            ("CONVERSATION.AUTO_REPLY_ENABLED", "conversation.auto_reply_enabled"),
            scope=MemoryConfigScope(space_id=space),
            session=writer,
        )
        assert len(values) == 2 and all(v.saved.value is False and v.version == 5 for v in values)
        rows = await runtime._repository.list_all(
            keys=("conversation.auto_reply_enabled",),
            scope=MemoryConfigScope(space_id=space),
            session=writer,
        )
        assert len(rows) == 1 and rows[0].version == 5
        assert (
            await runtime._repository.list_all(
                keys=(), scope=MemoryConfigScope(space_id=space), session=writer
            )
            == ()
        )
        assert (
            await runtime.inspect_configs(
                (), scope=MemoryConfigScope(space_id=space), session=writer
            )
            == ()
        )


async def test_self_duplicate_precedes_product_predicate_and_new_disabled_has_no_run(database):
    scene = await _scene(database)
    repository = AutonomyRepository(database)
    binding = await _enable(repository, scene)
    first = await _accept(repository, scene, binding)
    calls = []

    async def disabled(session):
        calls.append(session)
        return False

    common = dict(
        binding=binding,
        owner=binding.effective_owner,
        space_id=scene.space,
        presence_id=scene.presence,
        sources=first.run.sources,
        support_refs=("observation:1",),
        admission_allowed=disabled,
    )
    replay = await repository.accept_host_proposal(proposal_id="p1", **common)
    assert replay.run.run_id == first.run.run_id and not calls
    new = await repository.accept_host_proposal(proposal_id="p2", **common)
    assert new.outcome == "disabled" and new.run is None and len(calls) == 1


@pytest.mark.parametrize("close_first", [False, True])
async def test_ordinary_writer_serializes_flag_close_and_preserves_original_admission(
    database, tmp_path, close_first
):
    import asyncio

    from qq_ai_bot.conversation.ordinary_admission import OrdinaryAdmissionRepository
    from qq_ai_bot.persistence.event_repository import EventLedgerRepository

    env, harness, worker, message = await _open_group_scene(
        database, tmp_path, FakeLLMProvider(), debounce=60
    )
    await harness.processor.handle(message(1, "waiting ordinary"), MemorySender())
    observed = next(iter(harness.processor._observed_human_turns.values()))
    event = await EventLedgerRepository(database).get_event(observed.record.id)
    ordinary = OrdinaryAdmissionRepository(database)
    prepared = ordinary.prepare(event, 1, 0)
    runtime = harness.processor._runtime_config
    started, release = asyncio.Event(), asyncio.Event()

    async def allowed(writer):
        values = await runtime.inspect_configs(
            ("conversation.auto_reply_enabled",),
            scope=MemoryConfigScope(space_id=env.space),
            session=writer,
        )
        return values[0].effective.value

    async def admit():
        async with database.immediate_session() as writer:
            result = await ordinary.commit(prepared, session=writer, admission_allowed=allowed)
            if not close_first:
                started.set()
                await release.wait()
            return result

    async def close():
        now = datetime.now(UTC)
        async with database.immediate_session() as writer:
            writer.add(
                RuntimeConfigOverrideModel(
                    config_key="conversation.auto_reply_enabled",
                    scope_type="group",
                    canonical_space_id=env.space,
                    value_json="false",
                    value_type="boolean",
                    apply_mode="hot",
                    version=1,
                    created_at=now,
                    updated_at=now,
                    updated_by="synthetic-test",
                )
            )
            await writer.flush()
            if close_first:
                started.set()
                await release.wait()

    first = asyncio.create_task(close() if close_first else admit())
    await started.wait()
    second = asyncio.create_task(admit() if close_first else close())
    release.set()
    result_first, result_second = await asyncio.gather(first, second)
    admitted = result_second if close_first else result_first
    assert admitted is (not close_first)
    prior = await ordinary.get(event.id)
    assert (prior is not None) is (not close_first)
    if prior is not None:
        assert not await ordinary.commit(prepared, admission_allowed=allowed)
        assert await ordinary.get(event.id) == prior
    await worker.close()


@pytest.mark.parametrize(
    "auto,proactive", [(True, True), (True, False), (False, True), (False, False)]
)
async def test_actual_self_host_uses_proactive_flag_independently_of_auto_reply(
    database, tmp_path, auto, proactive
):
    import time

    from tests.support.semantic_participation_host_helpers import _event_and_route, _host
    from yuki_participation.models import CandidateKind, Proposal

    host, _ = await _host(database, tmp_path, observer=False)
    try:
        event = await _event_and_route(database, host.app.ledger)
        item = await host._session(await host._scene(event.canonical_conversation_id))
        await host._hydrate(item)
        binding = await host._binding(item)
        await override(database, item.scene.space_id, "conversation.auto_reply_enabled", auto)
        await override(
            database, item.scene.space_id, "conversation.proactive_wakeup_enabled", proactive
        )
        now = time.time()
        proposal = Proposal(
            proposal_id="synthetic-intrinsic-flag",
            scope=item.scene.scope,
            controller_epoch=binding.controller_epoch,
            kind=CandidateKind.INTRINSIC,
            thread="synthetic-intrinsic",
            target_hint="group",
            sources=(),
            support=None,
            created_at=now,
            expires_at=now + 60,
        )
        item.controller.state.proposals[proposal.proposal_id] = proposal
        item.controller._set(pending=proposal.proposal_id)
        result = await host._admit_proposal(item, binding, proposal)
        assert result["outcome"] == ("accepted" if proactive else "disabled")
        if proactive:
            assert (await host.repository.get_run(result["run_id"])).sources == ()
        else:
            assert result["run_id"] is None and item.controller.state.pending is None
        assert (await host.repository.get_binding(item.scene.conversation_id, 1)) == binding
    finally:
        await host.close()
