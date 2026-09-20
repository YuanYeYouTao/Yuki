"""One fenced work activation's dispatch and per-call recovery checkpoints."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ToolCall
from qq_ai_bot.runtime.work_journal import WorkJournal, decode_transcript
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict
from qq_ai_bot.services.turn_transcript import TurnTranscript

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl


class WorkSession:
    def __init__(self, control: WorkControl, contract: str) -> None:
        self.control = control
        self.journal = WorkJournal(control.repository)
        self.contract = contract
        self.transcript: TurnTranscript | None = None
        self.source_revision = 0
        self.pending: list[dict[str, Any]] = []
        self.event_ids: list[int] = []
        self.input_ids: list[int] = []
        self.sequence = 0
        self.recovered_delivery: str | None = None
        self.progress: dict[str, Any] = {}
        self.reply_state_reader: Callable[[], dict[str, Any]] | None = None
        self.initial: TurnTranscript | None = None
        self.handoff_work_id: str | None = None

    async def restore(self, initial: TurnTranscript) -> TurnTranscript:
        control = self.control
        async with control.repository.database.sessions() as session:
            source = await session.get(CanonicalConversationModel, control.lease.conversation_id)
            if source is None or source.generation != control.lease.generation:
                raise WorkConflict("work_source_generation_changed")
            self.source_revision = source.prompt_source_revision
        loaded = (
            await self.journal.load(control.lease, control.current["id"], self.contract)
            if control.current
            else None
        )
        row = loaded.record if loaded else None
        if loaded and loaded.reason in {"contract_changed", "source_changed"}:
            self.progress["chain_links"] = [
                {"from": loaded.previous_chain, "to": initial.chain_id, "reason": loaded.reason}
            ]
        self.transcript = initial
        self.initial = initial
        if not row and control.current and control.current["model_requests"]:
            evidence = json.loads(control.current["checkpoint_json"]).get("execution_evidence", [])
            control.known_effects = list(evidence)
            initial.append(
                ChatMessage(
                    role="user",
                    content=(
                        "[持续工作恢复：来源或模型合同发生变化，建立新上下文。"
                        "以下为已记录执行证据；先查询原 run_id，不能盲目重跑或重复发送。]\n"
                        + json.dumps(evidence, ensure_ascii=False)
                    ),
                )
            )
        if row:
            value = json.loads(row["payload_json"])
            self.transcript = decode_transcript(value["transcript"])
            metadata = value.get("metadata", {})
            self.handoff_work_id = metadata.get("handoff_work_id")
            self.progress = dict(metadata.get("progress", {}))
            self.sequence = int(metadata.get("sequence", 0))
            self.event_ids = list(metadata.get("event_ids", []))
            self.input_ids = list(metadata.get("input_ids", []))
            control.known_effects = list(metadata.get("effects", []))
            if (
                row["phase"] in {"delivery", "delivered"}
                and control.source.get("trigger_event_id") in self.event_ids
                and not await control.pending()
            ):
                self.recovered_delivery = row["phase"]
                control.ending = (
                    metadata.get("ending") if row["phase"] == "delivered" else "suspended"
                )
                control.final_delivery = row["phase"] == "delivered"

            # Never run calls from a recovered model response. Attach persisted
            # outcomes, or uncertainty, before any fresh input/model dispatch.
            for call in value["pending"]:
                result = await self.journal.effect_result(self.call_key(call["id"]))
                self.transcript.append_result(call["id"], result)
                control.observe_result(
                    call["name"], result, True, arguments=call.get("arguments", "{}")
                )
            if control.source.get("trigger_event_id") not in self.event_ids:
                if control.current_message is not None:
                    self.transcript.append(control.current_message)
        trigger = control.source.get("trigger_event_id")
        if isinstance(trigger, int) and trigger not in self.event_ids:
            self.event_ids.append(trigger)
        if control.current is not None:
            await self.journal.recovered_inputs(
                control.lease, self.input_ids, control.current["id"]
            )
        await control.reconcile_completed_children()
        await control.restore_handoff(self.handoff_work_id)
        if control.handoff_work_id is not None:
            self.transcript.append(
                ChatMessage(
                    role="user",
                    content=(
                        f"[工作交接已提交：独立请求由 {control.handoff_work_id} 负责执行和交付。"
                        "当前工作只保留自己的原目标，不得代做或重发该独立请求。]"
                    ),
                )
            )
        return self.transcript

    async def needs_compaction(self) -> bool:
        if self.control.current is None:
            return False
        from sqlalchemy import LargeBinary, func, select

        from qq_ai_bot.runtime.work_recovery_schema import quota
        from qq_ai_bot.runtime.work_schema_v1 import journal

        async with self.control.repository.database.sessions() as session:
            size = await session.scalar(
                select(func.length(journal.c.payload_json.cast(LargeBinary))).where(
                    journal.c.work_id == self.control.current["id"]
                )
            )
            total = await session.scalar(select(quota.c.bytes).where(quota.c.id == 1))
        return (
            bool(self.progress.get("compacting"))
            or int(size or 0) >= 3 * 1024 * 1024
            or (int(total or 0) >= 56 * 1024 * 1024 and int(size or 0) >= 128 * 1024)
        )

    async def compact(self, summary: str) -> TurnTranscript:
        if not summary.strip() or len(summary.encode()) > 65536:
            raise ValueError("invalid_worker_compaction_summary")
        assert self.transcript is not None and self.initial is not None
        previous = self.transcript.chain_id
        # Stable system contract and original task brief survive verbatim.
        initial_messages = self.initial.request().messages
        boundary = next(
            (index + 1 for index, message in enumerate(initial_messages) if message.role == "user"),
            len(initial_messages),
        )
        self.transcript = TurnTranscript(initial_messages[:boundary])
        self.transcript.append(
            ChatMessage(
                role="user",
                content=json.dumps(
                    {
                        "kind": "explicit_context_compaction",
                        "previous_chain_id": previous,
                        "summary": summary,
                        "execution_evidence": self.control.known_effects,
                        "instruction": "继续原目标；先核对原执行 ID，不能因压缩重跑或重复发布。",
                    },
                    ensure_ascii=False,
                ),
            )
        )
        self.progress["compacting"] = False
        self.progress["context_tokens"] = 0
        self.progress["chain_links"] = [
            *self.progress.get("chain_links", []),
            {
                "from": previous,
                "to": self.transcript.chain_id,
                "reason": "capacity_or_context",
            },
        ][-64:]
        await self.save("paired")
        return self.transcript

    def call_key(self, call_id: str) -> str:
        assert self.transcript is not None
        return f"{self.transcript.chain_id}:{self.sequence}:{call_id}"

    async def save(self, phase: str, calls: tuple[ToolCall, ...] = ()) -> None:
        if self.control.current is None:
            return
        assert self.transcript is not None
        if self.reply_state_reader is not None:
            self.progress["reply_state"] = self.reply_state_reader()
        self.handoff_work_id = self.control.handoff_work_id or self.handoff_work_id
        self.pending = [
            {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
            for call in calls
        ]
        try:
            await self.journal.save(
                self.control.lease,
                self.control.current["id"],
                self.contract,
                self.transcript,
                phase=phase,
                pending=self.pending,
                source_revision=self.source_revision,
                metadata={
                    "sequence": self.sequence,
                    "event_ids": self.event_ids[-256:],
                    "input_ids": self.input_ids[-256:],
                    "effects": self.control.known_effects,
                    "ending": self.control.ending,
                    "progress": self.progress,
                    "handoff_work_id": self.handoff_work_id,
                },
            )
        except IntegrityError as exc:
            if "ck_runtime_checkpoint_bytes" in str(exc.orig):
                raise WorkCapacityError("work_checkpoint_capacity") from exc
            raise
        except ValueError as exc:
            if str(exc) in {"work_record_too_large", "work_journal_capacity"}:
                raise WorkCapacityError("work_checkpoint_capacity") from exc
            raise

    async def execute(
        self, call: ToolCall, invoke: Callable[[], Awaitable[str]], *, side_effecting: bool = True
    ) -> str:
        control = self.control
        if control.current is None:
            return await invoke()
        if await control.pending():
            return json.dumps(
                {"ok": False, "executed": False, "error": "new_input_before_execution"}
            )
        await control.validate()
        if side_effecting and any(effect.get("uncertain") for effect in control.known_effects):
            return json.dumps(
                {
                    "ok": False,
                    "error": "unresolved_prior_effect",
                    "executed": False,
                    "detail": "先查询原执行结果；结果未知时不能继续副作用。",
                },
                ensure_ascii=False,
            )
        key = self.call_key(call.id)
        if not await control.repository.prepare_effect(
            control.lease, control.current["id"], key, "tool"
        ):
            return await self.journal.effect_result(key)
        from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded

        try:
            await control.charge_tools(1)
        except WorkBudgetExceeded:
            await control.repository.record_effect(
                key,
                "accepted",
                {
                    "result": json.dumps(
                        {"ok": False, "executed": False, "error": "work_total_budget_exhausted"}
                    )
                },
            )
            raise
        try:
            result = await invoke()
        except BaseException as exc:
            from qq_ai_bot.runtime.activation_outcome import DeliveryDeferred

            try:
                await control.repository.record_effect(
                    key,
                    "failed" if isinstance(exc, DeliveryDeferred) else "unknown",
                    {
                        "error": "never_dispatched"
                        if isinstance(exc, DeliveryDeferred)
                        else "execution_interrupted"
                    },
                )
            except Exception as secondary:
                exc.add_note(f"effect receipt persistence deferred: {type(secondary).__name__}")
            raise
        await control.repository.record_effect(key, "accepted", {"result": result})
        return result
