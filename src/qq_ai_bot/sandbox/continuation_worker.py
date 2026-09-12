"""Resume completed sandbox work through the Main Agent and original authority."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from qq_ai_bot.adapters.onebot.sender import parse_onebot_send_receipt
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.trigger import SandboxTaskTurnTrigger
from qq_ai_bot.sandbox.continuations import SandboxContinuationRepository
from qq_ai_bot.sandbox.progress import PROCESS_ID, TaskProgress, current_progress
from qq_ai_bot.sandbox.source_recovery import recover_message_source
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.turn_coordinator import TurnToken

if TYPE_CHECKING:
    from qq_ai_bot.container import ApplicationContainer

logger = logging.getLogger(__name__)


class SandboxContinuationWorker:
    def __init__(self, app: ApplicationContainer) -> None:
        self.app = app
        self.repository = SandboxContinuationRepository(app.database)
        self._worker: asyncio.Task[None] | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            await self.repository.recover_abandoned(PROCESS_ID)
            self._worker = asyncio.create_task(self._loop(), name="sandbox-continuations")

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def health(self) -> dict[str, object]:
        return {
            "running": self._worker is not None and not self._worker.done(),
            "last_error_category": self._last_error,
        }

    async def _loop(self) -> None:
        while True:
            try:
                await self.drain_once()
                self._last_error = None
            except Exception as exc:
                category = type(exc).__name__
                if self._last_error != category:
                    logger.warning("sandbox_continuation_failed category=%s", category)
                self._last_error = category
            await asyncio.sleep(2)

    async def drain_once(self) -> None:
        for request_id in await self.repository.ready():
            try:
                await self._drain_request(request_id)
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.warning("sandbox_task_resume_failed category=%s", self._last_error)
            finally:
                await self.repository.rotate(request_id)

    async def _drain_request(self, request_id: str) -> None:
        task = await self.app.sandbox_tasks.get(request_id)
        if task is None:
            return
        source = json.loads(task.source_json)
        if source.get("origin") == "scheduled_automation":
            await self._automation(request_id)
            return
        recovered = await recover_message_source(self.app.database, request_id)
        scope = (
            ConversationScope.group(recovered.bot_user_id, recovered.external_target_id)
            if recovered.target_space_id
            else ConversationScope.private(recovered.bot_user_id, recovered.external_target_id)
        )
        state = await self.app.conversation_scopes.get(scope)
        if state is None or state.generation != recovered.generation:
            return
        key = state.runtime_scope_key or scope.key
        async with self.app.turn_coordinator.background_turn(key) as token:
            if token is None:
                return
            claim = await self.repository.claim_group(request_id)
            if claim is None:
                return
            claim_token, ids = claim
            try:
                progress, payload = await self._inputs(ids)
                await self._message(
                    request_id, scope, state.id, key, token, claim_token, progress, payload
                )
            except BaseException:
                await self._settle(ids, claim_token, "uncertain", "execution_interrupted")
                raise
            await self._settle(ids, claim_token, "finished", "continuation_completed")

    async def _inputs(self, ids: tuple[str, ...]) -> tuple[TaskProgress, str]:
        tasks = [await self.app.sandbox_tasks.get(item) for item in ids]
        assert all(task is not None for task in tasks)
        first = tasks[0]
        assert first is not None
        previous = json.loads(first.progress_json)
        progress = TaskProgress(0, 0, previous=previous)
        if progress.models_used >= progress.max_models:
            raise ValueError("sandbox_task_budget_exhausted")
        results = []
        for task in tasks:
            assert task is not None
            if task.source_json != first.source_json or task.progress_json != first.progress_json:
                raise ValueError("sandbox_task_group_source_changed")
            results.append(json.loads(task.completion_json or "{}"))
        # Bind only after validating the entire group; all later checkpoints
        # include earlier usage and update every completion in this attempt.
        for item in ids:
            await progress.bind(self.app.sandbox_tasks, item)
        payload = json.dumps({"kind": "sandbox_completion", "results": results}, ensure_ascii=False)
        if len(payload) > 12_000:
            # Keep durable identifiers so the Agent can page results with get_code_run.
            payload = json.dumps(
                {
                    "kind": "sandbox_completion",
                    "results": [
                        {key: value.get(key) for key in ("run_id", "request_id", "status")}
                        for value in results
                    ],
                    "details": "Read full results with get_code_run.",
                }
            )
        return progress, payload

    async def _settle(self, ids: tuple[str, ...], token: str, state: str, reason: str) -> None:
        for request_id in ids:
            await self.repository.settle(request_id, token, state=state, reason=reason)

    async def _message(
        self,
        request_id: str,
        scope: ConversationScope,
        scope_id: int,
        key: str,
        token: TurnToken,
        claim_token: str,
        progress: TaskProgress,
        payload: str,
    ) -> None:
        recovered = await recover_message_source(self.app.database, request_id)
        original = await self.app.ledger.get_event(recovered.event_id)
        task = await self.app.sandbox_tasks.get(request_id)
        assert original is not None and task is not None
        source = json.loads(task.source_json)
        resolved = await self.app.presence_router.resolve_presence(recovered.presence_id)
        runtime = await self.app.runtime_config.snapshot(
            user_id=recovered.actor_person_id, group_id=recovered.target_space_id
        )
        event = original
        snapshot = ConversationTurnSnapshot(
            scope_id, key, recovered.generation, event.id, token.version, scope.key
        )
        # Rehydrate the persisted real message only as the delegated tool source.
        # It is never appended as new inbound or used as the completion prompt.
        inbound = InboundMessage(
            message_id=original.platform_message_id,
            event_type="message",
            scope_type=original.scope_type,
            sender=SenderIdentity(recovered.actor_user_id),
            text=original.content,
            bot_user_id=recovered.bot_user_id,
            group_id=original.group_id,
            received_at=original.occurred_at,
            person_id=recovered.actor_person_id,
            space_id=recovered.target_space_id,
            conversation_id=recovered.conversation_id,
            presence_id=recovered.presence_id,
            legacy_conversation_key=key,
        )

        async def validate() -> None:
            if await recover_message_source(self.app.database, request_id) != recovered:
                raise ValueError("sandbox_source_changed")
            fresh = await self.app.presence_router.resolve_presence(recovered.presence_id)
            if fresh.connection.snapshot != resolved.connection.snapshot:
                raise ValueError("sandbox_presence_changed")

        tool_runtime = ToolRuntime(
            inbound=inbound,
            gateway=None,
            allow_generic_onebot=False,
            allow_admin_actions=False,
            allow_automation=bool(source.get("allow_automation")),
            actor_user_id=recovered.actor_user_id,
            actor_is_superuser=False,
            current_group_id=original.group_id,
            conversation_key=key,
            trigger_message_id=original.platform_message_id,
            execution_id=claim_token,
            origin=TurnOrigin(recovered.origin),
            conversation_id=recovered.conversation_id,
            person_id=recovered.actor_person_id,
            space_id=recovered.target_space_id,
            presence_id=recovered.presence_id,
            sandbox_source=source,
            task_progress=progress,
            max_model_requests_override=progress.max_models - progress.models_used,
            max_tool_calls_override=progress.max_tools - progress.tools_used,
        )
        result = await self.app.chat.generate_main_agent_wakeup(
            event=event,
            trigger=SandboxTaskTurnTrigger(
                event.id, scope.scope_type.value, recovered.external_target_id, payload
            ),
            identity=scope,
            runtime=runtime,
            turn_token=token,
            turn_snapshot=snapshot,
            gateway=None,
            person_id=recovered.target_person_id,
            space_id=recovered.target_space_id,
            presence_id=recovered.presence_id,
            conversation_id=recovered.conversation_id,
            before_model_request=validate,
            source_runtime=tool_runtime,
        )
        await self.repository.record_outcome(
            claim_token,
            {
                "text": result.text,
                "suppressed": result.suppress_delivery,
                "delivery": "not_started",
            },
        )
        if result.suppress_delivery or not result.text:
            return

        async def deliver() -> None:
            await validate()
            await progress.reserve_message()
            group = original.scope_type is ScopeType.GROUP
            call_api = getattr(resolved.connection.bot, "call_api", None)
            if not callable(call_api):
                raise ValueError("sandbox_gateway_unavailable")
            response = await call_api(
                "send_group_msg" if group else "send_private_msg",
                **{
                    "group_id" if group else "user_id": int(recovered.external_target_id),
                    "message": [{"type": "text", "data": {"text": result.text}}],
                },
            )
            receipt = parse_onebot_send_receipt(response)
            await self.repository.record_outcome(
                claim_token,
                {
                    "text": result.text,
                    "delivery": "accepted",
                    "platform_message_id": receipt.platform_message_id,
                    "presence_id": recovered.presence_id,
                },
            )
            await self.app.ledger.append(
                bot_user_id=recovered.bot_user_id,
                platform_message_id=receipt.platform_message_id,
                scope_type=original.scope_type,
                sender_user_id=recovered.bot_user_id,
                direction="outbound",
                content=result.text,
                group_id=original.group_id,
                private_peer_user_id=recovered.external_target_id if not group else None,
                sender_is_bot=True,
                origin=TurnOrigin.SYSTEM_TASK.value,
                caused_by_event_id=event.id,
            )

        await self.app.chat._run_effect(snapshot, deliver)

    async def _automation(self, request_id: str) -> None:
        from qq_ai_bot.sandbox.automation_recovery import recover_automation_source

        recovered = await recover_automation_source(self.app.automation_executor, request_id)
        async with self.app.turn_coordinator.background_turn(
            recovered.context.conversation_key
        ) as token:
            if token is None:
                return
            claim = await self.repository.claim_group(request_id)
            if claim is None:
                return
            claim_token, ids = claim
            try:
                progress, payload = await self._inputs(ids)
                limits = recovered.automation.script.limits
                progress.max_models = min(
                    progress.max_models,
                    progress.models_used
                    + max(0, limits.max_llm_calls - recovered.prior_model_calls),
                )
                progress.max_tools = min(
                    progress.max_tools,
                    progress.tools_used
                    + max(0, limits.max_tool_calls - recovered.prior_tool_calls),
                )
                progress.max_messages = min(
                    progress.max_messages,
                    progress.messages_used
                    + max(0, limits.max_messages - recovered.prior_messages_sent),
                )
                if progress.models_used >= progress.max_models:
                    raise ValueError("sandbox_task_budget_exhausted")
                result = await self.app._automation_handlers.agent(
                    {
                        "instruction": recovered.instruction,
                        "context_profile": recovered.context_profile,
                        "max_model_requests": progress.max_models - progress.models_used,
                        "max_tool_calls": progress.max_tools - progress.tools_used,
                    },
                    replace(
                        recovered.context, step_id=f"{recovered.context.step_id}:{claim_token}"
                    ),
                    task_progress=progress,
                    completion_payload=payload,
                )
                text = str(result.data.get("text") or "").strip()
                await self.repository.record_outcome(
                    claim_token, {"text": text, "delivery": "delegated_tools_only"}
                )
                group_id = recovered.context.current_group_id
                capability = (
                    "onebot.send_group_message" if group_id else "onebot.send_private_message"
                )
                if text and capability in recovered.context.authority.allowed_capabilities:
                    assert recovered.context.revalidate_authority is not None
                    await recovered.context.revalidate_authority(capability)
                    if not self.app.turn_coordinator.version_matches(
                        token.conversation_key, token.version
                    ):
                        raise ValueError("sandbox_turn_superseded")
                    tracking = current_progress.set(progress)
                    try:
                        if group_id:
                            await self.app._automation_handlers.send_group(
                                {"group_id": group_id, "text": text}, recovered.context
                            )
                        else:
                            await self.app._automation_handlers.send_private(
                                {"user_id": recovered.context.creator_user_id, "text": text},
                                recovered.context,
                            )
                    finally:
                        current_progress.reset(tracking)
            except BaseException:
                await self._settle(ids, claim_token, "uncertain", "execution_interrupted")
                raise
            await self._settle(ids, claim_token, "finished", "continuation_completed")
