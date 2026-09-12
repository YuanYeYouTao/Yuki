"""One fenced work activation's dispatch and per-call recovery checkpoints."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

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
        self.recoverable = True
        self.sequence = 0
        self.recovered_delivery: str | None = None

    async def restore(self, initial: TurnTranscript) -> TurnTranscript:
        control = self.control
        async with control.repository.database.sessions() as session:
            source = await session.get(CanonicalConversationModel, control.lease.conversation_id)
            if source is None or source.generation != control.lease.generation:
                raise WorkConflict("work_source_generation_changed")
            self.source_revision = source.prompt_source_revision
        row = (
            await self.journal.load(control.lease, control.current["id"], self.contract)
            if control.current
            else None
        )
        self.transcript = initial
        if not row and control.current and control.current["model_requests"]:
            evidence = json.loads(control.current["checkpoint_json"]).get("execution_evidence", [])
            control.known_effects = list(evidence)
            initial.append(
                ChatMessage(
                    role="user",
                    content=(
                        "[持续工作恢复：来源、模型合同或临时媒体发生变化，建立新上下文。"
                        "以下为已记录执行证据；先查询原 run_id，不能盲目重跑或重复发送。]\n"
                        + json.dumps(evidence, ensure_ascii=False)
                    ),
                )
            )
        if row:
            value = json.loads(row["payload_json"])
            self.transcript = decode_transcript(value["transcript"])
            metadata = value.get("metadata", {})
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
        return self.transcript

    def call_key(self, call_id: str) -> str:
        assert self.transcript is not None
        return f"{self.transcript.chain_id}:{self.sequence}:{call_id}"

    async def save(self, phase: str, calls: tuple[ToolCall, ...] = ()) -> None:
        if self.control.current is None:
            return
        await self.control.repository.checkpoint(
            self.control.lease,
            self.control.current["id"],
            None,
            evidence=self.control.known_effects,
        )
        if not self.recoverable:
            return
        assert self.transcript is not None
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
                },
            )
        except ValueError as exc:
            if str(exc) in {"work_record_too_large", "work_journal_capacity"}:
                await self.journal.invalidate(self.control.lease, self.control.current["id"])
                raise WorkCapacityError("work_checkpoint_capacity") from exc
            if str(exc) != "work_checkpoint_ephemeral_media":
                raise
            await self.journal.invalidate(self.control.lease, self.control.current["id"])
            # Inline media is not persisted. Keep operating while in memory;
            # recovery must explicitly rehydrate the canonical attachment.
            self.recoverable = False

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
        await control.charge_tools(1)
        try:
            result = await invoke()
        except BaseException:
            await control.repository.record_effect(
                key, "unknown", {"error": "execution_interrupted"}
            )
            raise
        await control.repository.record_effect(key, "accepted", {"result": result})
        return result
