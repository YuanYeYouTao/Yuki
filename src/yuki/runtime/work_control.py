"""Provider-neutral lifecycle controls; host callbacks retain delivery authority."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from yuki.domain.messages import ChatMessage, ChatTool, ToolCall
from yuki.identity.person_identity import same_person_condition
from yuki.runtime.activation_outcome import ActivationOutcome
from yuki.runtime.work_repository import WorkConflict, WorkLease, WorkRepository, encode_json
from yuki.tool_results.access import ArtifactAccess

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from yuki.capabilities.invocation import Invocation
    from yuki.runtime.work_session import WorkSession


WORK_CONTROL_NAMES = frozenset(
    {"get_tasks", "manage_task", "start_subtask", "message_task", "wait"}
)
# Lifecycle controls that only read Work facts.
READ_ONLY_CONTROLS = frozenset({"get_tasks"})


# Accepted lifecycle actions and the Work state each one proposes to the writer.
# These are the durable checkpoint names (accepted_control.action), not tool actions.
ACCEPTED_ENDINGS = {
    "complete": "completed",
    "fail": "failed",
    "cancel": "cancelled",
    "need_input": "waiting_user",
    "wait": "waiting_external",
}

_ERROR_MESSAGES = {
    "no_active_work": "当前没有任务；先用 manage_task(action=start, goal=…) 登记。",
    "task_id_required": "id 必须是已有任务的 id。",
    "work_not_found_or_not_authorized": "id 对应的任务不存在或无权操作；先用 get_tasks 查看。",
    "work_goal_required": "goal 不能为空。",
    "task_action_invalid": "action 必须是 start、update、finish、fail、cancel 或 resume。",
    "worker_already_registered": "子任务已经登记，直接推进；修改目标用 update。",
    "resume_requires_neutral_foreground": "当前已有任务；恢复另一项任务请在没有当前任务时调用。",
    "resume_work_not_authorized": "该任务已结束或无权恢复。",
    "work_already_active_use_update": "当前已有任务；同一目标用 update 修改，不重复 start。",
    "independent_work_requires_new_user_input": "只有新的用户消息才能另起一项独立任务。",
    "independent_work_source_invalid": "找不到能另起任务的新用户消息。",
    "waiting_requires_owned_execution": "for 中的 run 必须是自己的子任务或自己运行的命令 id。",
    "wait_for_invalid": (
        "for 是非空数组，每项恰含一个键：run、message、seconds、at 或 plugin_event。"
    ),
    "wait_time_requires_timezone": "at 和 deadline 必须是带时区的 ISO-8601 时间。",
    "invalid_wait_time": "at 和 deadline 必须是带时区的 ISO-8601 时间。",
    "invalid_wait_delay": "seconds 必须是有限数字。",
    "invalid_wait_plugin_filter": (
        "plugin_event 需要 plugin_id 与 event_type（各 1–128 字），source 可选。"
    ),
    "invalid_wait_run": "run 必须是子任务或命令的 id。",
    "subtask_requires_current_task": (
        "start_subtask 需要当前任务；先用 manage_task(action=start) 登记。"
    ),
    "subtask_parent_unavailable": "当前不是子任务，没有父任务可联系；给子任务发消息请填 id。",
    "subtask_not_owned": "id 不是当前任务的子任务。",
    "subagent_not_owned": "id 不是当前任务的子任务。",
    "subagent_archived": "该子任务已归档，不能再发送或恢复。",
    "subagent_not_resumable": "该子任务已取消，不能恢复。",
    "subagent_message_target_terminal": "该子任务已取消，不能再发消息。",
    "message_text_required": "text 不能为空。",
    "invalid_note": "note 的 facts、unresolved、next_steps 每项须含 text 与非空 refs。",
    "invalid_task_query": (
        "limit 为 1–50，status 为 active、done 或 all，cursor 用上次返回的 next_cursor。"
    ),
    "invalid_work_query_cursor": "cursor 只能使用上次返回的 next_cursor。",
    "invalid_work_progress_cursor": "cursor 只能使用上次返回的 next_cursor。",
    "work_activation_obsolete": "本次执行已失效，操作未执行。",
}


def control_failure_message(code: str) -> str:
    return _ERROR_MESSAGES.get(code, f"任务操作未完成（{code}）；检查参数后再试。")


def control_result_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Model projection of an accepted lifecycle control; its receipt stores the same text."""
    return {key: value for key, value in data.items() if value is not None}


