"""Provider-neutral lifecycle controls; host callbacks retain delivery authority."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qq_ai_bot.domain.messages import ChatImage, ChatMessage, ChatTool
from qq_ai_bot.runtime.activation_outcome import ActivationOutcome
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkLease, WorkRepository

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_session import WorkSession


WORK_CONTROL_NAMES = frozenset(
    {"task_control", "subagent_start", "subagent_control", "subagent_message"}
)


class WorkInputsPreparing(RuntimeError):
    """Input is durable but its admitted attachment preparation is unfinished."""


def work_control_tools() -> tuple[ChatTool, ...]:
    return (
        ChatTool(
            name="task_control",
            result_cacheable=False,
            description=(
                "管理当前持续工作。没有 work_id 而需要执行操作时，第一步单独调用 "
                "action=accept，并填写 goal、output_kind；成功后下一步才调用执行工具。"
                "已有 work_id 的同一工作直接继续，不重复 accept；不要先试执行再补登记。"
                "新一轮要续接 available_work 中的原目标，单独使用 resume 和 work_id；不重复登记。"
                "普通聊天不必登记；需要发言用 send_message。"
                "新输入另提独立工作时再次 accept 排队，不能用 update 覆盖旧目标；"
                "update 仅修正当前目标；wait 必须有真实待完成 run_id；"
                "need_input 必须说明缺失信息；complete 提出结束，后端核对未决执行和 artifact。"
                "不能把口头承诺当作开始或完成，不能在同批混合此工具与其他副作用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "accept",
                            "resume",
                            "update",
                            "wait",
                            "need_input",
                            "complete",
                            "fail",
                        ],
                    },
                    "goal": {"type": "string", "maxLength": 8192},
                    "work_id": {"type": "string", "maxLength": 36},
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
    corrections: int = 0
    final_delivery: bool = False
    requests_started: int = 0
    segment_model_limit: int = 24
    tools_started: int = 0
    yield_segment: bool = False
    handoff_work_id: str | None = None
    completion_delivered: bool = False
    settled: bool = False
    recovery_deferred: bool = False
    outcome: ActivationOutcome | None = None
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

        children = await SubagentRepository(self.repository).list(self.current["id"])
        unfinished = [r for r in children if r["state"] not in {"completed", "failed", "cancelled"}]
        if any(r["state"] in {"queued", "running", "waiting_external"} for r in unfinished):
            return "waiting_external"
        if unfinished:
            return "suspended"
        return None

    async def reconcile_completed_children(self) -> None:
        if self.current is None:
            return
        if not self.lease.work_id:
            from qq_ai_bot.runtime.subagent_repository import SubagentRepository

            for child in await SubagentRepository(self.repository).list(self.current["id"]):
                if child["state"] != "completed":
                    continue
                evidence = (
                    json.loads(child["result_json"])
                    .get("checkpoint", {})
                    .get("execution_evidence", [])
                )
                self.known_effects[:] = [
                    e for e in self.known_effects if e.get("child_id") != child["work_id"]
                ]
                self.known_effects.append(
                    {
                        "child_id": child["work_id"],
                        "ok": True,
                        "tool": "subagent_result",
                        "artifacts": list(
                            dict.fromkeys(
                                a for e in evidence if e.get("ok") for a in e.get("artifacts", [])
                            )
                        ),
                        "side_effecting": any(
                            e.get("ok") and e.get("side_effecting") for e in evidence
                        ),
                    }
                )
        run_ids = list(
            dict.fromkeys(
                effect["run_id"]
                for effect in self.known_effects
                if isinstance(effect.get("run_id"), str)
                and (effect.get("pending") or effect.get("uncertain"))
            )
        )
        for result in await self.repository.completed_children(
            self.lease, self.current["id"], run_ids
        ):
            self.observe_result(
                "sandbox_completion",
                json.dumps({"ok": True, "data": result}),
                True,
                side_effecting=False,
            )

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
            await self.reconcile_completed_children()
            await self.repository.stage(self.lease, selected, attempt)
            self.staged_attempt = attempt
            self.ending = None
            self.completion_delivered = False
            self.corrections = 0
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

    async def charge_tools(self, count: int) -> None:
        if self.current is not None and count:
            await self.repository.checkpoint(self.lease, self.current["id"], None, tools=count)
            self.current["tool_calls"] += count
        self.tools_started += count

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
        caption_delivered = False
        delivered_message = False
        try:
            args = json.loads(arguments)
        except ValueError:
            args = {}
        if (
            name in {"send_message", "send_private_message", "send_group_message"}
            and isinstance(args, dict)
            and isinstance(args.get("artifact_id"), str)
        ):
            file_receipt = body.get("file", body)
            if isinstance(file_receipt, dict) and file_receipt.get("status") == "succeeded":
                delivered.append(args["artifact_id"])
                artifacts.append(args["artifact_id"])
                caption = body.get("caption")
                caption_delivered = bool(
                    isinstance(args.get("text"), str)
                    and args["text"].strip()
                    and (
                        (isinstance(caption, dict) and caption.get("status") == "succeeded")
                        or (
                            args.get("attachment_kind") == "image"
                            and body.get("status") == "succeeded"
                        )
                    )
                )
        if name == "send_message" and isinstance(args, dict):
            delivered_message = bool(
                isinstance(args.get("text"), str)
                and args["text"].strip()
                and (
                    caption_delivered
                    if args.get("attachment_kind") == "file"
                    else body.get("status") == "succeeded"
                )
            )
        entry = {
            "tool": name,
            "side_effecting": side_effecting,
            "artifacts": artifacts,
            "delivered_artifacts": delivered,
            "caption_delivered": caption_delivered,
            "delivered_message": delivered_message,
            "delivery_target": body.get("target") if delivered or delivered_message else None,
            "run_id": identity,
            "ok": bool(value.get("ok", not value.get("error")))
            and not body.get("error")
            and body.get("status") not in {"failed", "cancelled", "uncertain", "unknown"}
            and body.get("exit_code") in (None, 0),
            "pending": bool(body.get("pending"))
            or body.get("status") in {"running", "queued", "waiting"},
            "uncertain": bool(value.get("uncertain") or body.get("uncertain"))
            or body.get("status") in {"uncertain", "unknown"},
        }
        if identity:
            prior = [item for item in self.known_effects if item.get("run_id") == identity]
            # A read receipt resolves the same execution; it does not erase
            # the fact that the parent actually started a mutating job.
            entry["side_effecting"] = side_effecting or any(
                item.get("side_effecting", False) for item in prior
            )
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
            if name == "task_control":
                result = await self._control(args, call_key)
            elif name.startswith("subagent_"):
                from qq_ai_bot.runtime.subagent_tools import execute_subagent

                result = await execute_subagent(self, name, args, call_key)
            else:
                raise ValueError("unknown_work_control")
            return json.dumps({"ok": True, **result}, ensure_ascii=False)
        except (ValueError, WorkConflict) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    async def _control(self, args: dict[str, Any], call_key: str) -> dict[str, Any]:
        action = args.get("action")
        if not isinstance(action, str):
            raise ValueError("work_action_required")
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

                children = await SubagentRepository(self.repository).list(self.current["id"])
                if any(
                    row["state"] not in {"completed", "failed", "cancelled"} for row in children
                ):
                    raise ValueError("work_has_unfinished_subagents")
            await self.reconcile_completed_children()
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
                    if effect.get("ok") or effect.get("delivered_artifacts")
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
                    for identity in effect.get("delivered_artifacts", [])
                }
                if self.current["deliver_artifacts"] and not set(selected) <= delivered:
                    raise ValueError("work_completion_requires_artifact_delivery_receipt")
                # Only a confirmed caption in this conversation replaces the
                # final reply. Sending to somebody else still needs a report here.
                from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

                async with self.repository.database.sessions() as session:
                    conversation = await session.get(
                        CanonicalConversationModel, self.lease.conversation_id
                    )
                if conversation is not None and not self.lease.work_id:
                    target = {
                        "kind": "space" if conversation.space_id else "person",
                        "id": conversation.space_id or conversation.person_id,
                    }
                    explained = {
                        identity
                        for effect in self.known_effects
                        if effect.get("caption_delivered")
                        and effect.get("delivery_target") == target
                        for identity in effect.get("delivered_artifacts", [])
                    }
                    self.completion_delivered = set(selected) <= explained
            elif kind == "answer" and self.source.get("delivery_contract") != "return_to_caller":
                from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

                async with self.repository.database.sessions() as session:
                    conversation = await session.get(
                        CanonicalConversationModel, self.lease.conversation_id
                    )
                if conversation is None:
                    raise ValueError("work_delivery_conversation_missing")
                target = {
                    "kind": "space" if conversation.space_id else "person",
                    "id": conversation.space_id or conversation.person_id,
                }
                self.completion_delivered = any(
                    effect.get("delivered_message")
                    and effect.get("delivery_target") == target
                    for effect in self.known_effects
                )
                # Explicit completion may be silent. The model's final text is
                # internal state; it never becomes a fallback outbound message.
                self.final_delivery = True
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

    async def available_work(self) -> list[dict[str, Any]]:
        if self.lease.work_id:
            return []
        result = []
        for row in await self.repository.active(self.lease.conversation_id, self.lease.generation):
            source = json.loads(row["source_json"])
            if all(
                source.get(k) == self.source.get(k)
                for k in (
                    "actor_user_id",
                    "origin",
                    "plugin_id",
                    "delegation_id",
                    "execution_boundary",
                )
            ):
                result.append({"work_id": row["id"], "goal": row["goal"], "state": row["state"]})
        return result[:16]

    async def settle(self, *, delivered: bool, pending_inputs: bool) -> None:
        from qq_ai_bot.runtime.work_supervisor import settle

        await settle(self, delivered=delivered, pending_inputs=pending_inputs)
