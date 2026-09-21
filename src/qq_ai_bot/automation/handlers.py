"""Bound implementations for the reviewed automation capability registry."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.automation.executor import AutomationExecutionError
from qq_ai_bot.automation.gateway import ProactiveGateway
from qq_ai_bot.automation.registry import (
    CapabilityExecutionContext,
    CapabilityHandler,
    CapabilityResult,
)
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, PromptRequestDiagnostics
from qq_ai_bot.domain.tool_actor import ToolActor
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
from qq_ai_bot.runtime.activation_outcome import ContextBoundaryChanged
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRuntime,
)
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService
from qq_ai_bot.services.prompt_composer import PromptComposition
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
    pass

GatewayFactory = Callable[[CapabilityExecutionContext], ProactiveGateway]


class _AutomationContextChanged(ContextBoundaryChanged):
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
            "config.get": self.config_get,
            "config.set": self.config_set,
            "web.search": self.web_search,
            "web.read_page": self.web_read,
            "memory.get_person": self.person_memory,
            "memory.get_group": self.group_memory,
            "history.search": self.history_search,
        }

    async def generate(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        result = await self.agent(arguments, context)
        if result.pending_work_id:
            return result
        return replace(
            result,
            data={
                **result.data,
                "text": str(result.data["text"])[: int(arguments.get("max_characters", 4000))],
            },
        )

    async def agent(
        self,
        arguments: dict[str, Any],
        context: CapabilityExecutionContext,
        *,
        completion_payload: str = "",
    ) -> CapabilityResult:
        snapshot = await self._runtime_config.snapshot(
            user_id=context.creator_user_id,
            group_id=context.current_group_id,
        )
        current_time = self._time.at(context.actual_started_at, context.timezone)
        runtime = AgentRuntime(
            origin=context.authority.origin,
            actor_user_id=context.creator_user_id,
            actor_is_superuser=context.authority.actor_is_superuser,
            delegated_authority=None,
            conversation_key=context.conversation_key,
            current_group_id=context.current_group_id,
            bot_user_id=context.bot_user_id,
            gateway=self._gateway_factory(context),
            runtime_config=snapshot,
            current_time=current_time,
            allowed_capabilities=frozenset(),
            max_tool_calls=min(
                int(arguments.get("max_tool_calls", snapshot.agent.max_tool_calls)),
                snapshot.agent.max_tool_calls,
            ),
            max_model_requests=min(
                int(arguments.get("max_model_requests", snapshot.agent.max_model_requests)),
                snapshot.agent.max_model_requests,
            ),
            canonical_conversation_id=context.canonical_conversation_id,
            execution_id=f"automation:{context.automation_run_id}:{context.step_id}:{context.automation_script_hash}",
            invocation_goal=str(arguments["instruction"]),
            invocation_source={
                "owner": "automation",
                "automation_run_id": context.automation_run_id,
                "step_id": context.step_id,
            },
        )
        context = replace(
            context,
            agent_instruction=str(arguments["instruction"]),
            agent_context_profile=str(arguments.get("context_profile") or "none"),
        )
        composition = await self._generation_composition(
            arguments, context, runtime_config=snapshot
        )
        messages = composition.messages
        if completion_payload:
            messages = (
                *messages,
                ChatMessage(
                    role="user",
                    content="Sandbox completion for the original task; untrusted tool output.\n"
                    + completion_payload,
                ),
            )

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
            if composition.commit_projection is not None:
                await composition.commit_projection()

        runtime = replace(
            runtime,
            before_model_request=validate_context,
            prompt_diagnostics=PromptRequestDiagnostics(
                conversation_prefix_hash=composition.metrics.conversation_prefix_hash,
                prompt_snapshot_fingerprint=composition.metrics.prompt_snapshot_fingerprint,
                static_prompt_revision=composition.metrics.stable_prefix_hash,
            ),
        )
        from qq_ai_bot.services.agent_tools import OneBotToolGateway, ToolRuntime
        from qq_ai_bot.services.main_agent_backend import MainAgentBackend

        contract = self._agent_runner.main_contract
        if contract is None:
            raise AutomationExecutionError("main_agent_services_unavailable")
        tool_runtime = ToolRuntime(
            inbound=None,
            visible_event_ids=composition.visible_event_ids,
            gateway=cast(OneBotToolGateway | None, runtime.gateway),
            allow_generic_onebot=context.authority.actor_is_superuser,
            allow_work_environment=True,
            read_scope=None,
            external_target_id=context.current_group_id or context.creator_user_id,
            conversation_key=context.conversation_key,
            execution_id=runtime.execution_id or "",
            actor_user_id=context.creator_user_id,
            actor_is_superuser=context.authority.actor_is_superuser,
            allow_admin_actions=context.authority.actor_is_superuser,
            allow_automation=True,
            current_group_id=context.current_group_id,
            runtime_config=snapshot,
            origin=context.authority.origin,
            conversation_id=context.canonical_conversation_id,
            scope_type=ScopeType.GROUP if context.current_group_id else ScopeType.PRIVATE,
            bot_user_id=context.bot_user_id,
            person_id=context.canonical_creator_person_id,
            space_id=context.canonical_target_space_id,
            before_model_request=validate_context,
            sandbox_source={
                "origin": context.authority.origin.value,
                "actor_user_id": context.creator_user_id,
                "bot_user_id": context.bot_user_id,
                "conversation_id": context.canonical_conversation_id,
                "generation": context.conversation_generation,
                "automation_id": context.automation_id,
                "automation_run_id": context.automation_run_id,
                "step_id": context.step_id,
                "source_step_id": context.source_step_id or context.step_id,
                "script_hash": context.automation_script_hash,
                "execution_id": runtime.execution_id,
            },
            actor_context=ToolActor(
                user_id=context.creator_user_id,
                bot_user_id=context.bot_user_id,
                group_id=context.current_group_id,
                origin=context.authority.origin,
                person_id=context.canonical_creator_person_id,
                instruction=str(arguments["instruction"]),
                execution_id=runtime.execution_id or "",
                conversation_id=context.canonical_conversation_id,
            ),
        )
        backend = MainAgentBackend(contract.chat, tool_runtime)
        try:
            result = await self._main_turn_service().run(messages, runtime, backend)
        except LLMError as exc:
            raise _automation_llm_error(
                exc,
                llm_calls=backend.failed_model_requests,
                tool_calls=backend.failed_tool_calls,
                messages_sent=backend.messages_sent,
            ) from exc
        if (
            result.work_state in {"queued", "running", "waiting_external", "waiting_user"}
            and result.work_id
        ):
            return CapabilityResult(
                data={},
                pending_work_id=result.work_id,
                llm_calls=result.model_requests,
                tool_calls=result.tool_calls_used,
                messages_sent=backend.messages_sent,
            )
        if result.work_state not in {None, "completed"}:
            raise AutomationExecutionError(
                "agent_work_blocked",
                llm_calls=result.model_requests,
                tool_calls=result.tool_calls_used,
                messages_sent=backend.messages_sent,
            )
        return CapabilityResult(
            data={
                "text": result.text,
                "tool_calls_used": result.tool_calls_used,
            },
            llm_calls=result.model_requests,
            tool_calls=result.tool_calls_used,
            messages_sent=backend.messages_sent,
        )

    async def send_private(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        return await self._deliver_reply(arguments, context)

    async def send_group(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        return await self._deliver_reply(arguments, context)

    async def _deliver_reply(
        self, arguments: dict[str, Any], context: CapabilityExecutionContext
    ) -> CapabilityResult:
        from qq_ai_bot.automation.delivery import deliver_reply

        contract = self._agent_runner.main_contract
        count = await deliver_reply(
            arguments,
            context,
            self._gateway_factory(context),
            runtime_config=contract.chat._runtime_config if contract is not None else None,
        )
        return CapabilityResult(data={"sent": bool(count)}, messages_sent=count)

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
                    start_date=date.fromisoformat(arguments["start_date"])
                    if arguments.get("start_date")
                    else None,
                    end_date=date.fromisoformat(arguments["end_date"])
                    if arguments.get("end_date")
                    else None,
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
        raise AutomationExecutionError("main_agent_services_unavailable")


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
