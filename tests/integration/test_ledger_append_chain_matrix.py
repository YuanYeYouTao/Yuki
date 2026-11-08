"""Scoped ledger append chain: one input sequence, every outcome and stored fact.

The failure matrix is written before the B3 refactor and replayed unchanged
afterwards.  Each step records ``created``/``reused``/error category; the end
state dumps chat_events, receipts and conversation watermarks with random IDs
replaced by first-seen labels.  The JSON lands in
``test-results/ledger-append-chain-b3/matrix.json`` so the run before and after
a change can be compared byte for byte.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from tests.support.event_seed import seed_scoped_event

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import (
    create_person_binding,
    find_identity_binding,
    find_presence,
    find_space_binding,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import (
    ScopedAppendResult,
    ScopedEventLedgerUnitOfWork,
    prepare_append,
)

AT = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


class _Labels:
    def __init__(self) -> None:
        self._seen: dict[str, dict[Any, str]] = {}

    def __call__(self, kind: str, value: Any) -> str | None:
        if value is None:
            return None
        bucket = self._seen.setdefault(kind, {})
        return bucket.setdefault(value, f"{kind}{len(bucket) + 1}")


async def _private_scope(database: Database, peer: str) -> ConversationScope:
    async with database.immediate_session() as writer:
        binding = await find_identity_binding(writer, peer)
        presence = await find_presence(writer, "8000")
        assert binding is not None and presence is not None
        conversation = await ensure_canonical_conversation(
            writer,
            kind="private",
            primary_scope_key=ConversationScope.private("8000", peer).key,
            person_id=binding.person_id,
            identity_binding_id=binding.id,
            presence_id=presence.id,
        )
    return ConversationScope.private(
        "8000", peer, presence_id=presence.id, conversation_id=conversation.conversation_id
    )


def _write_artifact(artifact: dict[str, Any]) -> None:
    directory = Path(__file__).resolve().parents[2] / "test-results" / "ledger-append-chain-b3"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "matrix.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


async def _dump(database: Database, steps: list[dict[str, Any]]) -> dict[str, Any]:
    labels = _Labels()
    async with database.read_session() as reader:
        rows = list(await reader.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
        receipts = list(
            await reader.scalars(
                select(CanonicalEventReceiptModel).order_by(
                    CanonicalEventReceiptModel.transport_namespace,
                    CanonicalEventReceiptModel.event_type,
                    CanonicalEventReceiptModel.ingress_key,
                )
            )
        )
        conversations = list(
            await reader.scalars(
                select(CanonicalConversationModel).order_by(CanonicalConversationModel.created_at)
            )
        )
        events = [
            {
                "id": row.id,
                "bot_user_id": row.bot_user_id,
                "origin_key": row.origin_key,
                "native_message_id": row.native_message_id,
                "scope_type": row.scope_type,
                "group_id": row.group_id,
                "private_peer_user_id": row.private_peer_user_id,
                "sender_user_id": row.sender_user_id,
                "sender_nickname": row.sender_nickname,
                "sender_group_card": row.sender_group_card,
                "direction": row.direction,
                "event_kind": row.event_kind,
                "source_plugin_id": row.source_plugin_id,
                "external_source": row.external_source,
                "external_event_key": row.external_event_key,
                "external_event_type": row.external_event_type,
                "external_payload_json": row.external_payload_json,
                "external_target_id": row.external_target_id,
                "content": row.content,
                "segments_json": row.segments_json,
                "reply_to_message_id": row.reply_to_message_id,
                "reply_to_event_id": row.reply_to_event_id,
                "origin": row.origin,
                "automation_id": row.automation_id,
                "automation_run_id": row.automation_run_id,
                "caused_by_event_id": row.caused_by_event_id,
                "occurred_at": "observed_at"
                if row.occurred_at == row.observed_at
                else row.occurred_at.replace(tzinfo=UTC).isoformat(),
                "canonical_conversation_id": labels("C", row.canonical_conversation_id),
                "canonical_event_id": labels("E", row.canonical_event_id),
                "author_kind": row.author_kind,
                "author_person_id": labels("P", row.author_person_id),
                "author_presence_id": labels("Y", row.author_presence_id),
                "author_account_id": labels("A", row.author_account_id),
                "ingress_presence_id": labels("Y", row.ingress_presence_id),
                "suppression_status": row.suppression_status,
            }
            for row in rows
        ]
        stored_receipts = [
            {
                "transport_namespace": receipt.transport_namespace,
                "event_type": receipt.event_type,
                "ingress_key": receipt.ingress_key,
                "canonical_event_id": labels("E", receipt.canonical_event_id),
                "ingress_presence_id": labels("Y", receipt.ingress_presence_id),
                "ingress_account_id": labels("A", receipt.ingress_account_id),
            }
            for receipt in receipts
        ]
        stored_conversations = sorted(
            (
                {
                    "id": labels("C", conversation.id),
                    "kind": conversation.kind,
                    "last_event_id": conversation.last_event_id,
                    "revision": conversation.revision,
                    "prompt_source_revision": conversation.prompt_source_revision,
                }
                for conversation in conversations
            ),
            key=lambda item: int(str(item["id"])[1:]),
        )
    return {
        "steps": [
            {
                **step,
                "conversation_id": labels("C", step.get("conversation_id")),
            }
            for step in steps
        ],
        "chat_events": events,
        "receipts": stored_receipts,
        "conversations": stored_conversations,
    }


async def test_ledger_append_chain_failure_matrix(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ScopedEventLedgerUnitOfWork(database)
    steps: list[dict[str, Any]] = []

    async def step(name: str, call: Callable[[], Awaitable[ScopedAppendResult]]) -> int | None:
        try:
            result = await call()
        except CanonicalIdentityError as exc:
            steps.append({"step": name, "error": exc.category})
            return None
        steps.append(
            {
                "step": name,
                "event_id": result.event.id,
                "created": result.created,
                "conversation_id": result.scope.id,
                "scope_key": result.scope.scope.key.split(":", 1)[0],
                "generation": result.scope.generation,
                "last_event_id": result.scope.last_event_id,
            }
        )
        return result.event.id

    private = await _private_scope(database, "1001")
    other_private = await _private_scope(database, "1002")

    def outbound(**fields: Any) -> Callable[[], Awaitable[ScopedAppendResult]]:
        arguments: dict[str, Any] = {
            "scope": private,
            "origin_key": "p-1",
            "sender_user_id": "8000",
            "direction": "outbound",
            "content": "hello world",
            "segments": ({"type": "text", "data": {"text": "hello world"}},),
            "occurred_at": AT,
            **fields,
        }
        return lambda: writer.append(**arguments)

    # 1. Private entry with an existing conversation: create, then idempotent reuse.
    first = await step("private_create", outbound())
    assert first is not None
    await step("private_same_key_same_content_reuse", outbound())
    await step(
        "private_same_key_whitespace_only_reuse",
        outbound(
            content="hello   world",
            segments=({"data": {"text": "hello world"}, "type": "text"},),
        ),
    )
    await step("private_same_key_unspecified_time_reuse", outbound(occurred_at=None))
    await step("private_same_key_other_content_conflict", outbound(content="other"))
    await step(
        "private_same_key_other_segments_conflict",
        outbound(segments=({"type": "text", "data": {"text": "other"}},)),
    )
    await step("private_same_key_other_time_conflict", outbound(occurred_at=LATER))
    await step("private_same_key_other_sender_conflict", outbound(sender_user_id="8001"))
    await step("private_same_key_other_direction_conflict", outbound(direction="external"))

    # 2. Duplicate suppression: a receipt whose keeper was suppressed fails closed.
    await step("private_duplicate_seed", outbound(origin_key="dup-1"))
    async with database.immediate_session() as session:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.origin_key == "dup-1")
            .values(suppression_status="duplicate", utterance_fingerprint="dup")
        )
    await step("private_duplicate_receipt_without_keeper_conflict", outbound(origin_key="dup-1"))

    # 3. Reply references must stay inside the conversation.
    await step(
        "reply_same_conversation_create", outbound(origin_key="r-1", reply_to_event_id=first)
    )
    await step("reply_same_conversation_reuse", outbound(origin_key="r-1", reply_to_event_id=first))
    foreign = await step(
        "reply_foreign_seed", outbound(scope=other_private, origin_key="foreign-1")
    )
    await step(
        "reply_foreign_conversation_conflict",
        outbound(origin_key="r-2", reply_to_event_id=foreign),
    )

    # 4. Causal sources: plugin replies need a same-conversation plugin event,
    # system tasks a same-conversation inbound keeper; others reject a cause.
    def external(**fields: Any) -> Callable[[], Awaitable[ScopedAppendResult]]:
        arguments: dict[str, Any] = {
            "scope": private,
            "origin_key": None,
            "source_plugin_id": "test.matrix",
            "external_source": "fixture",
            "external_event_key": "evt-1",
            "external_event_type": "fixture.created",
            "external_payload": {"id": "evt-1", "n": 1},
            "external_target_id": "1001",
            "content": "fixture event",
            "occurred_at": AT,
            **fields,
        }
        return lambda: writer.append_external(**arguments)

    plugin_event = await step("external_create", external())
    await step("external_same_key_same_content_reuse", external())
    await step(
        "external_same_key_reordered_payload_reuse",
        external(external_payload={"n": 1, "id": "evt-1"}),
    )
    await step(
        "external_same_key_other_payload_conflict",
        external(external_payload={"id": "evt-1", "n": 2}),
    )
    await step("external_same_key_other_content_conflict", external(content="changed"))
    await step("external_missing_target_conflict", external(external_target_id=None))
    foreign_plugin_event = await step(
        "external_other_conversation_create",
        external(scope=other_private, external_target_id="1002"),
    )

    await step(
        "causal_plugin_reply_without_cause_required",
        outbound(origin_key="c-1", origin="plugin_background"),
    )
    await step(
        "causal_plugin_reply_cross_conversation_invalid",
        outbound(
            origin_key="c-1", origin="plugin_background", caused_by_event_id=foreign_plugin_event
        ),
    )
    await step(
        "causal_plugin_reply_on_message_invalid",
        outbound(origin_key="c-1", origin="plugin_background", caused_by_event_id=first),
    )
    await step(
        "causal_user_message_with_cause_not_allowed",
        outbound(origin_key="c-1", caused_by_event_id=plugin_event),
    )
    await step(
        "causal_plugin_reply_create",
        outbound(origin_key="c-1", origin="plugin_background", caused_by_event_id=plugin_event),
    )
    await step(
        "causal_plugin_reply_reuse",
        outbound(origin_key="c-1", origin="plugin_background", caused_by_event_id=plugin_event),
    )
    inbound = await seed_scoped_event(
        writer,
        scope=private,
        origin_key="in-1",
        sender_user_id="1001",
        direction="inbound",
        content="user asks",
        occurred_at=AT,
    )
    inbound_two = await seed_scoped_event(
        writer,
        scope=private,
        origin_key="in-2",
        sender_user_id="1001",
        direction="inbound",
        content="user asks again",
        occurred_at=AT,
    )
    await step(
        "causal_system_task_on_plugin_event_invalid",
        outbound(origin_key="t-1", origin="system_task", caused_by_event_id=plugin_event),
    )
    await step(
        "causal_system_task_create",
        outbound(origin_key="t-1", origin="system_task", caused_by_event_id=inbound.event.id),
    )
    await step(
        "causal_system_task_same_key_other_cause_conflict",
        outbound(origin_key="t-1", origin="system_task", caused_by_event_id=inbound_two.event.id),
    )

    # 5. Group entry without a conversation: SpaceBinding proves the owner.
    async with database.read_session() as reader:
        presence = await find_presence(reader, "8000")
        space = await find_space_binding(reader, "2001")
        other_space = await find_space_binding(reader, "2002")
        peer_binding = await find_identity_binding(reader, "1003")
        assert presence and space and other_space and peer_binding
    group = ConversationScope.group("8000", "2001", presence_id=presence.id)
    await step(
        "group_create",
        outbound(scope=group, origin_key="g-1", space_binding_id=space.id),
    )
    await step(
        "group_reuse",
        outbound(scope=group, origin_key="g-1", space_binding_id=space.id),
    )
    await step(
        "group_wrong_space_binding_mismatch",
        outbound(scope=group, origin_key="g-2", space_binding_id=other_space.id),
    )
    group_conversation = next(
        item["conversation_id"] for item in steps if item["step"] == "group_create"
    )
    await step(
        "group_with_conversation_reuse",
        outbound(
            scope=ConversationScope.group(
                "8000", "2001", presence_id=presence.id, conversation_id=group_conversation
            ),
            origin_key="g-1",
        ),
    )
    await step(
        "group_private_scope_on_space_conversation_mismatch",
        outbound(
            scope=ConversationScope.private(
                "8000", "1001", presence_id=presence.id, conversation_id=group_conversation
            ),
            origin_key="g-3",
        ),
    )

    # 6. Private entry without a conversation: the IdentityBinding proves it.
    peer = ConversationScope.private("8000", "1003", presence_id=presence.id)
    await step(
        "private_binding_create",
        outbound(scope=peer, origin_key="b-1", identity_binding_id=peer_binding.id),
    )
    await step(
        "private_binding_wrong_binding_mismatch",
        outbound(scope=peer, origin_key="b-2", identity_binding_id=space.id),
    )
    await step(
        "private_unknown_presence",
        outbound(scope=replace(peer, presence_id="missing"), origin_key="b-3"),
    )

    # 7. App entry: its own Presence/Binding and transport namespace.
    async with database.immediate_session() as session:
        app_presence = await find_presence(session, "yuki-app", platform="app")
        assert app_presence is not None
        app_binding = await create_person_binding(
            session,
            external_account_id="app-user-1",
            display_name="app user",
            now=AT,
            platform="app",
        )
        app_binding_id = app_binding.id
    app = ConversationScope.private(
        "yuki-app", "app-user-1", platform="app", presence_id=app_presence.id
    )
    await step(
        "app_create",
        outbound(
            scope=app,
            origin_key="p-1",
            sender_user_id="yuki-app",
            identity_binding_id=app_binding_id,
        ),
    )
    await step(
        "app_reuse",
        outbound(
            scope=app,
            origin_key="p-1",
            sender_user_id="yuki-app",
            identity_binding_id=app_binding_id,
        ),
    )

    # 8. Caller-owned writer: the precomputed preparation is required and reused.
    async def in_session(**fields: Any) -> ScopedAppendResult:
        content = fields.pop("content", "hello world")
        segments = fields.pop("segments", ({"type": "text", "data": {"text": content}},))
        prepared = await prepare_append(content, segments=segments)
        async with database.immediate_session() as session:
            return await writer.append(
                scope=private,
                origin_key=fields.pop("origin_key"),
                sender_user_id="8000",
                direction="outbound",
                content=content,
                segments=segments,
                occurred_at=AT,
                session=session,
                prepared=prepared,
                **fields,
            )

    await step("session_create", lambda: in_session(origin_key="s-1"))
    await step("session_reuse", lambda: in_session(origin_key="s-1"))
    await step("session_conflict", lambda: in_session(origin_key="s-1", content="changed"))

    async def session_without_preparation() -> ScopedAppendResult:
        async with database.immediate_session() as session:
            return await writer.append(
                scope=private,
                origin_key="s-2",
                sender_user_id="8000",
                direction="outbound",
                content="x",
                session=session,
            )

    try:
        await session_without_preparation()
    except ValueError as exc:
        steps.append({"step": "session_without_preparation", "error": str(exc)})

    # 9. Races: a writer that missed the winner reuses it only when identical.
    original_find = ScopedEventLedgerUnitOfWork._find_existing_live
    misses = {"left": 0}

    async def miss_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        if misses["left"]:
            misses["left"] -= 1
            return None, None
        return await original_find(self, *args, **kwargs)

    monkeypatch.setattr(ScopedEventLedgerUnitOfWork, "_find_existing_live", miss_once)
    misses["left"] = 1
    await step("race_receipt_identical_reuse", lambda: in_session(origin_key="s-1"))
    misses["left"] = 1
    await step(
        "race_receipt_changed_conflict", lambda: in_session(origin_key="s-1", content="race")
    )

    async def external_in_session(**fields: Any) -> ScopedAppendResult:
        payload = fields.pop("external_payload", {"id": "evt-1", "n": 1})
        key = fields.pop("external_event_key", "evt-1")
        # Plugin notifications store their context segment through the
        # preparation only; append_external itself has no segments argument.
        prepared = await prepare_append(
            "fixture event", segments=fields.pop("segments", ()), external_payload=payload
        )
        async with database.immediate_session() as session:
            return await writer.append_external(
                scope=private,
                origin_key=None,
                source_plugin_id="test.matrix",
                external_source="fixture",
                external_event_key=key,
                external_event_type="fixture.created",
                external_payload=payload,
                external_target_id="1001",
                content="fixture event",
                occurred_at=AT,
                session=session,
                prepared=prepared,
                **fields,
            )

    misses["left"] = 1
    await step("race_plugin_external_identical_reuse", external_in_session)
    misses["left"] = 1
    await step(
        "race_plugin_external_changed_conflict",
        lambda: external_in_session(external_payload={"id": "evt-1", "n": 3}),
    )
    monkeypatch.setattr(ScopedEventLedgerUnitOfWork, "_find_existing_live", original_find)

    # 10. A stored plugin event is compared with the row that would be stored.
    context = ({"type": "yuki_context", "data": {"source": "notification"}},)
    await step(
        "external_prepared_context_create",
        lambda: external_in_session(external_event_key="evt-ctx", segments=context),
    )
    await step(
        "external_prepared_context_reuse",
        lambda: external_in_session(external_event_key="evt-ctx", segments=context),
    )

    artifact = await _dump(database, steps)
    _write_artifact(artifact)

    outcomes = {item["step"]: item for item in artifact["steps"]}
    created = [name for name, item in outcomes.items() if item.get("created") is True]
    reused = [name for name, item in outcomes.items() if item.get("created") is False]
    errors = {name: item["error"] for name, item in outcomes.items() if "error" in item}
    assert all(name.endswith(("_create", "_seed")) for name in created), created
    assert all(name.endswith("_reuse") for name in reused), reused
    assert outcomes["private_same_key_same_content_reuse"]["event_id"] == first
    assert outcomes["session_reuse"]["event_id"] == outcomes["session_create"]["event_id"]
    assert (
        outcomes["race_receipt_identical_reuse"]["event_id"]
        == outcomes["session_create"]["event_id"]
    )
    assert outcomes["race_plugin_external_identical_reuse"]["event_id"] == plugin_event
    assert errors == {
        "private_same_key_other_content_conflict": "receipt_conflict",
        "private_same_key_other_segments_conflict": "receipt_conflict",
        "private_same_key_other_time_conflict": "receipt_conflict",
        "private_same_key_other_sender_conflict": "receipt_conflict",
        "private_same_key_other_direction_conflict": "receipt_conflict",
        "private_duplicate_receipt_without_keeper_conflict": "receipt_conflict",
        "reply_foreign_conversation_conflict": "receipt_conflict",
        "external_same_key_other_payload_conflict": "receipt_conflict",
        "external_same_key_other_content_conflict": "receipt_conflict",
        "external_missing_target_conflict": "receipt_conflict",
        "causal_plugin_reply_without_cause_required": "causal_source_required",
        "causal_plugin_reply_cross_conversation_invalid": "causal_source_invalid",
        "causal_plugin_reply_on_message_invalid": "causal_source_invalid",
        "causal_user_message_with_cause_not_allowed": "causal_source_not_allowed",
        "causal_system_task_on_plugin_event_invalid": "causal_source_invalid",
        "causal_system_task_same_key_other_cause_conflict": "receipt_conflict",
        "group_wrong_space_binding_mismatch": "canonical_owner_mismatch",
        "group_private_scope_on_space_conversation_mismatch": "canonical_owner_mismatch",
        "private_binding_wrong_binding_mismatch": "canonical_owner_mismatch",
        "private_unknown_presence": "no_presence",
        "session_conflict": "receipt_conflict",
        "session_without_preparation": "scoped_event_preparation_required",
        "race_receipt_changed_conflict": "receipt_conflict",
        "race_plugin_external_changed_conflict": "receipt_conflict",
    }
