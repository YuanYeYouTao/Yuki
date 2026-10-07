"""Concrete per-turn memory session (R2 C5)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage
from qq_ai_bot.memory.attribution import (
    MemoryAttributionJob,
    MemoryAttributionWorker,
    MemoryExposure,
)
from qq_ai_bot.memory.context import MemoryContextService
from qq_ai_bot.memory.enums import (
    MemoryRecallPurpose,
    MemoryRetrievalMode,
)
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryRetrievalResult
from qq_ai_bot.memory.runtime.capability_view import build_capability_view
from qq_ai_bot.memory.runtime.contract import (
    MemoryAvailability,
    MemoryTurnContract,
    active_read_contract,
    forbidden_contract,
)
from qq_ai_bot.memory.runtime.partition_lookup import MemoryPartitionLookup
from qq_ai_bot.memory.runtime.query_plane import (
    MemoryQueryPlane,
)
from qq_ai_bot.memory.runtime.resolver import (
    MemoryAccessDecision,
    MemoryAccessReason,
    MemoryStructuredCommand,
    resolve_memory_access,
    resolve_scope_from_scene,
)
from qq_ai_bot.memory.runtime.state import (
    MemorySessionState,
    RecallHandle,
)
from qq_ai_bot.memory.self_origin import SelfMemoryOrigin
from qq_ai_bot.runtime.authority import TurnAuthority, TurnSceneFacts
from qq_ai_bot.runtime.contracts import DeliverySummary, MemoryCapabilityView, MemoryReceiptHandle
from qq_ai_bot.runtime.delivery import DeliveryStatus
from qq_ai_bot.runtime.keys import ResolvedMemoryScope
from qq_ai_bot.runtime.origin import TurnOrigin

_MEMORY_READ_TOOLS = frozenset(
    {
        "search_memory",
        "get_memory_fact",
        "get_memory_evidence",
    }
)


def scene_from_inbound(
    inbound: InboundMessage, *, image_present: bool | None = None
) -> TurnSceneFacts:
    """Trusted scene facts for the resolver.  Never derived from model text."""

    attachments = (*inbound.attachments, *inbound.reply_attachments)
    images = image_present
    if images is None:
        images = any(item.kind is AttachmentKind.IMAGE for item in attachments)
    return TurnSceneFacts(
        scope_type=inbound.scope_type,
        group_id=inbound.group_id,
        image_present=images,
        mentions_bot=inbound.mentions_bot,
        reply_present=bool(inbound.reply_text or inbound.reply_sender_user_id),
    )


class TurnMemorySession:
    """I/O session that Chat may call.  It never holds ChatService."""

    def __init__(
        self,
        *,
        decision: MemoryAccessDecision,
        scope: ResolvedMemoryScope,
        inbound: InboundMessage | None,
        identity: ConversationScope,
        runtime: RuntimeConfigSnapshot,
        memory_context: MemoryContextService,
        partition_lookup: MemoryPartitionLookup,
        origin: TurnOrigin,
        user_question: str,
        runtime_turn_id: str,
        attribution: MemoryAttributionWorker | None = None,
        self_origin: SelfMemoryOrigin | None = None,
    ) -> None:
        self._decision = decision
        self._state = MemorySessionState(decision.contract, scope)
        self._inbound = inbound
        self._self_origin = self_origin
        self._source_key = (
            inbound.source_key
            if inbound
            else f"initiative:{self_origin.initiative_run_id}"
            if self_origin
            else ""
        )
        self._identity = identity
        self._runtime = runtime
        self._memory_context = memory_context
        self._partition_lookup = partition_lookup
        self._query = MemoryQueryPlane(memory_context)
        self._origin = origin
        self._user_question = user_question
        self._runtime_turn_id = runtime_turn_id
        self._attribution = attribution
        self._pending_tool_exposures: tuple[MemoryExposure, ...] = ()
        self._confirmed_exposures: list[MemoryExposure] = []
        self._read_receipt_lock = asyncio.Lock()
        self._delivery_reported = False

    @classmethod
    def open(
        cls,
        *,
        inbound: InboundMessage,
        identity: ConversationScope,
        runtime: RuntimeConfigSnapshot,
        memory_context: MemoryContextService,
        partition_lookup: MemoryPartitionLookup,
        origin: TurnOrigin,
        user_question: str,
        authority: TurnAuthority,
        structured_command: MemoryStructuredCommand = MemoryStructuredCommand.NONE,
        image_present: bool | None = None,
        runtime_turn_id: str | None = None,
        attribution: MemoryAttributionWorker | None = None,
        memory_available: bool = True,
    ) -> TurnMemorySession:
        scene = scene_from_inbound(inbound, image_present=image_present)
        decision = resolve_memory_access(
            authority=authority,
            scene=scene,
            structured_command=structured_command,
            memory_available=memory_available,
            retrieval_enabled=runtime.memory.retrieval_enabled,
        )
        return cls(
            decision=decision,
            scope=resolve_scope_from_scene(authority=authority, scene=scene),
            inbound=inbound,
            identity=identity,
            runtime=runtime,
            memory_context=memory_context,
            partition_lookup=partition_lookup,
            origin=origin,
            user_question=user_question,
            runtime_turn_id=runtime_turn_id or str(uuid.uuid4()),
            attribution=attribution,
        )

    @property
    def contract(self) -> MemoryTurnContract:
        return self._state.contract

    @classmethod
    async def open_self_origin(
        cls,
        *,
        initiative_run_id: str,
        canonical_conversation_id: str,
        identity: ConversationScope,
        runtime: RuntimeConfigSnapshot,
        memory_context: MemoryContextService,
        partition_lookup: MemoryPartitionLookup,
        user_question: str,
        runtime_turn_id: str | None = None,
        memory_available: bool = True,
    ) -> TurnMemorySession:
        source = await partition_lookup.resolve_self_origin(
            initiative_run_id=initiative_run_id,
            canonical_conversation_id=canonical_conversation_id,
        )
        if (
            identity.scope_type is not ScopeType.GROUP
            or identity.group_id != source.group_id
            or identity.bot_user_id != source.bot_user_id
        ):
            raise ValueError("SELF Memory scope does not match its initiative")
        decision = MemoryAccessDecision(
            contract=active_read_contract(
                MemoryRecallPurpose.BACKGROUND,
                persistent_write_allowed=False,
            )
            if memory_available
            else forbidden_contract(MemoryRecallPurpose.BACKGROUND),
            reason=MemoryAccessReason.SELF_ORIGIN,
            retrieval_degraded=not runtime.memory.retrieval_enabled,
        )
        return cls(
            decision=decision,
            scope=ResolvedMemoryScope.for_group(source.group_id),
            inbound=None,
            identity=identity,
            runtime=runtime,
            memory_context=memory_context,
            partition_lookup=partition_lookup,
            origin=TurnOrigin.SELF_INITIATIVE,
            user_question=user_question,
            runtime_turn_id=runtime_turn_id or str(uuid.uuid4()),
            self_origin=source,
        )

    @property
    def scope(self) -> ResolvedMemoryScope:
        return self._state.scope

    @property
    def reason(self) -> MemoryAccessReason:
        return self._decision.reason

    @property
    def retrieval_degraded(self) -> bool:
        return self._decision.retrieval_degraded

    def capability_view(self) -> MemoryCapabilityView:
        return build_capability_view(
            self._state.contract,
            transition_revision=1,
        )

    async def _memory_partition_key(self) -> str:
        if self._self_origin is not None:
            return self._self_origin.partition
        assert self._inbound is not None
        return await self._partition_lookup.resolve_from_scope(
            group_id=self._inbound.group_id,
            private_peer_user_id=(None if self._inbound.group_id else self._inbound.sender.user_id),
        )

    async def _ensure_read_receipt(self) -> str | None:
        """Associate execution statistics without claiming prompt exposure."""
        async with self._read_receipt_lock:
            handles = self._state.recall_handles()
            if handles:
                return handles[-1].receipt_turn_id
            empty = MemoryRetrievalResult(
                blocks=(),
                hits=(),
                candidate_count=0,
                selected_count=0,
                query_hash="",
                mode=MemoryRetrievalMode.RELEVANT,
            )
            intent = MemoryQueryIntent(purpose=MemoryRecallPurpose.RECALL)
            recall = await self._memory_context.record_recall(
                conversation_key=await self._memory_partition_key(),
                source_key=self._source_key,
                origin=self._origin.value,
                intent=intent,
                result=empty,
                injected_fact_ids=(),
                runtime=self._runtime,
                consumer="agent_tool",
            )
            if recall is None:
                return None
            self._state.record_recall(
                RecallHandle(
                    runtime_turn_id=self._runtime_turn_id,
                    receipt_turn_id=recall.turn_id,
                    purpose=intent.purpose,
                    injected_fact_ids=(),
                )
            )
            return recall.turn_id

    async def record_read_outcome(self, outcome: str) -> None:
        """A completed read is not necessarily injected into a model request."""
        self._state.require_open()
        if self._origin not in {
            TurnOrigin.USER_MESSAGE,
            TurnOrigin.AUTONOMOUS_GROUP,
            TurnOrigin.SELF_INITIATIVE,
        }:
            return
        if self.contract.availability is MemoryAvailability.FORBIDDEN:
            return
        receipt_id = await self._ensure_read_receipt()
        if receipt_id is not None:
            await self._memory_context.record_tool_read_outcome(receipt_id, outcome)

    async def confirm_prompt_exposure(self) -> MemoryReceiptHandle | None:
        self._state.require_open()
        if self._pending_tool_exposures:
            fact_ids = tuple(dict.fromkeys(item.fact_id for item in self._pending_tool_exposures))
            receipt_id = await self._ensure_read_receipt()
            if receipt_id is not None:
                await self._memory_context.mark_tool_injected(receipt_id, fact_ids)
                self._state.extend_recall_exposures(receipt_id, fact_ids)
            self._confirmed_exposures.extend(self._pending_tool_exposures)
            self._pending_tool_exposures = ()
        return None

    async def observe_tool_result(self, capability_id: str, result_json: str) -> None:
        if capability_id in _MEMORY_READ_TOOLS:
            self._observe_read(result_json)

    async def on_delivery_confirmed(self, summary: DeliverySummary) -> None:
        if self._state.closed:
            return
        self._delivery_reported = True
        if summary.status in {DeliveryStatus.CANCELLED, DeliveryStatus.FAILED}:
            for handle in self._state.recall_handles():
                await self._memory_context.set_attribution_outcome(
                    handle.receipt_turn_id, "skipped", "delivery_failed"
                )
            self._state.skip_attribution()
            self._delivery_reported = True
            return
        if (
            self._attribution is None
            or not self._runtime.memory.usage_attribution_enabled
            or self._origin not in {TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP}
            or not summary.delivered_text.strip()
            or not self._confirmed_exposures
        ):
            if self._confirmed_exposures:
                logging.getLogger(__name__).info(
                    "memory_attribution_not_scheduled coverage_incomplete=true"
                )
            self._state.skip_attribution()
            self._delivery_reported = True
            return
        self._state.freeze_exposures()
        exposures = tuple({item.fact_id: item for item in self._confirmed_exposures}.values())
        handles = self._state.recall_handles()
        for handle in handles:
            matched = tuple(item for item in exposures if item.fact_id in handle.injected_fact_ids)
            if not matched:
                continue
            await self._enqueue_job(
                handle.receipt_turn_id,
                MemoryQueryIntent(purpose=handle.purpose),
                matched,
                summary,
            )
        self._state.queue_attribution()
        self._delivery_reported = True

    async def close(self) -> None:
        if self._state.closed:
            return
        try:
            if not self._delivery_reported:
                for handle in self._state.recall_handles():
                    await self._memory_context.set_attribution_outcome(
                        handle.receipt_turn_id, "skipped", "interrupted"
                    )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "memory_session_cleanup_failed exception_category=%s", type(exc).__name__
            )
        finally:
            self._state.close()

    def _observe_read(self, result_json: str) -> None:
        decoded = _decode_json(result_json)
        data = decoded.get("data")
        if not isinstance(data, dict):
            return
        registry_payload = data.get("memories") or data.get("memory") or data
        from qq_ai_bot.memory.attribution import MemoryExposureRegistry

        registry = MemoryExposureRegistry()
        fact_ids = registry.register_tool_payload(registry_payload)
        snapshot = registry.snapshot()
        if snapshot:
            self._pending_tool_exposures = (*self._pending_tool_exposures, *snapshot)
        del fact_ids

    async def _enqueue_job(
        self,
        turn_id: str,
        intent: MemoryQueryIntent,
        exposures: tuple[MemoryExposure, ...],
        summary: DeliverySummary,
    ) -> None:
        if self._attribution is None or not turn_id or self._inbound is None:
            return
        await self._attribution.enqueue(
            MemoryAttributionJob(
                turn_id=turn_id,
                user_id=self._inbound.sender.user_id,
                group_id=self._inbound.group_id,
                user_question=self._user_question,
                final_response=summary.delivered_text,
                intent=intent,
                exposures=exposures,
                runtime=self._runtime,
                enqueued_at=datetime.now(UTC),
            )
        )


def _decode_json(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def empty_retrieval() -> MemoryRetrievalResult:
    return MemoryRetrievalResult(
        blocks=(),
        hits=(),
        candidate_count=0,
        selected_count=0,
        query_hash="",
        mode=MemoryRetrievalMode.RELEVANT,
        semantic_status="session_skipped",
    )
