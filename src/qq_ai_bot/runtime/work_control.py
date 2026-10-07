"""Provider-neutral lifecycle controls; host callbacks retain delivery authority."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qq_ai_bot.domain.messages import ChatMessage, ChatTool
from qq_ai_bot.runtime.activation_outcome import ActivationOutcome
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository
from qq_ai_bot.tool_results.access import ArtifactAccess

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_session import WorkSession


WORK_CONTROL_NAMES = frozenset(
    {"task_control", "subagent_start", "subagent_control", "subagent_message"}
)


class WorkInputsPreparing(RuntimeError):
    """Input is durable but its admitted attachment preparation is unfinished."""


def work_control_tools() -> tuple[ChatTool, ...]:
    from qq_ai_bot.runtime.work_context_note import ContextNote

    note_schema = ContextNote.model_json_schema()
    note_definitions = note_schema.pop("$defs", {})
    return (
        ChatTool(
            name="task_control",
            result_cacheable=False,
            description=(
                "管理持久工作。get 用原 work_id 查看状态、完整 goal 和等待条件；"
                "list 默认 active，终态用 status=terminal，全部用 all。"
                "新操作先单独 accept(goal,output_kind)，成功后再调用执行工具；"
                "普通聊天无需登记，发言用 send_message。"
                "原工作用 resume(work_id) 续接，不重复 accept；"
                "独立新工作用 accept 排队，update 只修正当前目标。"
                "update 也可仅保存 context_note：version=1，facts/unresolved/next_steps "
                "每项含 text 和 refs（goal、input:ID、event:ID、effect:原键、"
                "artifact:handle、child:ID）；线索不改变执行状态，研究原文按 artifact 回读。"
                "分段前保存累积发现、必要中间值与下一步；业务续跑使用当前聊天和 note，"
                "不会自动恢复此前整段工具往返。"
                "wait 登记 conditions：time_due(after_seconds 或含时区 at)、conversation、"
                "plugin_event(plugin_id,event_type)、owned_run(run_id)，"
                "wait_mode=any/all，deadline_at 可选；信号到达续原 work_id。"
                "wait_status 查询，cancel_wait 撤销。need_input 说明缺失信息；"
                "complete 提出结束，后端核对未决执行和 artifact。"
                "get/list/wait_status 是只读查询；其余生命周期 action 必须独占一个工具批次。"
            ),
            parameters={
                "type": "object",
                "$defs": note_definitions,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "get",
                            "list",
                            "accept",
                            "resume",
                            "update",
                            "wait",
                            "wait_status",
                            "cancel_wait",
                            "need_input",
                            "complete",
                            "fail",
                        ],
                    },
                    "goal": {"type": "string", "maxLength": 8192},
                    "context_note": note_schema,
                    "reporting": {
                        "type": "string",
                        "enum": ["interactive", "quiet"],
                        "description": (
                            "较长交互式任务在 accept 时用 interactive："
                            "先发送 start 说明再执行，"
                            "阶段按需要汇报，明确 complete/wait/need_input/fail 收尾。"
                            "quiet 仅用于用户要求安静执行；新真人追问仍需处理。"
                            "update 可只修改此字段，不改变目标或等待。"
                        ),
                    },
                    "work_id": {
                        "type": "string",
                        "maxLength": 36,
                        "description": "get 查询或 resume 续接原工作的内部 ID。",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "status": {"type": "string", "enum": ["active", "terminal", "all"]},
                    "cursor": {"type": "string", "maxLength": 256},
                    "output_kind": {
                        "type": "string",
                        "enum": ["answer", "artifact", "state_change"],
                        "description": (
                            "accept 必填。调查/写作用 answer；绘图/文件生成用 artifact；"
                            "修改状态用 state_change。"
                        ),
                    },
                    "deliver_artifacts": {
                        "type": "boolean",
                        "description": (
                            "文件任务默认需要实际发送。用户明确只要求保存在工作区时才设 false。"
                        ),
                    },
                    "artifact_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "reason": {"type": "string", "maxLength": 1000},
                    "run_id": {"type": "string", "maxLength": 36},
                    "wait_mode": {"type": "string", "enum": ["any", "all"]},
                    "conditions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"type": "object", "additionalProperties": True},
                    },
                    "deadline_at": {"type": "string", "maxLength": 40},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        ),
    )


@dataclass(slots=True)
class WorkControl:
    repository: WorkRepository
    lease: WorkLease
    source_key: str
    source: dict[str, Any]
    # Callbacks are installed by the trusted entrypoint, never by model arguments.
    validate: Callable[[], Awaitable[None]]
    resolve_child: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None
    session: WorkSession | None = None
    current_message: ChatMessage | None = None
    current: dict[str, Any] | None = None
    ending: str | None = None
    known_effects: list[dict[str, Any]] = field(default_factory=list)
    final_delivery: bool = False
    requests_started: int = 0
    deferred_failure: Any = None
    protocol_recovery_preparation: Any = None
    segment_model_limit: int = 24
    tools_started: int = 0
    yield_segment: bool = False
    handoff_work_id: str | None = None
    settled: bool = False
    recovery_deferred: bool = False
    outcome: ActivationOutcome | None = None
    staged_attempt: str | None = None
    context_access: ArtifactAccess | None = None

    def bind_context_access(self, access: ArtifactAccess) -> None:
        """Bind the authenticated entrypoint; model arguments cannot call this."""
        if access.conversation_id != self.lease.conversation_id or (
            access.generation != self.lease.generation
        ):
            raise ValueError("artifact_source_changed")
        self.context_access = access
        self.source.update(
            actor_person_id=access.actor_person_id,
            principal_kind=access.principal_kind,
            read_scope=access.read_scope,
        )

    @property
    def communication(self) -> dict[str, Any]:
        if self.current is None:
            return {}
        return dict(json.loads(self.current["checkpoint_json"]).get("communication", {}))

    @property
    def reporting(self) -> str | None:
        return self.communication.get("reporting")

    async def patch_communication(self, **updates: Any) -> None:
        if self.current is None:
            raise ValueError("no_active_work")
        self.current = await self.repository.patch_communication(
            self.lease, self.current["id"], updates
        )

    async def communication_inputs(
        self, *, after_id: int = 0, limit: int = 8
    ) -> list[dict[str, int]]:
        if self.current is None:
            return []
        return await self.repository.communication_inputs(
            self.lease, self.current["id"], after_id=after_id, limit=limit
        )

    async def communication_target(self) -> dict[str, str]:
        from sqlalchemy import select

        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

        async with self.repository.database.sessions() as session:
            conversation = (
                await session.execute(
                    select(
                        CanonicalConversationModel.space_id,
                        CanonicalConversationModel.person_id,
                    ).where(CanonicalConversationModel.id == self.lease.conversation_id)
                )
            ).first()
        if conversation is None:
            raise ValueError("work_delivery_conversation_missing")
        target_id = conversation.space_id or conversation.person_id
        if not target_id:
            raise ValueError("work_delivery_target_missing")
        return {
            "kind": "space" if conversation.space_id else "person",
            "id": target_id,
        }

    async def communication_consumed_watermark(self) -> int:
        if self.current is None:
            return 0
        return await self.repository.communication_consumed_watermark(
            self.lease, self.current["id"]
        )

    async def communication_reports(
        self,
        *,
        kind: str | None = None,
        event_ids: tuple[int, ...] = (),
        effect_keys: tuple[str, ...] = (),
        delivered_only: bool = False,
    ) -> list[dict[str, Any]]:
        if self.current is None:
            return []
        target = await self.communication_target()
        if effect_keys:
            result = []
            # Exact batch witnesses use bounded SQL bind pages, without imposing
            # a new limit on the configured number of model tool calls.
            for offset in range(0, len(effect_keys), 128):
                result.extend(
                    await self.repository.communication_reports(
                        self.lease,
                        self.current["id"],
                        target,
                        kind=kind,
                        event_ids=tuple(event_ids),
                        effect_keys=effect_keys[offset : offset + 128],
                        delivered_only=delivered_only,
                    )
                )
            return result
        return await self.repository.communication_reports(
            self.lease,
            self.current["id"],
            target,
            kind=kind,
            event_ids=tuple(event_ids),
            effect_keys=effect_keys,
            delivered_only=delivered_only,
        )

    def _validate_reporting(self, value: Any) -> None:
        if not isinstance(value, str) or value not in {"interactive", "quiet"}:
            raise ValueError("work_reporting_invalid")
        if value == "interactive" and (
            self.lease.work_id
            or self.source.get("parent_work_id")
            or self.source.get("principal_kind") == "self"
            or self.source.get("delivery_contract") in {"return_to_caller", "none"}
        ):
            raise ValueError("work_reporting_delivery_not_interactive")

    async def validate_work_report(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Validate host-owned association before any social side effect."""
        if "work_report" not in arguments:
            return None
        report = arguments["work_report"]
        if self.current is None or self.lease.work_id or self.source.get("parent_work_id"):
            raise ValueError("work_report_requires_main_work")
        if (
            not isinstance(report, dict)
            or set(report) - {"kind", "reply_to_event_ids"}
            or not isinstance(report.get("kind"), str)
            or report.get("kind") not in {"start", "progress", "reply", "final"}
        ):
            raise ValueError("work_report_invalid")
        event_ids = report.get("reply_to_event_ids", [])
        if (
            not isinstance(event_ids, list)
            or len(event_ids) > 8
            or any(type(identity) is not int or identity < 1 for identity in event_ids)
            or len(set(event_ids)) != len(event_ids)
        ):
            raise ValueError("work_report_event_ids_invalid")
        from sqlalchemy import select

        from qq_ai_bot.persistence.models import ChatEventModel
        from qq_ai_bot.runtime.work_schema_v1 import inputs

        async with self.repository.database.sessions() as session:
            admitted = set(
                await session.scalars(
                    select(inputs.c.event_id).where(
                        inputs.c.work_id == self.current["id"],
                        inputs.c.conversation_id == self.lease.conversation_id,
                        inputs.c.generation == self.lease.generation,
                        inputs.c.state.in_(("staged", "consumed")),
                        inputs.c.event_id.in_(event_ids),
                    )
                )
            )
            trigger = json.loads(self.current["source_json"]).get("trigger_event_id")
            if trigger in event_ids:
                original = await session.get(ChatEventModel, trigger)
                if original and original.canonical_conversation_id == self.lease.conversation_id:
                    admitted.add(trigger)
        if not set(event_ids) <= admitted:
            raise ValueError("work_report_event_not_admitted")
        target = await self.communication_target()
        selected = arguments.get("target")
        if selected is not None and (
            not isinstance(selected, dict)
            or selected.get("kind") != target["kind"]
            or selected.get("target_id") != target["id"]
            or set(selected) - {"kind", "target_id"}
        ):
            raise ValueError("work_report_target_not_current")
        return {"kind": report["kind"], "reply_to_event_ids": list(event_ids)}

    async def pending(self) -> list[dict[str, Any]]:
        if self.current is None:
            return []
        return await self.repository.pending(self.lease, work_id=self.current["id"])

    async def recover_failure(self, exc: BaseException) -> ActivationOutcome:
        from qq_ai_bot.runtime.activation_outcome import WorkRecoveryDeferred
        from qq_ai_bot.runtime.work_supervisor import recover_failure

        try:
            return await recover_failure(self, exc)
        except BaseException as secondary:
            self.recovery_deferred = True
            exc.add_note(f"recovery persistence failed: {type(secondary).__name__}")
            raise WorkRecoveryDeferred(type(exc).__name__) from exc

    async def background_state(self) -> str | None:
        """A foreground answer/finalization cannot terminate unfinished workers."""
        if self.current is None or self.lease.work_id:
            return None
        from qq_ai_bot.runtime.subagent_repository import SubagentRepository

        children = await SubagentRepository(self.repository).unfinished(self.current["id"])
        unfinished = children
        if any(r["state"] in {"queued", "running", "waiting_external"} for r in unfinished):
            return "waiting_external"
        if unfinished:
            return "suspended"
        return None

    async def complete_internal(self, call_key: str) -> None:
        """Settle an internal final using the existing complete receipt contract."""
        try:
            await self.validate()
            if not await self.repository.valid(self.lease):
                raise WorkConflict("work_activation_obsolete")
            await self._control({"action": "complete"}, call_key, all_artifacts=True)
            return
        except (ValueError, WorkConflict):
            pass
        self.ending = await self.background_state()
        if self.ending is None and await self.has_unresolved_effects(uncertain=False):
            self.ending = "waiting_external"
        if await self.has_unresolved_effects(pending=False):
            self.ending = None

    async def has_finite_model_budget(self) -> bool:
        """Read the actual persistent root/run limit; never invent a default."""
        if self.current is None:
            return False
        from sqlalchemy import select

        from qq_ai_bot.runtime.subagent_schema import children
        from qq_ai_bot.runtime.work_budget_schema import automation_budgets, budgets

        async with self.repository.database.sessions() as session:
            root = (
                await session.scalar(
                    select(children.c.root_id).where(children.c.work_id == self.current["id"])
                )
                or self.current["id"]
            )
            limit = await session.scalar(
                select(budgets.c.model_limit).where(budgets.c.root_id == root)
            )
            if limit is not None:
                return True
            run_id = self.source.get("automation_run_id")
            if self.source.get("owner") == "automation" and isinstance(run_id, int):
                return (
                    await session.scalar(
                        select(automation_budgets.c.model_limit).where(
                            automation_budgets.c.run_id == run_id
                        )
                    )
                    is not None
                )
        return False

    async def effect_evidence(self) -> list[dict[str, Any]]:
        if self.current is None:
            return []
        return await self.repository.effect_evidence(self.lease, self.current["id"])

    async def refresh_effects(self) -> None:
        if self.current is None:
            self.known_effects = []
        else:
            recent = await self.repository.effect_evidence(
                self.lease,
                self.current["id"],
                limit=64,
            )
            unresolved = await self.repository.effect_evidence(
                self.lease, self.current["id"], only_unresolved=True
            )
            self.known_effects = list(
                {item["effect_key"]: item for item in [*recent, *unresolved]}.values()
            )

    async def has_unresolved_effects(
        self,
        *,
        pending: bool = True,
        uncertain: bool = True,
    ) -> bool:
        if self.current is None:
            return False
        return await self.repository.has_unresolved_effects(
            self.lease,
            self.current["id"],
            pending=pending,
            uncertain=uncertain,
        )

    async def reconcile_completed_children(self) -> None:
        if self.current is None:
            return
        # Run receipts resolve original effects; no result is copied from a child
        # checkpoint into a second authority list.
        from qq_ai_bot.capabilities.results import normalize_legacy_result
        from qq_ai_bot.runtime.effect_outcomes import execution_evidence
        from qq_ai_bot.sandbox.environment_tools import EXECUTION_TOOLS

        evidence = await self.repository.effect_evidence(
            self.lease,
            self.current["id"],
            only_unresolved=True,
        )
        owners: dict[str, list[str]] = {}
        requests: dict[str, dict[str, str]] = {}
        for effect in evidence:
            identity = effect.get("run_id")
            if isinstance(identity, str) and (effect.get("pending") or effect.get("uncertain")):
                owners.setdefault(effect["work_id"], []).append(identity)
            elif (
                effect.get("tool") in EXECUTION_TOOLS
                and isinstance(effect.get("request_id"), str)
                and effect.get("uncertain")
            ):
                requests.setdefault(effect["work_id"], {})[effect["request_id"]] = effect[
                    "effect_key"
                ]
        for owner, ids in owners.items():
            ids = list(dict.fromkeys(ids))
            for offset in range(0, len(ids), 32):
                for result in await self.repository.completed_children(
                    self.lease,
                    owner,
                    ids[offset : offset + 32],
                ):
                    receipt = normalize_legacy_result(
                        {"ok": True, "data": result},
                        provider_id="core",
                        tool_name="sandbox_completion",
                    )
                    outcome = execution_evidence(
                        receipt, tool="sandbox_completion", side_effecting=False
                    )
                    await self.repository.resolve_run_effects(
                        self.lease,
                        owner,
                        result["run_id"],
                        outcome,
                    )
        for owner, original in requests.items():
            ids = list(original)
            for offset in range(0, len(ids), 32):
                for result in await self.repository.completed_children(
                    self.lease, owner, [], request_ids=ids[offset : offset + 32]
                ):
                    receipt = normalize_legacy_result(
                        {"ok": True, "data": result},
                        provider_id="core",
                        tool_name="sandbox_completion",
                    )
                    outcome = execution_evidence(
                        receipt, tool="sandbox_completion", side_effecting=False
                    )
                    await self.repository.resolve_run_effects(
                        self.lease,
                        owner,
                        result["run_id"],
                        outcome,
                        effect_key=original[result["request_id"]],
                        request_id=result["request_id"],
                    )
        await self.refresh_effects()

    async def take_inputs(
        self,
        attempt: str,
        *,
        observed_event_ids: frozenset[int] = frozenset(),
    ) -> tuple[ChatMessage, ...]:
        pending = await self.pending()
        if pending and not pending[0]["ready"]:
            raise WorkInputsPreparing("work_input_preparing")
        selected: list[int] = []
        messages = []
        size = 0
        for item in pending:
            if not item["ready"]:
                break
            payload = json.loads(item["payload_json"])
            text = str(payload.get("text", ""))
            if payload.get("signal"):
                # Wait metadata references the event ledger. Never resurrect the
                # obsolete text embedded by older wait writers after deletion.
                try:
                    signal = json.loads(text)
                except (TypeError, ValueError):
                    signal = None
                if isinstance(signal, dict) and signal.get("kind") == "work_signal":
                    ids = []
                    for condition in signal.get("conditions", []):
                        matched = condition.get("matched") if isinstance(condition, dict) else None
                        if isinstance(matched, dict):
                            matched.pop("text", None)
                            event_id = matched.get("event_id")
                            if isinstance(event_id, int) and event_id not in ids:
                                ids.append(event_id)
                    from sqlalchemy import select

                    from qq_ai_bot.conversation.canonical_db_models import (
                        CanonicalConversationModel,
                    )
                    from qq_ai_bot.persistence.models import ChatEventModel

                    async with self.repository.database.sessions() as reader:
                        scope = await reader.get(
                            CanonicalConversationModel, self.lease.conversation_id
                        )
                        events = (
                            (
                                await reader.scalars(
                                    select(ChatEventModel).where(
                                        ChatEventModel.id.in_(ids),
                                        ChatEventModel.canonical_conversation_id
                                        == self.lease.conversation_id,
                                        ChatEventModel.suppression_status == "keeper",
                                        ChatEventModel.id
                                        > (scope.starts_after_event_id if scope else 0),
                                    )
                                )
                            ).all()
                            if scope and scope.generation == self.lease.generation
                            else []
                        )
                    bodies = {event.id: event.content for event in events}
                    signal["events"] = [
                        {"event_id": identity, "text": bodies.get(identity, "原事件正文不可读")}
                        for identity in ids
                    ]
                    text = json.dumps(signal, ensure_ascii=False)
                    if self.session is not None:
                        self.session.event_ids.extend(
                            identity for identity in ids if identity in bodies
                        )

            already_visible = item["event_id"] is not None and (
                item["event_id"] in observed_event_ids
                or (self.session is not None and item["event_id"] in self.session.public_event_ids)
            )
            content = (
                f"[Work input_id={item['id']} event_id={item['event_id']} 已在当前聊天展示；"
                "关联原输入并按需回答，不重复原文]"
                if already_visible
                else f"[Work 信号 event_id={item['event_id']}；续原任务]\n{text}"
                if item["kind"] == "completion" or payload.get("signal")
                else f"[新增输入 event_id={item['event_id']}；保持原任务，按内容补充或回答]\n{text}"
            )
            if size + len(content.encode()) > 8192 and selected:
                break
            # This is a batching quantum, not permission to erase a user's
            # requirements. One oversized input stays whole; capacity planning
            # either admits it or preserves the original Work with an explanation.
            size += len(content.encode())
            selected.append(item["id"])
            if self.session is not None:
                self.session.input_ids.append(item["id"])
                if item["event_id"] is not None:
                    self.session.event_ids.append(item["event_id"])
                    self.session.public_event_ids.add(item["event_id"])
            images = await self.repository.input_images(item)
            if already_visible and self.session is not None and self.session.transcript is not None:
                present_images = tuple(
                    image
                    for message in self.session.transcript.request().messages
                    for image in message.images
                )
                images = tuple(image for image in images if image not in present_images)
            messages.append(
                ChatMessage(
                    role="user",
                    content=content,
                    images=images,
                )
            )
        if selected:
            await self.reconcile_completed_children()
            await self.repository.stage(self.lease, selected, attempt)
            self.staged_attempt = attempt
            self.ending = None
        return tuple(messages)

    async def confirm_inputs(self) -> None:
        if self.staged_attempt is not None:
            await self.repository.consume(self.lease, self.staged_attempt)
            self.staged_attempt = None

    async def restore_handoff(self, acknowledged: str | None) -> None:
        """A committed independent acceptance ends the old activation, even after a crash."""
        if self.current is None or self.lease.work_id:
            return
        identity = json.loads(self.current["checkpoint_json"]).get("handoff_work_id")
        if not isinstance(identity, str) or identity == acknowledged:
            return
        target = await self.repository.get(identity)
        if (
            target is not None
            and target["conversation_id"] == self.lease.conversation_id
            and target["generation"] == self.lease.generation
        ):
            self.handoff_work_id = identity

    async def reserve_request(self, *, auxiliary: bool = False) -> None:
        if auxiliary and self.requests_started >= self.segment_model_limit:
            from qq_ai_bot.runtime.activation_outcome import SegmentBudgetReached

            raise SegmentBudgetReached()
        if self.current is not None:
            await self.repository.checkpoint(self.lease, self.current["id"], None, models=1)
            self.current["model_requests"] += 1
        self.requests_started += 1
        if self.session is not None and not auxiliary:
            self.session.sequence += 1

    def observe_evidence(self, evidence: dict[str, Any]) -> None:
        """Bounded view of already accepted typed facts, never a result-text decoder."""
        if evidence.get("tool") in WORK_CONTROL_NAMES or not evidence.get("executed"):
            return
        entry = dict(evidence)
        side_effecting = entry.get("side_effecting") is True
        identity = entry.get("run_id")
        if identity:
            previous = [item for item in self.known_effects if item.get("run_id") == identity]
            entry["side_effecting"] = side_effecting or any(
                item.get("side_effecting") for item in previous
            )
            self.known_effects[:] = [
                item for item in self.known_effects if item.get("run_id") != identity
            ]
        self.known_effects.append(entry)
        self.known_effects[:] = self.known_effects[-64:]

    async def execute(self, name: str, args: dict[str, Any], call_key: str) -> str:
        from qq_ai_bot.capabilities.results import ToolExecutionResult
        from qq_ai_bot.runtime.effect_outcomes import current_result_capture

        try:
            await self.validate()
            if not await self.repository.valid(self.lease):
                raise WorkConflict("work_activation_obsolete")
            if name == "task_control":
                result = await self._control(args, call_key)
            elif name.startswith("subagent_"):
                from qq_ai_bot.runtime.subagent_tools import execute_subagent

                result = await execute_subagent(self, name, args, call_key)
            else:
                raise ValueError("unknown_work_control")
            payload = {"ok": True, **result}
            outcome = ToolExecutionResult(
                ok=payload["ok"] is True,
                data=result,
                tool_name=name,
                provider_id="work_control",
            )
        except (ValueError, WorkConflict) as exc:
            payload = {"ok": False, "error": str(exc)}
            outcome = ToolExecutionResult(
                ok=False,
                error_code=str(exc),
                tool_name=name,
                provider_id="work_control",
            )
        capture = current_result_capture.get()
        if capture is not None:
            capture.outcome = outcome
        return json.dumps(payload, ensure_ascii=False)

    async def update_context_note(
        self,
        value: Any,
        call_key: str,
        prepared: tuple[dict[str, Any], tuple[str, ...], int, int] | None = None,
    ) -> None:
        from qq_ai_bot.runtime.work_context_note import publish_pending_note, validate_note

        if self.current is None:
            raise ValueError("no_active_work")
        refreshed = await self.repository.get(self.current["id"])
        if refreshed is None:
            raise WorkConflict("work_context_note_obsolete")
        self.current = refreshed
        previous = json.loads(refreshed["checkpoint_json"]).get("context_note", {})
        payload, handles, source_revision, privacy = prepared or await validate_note(self, value)
        if previous.get("call_key") == call_key:
            if previous["payload"] != payload:
                raise ValueError("work_context_note_intent_changed")
        else:
            expected = previous.get("revision", 0)
            note = {
                "revision": expected + 1,
                "call_key": call_key,
                "payload": payload,
                "artifact_handles": list(handles),
                "source_revision": source_revision,
                "privacy_generation": privacy,
                "access": json.loads(self.context_access.encode(privacy))
                if self.context_access is not None
                else {
                    "conversation_id": self.lease.conversation_id,
                    "generation": self.lease.generation,
                    "actor_person_id": json.loads(refreshed["source_json"]).get("actor_person_id"),
                    "principal_kind": json.loads(refreshed["source_json"]).get(
                        "principal_kind", "person"
                    ),
                    "read_scope": json.loads(refreshed["source_json"]).get("read_scope", ""),
                },
            }
            await self.validate()
            self.current = await self.repository.patch_context_note(
                self.lease, refreshed["id"], expected, note
            )
        # Publication is independently retryable. Its failure does not erase a
        # saved note or pretend the update/tool had no durable effect.
        from qq_ai_bot.conversation.projections import ProjectionConflict

        try:
            await publish_pending_note(self)
        except (ValueError, ProjectionConflict):
            pass

    async def _control(
        self, args: dict[str, Any], call_key: str, *, all_artifacts: bool = False
    ) -> dict[str, Any]:
        action = args.get("action")
        if not isinstance(action, str):
            raise ValueError("work_action_required")
        if "context_note" in args and action != "update":
            raise ValueError("work_context_note_action_invalid")
        if "reporting" in args:
            if action not in {"accept", "update"}:
                raise ValueError("work_reporting_action_invalid")
            self._validate_reporting(args["reporting"])
        if action in {"get", "list"}:
            from qq_ai_bot.runtime.work_queries import WorkQueries

            queries = WorkQueries(self.repository)
            if action == "get":
                identity = args.get("work_id")
                if not isinstance(identity, str) or not identity.strip() or len(identity) > 36:
                    raise ValueError("work_id_required")
                row = await queries.get(self.lease, self.source, identity)
                if row is None:
                    raise ValueError("work_not_found_or_not_authorized")
                return {"work": row}
            limit = args.get("limit", 8)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
                raise ValueError("work_list_limit_invalid")
            status = args.get("status", "active")
            if not isinstance(status, str) or status not in {"active", "terminal", "all"}:
                raise ValueError("work_list_status_invalid")
            cursor = args.get("cursor")
            if cursor is not None and (
                not isinstance(cursor, str) or not cursor or len(cursor) > 256
            ):
                raise ValueError("work_list_cursor_invalid")
            return await queries.list(
                self.lease, self.source, limit=limit, status=status, cursor=cursor
            )
        if self.lease.work_id:
            if action == "accept":
                raise ValueError("worker_already_registered")
            if action in {"answer", "need_input"} and self.source.get("parent_work_id"):
                from qq_ai_bot.runtime.subagent_tools import execute_subagent

                return await execute_subagent(
                    self,
                    "subagent_message",
                    {
                        "text": args.get("text") if action == "answer" else args.get("reason"),
                        "ask": action == "need_input",
                    },
                    call_key,
                )
        if action == "resume":
            if self.current is not None or self.lease.work_id:
                raise ValueError("resume_requires_neutral_foreground")
            identity = args.get("work_id")
            candidates = await self.available_work()
            if not isinstance(identity, str) or identity not in {r["work_id"] for r in candidates}:
                raise ValueError("resume_work_not_authorized")
            await self.repository.enqueue(
                self.lease.conversation_id,
                self.lease.generation,
                self.source_key,
                kind="message",
                event_id=self.source.get("trigger_event_id"),
                work_id=identity,
                ready=True,
                resume=(
                    self.lease,
                    {
                        "text": self.current_message.content
                        if self.current_message
                        else "继续原目标"
                    },
                ),
            )
            self.handoff_work_id = identity
            return {"resumed_work_id": identity, "state": "queued", "continue_original_chain": True}
        if action == "accept":
            if self.current is not None:
                return await self._queue_work(args)
            goal = args.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("work_goal_required")
            output_kind = args.get("output_kind")
            if not isinstance(output_kind, str) or output_kind not in {
                "answer",
                "artifact",
                "state_change",
            }:
                raise ValueError("work_output_kind_required")
            self.current = await self.repository.accept(
                self.lease,
                source_key=self.source_key,
                source=self.source,
                goal=goal,
                output_kind=output_kind,
                deliver_artifacts=args.get("deliver_artifacts") is not False,
                reporting=args.get("reporting"),
            )
            if self.requests_started and self.current["model_requests"] == 0:
                await self.repository.checkpoint(
                    self.lease,
                    self.current["id"],
                    None,
                    models=self.requests_started,
                )
                self.current["model_requests"] = self.requests_started
        elif self.current is None:
            raise ValueError("no_active_work")
        elif action == "wait_status":
            from qq_ai_bot.runtime.work_wait import WorkWaitRepository

            return {
                "work_id": self.current["id"],
                "wait": await WorkWaitRepository(self.repository).describe(self.current["id"]),
            }
        elif action == "cancel_wait":
            from qq_ai_bot.runtime.work_wait import WorkWaitRepository

            cancelled = await WorkWaitRepository(self.repository).cancel(
                self.lease, self.current["id"]
            )
            if cancelled:
                self.ending = None
            return {"work_id": self.current["id"], "wait_cancelled": cancelled}
        elif action == "update":
            goal = args.get("goal")
            note_plan = None
            if "context_note" in args:
                from qq_ai_bot.runtime.work_context_note import validate_note

                note_plan = await validate_note(self, args["context_note"])
            if args.get("reporting") == "quiet" and self.reporting == "interactive":
                raise ValueError("work_reporting_cannot_quiet_interactive")
            if goal is None:
                if "reporting" not in args and "context_note" not in args:
                    raise ValueError("work_goal_required")
                if "reporting" in args:
                    await self.patch_communication(reporting=args["reporting"])
            else:
                if not isinstance(goal, str) or not goal.strip():
                    raise ValueError("work_goal_required")
                self.current = await self.repository.transition(
                    self.lease,
                    self.current["id"],
                    self.current["revision"],
                    "running",
                    goal=goal,
                )
                self.ending = None
                if "reporting" in args:
                    await self.patch_communication(reporting=args["reporting"])
            if "context_note" in args:
                await self.update_context_note(args["context_note"], call_key, note_plan)
        elif action == "wait":
            if args.get("conditions") is not None:
                if args.get("run_id") is not None or self.lease.work_id:
                    raise ValueError("signal_wait_requires_parent_work")
                from qq_ai_bot.runtime.work_wait import WorkWaitRepository, normalize_conditions

                conditions = args["conditions"]
                normalized = normalize_conditions(conditions, time.time())
                for condition in normalized:
                    if condition["kind"] != "owned_run":
                        continue
                    identity = condition["run_id"]
                    child = await self.resolve_child(identity) if self.resolve_child else None
                    if child is None:
                        from qq_ai_bot.runtime.subagent_repository import SubagentRepository

                        try:
                            child = await SubagentRepository(self.repository).related(
                                self.current["id"], identity
                            )
                        except ValueError:
                            pass
                    if child is None:
                        raise ValueError("waiting_requires_owned_execution")
                wait = await WorkWaitRepository(self.repository).register(
                    self.lease,
                    work_id=self.current["id"],
                    source=self.source,
                    call_key=f"wait:{self.current['id']}:{hashlib.sha256(call_key.encode()).hexdigest()}",
                    mode=args.get("wait_mode", "any"),
                    conditions=conditions,
                    deadline_at=args.get("deadline_at"),
                )
                self.ending = "waiting_external"
                return {
                    "work_id": self.current["id"],
                    "wait_id": wait["id"],
                    "ending_proposed": self.ending,
                    "mode": wait["mode"],
                }
            identity = args.get("run_id")
            child = (
                await self.resolve_child(identity)
                if isinstance(identity, str) and self.resolve_child
                else None
            )
            if (
                not child
                and isinstance(identity, str)
                and self.current is not None
                and not self.lease.work_id
            ):
                from qq_ai_bot.runtime.subagent_repository import SubagentRepository

                try:
                    row = await SubagentRepository(self.repository).related(
                        self.current["id"], identity
                    )
                    child = {
                        "pending": row["state"]
                        in {"queued", "running", "waiting_external", "waiting_user"}
                    }
                except ValueError:
                    pass
            if not child or not child.get("pending"):
                raise ValueError("waiting_requires_owned_pending_execution")
            await self.repository.checkpoint(
                self.lease, self.current["id"], {"pending_run_id": identity}
            )
            self.ending = "waiting_external"
        elif action in {"need_input", "fail"}:
            if action == "fail" and await self.background_state() is not None:
                raise ValueError("unfinished_subagents_use_wait_or_cancel_explicitly")
            reason = args.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                raise ValueError("work_reason_required")
            await self.repository.checkpoint(self.lease, self.current["id"], {"reason": reason})
            self.ending = "waiting_user" if action == "need_input" else "failed"
        elif action == "complete":
            if not self.lease.work_id:
                from qq_ai_bot.runtime.subagent_repository import SubagentRepository

                children = await SubagentRepository(self.repository).unfinished(self.current["id"])
                if children:
                    raise ValueError("work_has_unfinished_subagents")
            await self.reconcile_completed_children()
            if await self.has_unresolved_effects():
                raise ValueError("work_has_unresolved_execution")
            facts = await self.effect_evidence()
            kind = self.current["output_kind"]
            if kind == "artifact":
                known = {
                    identity
                    for effect in facts
                    if effect.get("ok") or effect.get("delivered_artifacts")
                    for identity in effect.get("artifacts", [])
                }
                selected = list(known) if all_artifacts else args.get("artifact_ids")
                if (
                    not isinstance(selected, list)
                    or not selected
                    or any(
                        not isinstance(identity, str) or identity not in known
                        for identity in selected
                    )
                ):
                    raise ValueError("work_completion_requires_verified_artifacts")
                delivered = {
                    identity
                    for effect in facts
                    for identity in effect.get("delivered_artifacts", [])
                }
                if self.current["deliver_artifacts"] and not set(selected) <= delivered:
                    raise ValueError("work_completion_requires_artifact_delivery_receipt")
            elif (
                kind == "answer"
                and self.source.get("delivery_contract") != "return_to_caller"
                and self.source.get("principal_kind") != "self"
            ):
                if self.reporting == "interactive" and not await self.communication_reports(
                    kind="final", delivered_only=True
                ):
                    raise ValueError("work_completion_requires_final_delivery_receipt")
                # Explicit completion may be silent. The model's final text is
                # internal state; it never becomes a fallback outbound message.
                self.final_delivery = True
            elif kind == "answer" and self.source.get("principal_kind") == "self":
                # A SELF decision may end with NO_REPLY. Completion acknowledges
                # the internal decision, not a QQ transport effect. Side effects
                # remain separately backed by their original tool receipts.
                self.final_delivery = True
            elif kind == "state_change" and not any(
                effect.get("ok")
                and effect.get("side_effecting", True)
                and not effect.get("work_report")
                for effect in facts
            ):
                raise ValueError("work_completion_requires_execution_evidence")
            self.ending = "completed"
        else:
            raise ValueError("invalid_work_action")
        return {
            "work_id": self.current["id"],
            "state": self.current["state"],
            "revision": self.current["revision"],
            "ending_proposed": self.ending,
        }

    async def _queue_work(self, args: dict[str, Any]) -> dict[str, Any]:
        from sqlalchemy import select

        from qq_ai_bot.persistence.models import ChatEventModel

        assert self.current is not None
        if self.source.get("origin") != "user_message":
            raise ValueError("independent_work_requires_new_user_input")
        anchors = [self.source.get("trigger_event_id")]
        if self.session is not None:
            anchors.extend(self.session.event_ids)
        event_id = max((value for value in anchors if isinstance(value, int)), default=0)
        original = json.loads(self.current["source_json"])
        if not event_id or event_id == original.get("trigger_event_id"):
            raise ValueError("work_already_active_use_update")
        async with self.repository.database.sessions() as session:
            event = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.id == event_id,
                    ChatEventModel.canonical_conversation_id == self.lease.conversation_id,
                    ChatEventModel.direction == "inbound",
                    ChatEventModel.event_kind == "message",
                    ChatEventModel.sender_user_id == self.source.get("actor_user_id"),
                )
            )
        if event is None:
            raise ValueError("independent_work_source_invalid")
        goal, kind = args.get("goal"), args.get("output_kind")
        if (
            not isinstance(goal, str)
            or not goal.strip()
            or not isinstance(kind, str)
            or kind not in {"answer", "artifact", "state_change"}
        ):
            raise ValueError("work_goal_and_output_kind_required")
        source = {
            **self.source,
            "trigger_event_id": event.id,
            "presence_id": event.ingress_presence_id,
        }
        queued = await self.repository.accept(
            self.lease,
            source_key=f"event:{self.lease.conversation_id}:{event.id}",
            source=source,
            goal=goal,
            output_kind=kind,
            deliver_artifacts=args.get("deliver_artifacts") is not False,
            handoff_from=self.current["id"],
            reporting=args.get("reporting"),
        )
        self.handoff_work_id = queued["id"]
        return {
            "queued_work_id": queued["id"],
            "state": queued["state"],
            "current_work_id": self.current["id"],
            "instruction": (
                "新工作已交给 queued_work_id，本轮立即让出执行位置。"
                "后续由新工作的原始请求开始执行和交付；当前工作不得代做、代发。"
            ),
        }

    async def runtime_state(self) -> dict[str, Any]:
        """Append fresh scoped facts without replacing any submitted request prefix."""
        active = self.current
        state: dict[str, Any] = {
            "state": active["state"] if active else "no_active_work",
            "state_scope": "current_activation",
        }
        if active is not None:
            state.update(work_id=active["id"], goal=active["goal"])
            if self.reporting is not None:
                state["reporting"] = self.reporting
            return state

        from qq_ai_bot.runtime.work_queries import WorkQueries

        queries = WorkQueries(self.repository)
        available = await queries.available(self.lease, self.source)
        if available:
            state["available_work"] = available
        recent = await queries.recent(self.lease, self.source)
        if recent is not None:
            # Absence of the marker never turns an incomplete excerpt into a full goal.
            if recent["goal_complete"]:
                recent.pop("goal_complete")
            state["recent_work"] = recent
        return state

    async def available_work(self) -> list[dict[str, Any]]:
        from qq_ai_bot.runtime.work_queries import WorkQueries

        return await WorkQueries(self.repository).available(self.lease, self.source)

    async def settle(self, *, delivered: bool, pending_inputs: bool) -> None:
        from qq_ai_bot.runtime.work_supervisor import settle

        await settle(self, delivered=delivered, pending_inputs=pending_inputs)