def _iso(stamp: Any) -> str | None:
    from datetime import datetime, timedelta, timezone

    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        return None
    return datetime.fromtimestamp(stamp, timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def task_view(row: dict[str, Any]) -> dict[str, Any]:
    """Model view of one Work row: no platform accounts, generations or epoch floats."""
    view: dict[str, Any] = {
        "id": row["work_id"],
        "goal": row["goal"],
        "status": "waiting_parent"
        if row["state"] == "waiting_user" and row.get("parent_work_id")
        else row["state"],
        "parent": row.get("parent_work_id"),
        "reason": row.get("reason") or None,
        "creator": row.get("creator_display_name"),
        "creator_person_id": row.get("creator_person_id"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }
    if row.get("model_requests") or row.get("tool_calls"):
        view["usage"] = {
            "model_requests": row.get("model_requests", 0),
            "tool_calls": row.get("tool_calls", 0),
        }
    if row.get("progress_available") is False:
        view["progress_available"] = False
    if isinstance(row.get("steps"), list):
        view["steps"] = [
            {
                key: value
                for key, value in {
                    **{
                        name: step.get(name)
                        for name in (
                            "effect_key",
                            "tool",
                            "action",
                            "ok",
                            "status",
                            "pending",
                            "uncertain",
                            "run_id",
                            "error_code",
                            "artifacts",
                        )
                    },
                    "at": _iso(step.get("updated_at")),
                    "result": step.get("result"),
                }.items()
                if value not in (None, [], False) or key == "ok"
            }
            for step in row["steps"]
        ]
    note = row.get("context_note")
    if isinstance(note, dict) and isinstance(note.get("payload"), dict):
        view["note"] = {
            key: note["payload"][key]
            for key in ("facts", "unresolved", "next_steps")
            if note["payload"].get(key)
        }
    wait = row.get("wait")
    if isinstance(wait, dict):
        view["wait"] = {
            "status": wait.get("status"),
            "all": wait.get("mode") == "all",
            "since": _iso(wait.get("registered_at")),
            "deadline": _iso(wait.get("deadline")),
            "for": wait.get("conditions"),
        }
    if row.get("next_cursor"):
        view["next_cursor"] = row["next_cursor"]
    return {key: value for key, value in view.items() if value is not None}


def _wait_conditions(raw: Any) -> list[dict[str, Any]]:
    """Translate the model's single-key ``for`` items into stored wait conditions."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("wait_for_invalid")
    conditions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or len(item) != 1:
            raise ValueError("wait_for_invalid")
        ((key, value),) = item.items()
        if key == "run":
            conditions.append({"kind": "owned_run", "run_id": value})
        elif key == "message":
            if value is not True:
                raise ValueError("wait_for_invalid")
            conditions.append({"kind": "conversation"})
        elif key == "seconds":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("invalid_wait_delay")
            conditions.append({"kind": "time_due", "after_seconds": value})
        elif key == "at":
            conditions.append({"kind": "time_due", "at": value})
        elif key == "plugin_event":
            if not isinstance(value, dict) or set(value) - {"plugin_id", "event_type", "source"}:
                raise ValueError("invalid_wait_plugin_filter")
            condition = {
                "kind": "plugin_event",
                "plugin_id": value.get("plugin_id"),
                "event_type": value.get("event_type"),
            }
            if "source" in value:
                condition["external_source"] = value["source"]
            conditions.append(condition)
        else:
            raise ValueError("wait_for_invalid")
    return conditions


class WorkInputsPreparing(RuntimeError):
    """Input is durable but its admitted attachment preparation is unfinished."""


def work_control_tools() -> tuple[ChatTool, ...]:
    from yuki.runtime.work_context_note import ContextNote

    note_schema = ContextNote.model_json_schema()
    note_definitions = note_schema.pop("$defs", {})
    # ``version`` is an internal storage field, never model input.
    note_schema["properties"].pop("version", None)
    note_schema.update(additionalProperties=False)
    note_schema.pop("title", None)
    string = {"type": "string"}
    timestamp = {"type": "string", "description": "带时区 ISO-8601 时间"}
    return (
        ChatTool(
            name="get_tasks",
            result_cacheable=False,
            description=(
                "查看任务和子任务。给 id 读取该任务的完整目标、状态、等待条件、最近步骤与结果；"
                "不给 id 列出任务，status 默认 active，parent 列出某任务的子任务。"
                "状态只说明生命周期，成果以步骤结果和交付回执为准。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 36},
                    "status": {"type": "string", "enum": ["active", "done", "all"]},
                    "parent": {"type": "string", "maxLength": 36},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "cursor": string,
                },
                "additionalProperties": False,
            },
        ),
        ChatTool(
            name="manage_task",
            result_cacheable=False,
            description=(
                "登记或推进需要跨轮持续的任务。start 登记新目标；update 修改目标或保存 note；"
                "finish 结束并可在 result 给出内部结果；fail、cancel 终止 id 指定的任务"
                "（默认当前任务，text 写原因）；"
                "resume 恢复 id 指定的任务，子任务可在 text 附新要求。"
            ),
            parameters={
                "type": "object",
                "$defs": note_definitions,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["start", "update", "finish", "fail", "cancel", "resume"],
                    },
                    "id": {"type": "string", "maxLength": 36},
                    "goal": string,
                    "note": {
                        **note_schema,
                        "description": (
                            "跨轮线索：facts、unresolved、next_steps 每项含 text 与 refs"
                            "（goal、input:ID、event:ID、effect:键、artifact:handle、child:ID）"
                        ),
                    },
                    "result": string,
                    "text": string,
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        ),
        ChatTool(
            name="wait",
            result_cacheable=False,
            description=(
                "暂停当前任务，直到 for 中的信号到达再继续：run 等子任务或命令结束，"
                "message 等当前会话的新消息，seconds 或 at 等到时间，plugin_event 等插件事件。"
                "默认任一信号即唤醒，all=true 等全部；deadline 为最晚唤醒时间。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "for": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {"run": {"type": "string", "minLength": 1}},
                                    "required": ["run"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {"message": {"const": True}},
                                    "required": ["message"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {"seconds": {"type": "number"}},
                                    "required": ["seconds"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {"at": timestamp},
                                    "required": ["at"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "plugin_event": {
                                            "type": "object",
                                            "properties": {
                                                "plugin_id": string,
                                                "event_type": string,
                                                "source": string,
                                            },
                                            "required": ["plugin_id", "event_type"],
                                            "additionalProperties": False,
                                        }
                                    },
                                    "required": ["plugin_event"],
                                    "additionalProperties": False,
                                },
                            ]
                        },
                    },
                    "all": {"type": "boolean"},
                    "deadline": timestamp,
                },
                "required": ["for"],
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
    requests_started: int = 0
    deferred_failure: Any = None
    # Set only by the original SELF source when its first scene/Presence
    # preparation failed before any activation; see work_supervisor.
    protocol_recovery_preparation: Any = None
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

    async def pending(self) -> list[dict[str, Any]]:
        if self.current is None:
            return []
        return await self.repository.pending(self.lease, work_id=self.current["id"])

    async def has_pending_business_inputs(self) -> bool:
        if self.current is None:
            return False
        return await self.repository.has_pending_business_inputs(self.lease, self.current["id"])

    async def has_owned_execution(self) -> bool:
        if self.current is None:
            return False
        return await self.repository.has_owned_execution(self.lease, self.current["id"])

    async def recover_failure(self, exc: BaseException) -> ActivationOutcome:
        from yuki.runtime.activation_outcome import WorkRecoveryDeferred
        from yuki.runtime.work_supervisor import recover_failure

        try:
            return await recover_failure(self, exc)
        except BaseException as secondary:
            self.recovery_deferred = True
            exc.add_note(f"recovery persistence failed: {type(secondary).__name__}")
            raise WorkRecoveryDeferred(type(exc).__name__) from exc

    async def background_state(self) -> str | None:
        """Project background work while no lifecycle decision has been accepted."""
        if self.current is None or self.lease.work_id:
            return None
        from yuki.runtime.subagent_repository import SubagentRepository

        unfinished = await SubagentRepository(self.repository).unfinished(self.current["id"])
        if any(r["state"] in {"queued", "running", "waiting_external"} for r in unfinished):
            return "waiting_external"
        if unfinished:
            return "suspended"
        return None

    @property
    def accepted(self) -> dict[str, Any] | None:
        """The committed lifecycle decision; WorkControl.ending only projects it."""
        if self.current is None:
            return None
        value = json.loads(self.current["checkpoint_json"]).get("accepted_control")
        return value if isinstance(value, dict) else None

    def accepted_ending(self, call_key: str | None = None) -> str | None:
        accepted = self.accepted
        if accepted is None or (call_key is not None and accepted.get("call_key") != call_key):
            return None
        return ACCEPTED_ENDINGS.get(str(accepted.get("action")))

    async def _accept(self, action: str, call_key: str, **payload: Any) -> None:
        assert self.current is not None
        self.current = await self.repository.accept_control(
            self.lease, self.current["id"], {"action": action, "call_key": call_key, **payload}
        )
        self.ending = ACCEPTED_ENDINGS[action]

    async def retire_fulfilled_wait(self) -> bool:
        accepted = self.accepted
        if accepted is None or "wait_id" not in accepted:
            return False
        from sqlalchemy import select

        from yuki.runtime.work_wait_schema import waits

        assert self.current is not None
        async with self.repository.database.read_session() as reader:
            status = await reader.scalar(
                select(waits.c.status).where(
                    waits.c.id == accepted.get("wait_id"),
                    waits.c.work_id == self.current["id"],
                    waits.c.conversation_id == self.lease.conversation_id,
                    waits.c.generation == self.lease.generation,
                )
            )
        if status in {"delivered", "expired"}:
            await self.retire_accepted()
            return True
        return False

    async def retire_accepted(self) -> None:
        """Legal new input or an explicit revision withdraws the accepted decision."""
        self.ending = None
        if self.accepted is not None:
            assert self.current is not None
            self.current = await self.repository.accept_control(
                self.lease, self.current["id"], None
            )

    async def complete_final(self, text: str, call_key: str) -> None:
        """An ordinary final records the same completion decision as complete(result)."""
        await self.validate()
        await self._accept("complete", call_key, result=text)

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

    async def reconcile_completed_children(self) -> None:
        if self.current is None:
            return
        # Run receipts resolve original effects; no result is copied from a child
        # checkpoint into a second authority list.
        from yuki.capabilities.executions import is_execution
        from yuki.capabilities.results import normalize_legacy_result
        from yuki.runtime.effect_outcomes import execution_evidence, recorded_tool

        evidence = await self.repository.effect_evidence(
            self.lease,
            self.current["id"],
            only_unresolved=True,
        )
        owners: dict[str, list[str]] = {}
        requests: dict[str, dict[str, str]] = {}
        for effect in evidence:
            # Effects keep their recorded name; read it through the history map.
            name = recorded_tool(effect.get("tool", ""))
            execution = is_execution(name, {"action": effect.get("action")})
            if name in {"use_coding_agent", "get_coding_agents"} and not execution:
                continue
            identity = effect.get("run_id")
            if isinstance(identity, str) and (effect.get("pending") or effect.get("uncertain")):
                owners.setdefault(effect["work_id"], []).append(identity)
            elif (
                (execution or name in {"environment_packages", "run_python"})
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

                    from yuki.conversation.canonical_db_models import (
                        CanonicalConversationModel,
                    )
                    from yuki.persistence.models import ChatEventModel

                    async with self.repository.database.read_session() as reader:
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
                    from yuki.domain.audio import transcript_context

                    bodies = {
                        event.id: (
                            f"{event.content}\n{transcript_context(event.audio_transcript)}"
                            if event.audio_transcript
                            else event.content
                        )
                        for event in events
                    }
                    signal["events"] = [
                        {"event_id": identity, "text": bodies.get(identity, "原事件正文不可读")}
                        for identity in ids
                    ]
                    text = json.dumps(signal, ensure_ascii=False)
                    if self.session is not None:
                        self.session.event_ids.extend(
                            identity for identity in ids if identity in bodies
                        )

            # A signal carries its own wait/conditions; a shown anchor is no
            # reason to drop it. Only an ordinary message is deduplicated.
            signal_like = item["kind"] == "completion" or bool(payload.get("signal"))
            already_visible = (
                not signal_like
                and item["event_id"] is not None
                and (
                    item["event_id"] in observed_event_ids
                    or (
                        self.session is not None
                        and item["event_id"] in self.session.public_event_ids
                    )
                )
            )
            content = (
                f"[Work 信号 event_id={item['event_id']}；续原任务]\n{text}"
                if signal_like
                else f"[Work input_id={item['id']} event_id={item['event_id']} 已在当前聊天展示；"
                "关联原输入并按需回答，不重复原文]"
                if already_visible
                else (
                    f"[新增输入 event_id={item['event_id']}；"
                    "结合原目标吸收补充并继续处理，按实际需要答复或更新进展]\n"
                    f"{text}"
                )
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
            assert self.current is not None
            business_inputs = await self.repository.has_pending_business_inputs(
                self.lease, self.current["id"], input_ids=tuple(selected)
            )
            await self.reconcile_completed_children()
            await self.repository.stage(self.lease, selected, attempt)
            self.staged_attempt = attempt
            if business_inputs:
                await self.retire_accepted()
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

    async def remaining_tools(self) -> int | None:
        """Calls left under an explicit root tool limit; None when unlimited.

        A pre-dispatch view for refusing batch overflow cleanly; T2 still
        charges the root budget atomically.
        """
        if self.current is None:
            return None
        from sqlalchemy import select

        from yuki.runtime.work_budget_schema import budgets
        from yuki.runtime.work_tree import budget_root_id

        async with self.repository.database.read_session() as reader:
            root = await budget_root_id(reader, self.current["id"])
            row = (
                await reader.execute(
                    select(budgets.c.tools, budgets.c.tool_limit).where(budgets.c.root_id == root)
                )
            ).first()
        if row is None or row.tool_limit is None:
            return None
        return max(0, int(row.tool_limit) - int(row.tools))

    async def reserve_request(self, *, auxiliary: bool = False) -> None:
        if self.current is not None:
            await self.repository.checkpoint(self.lease, self.current["id"], None, models=1)
            self.current["model_requests"] += 1
        self.requests_started += 1
        if self.session is not None and not auxiliary:
            self.session.sequence += 1

    def observe_evidence(self, evidence: dict[str, Any]) -> None:
        """Bounded view of already accepted typed facts, never a result-text decoder."""
        from yuki.runtime.effect_outcomes import recorded_tool

        if recorded_tool(str(evidence.get("tool", ""))) in WORK_CONTROL_NAMES or not evidence.get(
            "executed"
        ):
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

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        call_key: str,
        *,
        invocation: Invocation | None = None,
    ) -> str:
        from yuki.capabilities.results import ToolExecutionResult
        from yuki.runtime.effect_outcomes import current_result_capture

        try:
            await self.validate()
            if not await self.repository.valid(self.lease):
                raise WorkConflict("work_activation_obsolete")
            if name == "get_tasks":
                result = await self._get_tasks(args)
            elif name == "manage_task":
                result = await self._manage(args, call_key, invocation=invocation)
            elif name == "wait":
                result = await self._wait(args, call_key)
            elif name == "start_subtask":
                from yuki.runtime.subagent_tools import start_subtask

                result = await start_subtask(self, args, call_key)
            elif name == "message_task":
                from yuki.runtime.subagent_tools import message_task

                result = await message_task(self, args, call_key)
            else:
                raise ValueError("unknown_work_control")
            payload = control_result_payload(result)
            # The receipt evidence reads ``status`` as an execution status; a
            # lifecycle state is not one. A started subtask stays pending as before.
            typed = {"state" if key == "status" else key: value for key, value in result.items()}
            if name == "start_subtask":
                typed["pending"] = True
            outcome = ToolExecutionResult(
                ok=True,
                data=typed,
                view=payload,
                tool_name=name,
                provider_id="work_control",
            )
        except Exception as exc:
            from yuki.persistence.database import (
                write_failure_committed,
                write_failure_phase,
                write_failure_retryable,
            )

            phase, committed = write_failure_phase(exc), write_failure_committed(exc)
            if phase is not None and committed is not False:
                outcome = ToolExecutionResult(
                    ok=False,
                    error_code="database_failure",
                    uncertain=committed is None,
                    mutation_committed=committed,
                    retryable=write_failure_retryable(exc),
                    metadata={"database_write_phase": phase},
                    tool_name=name,
                    provider_id="work_control",
                )
                payload = outcome.model_payload()
            elif isinstance(exc, (ValueError, WorkConflict)):
                from pydantic import ValidationError

                code = "invalid_note" if isinstance(exc, ValidationError) else str(exc)
                outcome = ToolExecutionResult(
                    ok=False,
                    error_code=code,
                    public_message=control_failure_message(code),
                    tool_name=name,
                    provider_id="work_control",
                )
                payload = outcome.model_payload()
            else:
                raise
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
        from yuki.runtime.work_context_note import validate_note

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

    async def _get_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        from yuki.runtime.work_queries import WorkQueries

        queries = WorkQueries(self.repository)
        limit = args.get("limit", 8)
        cursor = args.get("cursor")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
            or (cursor is not None and (not isinstance(cursor, str) or not cursor))
        ):
            raise ValueError("invalid_task_query")
        identity = args.get("id")
        if identity is not None:
            if not isinstance(identity, str) or not identity.strip() or len(identity) > 36:
                raise ValueError("task_id_required")
            row = await queries.get(
                self.lease,
                self.source,
                identity,
                include_progress=True,
                limit=limit,
                cursor=cursor,
            )
            if row is None:
                raise ValueError("work_not_found_or_not_authorized")
            view = task_view(row)
            if row["parent_work_id"] is not None:
                from yuki.runtime.subagent_tools import subtask_result

                result = await subtask_result(self, identity)
                if result is not None:
                    view["result"] = result
            return view
        status = {"active": "active", "done": "terminal", "all": "all"}.get(
            args.get("status", "active")
        )
        parent = args.get("parent")
        if status is None or (parent is not None and not isinstance(parent, str)):
            raise ValueError("invalid_task_query")
        page = await queries.list(
            self.lease,
            self.source,
            limit=limit,
            status=status,
            cursor=cursor,
            parent_work_id=parent,
        )
        return {
            "tasks": [task_view(row) for row in page["works"]],
            "next_cursor": page["next_cursor"],
        }

    async def _manage(
        self, args: dict[str, Any], call_key: str, *, invocation: Invocation | None = None
    ) -> dict[str, Any]:
        action = args.get("action")
        if action not in {"start", "update", "finish", "fail", "cancel", "resume"}:
            raise ValueError("task_action_invalid")
        target = args.get("id")
        if target is not None and (not isinstance(target, str) or not target):
            raise ValueError("task_id_required")
        text = args.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError("invalid_task_text")
        if (
            action in {"cancel", "resume"}
            and target is not None
            and (self.current is None or target != self.current["id"])
        ):
            from yuki.runtime.work_queries import WorkQueries

            candidate = await WorkQueries(self.repository).get(
                self.lease, self.source, target, local=True
            )
            if candidate is not None and await self._is_subtask(target):
                # A subtask is a child Work: cancel it, or resume it with new requirements.
                from yuki.runtime.subagent_tools import manage_subtask

                return await manage_subtask(self, action, target, call_key, text)
        if action in {"cancel", "fail", "resume"} and target is not None:
            if (
                action in {"cancel", "fail"}
                and (self.current is None or target != self.current["id"])
            ) or (action == "resume" and self.current is not None and target != self.current["id"]):
                from yuki.runtime.work_management import WorkManagementError, manage_work
                from yuki.runtime.work_queries import WorkQueries

                candidate = await WorkQueries(self.repository).get(
                    self.lease, self.source, target, local=True
                )
                if candidate is None:
                    raise ValueError("work_not_found_or_not_authorized")
                try:
                    async with self.repository.database.immediate_session() as writer:
                        await self.repository._assert_lease(writer, self.lease)
                        _revision, state = await manage_work(
                            writer,
                            target,
                            candidate["revision"],
                            action,
                            reason=text,
                        )
                except WorkManagementError as exc:
                    raise ValueError(exc.code) from exc
                return {"id": target, "status": state}
        if self.lease.work_id and action == "start":
            raise ValueError("worker_already_registered")
        if action == "resume":
            if self.current is not None or self.lease.work_id:
                raise ValueError("resume_requires_neutral_foreground")
            from yuki.runtime.work_queries import WorkQueries

            candidate = (
                await WorkQueries(self.repository).get(self.lease, self.source, target, local=True)
                if isinstance(target, str)
                else None
            )
            if candidate is None or candidate["state"] in {"completed", "cancelled"}:
                raise ValueError("resume_work_not_authorized")
            await self.repository.enqueue(
                self.lease.conversation_id,
                self.lease.generation,
                self.source_key,
                kind="message",
                event_id=self.source.get("trigger_event_id"),
                work_id=target,
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
            self.handoff_work_id = target
            # The original execution chain continues it after this round.
            return {"id": target, "status": "queued"}
        if action == "start":
            if self.current is not None:
                return await self._queue_work(args)
            self.current = await self._admit_goal(args.get("goal"), call_key, invocation)
        elif self.current is None:
            raise ValueError("no_active_work")
        elif action == "update":
            goal = args.get("goal")
            note_plan = None
            if "note" in args:
                from yuki.runtime.work_context_note import validate_note

                note_plan = await validate_note(self, args["note"])
            if goal is not None:
                if not isinstance(goal, str) or not goal.strip():
                    raise ValueError("work_goal_required")
                self.current = await self.repository.transition(
                    self.lease,
                    self.current["id"],
                    self.current["revision"],
                    "running",
                    goal=goal,
                )
                await self.retire_accepted()
            if "note" in args:
                await self.update_context_note(args["note"], call_key, note_plan)
        elif action in {"fail", "cancel"}:
            await self._accept(action, call_key, reason=text)
        else:
            result = args.get("result")
            if result is not None and not isinstance(result, str):
                raise ValueError("work_result_invalid")
            await self._accept("complete", call_key, result=result or "")
        return {
            "id": self.current["id"],
            "status": self.current["state"],
            "ending": self.ending,
        }

    async def _is_subtask(self, identity: str) -> bool:
        from sqlalchemy import select

        from yuki.runtime.subagent_schema import children

        async with self.repository.database.read_session() as reader:
            found = await reader.scalar(
                select(children.c.work_id).where(children.c.work_id == identity)
            )
        return found is not None

    async def _wait(self, args: dict[str, Any], call_key: str) -> dict[str, Any]:
        from yuki.runtime.work_wait import WorkWaitRepository, normalize_conditions

        if self.current is None:
            raise ValueError("no_active_work")
        every = args.get("all", False)
        deadline = args.get("deadline")
        if type(every) is not bool or (deadline is not None and not isinstance(deadline, str)):
            raise ValueError("wait_for_invalid")
        conditions = _wait_conditions(args.get("for"))
        normalized = normalize_conditions(conditions, time.time())
        for condition in normalized:
            if condition["kind"] != "owned_run":
                continue
            identity = condition["run_id"]
            owned = (
                self.resolve_child is not None and await self.resolve_child(identity) is not None
            )
            if not owned:
                from sqlalchemy import func, or_, select

                from yuki.runtime.work_schema_v1 import work
                from yuki.runtime.work_tree import descendants
                from yuki.sandbox.db_models import SandboxTaskRunModel

                owned_work = descendants(self.current["id"])
                async with self.repository.database.read_session() as reader:
                    owned = bool(
                        await reader.scalar(
                            select(
                                or_(
                                    select(work.c.id)
                                    .where(
                                        work.c.id == identity,
                                        work.c.id.in_(owned_work),
                                    )
                                    .exists(),
                                    select(SandboxTaskRunModel.request_id)
                                    .where(
                                        SandboxTaskRunModel.run_id == identity,
                                        or_(
                                            func.json_extract(
                                                SandboxTaskRunModel.source_json, "$.work_id"
                                            )
                                            == self.current["id"],
                                            func.json_extract(
                                                SandboxTaskRunModel.source_json, "$.work_id"
                                            ).in_(owned_work),
                                        ),
                                    )
                                    .exists(),
                                )
                            )
                        )
                    )
            if not owned:
                raise ValueError("waiting_requires_owned_execution")
        # A root task waiting only for the next message is waiting for its user:
        # entries that end a turn on a question (ACP, Client) read waiting_user.
        action = (
            "need_input"
            if conditions == [{"kind": "conversation"}] and self.current["parent_work_id"] is None
            else "wait"
        )
        await WorkWaitRepository(self.repository).register(
            self.lease,
            work_id=self.current["id"],
            source=self.source,
            call_key=f"wait:{self.current['id']}:{hashlib.sha256(call_key.encode()).hexdigest()}",
            mode="all" if every else "any",
            conditions=conditions,
            deadline_at=deadline,
            accepted={"action": action, "call_key": call_key},
        )
        self.current = await self.repository.get(self.current["id"]) or self.current
        self.ending = ACCEPTED_ENDINGS[action]
        return {"id": self.current["id"], "status": "waiting"}

    async def admit_goal(self, goal: str) -> None:
        """Host admission of an invocation's own goal, without a model control call.

        Same writer, budget charge and checks as a model ``accept``; no result
        text is produced and no effect receipt is written.
        """
        await self.validate()
        if not await self.repository.valid(self.lease):
            raise WorkConflict("work_activation_obsolete")
        self.current = await self._admit_goal(goal, "", None)

    async def _admit_goal(
        self, goal: Any, call_key: str, invocation: Invocation | None
    ) -> dict[str, Any]:
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("work_goal_required")
        from sqlalchemy.dialects.sqlite import insert

        from yuki.capabilities.results import ToolExecutionResult
        from yuki.runtime.effect_outcomes import effect_evidence
        from yuki.runtime.work_schema_v1 import effects

        metadata = invocation.durable_metadata() if invocation is not None else {}
        source_json = encode_json(self.source)
        async with self.repository.database.immediate_session() as writer:
            admitted = await self._admit_in_session(writer, goal, source_json)
            if invocation is not None:
                admission_result = {
                    "id": admitted["id"],
                    "status": admitted["state"],
                    "ending": self.ending,
                }
                outcome = ToolExecutionResult(
                    ok=True,
                    data={"id": admitted["id"], "state": admitted["state"]},
                    tool_name="manage_task",
                    provider_id="work_control",
                )
                await writer.execute(
                    insert(effects).values(
                        effect_key=call_key,
                        work_id=admitted["id"],
                        kind="tool",
                        state="accepted",
                        receipt_json=json.dumps(
                            {
                                # The same projection the live control call returns.
                                "result": json.dumps(
                                    control_result_payload(admission_result),
                                    ensure_ascii=False,
                                ),
                                "outcome": effect_evidence(
                                    outcome,
                                    tool="manage_task",
                                    side_effecting=True,
                                    arguments="{}",
                                ),
                                "invocation": {
                                    **metadata,
                                    "owner_execution_id": admitted["id"],
                                    "dispatch_started": True,
                                    "budget_admitted": False,
                                    "revision": 1,
                                },
                            },
                            ensure_ascii=False,
                        ),
                        created=admitted["created"],
                        updated=admitted["updated"],
                    )
                )
        return admitted

    async def _admit_in_session(
        self, writer: AsyncSession, goal: str, source_json: str, *, identity: str | None = None
    ) -> dict[str, Any]:
        """First admission of this source: paid requests and explicit root limits."""
        from sqlalchemy import update

        from yuki.runtime.work_budget import charge, limit
        from yuki.runtime.work_schema_v1 import work

        identity = identity or str(uuid4())
        admitted = await self.repository.accept_in_session(
            writer,
            self.lease,
            source_key=self.source_key,
            source=self.source,
            prepared_source_json=source_json,
            goal=goal,
            identity=identity,
        )
        if self.requests_started and admitted["model_requests"] == 0:
            await charge(writer, admitted["id"], models=self.requests_started, tools=0)
            await writer.execute(
                update(work)
                .where(work.c.id == admitted["id"])
                .values(model_requests=self.requests_started)
            )
            admitted["model_requests"] = self.requests_started
        models, tools = self.source.get("model_limit"), self.source.get("tool_limit")
        if admitted["id"] == identity and (models is not None or tools is not None):
            # An explicit caller limit is a total for this root and its workers.
            await limit(writer, identity, models=models, tools=tools)
        return admitted

    async def admit_execution(self, calls: tuple[ToolCall, ...]) -> None:
        """Host admission before a private execution's first tool batch.

        The Work, the requests already paid by this activation and the first
        ``phase=response`` journal commit in one writer. The goal references the
        original trigger event instead of copying the user's text.
        """
        assert self.current is None and self.session is not None
        await self.validate()
        identity = str(uuid4())
        goal = f"完成本人私聊请求 event:{self.source.get('trigger_event_id')}"
        source_json = encode_json(self.source)
        admitted: dict[str, Any] = {}

        async def admit(writer: AsyncSession) -> None:
            row = await self._admit_in_session(writer, goal, source_json, identity=identity)
            if row["id"] != identity:
                raise WorkConflict("work_admission_source_conflict")
            admitted.update(row)

        await self.session.save("response", calls, work_id=identity, publication=admit)
        self.current = admitted

    async def _queue_work(self, args: dict[str, Any]) -> dict[str, Any]:
        from sqlalchemy import select

        from yuki.persistence.models import ChatEventModel

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
        async with self.repository.database.read_session() as session:
            event = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.id == event_id,
                    ChatEventModel.canonical_conversation_id == self.lease.conversation_id,
                    ChatEventModel.direction == "inbound",
                    ChatEventModel.event_kind == "message",
                    same_person_condition(
                        ChatEventModel.author_person_id, self.source.get("actor_person_id")
                    ),
                )
            )
        if event is None:
            raise ValueError("independent_work_source_invalid")
        goal = args.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("work_goal_required")
        source = {
            **self.source,
            "actor_person_id": event.author_person_id,
            "actor_user_id": event.sender_user_id,
            "trigger_event_id": event.id,
            "bot_user_id": event.bot_user_id,
            "presence_id": event.ingress_presence_id,
        }
        queued = await self.repository.accept(
            self.lease,
            source_key=f"event:{self.lease.conversation_id}:{event.id}",
            source=source,
            goal=goal,
            handoff_from=self.current["id"],
        )
        self.handoff_work_id = queued["id"]
        return {
            "id": queued["id"],
            "status": queued["state"],
            "current_task": self.current["id"],
            "note": "新任务从它自己的原始请求开始执行和交付；当前任务不要代做或代发。",
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
            return state

        from yuki.runtime.work_queries import WorkQueries

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
        from yuki.runtime.work_queries import WorkQueries

        return await WorkQueries(self.repository).available(self.lease, self.source)

    async def settle(self, *, pending_inputs: bool) -> None:
        from yuki.runtime.work_supervisor import settle

        await settle(self, pending_inputs=pending_inputs)
