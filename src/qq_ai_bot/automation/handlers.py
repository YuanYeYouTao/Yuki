"""Bound implementations for the reviewed automation capability registry."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pydantic import ValidationError

from qq_ai_bot.admin.action_service import AdminActionService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.executor import AutomationExecutionError
from qq_ai_bot.automation.gateway import ProactiveGateway
from qq_ai_bot.automation.registry import (
    AutomationCapabilityRegistry,
    CapabilityExecutionContext,
    CapabilityHandler,
    CapabilityResult,
)
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatTool, PromptRequestDiagnostics, ToolCall
from qq_ai_bot.emoji.models import (
    EmojiPlacement,
    EmojiReplyMode,
    EmojiSelectionRequest,
)
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.llm.base import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMIncompleteResponseError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMNativeToolError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUnsupportedFeatureError,
)
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from qq_ai_bot.services.prompt_composer import PromptComposer, PromptComposition
from qq_ai_bot.speech.genie_client import GenieWorkerFailure, GenieWorkerUnavailable
from qq_ai_bot.speech.provider import SpeechSynthesisRequest
from qq_ai_bot.speech.service import (
    SpeechQueueFullError,
    SpeechService,
    SpeechUnavailableError,
)
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.web.base import WebSearchError, WebSearchProvider, normalize_public_url
from qq_ai_bot.web.models import WebSearchRequest

if TYPE_CHECKING:
    from qq_ai_bot.automation.service import AutomationService

GatewayFactory = Callable[[CapabilityExecutionContext], ProactiveGateway]


class _AutomationContextChanged(LLMInvalidRequestError):
    """The declared history no longer belongs to the current conversation epoch."""


class _AutomationAuthorityChanged(LLMInvalidRequestError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class AutomationCapabilityHandlers:
    """Dependency-bound handlers; registry metadata remains independent and testable."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: ModelCompleter | None = None,
        model_executor: ModelExecutor | None = None,
        concurrency: ConcurrencyManager,
        runtime_config: RuntimeConfigService,
        time_service: TimeContextService,
        ledger: EventLedgerRepository,
        memories: MemoryFactService,
        relationships: RelationshipRepository,
        admin_actions: AdminActionService,
        web_provider: WebSearchProvider | None,
        gateway_factory: GatewayFactory,
        emoji_repository: EmojiRepository | None = None,
        emoji_selector: EmojiSelector | None = None,
        emoji_storage: EmojiStorage | None = None,
        speech: SpeechService | None = None,
    ) -> None:
        self._settings = settings
        self._models = require_model_executor(
            model_executor,
            provider=provider,
            model=settings.llm_model or "fake",
        )
        self._concurrency = concurrency
        self._runtime_config = runtime_config
        self._time = time_service
        self._ledger = ledger
        self._memories = memories
        self._relationships = relationships
        self._admin_actions = admin_actions
        self._web = web_provider
        self._gateway_factory = gateway_factory
        self._emoji_repository = emoji_repository
        self._emoji_selector = emoji_selector
        self._emoji_storage = emoji_storage
        self._speech = speech
        self._agent_runner = AgentRunner(
            self._models,
            concurrency,
            task=ModelTask.AUTOMATION_AGENT,
        )
        self._registry: AutomationCapabilityRegistry | None = None
        self._automation_service: AutomationService | None = None

    def bind_registry(self, registry: AutomationCapabilityRegistry) -> None:
        self._registry = registry

    def bind_automation_service(self, service: AutomationService) -> None:
        self._automation_service = service

    def mapping(self) -> dict[str, CapabilityHandler]:
        return {
            "yuki.generate": self.generate,
            "yuki.agent": self.agent,
            "onebot.send_private_message": self.send_private,
            "onebot.send_group_message": self.send_group,
            "speech.send_private": self.send_speech,
            "speech.send_group": self.send_speech,
            "emoji.send": self.send_emoji,
            "emoji.send_by_id": self.send_emoji,
            "onebot.call_api": self.call_onebot,
            "admin.execute_action": self.admin_action,
            "config.get": self.config_get,
            "config.set": self.config_set,
            "web.search": self.web_search,
            "web.read_page": self.web_read,
            "memory.get_person": self.person_memory,
            "memory.get_group": self.group_memory,
            "history.search": self.history_search,
            "automation.create_task": self.automation_create_task,
            "automation.update_task": self.automation_update_task,
            "automation.cancel_task": self.automation_cancel_task,
            "automation.run_task_now": self.automation_run_task_now,
            "automation.list_tasks": self.automation_list_tasks,
        }

    def _require_automation_service(self) -> AutomationService:
        if self._automation_service is None:
            raise AutomationExecutionError("automation_service_unavailable")
        return self._automation_service

    async def automation_create_task(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        row, plan = await self._require_automation_service().create_task_delegated(
            arguments["task"],
            context=context,
            max_runs=cast(int | None, arguments.get("max_runs")),
        )
        return CapabilityResult(
            data={
                "automation_id": row.id,
                "name": row.name,
                "status": row.status.value,
                "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                "compiled_strategy": plan.strategy,
            }
        )

    async def automation_update_task(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        row, plan = await self._require_automation_service().update_task_delegated(
            int(arguments["automation_id"]),
            arguments["task"],
            context=context,
        )
        return CapabilityResult(
            data={
                "automation_id": row.id,
                "name": row.name,
                "status": row.status.value,
                "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                "compiled_strategy": plan.strategy,
            }
        )

    async def automation_cancel_task(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        automation_id = int(arguments["automation_id"])
        changed = await self._require_automation_service().cancel_delegated(
            automation_id, context=context
        )
        return CapabilityResult(data={"automation_id": automation_id, "cancelled": changed})

    async def automation_run_task_now(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        automation_id = int(arguments["automation_id"])
        changed = await self._require_automation_service().run_now_delegated(
            automation_id, context=context
        )
        return CapabilityResult(data={"automation_id": automation_id, "scheduled": changed})

    async def automation_list_tasks(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        service = self._require_automation_service()
        rows = (
            await service.list(context.creator_user_id)
            if bool(arguments.get("include_completed"))
            else await service.list_current(context.creator_user_id)
        )
        return CapabilityResult(
            data={
                "tasks": [
                    {
                        "automation_id": row.id,
                        "name": row.name,
                        "status": row.status.value,
                        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
                    }
                    for row in rows
                ]
            }
        )

    async def generate(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if self._agent_runner.main_contract is not None:
            # Text generation has the same declaration and state, but no delegated external effects.
            bounded_context = replace(
                context,
                authority=context.authority.model_copy(
                    update={"allowed_capabilities": frozenset()}
                ),
            )
            result = await self.agent(
                {**arguments, "max_tool_calls": 3, "max_model_requests": 4}, bounded_context
            )
            return replace(
                result, data={"text": str(result.data["text"])[: int(arguments["max_characters"])]}
            )
        messages = await self._generation_messages(arguments, context)
        snapshot = await self._runtime_config.snapshot(
            user_id=context.creator_user_id,
            group_id=context.current_group_id,
        )
        request = _chat_request(messages, snapshot, tools=())
        execute = (
            partial(
                self._models.execute,
                ModelTask.AUTOMATION_TEXT_GENERATION,
                request,
                canonical_conversation_id=context.canonical_conversation_id,
            )
            if context.canonical_conversation_id is not None
            else partial(
                self._models.execute,
                ModelTask.AUTOMATION_TEXT_GENERATION,
                request,
            )
        )
        try:
            response = await self._concurrency.run_llm(
                context.conversation_key,
                execute,
            )
        except LLMError as exc:
            raise _automation_llm_error(exc, llm_calls=1) from exc
        text = response.content.strip()[: int(arguments["max_characters"])]
        if not text:
            raise AutomationExecutionError("llm_empty_response")
        return CapabilityResult(data={"text": text}, llm_calls=1)

    async def agent(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if self._registry is None:
            raise AutomationExecutionError("agent_registry_unavailable")
        snapshot = await self._runtime_config.snapshot(
            user_id=context.creator_user_id,
            group_id=context.current_group_id,
        )
        current_time = self._time.at(context.actual_started_at, context.timezone)
        selected_capabilities = frozenset(
            str(name) for name in arguments.get("allowed_capabilities", ())
        )
        runtime = AgentRuntime(
            origin=context.authority.origin,
            actor_user_id=context.creator_user_id,
            actor_is_superuser=context.authority.actor_is_superuser,
            delegated_authority=context.authority.delegated_authority,
            conversation_key=context.conversation_key,
            current_group_id=context.current_group_id,
            bot_user_id=context.bot_user_id,
            gateway=self._gateway_factory(context),
            runtime_config=snapshot,
            current_time=current_time,
            allowed_capabilities=(
                context.authority.allowed_capabilities.intersection(selected_capabilities)
                if selected_capabilities
                else context.authority.allowed_capabilities
            ),
            max_tool_calls=min(int(arguments["max_tool_calls"]), snapshot.agent.max_tool_calls),
            max_model_requests=min(
                int(arguments["max_model_requests"]), snapshot.agent.max_model_requests
            ),
            canonical_conversation_id=context.canonical_conversation_id,
        )
        context = replace(
            context,
            authority=context.authority.model_copy(
                update={"allowed_capabilities": runtime.allowed_capabilities}
            ),
            agent_instruction=str(arguments["instruction"]),
            agent_context_profile=str(arguments.get("context_profile") or "none"),
        )
        backend = _AutomationAgentBackend(self._registry, context)
        backend.main_contract = self._agent_runner.main_contract
        backend.short_state = (
            self._agent_runner.main_contract.state if self._agent_runner.main_contract else None
        )
        composition = await self._generation_composition(
            arguments, context, runtime_config=snapshot
        )
        messages = composition.messages

        async def validate_context() -> None:
            if context.revalidate_authority is not None:
                try:
                    await context.revalidate_authority(None)
                except AutomationExecutionError as exc:
                    raise _AutomationAuthorityChanged(exc.category) from exc
            if composition.read_version is not None and not await self._ledger.read_version_matches(
                composition.read_version
            ):
                raise _AutomationContextChanged("automation context generation changed")

        runtime = replace(
            runtime,
            before_model_request=validate_context,
            prompt_diagnostics=PromptRequestDiagnostics(
                conversation_prefix_hash=composition.metrics.conversation_prefix_hash,
                prompt_snapshot_fingerprint=composition.metrics.prompt_snapshot_fingerprint,
                static_prompt_revision=composition.metrics.stable_prefix_hash,
            ),
        )
        try:
            result = await self._main_turn_service().run(messages, runtime, backend)
        except LLMError as exc:
            raise _automation_llm_error(
                exc,
                llm_calls=backend.failed_model_requests,
                tool_calls=1 + backend.failed_tool_calls,
                messages_sent=backend.messages_sent,
            ) from exc
        return CapabilityResult(
            data={"text": result.text, "tool_calls_used": result.tool_calls_used},
            llm_calls=result.model_requests + backend.nested_llm_calls,
            tool_calls=1 + result.tool_calls_used + backend.nested_tool_calls,
            messages_sent=backend.messages_sent,
        )

    async def send_private(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        gateway = self._gateway_factory(context)
        await gateway.send_private(str(arguments["user_id"]), str(arguments["text"]))
        return CapabilityResult(
            data={"sent": True, "user_id": str(arguments["user_id"])}, messages_sent=1
        )

    async def send_group(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        gateway = self._gateway_factory(context)
        await gateway.send_group(str(arguments["group_id"]), str(arguments["text"]))
        return CapabilityResult(
            data={"sent": True, "group_id": str(arguments["group_id"])}, messages_sent=1
        )

    async def send_speech(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if self._speech is None:
            raise AutomationExecutionError("speech_system_unavailable")
        user_id = str(arguments["user_id"]) if arguments.get("user_id") else None
        group_id = str(arguments["group_id"]) if arguments.get("group_id") else None
        if (user_id is None) == (group_id is None):
            raise AutomationExecutionError("speech_target_invalid")
        if not context.authority.actor_is_superuser:
            if user_id is not None and user_id != context.creator_user_id:
                raise AutomationExecutionError("person_scope_denied")
            if group_id is not None and group_id != context.current_group_id:
                raise AutomationExecutionError("group_scope_denied")
            if arguments.get("profile_id"):
                raise AutomationExecutionError("speech_profile_scope_denied")
        snapshot = await self._runtime_config.snapshot(
            user_id=context.creator_user_id,
            group_id=group_id,
        )
        if not snapshot.speech.automation_enabled:
            raise AutomationExecutionError("speech_automation_disabled")
        try:
            generated = await self._speech.synthesize(
                SpeechSynthesisRequest(
                    request_id=str(uuid4()),
                    profile_id=str(arguments.get("profile_id") or ""),
                    style_hint=str(arguments.get("style_hint") or ""),
                    text=str(arguments["text"]),
                    split_sentence=snapshot.speech.split_sentence,
                    conversation_key=context.conversation_key,
                    trigger_event_id=None,
                    turn_token=None,
                ),
                runtime=snapshot.speech,
            )
        except (
            ValueError,
            LookupError,
            SpeechUnavailableError,
            SpeechQueueFullError,
            GenieWorkerUnavailable,
            GenieWorkerFailure,
            OSError,
        ) as exc:
            raise AutomationExecutionError("speech_generation_failed") from exc
        await self._gateway_factory(context).send_voice(
            user_id=user_id,
            group_id=group_id,
            local_path=str(self._speech.audio_path(generated)),
            spoken_text=str(arguments["text"]),
            generation_id=generated.generation_id,
            profile_id=generated.profile_id,
            reference_key=generated.reference_key,
            duration_milliseconds=generated.duration_milliseconds,
        )
        await self._speech.mark_sent(generated.generation_id)
        return CapabilityResult(
            data={
                "sent": True,
                "generation_id": generated.generation_id,
                "profile_id": generated.profile_id,
                "reference_key": generated.reference_key,
                "duration_milliseconds": generated.duration_milliseconds,
            },
            messages_sent=1,
        )

    async def send_emoji(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        repository = self._emoji_repository
        selector = self._emoji_selector
        storage = self._emoji_storage
        if repository is None or selector is None or storage is None:
            raise AutomationExecutionError("emoji_system_unavailable")
        user_id = str(arguments["user_id"]) if arguments.get("user_id") else None
        group_id = str(arguments["group_id"]) if arguments.get("group_id") else None
        self._validate_emoji_target(user_id=user_id, group_id=group_id, context=context)
        snapshot = await self._runtime_config.snapshot(
            user_id=context.creator_user_id,
            group_id=group_id,
        )
        if not snapshot.emoji.enabled:
            raise AutomationExecutionError("emoji_disabled")
        emoji_id = str(arguments.get("emoji_id") or "")
        if not emoji_id:
            selected = await selector.select(
                EmojiSelectionRequest(
                    actor_user_id=context.creator_user_id,
                    group_id=group_id,
                    reply_text="",
                    goal=str(arguments.get("intended_tone") or "自然发送一个合适的表情"),
                    emotion=str(arguments.get("emotion") or ""),
                    explicit_request=True,
                    mode=EmojiReplyMode.PREFERRED,
                    placement=EmojiPlacement(str(arguments.get("placement") or "only")),
                ),
                runtime=snapshot.emoji,
                vision_runtime=snapshot.vision,
            )
            emoji_id = selected.emoji_id or ""
        if not emoji_id or not await repository.enabled_in_scope(emoji_id, group_id=group_id):
            raise AutomationExecutionError("emoji_not_available")
        asset = await repository.get(emoji_id)
        if asset is None:
            raise AutomationExecutionError("emoji_not_available")
        try:
            content = storage.read(asset.relative_path)
        except RuntimeError as exc:
            raise AutomationExecutionError("emoji_file_missing") from exc
        await self._gateway_factory(context).send_emoji(
            user_id=user_id,
            group_id=group_id,
            content=content,
            mime_type=asset.mime_type,
            emoji_id=asset.id,
            summary=asset.description or f"{self._settings.bot_display_name} 发送的表情",
        )
        await repository.mark_used(
            asset.id,
            actor_user_id=context.creator_user_id,
            group_id=group_id,
            trigger_message_id=f"automation:{context.automation_id}:{context.automation_run_id}",
            source="automation",
        )
        return CapabilityResult(
            data={
                "sent": True,
                "emoji_id": asset.id,
                "scope": "group" if group_id is not None else "private",
                "placement": str(arguments.get("placement") or "only"),
            },
            messages_sent=1,
        )

    @staticmethod
    def _validate_emoji_target(
        *,
        user_id: str | None,
        group_id: str | None,
        context: CapabilityExecutionContext,
    ) -> None:
        if (user_id is None) == (group_id is None):
            raise AutomationExecutionError("emoji_target_invalid")
        if context.authority.actor_is_superuser:
            return
        if user_id is not None and user_id != context.creator_user_id:
            raise AutomationExecutionError("person_scope_denied")
        if group_id is not None and group_id != context.current_group_id:
            raise AutomationExecutionError("group_scope_denied")

    async def call_onebot(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if not context.authority.actor_is_superuser:
            raise AutomationExecutionError("permission_revoked")
        result = await self._gateway_factory(context).call_api(
            str(arguments["action"]), cast(dict[str, object], arguments["params"])
        )
        return CapabilityResult(data={"ok": True, "result": _bounded_result(result)})

    async def admin_action(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if not context.authority.actor_is_superuser:
            raise AutomationExecutionError("permission_revoked")
        raise AutomationExecutionError("operation_unavailable")

    async def config_get(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if not context.authority.actor_is_superuser:
            raise AutomationExecutionError("permission_revoked")
        row = await self._runtime_config.get_effective(
            str(arguments["key"]),
            user_id=(str(arguments["scope_id"]) if arguments["scope_type"] == "user" else None),
            group_id=(str(arguments["scope_id"]) if arguments["scope_type"] == "group" else None),
        )
        return CapabilityResult(
            data={
                "key": row.key,
                "value": row.value,
                "source": row.source,
                "configured": row.configured,
            }
        )

    async def config_set(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if not context.authority.actor_is_superuser:
            raise AutomationExecutionError("permission_revoked")
        key = str(arguments["key"])
        if key.startswith("automation."):
            raise AutomationExecutionError("automation_control_is_immutable")
        result = await self._runtime_config.set_override(
            key,
            arguments["value"],
            scope_type=str(arguments["scope_type"]),
            scope_id=str(arguments["scope_id"]),
            actor_user_id=context.creator_user_id,
            trigger_message_id=f"automation:{context.automation_id}:{context.automation_run_id}",
            conversation_key=context.conversation_key,
        )
        if not result.success:
            raise AutomationExecutionError(result.error_category or "config_rejected")
        return CapabilityResult(data={"key": result.key, "after": result.after, "ok": True})

    async def web_search(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if self._web is None:
            raise AutomationExecutionError("web_not_configured")
        try:
            response = await self._web.search(
                WebSearchRequest(
                    query=str(arguments["query"]),
                    topic=cast(Any, arguments["topic"]),
                    time_range=cast(Any, arguments.get("time_range")),
                    max_results=self._settings.web_search_max_results,
                    extract_max_results=self._settings.web_extract_max_results,
                )
            )
        except WebSearchError as exc:
            raise AutomationExecutionError(exc.code, transient=True) from exc
        return CapabilityResult(
            data={
                "query": response.query,
                "sources": [
                    {
                        "title": source.title[:300],
                        "url": source.url,
                        "summary": source.relevant_content[:3000],
                    }
                    for source in response.sources[: self._settings.web_extract_max_results]
                ],
            }
        )

    async def web_read(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        if self._web is None:
            raise AutomationExecutionError("web_not_configured")
        try:
            source = await self._web.extract(
                normalize_public_url(str(arguments["url"])), str(arguments.get("question") or "")
            )
        except WebSearchError as exc:
            raise AutomationExecutionError(exc.code, transient=True) from exc
        return CapabilityResult(
            data={
                "title": source.title[:300],
                "url": source.url,
                "summary": source.relevant_content[:6000],
            }
        )

    async def person_memory(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        user_id = str(arguments["user_id"])
        if not context.authority.actor_is_superuser and user_id != context.creator_user_id:
            raise AutomationExecutionError("person_scope_denied")
        rows = await self._memories.list_person(user_id, limit=int(arguments["limit"]))
        return CapabilityResult(
            data={"memories": [{"id": row.id, "content": row.content} for row in rows]}
        )

    async def group_memory(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        group_id = str(arguments["group_id"])
        if not context.authority.actor_is_superuser and group_id != context.current_group_id:
            raise AutomationExecutionError("group_scope_denied")
        rows = await self._memories.list_group(group_id, limit=int(arguments["limit"]))
        return CapabilityResult(
            data={"memories": [{"id": row.id, "content": row.content} for row in rows]}
        )

    async def history_search(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        user_id = arguments.get("user_id")
        group_id = arguments.get("group_id")
        if not context.authority.actor_is_superuser:
            if user_id not in {None, context.creator_user_id}:
                raise AutomationExecutionError("person_scope_denied")
            if group_id not in {None, context.current_group_id}:
                raise AutomationExecutionError("group_scope_denied")
            if user_id is None and group_id is None:
                user_id = context.creator_user_id
        if context.canonical_conversation_id:
            keyword = str(arguments["keyword"]).casefold()
            after = _parse_time(arguments.get("after"))
            before = _parse_time(arguments.get("before"))
            recent = await self._ledger.list_canonical_recent(
                context.canonical_conversation_id,
                limit=max(int(arguments["limit"]), 1),
                message_only=True,
            )
            rows = tuple(
                row
                for row in recent
                if keyword in row.content.casefold()
                and (after is None or row.occurred_at >= after)
                and (before is None or row.occurred_at <= before)
            )
        else:
            rows = await self._ledger.search(
                keyword=str(arguments["keyword"]),
                limit=int(arguments["limit"]),
                user_id=str(user_id) if user_id else None,
                group_id=str(group_id) if group_id else None,
                after=_parse_time(arguments.get("after")),
                before=_parse_time(arguments.get("before")),
                message_only=True,
            )
        return CapabilityResult(
            data={
                "events": [
                    {
                        "sender_user_id": row.sender_user_id,
                        "content": row.content[:2000],
                        "event_kind": row.event_kind,
                        "author_kind": row.author_kind,
                        "source": row.origin,
                        "content_trust": "untrusted_conversation_message",
                        "occurred_at": row.occurred_at.isoformat(),
                    }
                    for row in rows
                ]
            }
        )

    async def _generation_messages(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        *,
        runtime_config: RuntimeConfigSnapshot | None = None,
    ) -> tuple[ChatMessage, ...]:
        return (
            await self._generation_composition(
                arguments,
                context,
                runtime_config=runtime_config,
            )
        ).messages

    async def _generation_composition(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        *,
        runtime_config: RuntimeConfigSnapshot | None = None,
    ) -> PromptComposition:
        snapshot = runtime_config or await self._runtime_config.snapshot(
            user_id=context.creator_user_id,
            group_id=context.current_group_id,
        )
        assembled = await ContextAssembler.assemble_automation(
            settings=self._settings,
            ledger=self._ledger,
            memories=self._memories,
            relationships=self._relationships,
            context=context,
            instruction=str(arguments["instruction"]),
            profile=str(arguments.get("context_profile") or "none"),
            current_time=self._time.at(context.actual_started_at, context.timezone),
        )
        composition = await self._main_turn_service().compose(
            inbound=None,
            context=assembled,
            runtime=snapshot,
            visual_observation=None,
            visual_failure=False,
            scope_type=ScopeType.GROUP if context.current_group_id else ScopeType.PRIVATE,
            include_plugin_context=False,
        )
        return composition

    def _main_turn_service(self) -> MainAgentTurnService:
        contract = self._agent_runner.main_contract
        if contract is not None:
            return cast(MainAgentTurnService, contract.chat._main_turns)
        return MainAgentTurnService(PromptComposer(self._settings), self._agent_runner)


class _AutomationAgentBackend(AgentToolBackend):
    def __init__(
        self,
        registry: AutomationCapabilityRegistry,
        context: CapabilityExecutionContext,
    ) -> None:
        self._registry = registry
        self.short_state: Any = None
        self.main_contract: Any = None
        self._context = context
        self._name_map: dict[str, str] = {}
        self._web_was_used = context.web_was_used
        self._messages_sent = 0
        self._nested_llm_calls = 0
        self._nested_tool_calls = 0
        self._failed_model_requests = 0
        self._failed_tool_calls = 0

    @property
    def messages_sent(self) -> int:
        return self._messages_sent

    @property
    def nested_llm_calls(self) -> int:
        return self._nested_llm_calls

    @property
    def nested_tool_calls(self) -> int:
        return self._nested_tool_calls

    @property
    def failed_model_requests(self) -> int:
        return self._failed_model_requests

    @property
    def failed_tool_calls(self) -> int:
        return self._failed_tool_calls

    def record_failure_usage(self, *, tool_calls: int, model_requests: int) -> None:
        self._failed_tool_calls = max(self._failed_tool_calls, tool_calls)
        self._failed_model_requests = max(self._failed_model_requests, model_requests)

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        tools: list[ChatTool] = []
        self._name_map.clear()
        self._web_was_used = self._web_was_used or web_was_used
        for capability in self._registry.list():
            if capability.name not in runtime.allowed_capabilities:
                continue
            if capability.name.startswith("yuki."):
                continue
            tool_name = (
                self.main_contract.automation_names.get(capability.name)
                if self.main_contract
                else None
            ) or self._registry.agent_tool_name(capability.name)
            self._name_map[tool_name] = capability.name
            tools.append(
                ChatTool(
                    name=tool_name,
                    description=capability.description,
                    parameters=capability.input_schema,
                    result_cacheable=capability.result_cacheable,
                )
            )
        return tuple(tools)

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        return None

    def did_use_web(self) -> bool:
        return self._web_was_used

    def parallel_safe(self, name: str, runtime: AgentRuntime) -> bool:
        capability_name = self._name_map.get(name)
        if capability_name is None:
            return False
        definition = self._registry.require(capability_name)
        return definition.risk_class.value == "read"

    def is_side_effecting(
        self,
        name: str,
        arguments_json: str,
        runtime: AgentRuntime,
    ) -> bool:
        del arguments_json, runtime
        if name == "update_short_state":
            return True
        capability_name = self._name_map.get(name)
        if capability_name is None:
            return False
        return self._registry.require(capability_name).risk_class.value != "read"

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        if name == "update_short_state" and self.short_state is not None:
            if self._context.revalidate_authority is not None:
                try:
                    await self._context.revalidate_authority(None)
                except AutomationExecutionError as exc:
                    return json.dumps({"ok": False, "error": exc.category})
            return cast(str, await self.short_state.execute(arguments_json))
        capability_name = self._name_map.get(name)
        if capability_name is None:
            if self.main_contract and name in self.main_contract.automation_names.values():
                return json.dumps({"ok": False, "error": "capability_not_allowed"})
            return json.dumps({"ok": False, "error": "unknown_tool"})
        definition = self._registry.require(capability_name)
        if definition.handler is None:
            return json.dumps({"ok": False, "error": "handler_unavailable"})
        try:
            raw = json.loads(arguments_json)
            arguments = definition.validate_arguments(raw)
            if self._context.revalidate_authority is not None:
                await self._context.revalidate_authority(capability_name)
            from qq_ai_bot.capabilities.invocation import current_invocation

            invocation = current_invocation.get()
            call_context = self._context
            if invocation is not None:
                from dataclasses import replace
                from hashlib import sha256

                call_context = replace(
                    self._context,
                    step_id="agent:"
                    + sha256(
                        (self._context.step_id + ":" + invocation.call_id).encode()
                    ).hexdigest(),
                )
            result = await definition.handler(arguments, call_context)
        except ValidationError as exc:
            issues = [
                {
                    "path": ".".join(str(part) for part in issue["loc"]),
                    "message": issue["msg"],
                    "type": issue["type"],
                }
                for issue in exc.errors(include_input=False)[:8]
            ]
            return json.dumps(
                {"ok": False, "error": "invalid_arguments", "issues": issues},
                ensure_ascii=False,
            )
        except (AutomationExecutionError, ValueError, json.JSONDecodeError) as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": getattr(exc, "category", "invalid_arguments"),
                    "detail": str(exc)[:1000],
                },
                ensure_ascii=False,
            )
        except Exception:
            return json.dumps(
                {"ok": False, "error": "capability_execution_failed"},
                ensure_ascii=False,
            )
        if capability_name in {"web.search", "web.read_page"}:
            self._web_was_used = True
        self._messages_sent += result.messages_sent
        self._nested_llm_calls += result.llm_calls
        self._nested_tool_calls += result.tool_calls
        return json.dumps({"ok": True, "data": result.data}, ensure_ascii=False)[:32000]

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        return content

    def exhausted(self, runtime: AgentRuntime) -> str:
        return "工具调用次数过多，自动化 Agent 已停止。"

    def post_commit_recovery_text(self) -> str | None:
        return None


def _chat_request(
    messages: tuple[ChatMessage, ...], snapshot: Any, *, tools: tuple[ChatTool, ...]
) -> Any:
    from qq_ai_bot.domain.messages import ChatRequest

    return ChatRequest(
        messages=messages,
        model=snapshot.llm.model or "fake",
        temperature=snapshot.llm.temperature,
        max_output_tokens=snapshot.llm.max_output_tokens,
        thinking_enabled=snapshot.llm.thinking_enabled,
        tools=tools,
        tool_choice="auto" if tools else None,
    )


def _automation_llm_error(
    error: LLMError,
    *,
    llm_calls: int,
    tool_calls: int = 0,
    messages_sent: int = 0,
) -> AutomationExecutionError:
    if isinstance(error, _AutomationAuthorityChanged):
        category, transient = error.category, False
    elif isinstance(error, _AutomationContextChanged):
        category, transient = "automation_context_changed", False
    elif isinstance(error, LLMRateLimitError):
        category, transient = "llm_rate_limited", True
    elif isinstance(error, LLMTimeoutError):
        category, transient = "llm_timeout", True
    elif isinstance(error, LLMUnavailableError):
        category, transient = "llm_unavailable", True
    elif isinstance(error, LLMAuthenticationError):
        category, transient = "llm_authentication_failed", False
    elif isinstance(error, LLMConfigurationError):
        category, transient = "llm_configuration_error", False
    elif isinstance(error, LLMInvalidRequestError):
        category, transient = "llm_invalid_request", False
    elif isinstance(error, LLMUnsupportedFeatureError):
        category, transient = "llm_unsupported_feature", False
    elif isinstance(error, LLMInvalidResponseError):
        category, transient = "llm_invalid_response", False
    elif isinstance(error, LLMIncompleteResponseError):
        category, transient = "llm_incomplete_response", False
    elif isinstance(error, LLMNativeToolError):
        category, transient = "llm_native_tool_error", False
    elif isinstance(error, LLMEmptyResponseError):
        category, transient = "llm_empty_response", False
    else:
        category, transient = "llm_error", False
    return AutomationExecutionError(
        category,
        transient=transient,
        llm_calls=llm_calls,
        tool_calls=tool_calls,
        messages_sent=messages_sent,
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AutomationExecutionError("history_time_requires_timezone")
    return parsed.astimezone(UTC)


def _bounded_result(value: object) -> object:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"type": type(value).__name__}
    if len(encoded) > 8000:
        return {"truncated": True, "characters": len(encoded)}
    return json.loads(encoded)
