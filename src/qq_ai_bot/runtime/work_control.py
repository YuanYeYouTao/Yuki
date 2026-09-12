"""Provider-neutral lifecycle controls; host callbacks retain delivery authority."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ChatTool
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_session import WorkSession


WORK_CONTROL_NAMES = frozenset({"task_control", "report_progress"})


class WorkInputsPreparing(RuntimeError):
    """Input is durable but its admitted attachment preparation is unfinished."""


def work_control_tools() -> tuple[ChatTool, ...]:
    return (
        ChatTool(
            name="task_control",
            result_cacheable=False,
            description=(
                "管理当前持续工作。明确工作请求先 accept 登记目标，再执行；"
                "普通聊天用 answer 和 text 直接回复，不建立长期工作。"
                "新输入另提独立工作时再次 accept 排队，不能用 update 覆盖旧目标；"
                "update 仅修正当前目标；wait 必须有真实待完成 run_id；"
                "need_input 必须说明缺失信息；complete 仅提出结束，后端核对交付后提交。"
                "不能把口头承诺当作开始或完成，不能在同批混合此工具与其他副作用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "answer",
                            "accept",
                            "update",
                            "wait",
                            "need_input",
                            "complete",
                            "fail",
                        ],
                    },
                    "goal": {"type": "string", "maxLength": 8192},
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
                    "text": {"type": "string", "maxLength": 8000},
                    "reason": {"type": "string", "maxLength": 1000},
                    "run_id": {"type": "string", "maxLength": 36},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        ),
        ChatTool(
            name="report_progress",
            result_cacheable=False,
            description=(
                "在当前获授权的会话发送简短过程说明，之后继续工作。"
                "只有确实启动了执行才说正在做；只登记可说已接下。"
                "返回真实投递回执，不要在最终回复重复这段已发送内容。"
                "没有当前会话发送授权的生成入口不可使用。"
            ),
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 1000}},
                "required": ["text"],
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
    deliver_progress: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None
    resolve_child: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None
    session: WorkSession | None = None
    current_message: ChatMessage | None = None
    current: dict[str, Any] | None = None
    ending: str | None = None
    known_effects: list[dict[str, Any]] = field(default_factory=list)
    corrections: int = 0
    progress_count: int = 0
    final_delivery: bool = False
    chat_answer: str | None = None
    requests_started: int = 0
    staged_attempt: str | None = None
    input_images: dict[int, tuple[ChatImage, ...]] = field(default_factory=dict)

    metered_at: float = field(default_factory=time.monotonic)

    async def meter_active_time(self) -> None:
        now = time.monotonic()
        elapsed, self.metered_at = max(0, now - self.metered_at), now
        if self.current is not None and elapsed:
            await self.repository.checkpoint(
                self.lease, self.current["id"], None, active_seconds=elapsed
            )
            self.current["active_seconds"] += elapsed

    async def pending(self) -> list[dict[str, Any]]:
        if self.current is None:
            return []
        return await self.repository.pending(self.lease, work_id=self.current["id"])

    async def take_inputs(self, attempt: str) -> tuple[ChatMessage, ...]:
        pending = await self.pending()
        deadline = time.monotonic() + 15
        while pending and not pending[0]["ready"]:
            if time.monotonic() >= deadline:
                raise WorkInputsPreparing("work_input_preparing")
            await asyncio.sleep(0.2)
            pending = await self.pending()
        selected: list[int] = []
        messages = []
        size = 0
        for item in pending:
            if not item["ready"]:
                break
            payload = json.loads(item["payload_json"])
            text = str(payload.get("text", ""))
            content = (
                f"[新增输入 event_id={item['event_id']}；保持原任务，按内容补充或回答]\n{text}"
            )
            if size + len(content.encode()) > 8192 and selected:
                break
            remaining = 8192 - size
            if len(content.encode("utf-8")) > remaining:
                marker = "\n[本轮输入已截断；完整内容按 event_id 查询]"
                content = (
                    content.encode("utf-8")[: remaining - len(marker.encode("utf-8"))].decode(
                        "utf-8", errors="ignore"
                    )
                    + marker
                )
            size += len(content.encode())
            selected.append(item["id"])
            if self.session is not None:
                self.session.input_ids.append(item["id"])
                if item["event_id"] is not None:
                    self.session.event_ids.append(item["event_id"])
            messages.append(
                ChatMessage(
                    role="user", content=content, images=self.input_images.pop(item["id"], ())
                )
            )
        if selected:
            await self.repository.stage(self.lease, selected, attempt)
            self.staged_attempt = attempt
            self.ending = None
            self.chat_answer = None
            self.corrections = 0
        return tuple(messages)

    async def confirm_inputs(self) -> None:
        if self.staged_attempt is not None:
            await self.repository.consume(self.lease, self.staged_attempt)
            self.staged_attempt = None

    async def reserve_request(self) -> None:
        self.requests_started += 1
        if self.session is not None:
            self.session.sequence += 1
        if self.current is not None:
            await self.repository.checkpoint(self.lease, self.current["id"], None, models=1)
            self.current["model_requests"] += 1

    async def charge_tools(self, count: int) -> None:
        if self.current is not None and count:
            await self.repository.checkpoint(self.lease, self.current["id"], None, tools=count)
            self.current["tool_calls"] += count

    def observe_result(
        self,
        name: str,
        result: str,
        executed: bool,
        *,
        side_effecting: bool = True,
        arguments: str = "{}",
    ) -> None:
        if name in WORK_CONTROL_NAMES or not executed:
            return
        try:
            value = json.loads(result)
        except (ValueError, TypeError):
            return
        if not isinstance(value, dict):
            return
        body = value.get("progress", value.get("data", value.get("result", value)))
        if not isinstance(body, dict):
            return
        identity = body.get("run_id")
        artifacts: list[str] = []

        def collect(item: Any, depth: int = 0) -> None:
            if depth > 5 or len(artifacts) >= 32:
                return
            if isinstance(item, dict):
                if isinstance(item.get("artifact_id"), str):
                    artifacts.append(item["artifact_id"])
                for child in item.values():
                    collect(child, depth + 1)
            elif isinstance(item, list):
                for child in item[:32]:
                    collect(child, depth + 1)

        collect(body)
        delivered: list[str] = []
        try:
            args = json.loads(arguments)
        except ValueError:
            args = {}
        if (
            name in {"send_private_message", "send_group_message"}
            and isinstance(args, dict)
            and isinstance(args.get("artifact_id"), str)
            and body.get("status") == "succeeded"
        ):
            delivered.append(args["artifact_id"])
            artifacts.append(args["artifact_id"])
        entry = {
            "tool": name,
            "side_effecting": side_effecting,
            "artifacts": artifacts,
            "delivered_artifacts": delivered,
            "run_id": identity,
            "ok": bool(value.get("ok", not value.get("error")))
            and not body.get("error")
            and body.get("status") not in {"failed", "cancelled", "uncertain", "unknown"}
            and body.get("exit_code") in (None, 0),
            "pending": bool(body.get("pending"))
            or body.get("status") in {"running", "queued", "waiting"},
            "uncertain": bool(body.get("uncertain"))
            or body.get("status") in {"uncertain", "unknown"},
        }
        if identity:
            self.known_effects[:] = [
                item for item in self.known_effects if item.get("run_id") != identity
            ]
        self.known_effects.append(entry)
        self.known_effects[:] = self.known_effects[-64:]

    async def execute(self, name: str, args: dict[str, Any], call_key: str) -> str:
        try:
            await self.validate()
            if not await self.repository.valid(self.lease):
                raise WorkConflict("work_activation_obsolete")
            if name == "report_progress":
                result = await self._progress(args, call_key)
            elif name == "task_control":
                result = await self._control(args, call_key)
            else:
                raise ValueError("unknown_work_control")
            return json.dumps({"ok": True, **result}, ensure_ascii=False)
        except (ValueError, WorkConflict) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    async def _control(self, args: dict[str, Any], call_key: str) -> dict[str, Any]:
        action = args.get("action")
        if not isinstance(action, str):
            raise ValueError("work_action_required")
        if action == "answer":
            text = args.get("text")
            if self.current is not None:
                return await self._progress({"text": text}, call_key)
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
                raise ValueError("chat_answer_required")
            self.chat_answer = text
            return {"chat_answer_prepared": True}
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
        elif action == "update":
            goal = args.get("goal")
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
        elif action == "wait":
            identity = args.get("run_id")
            child = (
                await self.resolve_child(identity)
                if isinstance(identity, str) and self.resolve_child
                else None
            )
            if not child or not child.get("pending"):
                raise ValueError("waiting_requires_owned_pending_execution")
            await self.repository.checkpoint(
                self.lease, self.current["id"], {"pending_run_id": identity}
            )
            self.ending = "waiting_external"
        elif action in {"need_input", "fail"}:
            reason = args.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                raise ValueError("work_reason_required")
            await self.repository.checkpoint(self.lease, self.current["id"], {"reason": reason})
            self.ending = "waiting_user" if action == "need_input" else "failed"
        elif action == "complete":
            if any(
                effect.get("pending") or effect.get("uncertain") for effect in self.known_effects
            ):
                raise ValueError("work_has_unresolved_execution")
            kind = self.current["output_kind"]
            if kind == "artifact":
                selected = args.get("artifact_ids")
                known = {
                    identity
                    for effect in self.known_effects
                    if effect.get("ok")
                    for identity in effect.get("artifacts", [])
                }
                if (
                    not isinstance(selected, list)
                    or not 1 <= len(selected) <= 8
                    or any(
                        not isinstance(identity, str) or identity not in known
                        for identity in selected
                    )
                ):
                    raise ValueError("work_completion_requires_verified_artifacts")
                delivered = {
                    identity
                    for effect in self.known_effects
                    if effect.get("ok")
                    for identity in effect.get("delivered_artifacts", [])
                }
                if self.current["deliver_artifacts"] and not set(selected) <= delivered:
                    raise ValueError("work_completion_requires_artifact_delivery_receipt")
            elif kind == "state_change" and not any(
                effect.get("ok") and effect.get("side_effecting", True)
                for effect in self.known_effects
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
            "trigger_id": event.platform_message_id,
            "presence_id": event.ingress_presence_id,
        }
        queued = await self.repository.accept(
            self.lease,
            source_key=f"message:{self.lease.conversation_id}:{event.platform_message_id}",
            source=source,
            goal=goal,
            output_kind=kind,
            deliver_artifacts=args.get("deliver_artifacts") is not False,
        )
        if queued["state"] == "running":
            queued = await self.repository.transition(
                self.lease, queued["id"], queued["revision"], "queued"
            )
        return {
            "queued_work_id": queued["id"],
            "state": queued["state"],
            "current_work_id": self.current["id"],
            "instruction": "新工作已排队。当前目标不变；当前后台执行未结束时用 wait 让出执行位置。",
        }

    async def _progress(self, args: dict[str, Any], call_key: str) -> dict[str, Any]:
        if self.deliver_progress is None:
            raise ValueError("progress_delivery_not_authorized")
        if self.current is None:
            raise ValueError("accept_work_before_progress")
        text = args.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
            raise ValueError("invalid_progress_text")
        if self.progress_count >= 4 or self.current["sent_messages"] >= 16:
            raise ValueError("progress_message_budget_exhausted")
        if not await self.repository.prepare_effect(
            self.lease, self.current["id"], call_key, "progress"
        ):
            return {"already_recorded": True, "replay_forbidden": True}
        await self.repository.checkpoint(self.lease, self.current["id"], None, messages=1)
        self.current["sent_messages"] += 1
        try:
            outcome = await self.deliver_progress(text, call_key)
        except BaseException:
            await self.repository.record_effect(
                call_key, "unknown", {"error": "delivery_outcome_unknown"}
            )
            raise
        accepted = bool(outcome.get("transport_accepted"))
        state = "accepted" if accepted else "unknown" if outcome.get("uncertain") else "failed"
        await self.repository.record_effect(call_key, state, outcome)
        if accepted:
            self.progress_count += 1
        return {"delivered": accepted, "receipt": outcome, "continue_work": True}

    async def settle(self, *, delivered: bool, pending_inputs: bool) -> None:
        """Host calls only after actual final delivery, never on a model claim."""
        if self.current is None or self.ending is None:
            return
        if pending_inputs:
            self.ending = None
            return
        state = self.ending if delivered else "suspended"
        self.current = await self.repository.transition(
            self.lease,
            self.current["id"],
            self.current["revision"],
            state,
            reason=None if delivered else "final_delivery_incomplete",
        )
