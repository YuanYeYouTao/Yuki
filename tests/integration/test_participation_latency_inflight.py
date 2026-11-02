"""Real Host/SQLite continuation withdrawal while simulated Main is in flight."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _event_and_route, _message
from tests.unit.test_participation_ordinary_feedback import ordinary_backend
from tests.unit.test_participation_pipeline_retirement import _relationship
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.self_report import SelfDelta

from yuki.conversation.hydrate import bump_canonical_generation
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ChatMessage, ChatResponse
from yuki.persistence.models import ChatEventModel
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.participation_feedback import sync_scope_effects
from yuki.social.db_models import SocialOperationModel


async def test_inflight_quiet_cannot_erase_withdrawal_after_digest_returns(
    database, tmp_path, monkeypatch, latency_evidence
):
    host, item, prior, _original, _unit = await _relationship(database, tmp_path)
    tasks = []
    release, started = asyncio.Event(), asyncio.Event()
    try:
        active = await _event_and_route(database, host.ledger, content="synthetic active input")
        binding, reason = await host.continuation_for_event(active.id)
        accepted = host.ordinary.prepare(active, item.scene.generation, 2, binding, reason=reason)
        assert await host.ordinary.commit(accepted)
        await host.on_ordinary_admitted(binding, accepted.admission)
        visible = await _event_and_route(
            database, host.ledger, content="synthetic visible continuation"
        )
        visible_binding, reason = await host.continuation_for_event(visible.id)
        linked = host.ordinary.prepare(
            visible, item.scene.generation, 3, visible_binding, reason=reason
        )
        async with database.immediate_session() as writer:
            assert await host.ordinary.commit_to_activation(
                linked, accepted.admission, session=writer
            )
        await host.on_ordinary_admitted(visible_binding, await host.ordinary.get(visible.id))
        latest = await _event_and_route(
            database, host.ledger, content="synthetic latest continuation"
        )
        latest_binding, reason = await host.continuation_for_event(latest.id)
        newest = host.ordinary.prepare(
            latest, item.scene.generation, 4, latest_binding, reason=reason
        )
        async with database.immediate_session() as writer:
            assert await host.ordinary.commit_to_activation(
                newest, accepted.admission, session=writer
            )
        await host.on_ordinary_admitted(latest_binding, await host.ordinary.get(latest.id))

        own, _ = await seed_event(
            host.ledger,
            bot_user_id="8000",
            origin_key=str(uuid4()),
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="synthetic confirmed send",
            group_id=active.group_id,
            occurred_at=datetime.now(UTC),
            origin="social_tool",
        )
        receipt = SocialOperationModel(
            id=str(uuid4()),
            source_turn_id=f"{item.scene.conversation_id}:event:{active.id}",
            tool_call_id="synthetic-inflight-send",
            source_conversation_id=item.scene.conversation_id,
            action="send_message",
            payload_hash="0" * 64,
            target_kind="space",
            target_id=item.scene.space_id,
            presence_id=active.ingress_presence_id,
            status="succeeded",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            event_id=own.id,
        )
        async with database.immediate_session() as writer:
            writer.add(receipt)
        await sync_scope_effects(host, item)
        await host._save(item)

        chat, provider, tool_runtime, runtime, _ = await ordinary_backend(
            database, 'NO_REPLY\n<yuki-state>{"engage":"quiet"}</yuki-state>'
        )
        chat.observe_main_response = host.observe_main_response
        tool_runtime = replace(
            tool_runtime,
            inbound=_message(active),
            actor_context=None,
            turn_snapshot=ConversationTurnSnapshot(
                item.scene.conversation_id, "synthetic", item.scene.generation, active.id, 1
            ),
        )
        backend = MainAgentBackend(chat, tool_runtime)
        complete = provider.complete

        async def delayed(request):
            started.set()
            await release.wait()
            return await complete(request)

        monkeypatch.setattr(provider, "complete", delayed)
        task = asyncio.create_task(
            chat.runtime.runner.run(
                (ChatMessage("system", "synthetic"), ChatMessage("user", active.content)),
                replace(runtime, visible_event_ids=frozenset({visible.id})),
                backend,
            )
        )
        tasks.append(task)
        await asyncio.wait_for(started.wait(), 15)

        ref = item.controller.state.events[f"event:{visible.id}"].ref
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == visible.id)
                .values(content="temporarily changed source")
            )
        assert not (await host._sources_current(item, (ref,)))[ref]
        await host._save(item)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == visible.id)
                .values(content=visible.content)
            )
        host._sessions.clear()
        restored = await host._session(item.scene)
        await host._hydrate(restored)
        assert restored.controller.state.invalidated[ref.event_id] >= ref.revision
        original_checkpoint = ParticipationCheckpoint.model_validate(
            restored.controller.state.host_checkpoint["participation_v1"]
        )
        assert ref in next(iter(original_checkpoint.units.values())).continuation_refs
        release.set()
        assert (await asyncio.wait_for(task, 15)).text == ""
        final = ParticipationCheckpoint.model_validate(
            restored.controller.state.host_checkpoint["participation_v1"]
        )
        assert next(iter(final.units.values())).hint.delta.engage == "stay"
        assert (
            await host.ordinary.get(active.id)
        ).activation_id == accepted.admission.activation_id
        assert (
            await host.ordinary.get(visible.id)
        ).activation_id == accepted.admission.activation_id
        async with database.read_session() as reader:
            retained_receipt = await reader.get(SocialOperationModel, receipt.id)
        assert retained_receipt.status == "succeeded" and retained_receipt.event_id == own.id

        # A genuinely new generation and new direct admission have their own
        # source facts; the old invalid ref must not poison this independent unit.
        async with database.immediate_session() as writer:
            generation = await bump_canonical_generation(
                writer, item.scene.conversation_id, event_id=own.id
            )
        fresh = await _event_and_route(
            database, host.ledger, content="synthetic independent new input"
        )
        fresh_binding = await host.binding_for_event(fresh.id)
        fresh_admission = host.ordinary.prepare(
            fresh, generation, 5, fresh_binding, reason="mention"
        )
        assert await host.ordinary.commit(fresh_admission)
        await host.on_ordinary_admitted(fresh_binding, fresh_admission.admission)
        fresh_runtime = replace(
            tool_runtime,
            inbound=_message(fresh),
            actor_context=None,
            turn_snapshot=ConversationTurnSnapshot(
                item.scene.conversation_id, "synthetic", generation, fresh.id, 2
            ),
            visible_event_ids=frozenset(),
        )
        await host.observe_main_response(
            fresh_runtime,
            1,
            ChatResponse("", 0, provider_request_id="fresh"),
            SelfDelta(engage="quiet"),
        )
        fresh_item = host._sessions[(item.scene.conversation_id, generation)]
        fresh_cp = ParticipationCheckpoint.model_validate(
            fresh_item.controller.state.host_checkpoint["participation_v1"]
        )
        assert next(iter(fresh_cp.units.values())).hint.delta.engage == "quiet"
        assert ref.event_id not in fresh_item.controller.state.invalidated
        latency_evidence.update(
            boundary="real Host/SQLite/admissions, simulated in-flight Main provider and receipt",
            prior_event_id=prior.id,
            active_event_id=active.id,
            visible_event_id=visible.id,
            latest_event_id=latest.id,
            original_activation_id=accepted.admission.activation_id,
            receipt_id=receipt.id,
            original_digest_restored=True,
            invalidated_ref_retained=True,
            late_quiet_rejected=True,
            original_receipt_retained=True,
            independent_new_generation_quiet=True,
            provider_requests=len(provider.requests),
        )
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await host.close()
