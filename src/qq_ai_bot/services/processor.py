"""Person-centric message admission, observation, and chat pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from typing import Protocol, cast

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.action_service import ActionRegistry
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import (
    RuntimeConfigSnapshot,
)
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.asr.service import ASRService, AudioInput
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
)
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot, runtime_conversation_key
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import (
    ChatImage,
    InboundMessage,
    OutboundMessage,
    OutboundSendReceipt,
)
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.emoji.collector import EmojiCollector
from qq_ai_bot.emoji.worker import EmojiWorker
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.ingress import CanonicalIngressResolver, IngressPreAdmit
from qq_ai_bot.identity.readers import canonical_group_policy, canonical_private_policy
from qq_ai_bot.llm.base import LLMConfigurationError, LLMEmptyResponseError, LLMError
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    GroupSettingsRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
    RelationshipJobRepository,
    RelationshipRepository,
)
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.plugin_host.direct_command_router import DirectCommandMatch
from qq_ai_bot.runtime.activation_outcome import (
    WorkActivationHandled,
    WorkRecoveryDeferred,
    classify_failure,
    failure_status_text,
)
from qq_ai_bot.runtime.keys import ResolvedMemoryScope
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    TurnObservationRecorder,
    bind_runtime_turn,
    build_turn_observation,
    new_runtime_turn_id,
    record_observation_safely,
)
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.services.attachment_inputs import AttachmentInputService
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.command_service import CommandExecution, CommandService
from qq_ai_bot.services.concurrency import ConcurrencyManager, RequestCancelledError
from qq_ai_bot.services.deduplication import DeduplicationService, build_event_key
from qq_ai_bot.services.effect_gate import (
    ConversationEffectGate,
    EffectGateTimeoutError,
    EffectPermitRejectedError,
)
from qq_ai_bot.services.media_resolver import OneBotMediaGateway
from qq_ai_bot.services.plugin_events import (
    LifecycleEventPublisher,
    content_free_turn_payload,
    publish_notification,
)
from qq_ai_bot.services.policies import (
    CommandName,
    EffectiveGroupPolicy,
    EffectivePrivatePolicy,
    evaluate_message,
)
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import LLMRelationshipEvaluator
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.renderer import sanitize_input
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    TurnInterruptedError,
    TurnSupersededError,
)
from qq_ai_bot.services.user_profiles import (
    UserProfileResolver,
    UserProfileService,
    sanitize_profile_name,
)
from qq_ai_bot.services.vision_service import (
    VisionProcessingError,
    VisionService,
    compact_visual_summary,
)
from qq_ai_bot.speech.preference_service import VoicePreferenceService
from qq_ai_bot.vision.models import VisualObservation
from yuki_plugin_sdk.events import EventName
from yuki_plugin_sdk.models import AdmissionSignal as SdkAdmissionSignal

logger = logging.getLogger(__name__)

_UNRESOLVED_ADMISSION = object()


class GroupRecoveryHandler(Protocol):
    """Adapter-proven control requests, separate from canonical chat admission."""

    def is_enable_request(self, message: InboundMessage) -> bool: ...

    async def enable(self, bot: object, message: InboundMessage) -> str | None: ...

    async def hint(self, bot: object, message: InboundMessage, reason: str) -> str | None: ...


def _observation_canonical_refs(
    message: InboundMessage,
    admitted: IngressPreAdmit | None,
) -> tuple[str | None, str | None, str | None]:
    """Project canonical hydrate IDs onto a runtime observation.

    Group rows store Space, not the speaker Person. Yuki / external_bot /
    system authors never receive ``canonical_person_id``.
    """

    conversation_id = message.conversation_id
    if message.scope_type is ScopeType.GROUP:
        return conversation_id, None, message.space_id
    author_kind = admitted.author_kind if admitted is not None else None
    person_id = message.person_id if author_kind == AuthorKind.PERSON.value else None
    return conversation_id, person_id, None


class AdmissionSignalProvider(Protocol):
    async def collect(
        self,
        *,
        message: InboundMessage,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[SdkAdmissionSignal, ...]: ...


class DirectPluginCommandResolver(Protocol):
    def match(self, text: str) -> DirectCommandMatch | None: ...


RATE_LIMIT_MESSAGE = "请求过于频繁，请稍后再试。"
IMAGE_WRITE_ISOLATION_MESSAGE = "图片或回复图片所在的轮次不会执行写入操作，请改用纯文本消息。"
IMAGE_FAILURE_MESSAGE = "这张图片暂时没有识别成功，可以重新发送一张更清晰的版本。"
IMAGE_RATE_LIMIT_MESSAGE = "图片理解请求过于频繁，请稍后再试。"
IMAGE_QUEUE_BUSY_MESSAGE = "当前图片识别任务较多，请稍后再试。"
IMAGE_DOWNLOAD_TIMEOUT_MESSAGE = "图片下载超时，请稍后重试；如果仍然失败，请重新发送原图。"
IMAGE_DOWNLOAD_FAILED_MESSAGE = "图片资源下载失败或已经失效，请重新发送原图。"
IMAGE_RESOURCE_QUERY_FAILED_MESSAGE = "QQ 网关未能取得图片资源，请重新发送原图。"
IMAGE_FORMAT_FAILED_MESSAGE = "图片文件无法解析，请尝试重新保存或转换为 PNG、JPEG 后发送。"
IMAGE_TOO_LARGE_MESSAGE = "图片尺寸、帧数或文件大小超过处理范围，请压缩后重新发送。"
IMAGE_PROVIDER_TIMEOUT_MESSAGE = "图片已取得，但视觉模型响应超时，请稍后再试。"
IMAGE_PROVIDER_FAILED_MESSAGE = "图片已取得，但视觉模型暂时不可用，请稍后再试。"
REPLY_IMAGE_UNAVAILABLE_MESSAGE = "回复中的图片资源已过期或无法读取，请重新发送原图。"
MENTION_ONLY_CONTEXT = "[用户在群聊中只 @ 了你，没有附带文字；请自然地回应这次招呼。]"


def _attachment_only_context(message: InboundMessage) -> str:
    """Describe an unparsed non-visual attachment to the Agent instead of replying from a stub."""

    labels = tuple(dict.fromkeys(attachment.kind.value for attachment in message.attachments))
    if not labels:
        return ""
    readable = {
        "audio": "语音",
        "video": "视频",
        "file": "文件",
        "forward": "合并转发",
        "card": "分享卡片",
        "unknown": "暂未识别的消息段",
    }
    descriptions = "、".join(readable.get(label, label) for label in labels)
    return (
        f"[用户发送了{descriptions}，但该消息没有可解析的正文。"
        "请结合当前对话自然回应；不要假装已经读取未提供的内容。]"
    )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Observable result used by adapters and integration tests."""

    handled: bool
    sent_messages: int = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class VisualTurnState:
    """One optional vision attempt, including a model-safe failure category."""

    observation: VisualObservation | None = None
    failed: bool = False
    error_code: str | None = None
    images: tuple[ChatImage, ...] = ()
    attachment_text: str = ""


def _vision_failure_message(error_code: str | None, *, reply_only: bool) -> str:
    """Return a useful pure-image failure without exposing provider internals."""

    if error_code == "rate_limited":
        return IMAGE_RATE_LIMIT_MESSAGE
    if error_code in {"queue_full", "queue_timeout"}:
        return IMAGE_QUEUE_BUSY_MESSAGE
    if error_code == "media_download_timeout":
        return IMAGE_DOWNLOAD_TIMEOUT_MESSAGE
    if error_code == "get_image_failed":
        return IMAGE_RESOURCE_QUERY_FAILED_MESSAGE
    if error_code in {
        "download_failed",
        "dns_failed",
        "private_url",
        "redirect_rejected",
        "empty_media",
    }:
        return REPLY_IMAGE_UNAVAILABLE_MESSAGE if reply_only else IMAGE_DOWNLOAD_FAILED_MESSAGE
    if error_code == "resource_unavailable" and reply_only:
        return REPLY_IMAGE_UNAVAILABLE_MESSAGE
    if error_code in {
        "too_large",
        "prepared_too_large",
        "decompression_bomb",
        "extreme_aspect_ratio",
        "too_many_frames",
    }:
        return IMAGE_TOO_LARGE_MESSAGE
    if error_code in {
        "invalid_base64",
        "invalid_media_type",
        "invalid_media",
        "invalid_dimensions",
        "unsupported_format",
        "corrupt_image",
        "frame_decode_failed",
    }:
        return IMAGE_FORMAT_FAILED_MESSAGE
    if error_code == "timeout":
        return IMAGE_PROVIDER_TIMEOUT_MESSAGE
    if error_code in {
        "connection_failed",
        "provider_unavailable",
        "authentication_failed",
        "provider_rejected",
        "invalid_response",
        "empty_response",
    }:
        return IMAGE_PROVIDER_FAILED_MESSAGE
    return IMAGE_FAILURE_MESSAGE


class MessageProcessor:
    """Admission → dedup → identity → ledger → memory → reply → relationship job."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedgerRepository,
        scoped_events: ScopedEventLedgerUnitOfWork,
        conversation_scopes: ConversationScopeRepository,
        conversation_rollups: ConversationRollupRepository,
        effect_gate: ConversationEffectGate,
        groups: GroupSettingsRepository,
        private_users: PrivateUserSettingsRepository,
        user_profiles: UserProfileService,
        chat: ChatService,
        deduplication: DeduplicationService,
        rate_limiter: SlidingWindowRateLimiter,
        concurrency: ConcurrencyManager,
        onebot_connected: Callable[[], bool],
        people: PeopleRepository | None = None,
        memories: MemoryFactService | None = None,
        memory_worker: MemoryWorker | None = None,
        relationships: RelationshipRepository | None = None,
        relationship_worker: RelationshipWorker | None = None,
        autonomous_groups: AutonomousGroupService | None = None,
        runtime_config: RuntimeConfigService | None = None,
        relationship_admin: RelationshipAdminService | None = None,
        memory_admin: MemoryAdminService | None = None,
        preference_admin: PreferenceAdminService | None = None,
        group_admin: GroupAdminService | None = None,
        private_access_admin: PrivateAccessAdminService | None = None,
        config_admin: ConfigAdminService | None = None,
        permission_catalog: PermissionCatalogService | None = None,
        vision_service: VisionService | None = None,
        asr_service: ASRService | None = None,
        attachment_inputs: AttachmentInputService | None = None,
        automation_service: AutomationService | None = None,
        automation_repository: AutomationRepository | None = None,
        automation_worker: AutomationWorker | None = None,
        command_service: CommandService | None = None,
        direct_plugin_commands: DirectPluginCommandResolver | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        admission_signals: AdmissionSignalProvider | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
        emoji_collector: EmojiCollector | None = None,
        emoji_worker: EmojiWorker | None = None,
        voice_preferences: VoicePreferenceService | None = None,
        turn_observations: TurnObservationRecorder | None = None,
        canonical_ingress: CanonicalIngressResolver | None = None,
        group_recovery: GroupRecoveryHandler | None = None,
        canonical_uow: CanonicalIngressUnitOfWork | None = None,
    ) -> None:
        database = ledger._database
        self._turn_observations = turn_observations
        self._settings = settings
        self._asr = asr_service
        self._scoped_events = scoped_events
        self._conversation_scopes = conversation_scopes
        self._conversation_rollups = conversation_rollups
        self._effect_gate = effect_gate
        self._groups = groups
        self._group_name_refreshes: OrderedDict[str, float] = OrderedDict()
        self._private_users = private_users
        self._user_profiles = user_profiles
        self._chat = chat
        self._deduplication = deduplication
        self._rate_limiter = rate_limiter
        self._concurrency = concurrency
        self._onebot_connected = onebot_connected
        self._ledger = ledger
        self._people = people or PeopleRepository(database)
        self._memories = memories or MemoryFactService(MemoryFactRepository(database))
        self._memory_worker = memory_worker or MemoryWorker(
            settings=settings,
            jobs=MemoryJobRepository(database),
            facts=self._memories,
            ledger=self._ledger,
            model_executor=chat._models,
            concurrency=concurrency,
        )
        self._relationships = relationships or RelationshipRepository(
            database,
            initial_affection=settings.relationship_initial_affection,
            initial_trust=settings.relationship_initial_trust,
            trust_cap_offset=settings.trust_affection_cap_offset,
            max_affection_auto_delta=settings.affection_max_auto_delta,
            max_trust_auto_delta=settings.trust_max_auto_delta,
        )
        self._relationship_worker = relationship_worker or RelationshipWorker(
            settings=settings,
            jobs=RelationshipJobRepository(
                database,
                max_attempts=settings.relationship_max_attempts,
            ),
            relationships=self._relationships,
            evaluator=LLMRelationshipEvaluator(
                settings=settings,
                model_executor=chat._models,
                concurrency=concurrency,
            ),
        )
        self._autonomous = autonomous_groups
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )
        self._turn_coordinator = turn_coordinator or chat._turn_coordinator
        self._admission_signals = admission_signals
        self._voice_preferences = voice_preferences
        audit = AdminAuditService(database)
        self._relationship_admin = relationship_admin or RelationshipAdminService(
            relationships=self._relationships,
            audit=audit,
            runtime_config=self._runtime_config,
        )
        self._memory_admin = memory_admin or MemoryAdminService(
            settings=settings,
            memories=self._memories,
            audit=audit,
        )
        self._preference_admin = preference_admin or PreferenceAdminService(
            settings=settings,
            memories=self._memories,
            audit=audit,
        )
        self._group_admin = group_admin or GroupAdminService(
            groups=self._groups,
            runtime_config=self._runtime_config,
            audit=audit,
        )
        self._private_access_admin = private_access_admin or PrivateAccessAdminService(
            private_users=self._private_users,
            audit=audit,
            runtime_config=self._runtime_config,
        )
        self._config_admin = config_admin or ConfigAdminService(self._runtime_config)
        self._permission_catalog = permission_catalog or PermissionCatalogService(
            settings=settings,
            config_registry=self._runtime_config.registry,
            action_registry=ActionRegistry(),
        )
        self._vision = vision_service
        self._native_images = attachment_inputs
        self._automation = automation_service
        self._automation_repository = automation_repository
        self._automation_worker = automation_worker
        self._commands = command_service or CommandService(
            settings=settings,
            rollups=conversation_rollups,
            people=self._people,
            memories=self._memories,
            concurrency=concurrency,
            onebot_connected=onebot_connected,
            runtime_config=self._runtime_config,
            relationship_admin=self._relationship_admin,
            memory_admin=self._memory_admin,
            preference_admin=self._preference_admin,
            group_admin=self._group_admin,
            private_access_admin=self._private_access_admin,
            config_admin=self._config_admin,
            permission_catalog=self._permission_catalog,
            vision_service=vision_service,
            automation_service=automation_service,
            automation_repository=automation_repository,
            automation_worker=automation_worker,
            turn_coordinator=self._turn_coordinator,
        )
        self._direct_plugin_commands = direct_plugin_commands
        self._event_publisher: LifecycleEventPublisher | None = None
        self._emoji_collector = emoji_collector
        self._emoji_worker = emoji_worker
        self._canonical_ingress = canonical_ingress
        self._group_recovery = group_recovery
        self._canonical_uow = canonical_uow
        if event_publisher is not None:
            self.set_event_publisher(event_publisher)

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        """Attach one notification bus to the complete direct-chat lifecycle."""

        self._event_publisher = publisher
        self._chat.set_event_publisher(publisher)

    async def handle(
        self,
        message: InboundMessage,
        sender: OutboundSender,
        profile_resolver: UserProfileResolver | None = None,
    ) -> ProcessResult:
        """Bind one opaque runtime turn correlation around real message handling.

        The correlation travels as ambient context to every persistence write
        point (model invocations, tool invocations, memory recall receipts).
        A content-free observation row is recorded only for turns that actually
        engaged those write points or failed unexpectedly; pure command /
        observe-only turns stay silent.
        """

        started = time.perf_counter()
        correlation = RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=TurnOrigin.USER_MESSAGE,
        )
        result: ProcessResult | None = None
        error_category: str | None = None
        working = message
        admitted: IngressPreAdmit | None = None
        with bind_runtime_turn(correlation):
            try:
                if self._group_recovery is not None and self._group_recovery.is_enable_request(
                    message
                ):
                    limited = await self._rate_limiter.check(
                        user_id=message.sender.user_id,
                        group_id=message.group_id,
                        category="command",
                    )
                    if not limited.allowed:
                        result = ProcessResult(False, reason="rate_limited")
                        return result
                    response = await self._group_recovery.enable(
                        getattr(sender, "bot", None), message
                    )
                    if response is not None:
                        await sender.send(OutboundMessage(text=response))
                        result = ProcessResult(True, sent_messages=1, reason="group_recovery")
                        return result
                if self._canonical_ingress is not None:
                    admitted = await self._canonical_ingress.pre_admit(
                        getattr(sender, "bot", None),
                        message,
                    )
                    if admitted is not None and admitted.dropped:
                        result = await self._handle_ingress_drop(message, sender, admitted.reason)
                        return result
                    if admitted is not None:
                        working = admitted.message
                result = await self._handle_admitted(
                    working,
                    sender,
                    profile_resolver,
                    admitted=admitted,
                )
                return result
            except BaseException as exc:
                error_category = type(exc).__name__
                raise
            finally:
                if correlation.touched or error_category is not None:
                    conversation_id, person_id, space_id = _observation_canonical_refs(
                        working,
                        admitted,
                    )
                    observation = build_turn_observation(
                        correlation,
                        scope_type=working.scope_type.value,
                        conversation_key=runtime_conversation_key(
                            identity=working.scope(),
                            inbound=working,
                        ),
                        admission_outcome=result.reason if result is not None else None,
                        handled=result.handled if result is not None else False,
                        sent_messages=result.sent_messages if result is not None else 0,
                        error_category=error_category,
                        total_latency_ms=int((time.perf_counter() - started) * 1000),
                        subject_user_id=working.sender.user_id,
                        group_id=working.group_id,
                        canonical_conversation_id=conversation_id,
                        canonical_person_id=person_id,
                        canonical_space_id=space_id,
                    )
                    await record_observation_safely(self._turn_observations, observation)

    async def _handle_admitted(
        self,
        message: InboundMessage,
        sender: OutboundSender,
        profile_resolver: UserProfileResolver | None = None,
        admitted: IngressPreAdmit | None | object = _UNRESOLVED_ADMISSION,
    ) -> ProcessResult:
        """Process one message without deriving authority from model-visible data."""

        started = time.perf_counter()
        if admitted is _UNRESOLVED_ADMISSION:
            admitted = None
            if self._canonical_ingress is not None:
                admitted = await self._canonical_ingress.pre_admit(
                    getattr(sender, "bot", None),
                    message,
                )
                if admitted is not None and admitted.dropped:
                    return await self._handle_ingress_drop(message, sender, admitted.reason)
                if admitted is not None:
                    message = admitted.message
        admitted = cast(IngressPreAdmit | None, admitted)
        yuki_account_ids = admitted.yuki_account_ids if admitted is not None else frozenset()
        await publish_notification(
            self._event_publisher,
            EventName.MESSAGE_NORMALIZED,
            {
                "message_id": message.message_id,
                "scope_type": message.scope_type.value,
                "has_text": bool(message.text),
                "attachment_count": len(message.attachments),
                "reply_attachment_count": len(message.reply_attachments),
                "mentions_bot": message.mentions_bot,
                "is_self_message": message.is_self_message,
            },
        )
        group_policy = await self._effective_group_policy(message.group_id)
        private_policy = await self._effective_private_policy(message)
        direct_match = (
            self._direct_plugin_commands.match(message.text)
            if self._direct_plugin_commands is not None
            else None
        )
        decision = evaluate_message(
            message,
            self._settings,
            group_policy=group_policy,
            private_policy=private_policy,
            direct_triggered=direct_match is not None,
            yuki_account_ids=yuki_account_ids,
        )
        if decision.reason == "bot_message":
            await self._publish_turn_rejected(message, decision.reason)
            return ProcessResult(False, reason=decision.reason)
        is_superuser = message.sender.user_id in self._settings.superusers
        admin_candidate = bool(
            is_superuser
            and direct_match is None
            and decision.command is None
            and (
                decision.should_respond
                or (
                    decision.reason == "group_disabled"
                    and (
                        message.mentions_bot
                        or message.text.strip().startswith(self._settings.ai_prefix)
                    )
                )
            )
        )
        should_observe = (
            message.scope_type is ScopeType.PRIVATE and decision.should_respond
        ) or bool(self._settings.observe_enabled_groups and group_policy and group_policy.enabled)
        if not decision.should_respond and not should_observe and not admin_candidate:
            await self._publish_turn_rejected(message, decision.reason)
            return ProcessResult(False, reason=decision.reason)

        identity = message.scope()
        coordinator_key = runtime_conversation_key(
            identity=identity,
            inbound=message,
            primary_alias=admitted.primary_alias if admitted is not None else None,
        )
        event_key = build_event_key(message, identity.key)
        repairing_dedup_gap = False
        if admitted is None and not await self._deduplication.claim(event_key):
            existing = await self._ledger.find_by_platform_message(
                bot_user_id=identity.bot_user_id,
                platform_message_id=message.message_id,
            )
            if existing is not None:
                await self._publish_turn_rejected(message, "duplicate")
                return ProcessResult(False, reason="duplicate")
            logger.warning(
                "processed_event_without_chat_event_repair scope_key=%s message_id=%s",
                identity.key,
                message.message_id,
            )
            repairing_dedup_gap = True

        runtime_snapshot = await self._runtime_config.snapshot(
            user_id=message.sender.user_id,
            group_id=message.group_id,
        )
        self._chat.configure_runtime_controls(runtime_snapshot)
        self._turn_coordinator.configure_policy(
            interrupt_autonomous_on_new_message=(
                runtime_snapshot.conversation_policy().interrupt_autonomous_on_new_message
            ),
        )
        configure_signal_timeout = getattr(self._admission_signals, "configure_timeout", None)
        if callable(configure_signal_timeout):
            configure_signal_timeout(runtime_snapshot.plugins.hook_timeout_seconds)
        configure_hook_timeout = getattr(
            self._event_publisher,
            "configure_default_timeout",
            None,
        )
        if callable(configure_hook_timeout):
            configure_hook_timeout(runtime_snapshot.plugins.hook_timeout_seconds)
        direct_turn = decision.should_respond or admin_candidate
        turn_token = await self._turn_coordinator.notify_message(
            coordinator_key,
            TurnOrigin.USER_MESSAGE,
            observation=not direct_turn,
            protect_from_observations=direct_turn,
            preserve_active=(
                self._settings.runtime_work_enabled
                and decision.command is None
                and direct_match is None
                and self._chat.work_is_active(coordinator_key)
            ),
        )
        has_visual_input = VisionService.has_visual_input(message) or (
            self._native_images is not None
            and any(
                a.kind.value in {"video", "file"}
                for a in (*message.attachments, *message.reply_attachments)
            )
        )
        image_blocks_command = bool(
            has_visual_input
            and (
                direct_match is not None
                or (
                    decision.command is not None
                    and self._commands.may_write(decision.command, decision.content)
                )
            )
            and not (
                direct_match is None
                and decision.command is CommandName.EMOJI
                and decision.content.strip().casefold().startswith("import")
            )
        )
        if decision.should_respond or admin_candidate:
            await publish_notification(
                self._event_publisher,
                EventName.MESSAGE_TRIGGERED,
                {
                    "message_id": message.message_id,
                    "scope_type": message.scope_type.value,
                    "trigger_reason": decision.reason,
                    "command": (
                        "plugin_direct"
                        if direct_match is not None
                        else (decision.command.value if decision.command is not None else None)
                    ),
                    "visual_input_present": has_visual_input,
                    "mentions_bot": message.mentions_bot,
                },
            )

        # forgetme is deliberately neither re-observed nor re-written to the ledger.
        if decision.command is CommandName.FORGETME and not image_blocks_command:
            profile = self._event_profile(message)
            return await self._handle_privacy_command(
                message,
                identity,
                profile,
                decision.content,
                sender,
                event_key,
                started,
            )

        await self._observe_group_metadata(
            message,
            group_policy,
            profile_resolver,
        )
        profile = await self._user_profiles.capture(message, profile_resolver)
        is_authorized_new = bool(
            decision.command is CommandName.NEW
            and (message.scope_type is ScopeType.PRIVATE or is_superuser)
        )
        if is_authorized_new:
            await self._turn_coordinator.cancel_running_before_boundary(coordinator_key)
            try:
                async with self._effect_gate.hold(
                    coordinator_key,
                    timeout_seconds=self._settings.conversation_effect_gate_timeout_seconds,
                ):
                    if admitted is not None and self._canonical_uow is not None:
                        switched = await self._canonical_uow.append_new_generation(
                            message,
                            admitted,
                        )
                    else:
                        switched = await self._scoped_events.append_new_generation_command(
                            scope=identity,
                            inbound=message,
                        )
            except EffectGateTimeoutError:
                sent = await self._send_text(
                    message,
                    sender,
                    "当前会话正在完成上一项操作，请稍后再试。",
                )
                return ProcessResult(True, int(sent), "new_effect_gate_timeout")
            record = switched.event
            created = switched.generation_changed
            scope_state = switched.scope
        else:
            if admitted is not None and self._canonical_uow is not None:
                appended = await self._canonical_uow.append_inbound(message, admitted)
            else:
                appended = await self._scoped_events.append_inbound(message)
            record = appended.event
            created = appended.created
            scope_state = appended.scope
            if repairing_dedup_gap and created:
                self._scoped_events.metrics.scoped_append_repairs += 1
        message = replace(message, source_event_id=record.id)
        if self._autonomous is not None and message.scope_type is ScopeType.GROUP:
            self._autonomous.observe_context(
                message,
                direct=bool(decision.should_respond or admin_candidate),
            )
        turn_snapshot = ConversationTurnSnapshot(
            scope_id=scope_state.id,
            scope_key=coordinator_key,
            generation=scope_state.generation,
            trigger_event_id=record.id,
            coordinator_version=turn_token.version,
            transport_scope_key=(identity.key if identity.key != coordinator_key else None),
        )
        is_explicit_emoji_import = bool(
            decision.command is CommandName.EMOJI
            and decision.content.strip().casefold().startswith("import")
        )
        if (
            self._emoji_collector is not None
            and message.attachments
            and not is_explicit_emoji_import
        ):
            media_gateway = (
                cast(OneBotMediaGateway, sender)
                if callable(getattr(sender, "call_api", None))
                else None
            )
            self._emoji_collector.submit(
                message,
                source_event_id=record.id,
                runtime=runtime_snapshot.emoji,
                gateway=media_gateway,
            )
            if self._emoji_worker is not None:
                self._emoji_worker.wake()

        if not decision.should_respond and not admin_candidate:
            if (
                message.scope_type is ScopeType.GROUP
                and group_policy is not None
                and runtime_snapshot.conversation_policy().autonomous_enabled
                and group_policy.autonomous_enabled
            ):
                if self._autonomous is not None:
                    self._autonomous.observe(message, profile, sender, turn_token)
            return ProcessResult(False, reason="group_observed")

        category = "command" if direct_match is not None or decision.command is not None else "chat"
        rate = await self._rate_limiter.check(
            user_id=message.sender.user_id,
            group_id=message.group_id,
            category=category,
        )
        if not rate.allowed:
            sent = await self._send_text(
                message,
                sender,
                RATE_LIMIT_MESSAGE,
                turn_snapshot=turn_snapshot,
            )
            return ProcessResult(True, int(sent), f"{rate.scope}_rate_limited")

        if direct_match is not None:
            if image_blocks_command:
                sent = await self._send_text(
                    message,
                    sender,
                    IMAGE_WRITE_ISOLATION_MESSAGE,
                    turn_snapshot=turn_snapshot,
                )
                return ProcessResult(True, int(sent), "image_write_isolated")
            try:
                return await self._handle_direct_plugin_command(
                    direct_match,
                    message,
                    identity,
                    sender,
                    event_key,
                    started,
                    turn_snapshot,
                )
            except (TurnInterruptedError, TurnSupersededError, WorkConflict):
                return ProcessResult(True, reason="turn_interrupted")

        if decision.command is not None:
            if image_blocks_command:
                sent = await self._send_text(
                    message,
                    sender,
                    IMAGE_WRITE_ISOLATION_MESSAGE,
                    turn_snapshot=turn_snapshot,
                )
                return ProcessResult(True, int(sent), "image_write_isolated")
            try:
                return await self._handle_command(
                    decision.command,
                    message,
                    identity,
                    profile,
                    decision.content,
                    sender,
                    event_key,
                    started,
                    turn_snapshot,
                )
            except (TurnInterruptedError, TurnSupersededError, WorkConflict):
                return ProcessResult(True, reason="turn_interrupted")

        audio = AudioInput()
        if ASRService.has_audio(message):
            try:
                async with self._turn_coordinator.track(turn_token, "admission"):
                    if record.audio_transcript:
                        audio = AudioInput(transcript=record.audio_transcript)
                    elif self._asr is not None:
                        gateway = (
                            cast(OneBotMediaGateway, sender)
                            if callable(getattr(sender, "call_api", None))
                            else None
                        )
                        audio = await self._asr.prepare(message, gateway)
                    else:
                        audio = AudioInput(error="not_configured")
                    if not self._turn_coordinator.is_current(turn_token):
                        return ProcessResult(True, reason="turn_interrupted")
                    if audio.transcript and not record.audio_transcript:
                        stored = await self._ledger.set_audio_transcript(
                            record.id,
                            audio.transcript,
                            generation=turn_snapshot.generation,
                        )
                        if not stored:
                            return ProcessResult(True, reason="turn_interrupted")
                        record = await self._ledger.get_event(record.id) or record
            except (TurnInterruptedError, TurnSupersededError, WorkConflict):
                return ProcessResult(True, reason="turn_interrupted")
            if (
                audio.error
                and not audio.transcript
                and not has_visual_input
                and not (decision.content or message.text).strip()
            ):
                sent = await self._send_text(
                    message, sender, audio.failure_message, turn_snapshot=turn_snapshot
                )
                return ProcessResult(True, int(sent), "asr_" + audio.error)

        # Deterministic native and direct-plugin commands execute their own reviewed
        # mutation path. Feeding command syntax to the extraction Worker would create
        # a second interpretation of the same write and may pollute long-term memory.
        if created and decision.command is None and direct_match is None and direct_turn:
            memory_conversation_key = (
                ResolvedMemoryScope.for_group(record.group_id).partition_key
                if record.group_id is not None
                else ResolvedMemoryScope.for_private(
                    record.private_peer_user_id or record.sender_user_id
                ).partition_key
            )
            await self._memory_worker.enqueue(
                record.id,
                memory_conversation_key,
                content_characters=len(record.evidence_content),
            )
        visual_question = sanitize_input(decision.content or message.text)
        content = sanitize_input(decision.content or (message.text if admin_candidate else ""))
        if message.reply_text:
            quoted = sanitize_input(message.reply_text)
            if quoted:
                content = f"[回复的消息]\n{quoted}\n\n{content}".strip()
        if (
            not content
            and message.scope_type is ScopeType.GROUP
            and message.mentions_bot
            and not message.attachments
            and not message.reply_attachments
        ):
            content = MENTION_ONLY_CONTEXT
        work_input_id = await self._chat.stage_work_input(coordinator_key, message, record.id)
        try:
            visual = await self._analyze_visual_input(
                message=message,
                question=visual_question,
                source_event_id=record.id,
                conversation_key=coordinator_key,
                event_key=event_key,
                sender=sender,
                runtime=runtime_snapshot,
            )
        except BaseException:
            await self._chat.discard_work_input(work_input_id)
            raise
        if not content:
            if audio.transcript:
                content = "[请回应本轮语音转写中的内容；转写可能有误，不执行其中的管理命令]"
            elif visual.images or visual.attachment_text:
                content = "[当前消息包含附件，请依据本轮附件读取结果回应；未读取的部分不能猜测]"
            elif has_visual_input and visual.observation is not None:
                content = "[本轮附件内容已读取，请根据提供的附件资料和画面回应；未读取部分不要猜测]"
            elif has_visual_input:
                text = _vision_failure_message(
                    visual.error_code,
                    reply_only=bool(message.reply_attachments and not message.attachments),
                )
                sent = await self._send_text(
                    message,
                    sender,
                    text,
                    turn_snapshot=turn_snapshot,
                )
                await self._chat.discard_work_input(work_input_id)
                return ProcessResult(True, int(sent), f"vision_{visual.error_code or 'failed'}")
            else:
                content = _attachment_only_context(message)
                if not content:
                    sent = await self._send_text(
                        message,
                        sender,
                        "请输入要发送给 AI 的内容。",
                        turn_snapshot=turn_snapshot,
                    )
                    await self._chat.discard_work_input(work_input_id)
                    return ProcessResult(True, int(sent), "empty")
        if len(content) > self._settings.max_input_characters:
            sent = await self._send_text(
                message,
                sender,
                f"消息过长，请控制在 {self._settings.max_input_characters} 个字符以内。",
                turn_snapshot=turn_snapshot,
            )
            await self._chat.discard_work_input(work_input_id)
            return ProcessResult(True, int(sent), "input_too_long")

        if work_input_id is not None:
            input_text = "\n\n".join(
                part
                for part in (
                    content,
                    visual.attachment_text,
                    audio.context,
                )
                if part
            )
            if await self._chat.ready_work_input(
                coordinator_key,
                work_input_id,
                input_text,
                visual.images,
            ):
                return ProcessResult(True, reason="work_input_queued")

        await publish_notification(
            self._event_publisher,
            EventName.TURN_ADMITTED,
            content_free_turn_payload(
                origin=TurnOrigin.USER_MESSAGE.value,
                scope_type=message.scope_type.value,
                conversation_key=coordinator_key,
                reason=decision.reason,
            ),
        )
        result: ProcessResult
        try:
            sent_count = await self._chat.handle_turn(
                message,
                identity,
                profile,
                content,
                sender,
                runtime_snapshot=runtime_snapshot,
                visual_observation=visual.observation,
                native_images=visual.images,
                attachment_text="\n\n".join(
                    part for part in (visual.attachment_text, audio.context) if part
                ),
                visual_input_present=has_visual_input,
                visual_failure=visual.failed,
                turn_token=turn_token,
                turn_snapshot=turn_snapshot,
            )
        except (TurnInterruptedError, TurnSupersededError, WorkConflict):
            result = ProcessResult(True, reason="turn_interrupted")
        except (WorkActivationHandled, WorkRecoveryDeferred):
            result = ProcessResult(True, reason="work_recovery")
        except RequestCancelledError:
            result = ProcessResult(True, reason="cancelled")
        except LLMConfigurationError:
            sent = await self._send_text(
                message,
                sender,
                "AI 服务尚未配置，请联系管理员。",
                turn_snapshot=turn_snapshot,
            )
            result = ProcessResult(True, int(sent), "llm_not_configured")
        except LLMEmptyResponseError:
            sent = await self._send_text(
                message,
                sender,
                "AI 返回了空内容，请稍后重试。",
                turn_snapshot=turn_snapshot,
            )
            result = ProcessResult(True, int(sent), "empty_llm_response")
        except LLMError as exc:
            logger.warning("llm_failure exception_category=%s", type(exc).__name__)
            sent = await self._send_text(
                message,
                sender,
                failure_status_text(classify_failure(exc)),
                turn_snapshot=turn_snapshot,
            )
            result = ProcessResult(True, int(sent), "llm_failure")
        except ValidationError as exc:
            logger.error(
                "turn_validation_failure exception_category=%s",
                type(exc).__name__,
                exc_info=exc,
            )
            sent = await self._send_text(
                message,
                sender,
                failure_status_text(classify_failure(exc)),
                turn_snapshot=turn_snapshot,
            )
            result = ProcessResult(True, int(sent), "validation_failure")
        except (OSError, RuntimeError, TypeError) as exc:
            logger.error("message_send_or_storage_failure", exc_info=exc)
            result = ProcessResult(True, reason="send_or_storage_failure")
        except Exception as exc:
            logger.error(
                "turn_internal_failure exception_category=%s",
                type(exc).__name__,
                exc_info=exc,
            )
            sent = await self._send_text(
                message,
                sender,
                failure_status_text(classify_failure(exc)),
                turn_snapshot=turn_snapshot,
            )
            result = ProcessResult(True, int(sent), "internal_failure")
        else:
            if created and sent_count > 0:
                try:
                    await self._relationship_worker.enqueue(
                        trigger_event_id=record.id,
                        user_id=message.sender.user_id,
                        conversation_key=coordinator_key,
                    )
                except (SQLAlchemyError, OSError, RuntimeError, ValueError) as exc:
                    logger.warning(
                        "relationship_enqueue_failed exception_category=%s",
                        type(exc).__name__,
                    )

            self._log_result(
                event_key,
                identity,
                message,
                handler="chat",
                started=started,
                success=True,
                conversation_key=coordinator_key,
            )
            result = ProcessResult(True, sent_count, "chat")
        await publish_notification(
            self._event_publisher,
            EventName.TURN_CLOSED,
            content_free_turn_payload(
                origin=TurnOrigin.USER_MESSAGE.value,
                scope_type=message.scope_type.value,
                conversation_key=coordinator_key,
                outcome=result.reason,
                handled=result.handled,
                sent_messages=result.sent_messages,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
        )
        return result

    async def _analyze_visual_input(
        self,
        *,
        message: InboundMessage,
        question: str,
        source_event_id: int,
        conversation_key: str,
        event_key: str,
        sender: OutboundSender,
        runtime: RuntimeConfigSnapshot,
    ) -> VisualTurnState:
        has_video = any(
            a.kind.value in {"video", "file", "forward"}
            for a in (*message.attachments, *message.reply_attachments)
        )
        if not VisionService.has_visual_input(message) and not (
            self._native_images is not None and has_video
        ):
            return VisualTurnState()
        if self._native_images is not None and (self._native_images.images_enabled or has_video):
            gateway = (
                cast(OneBotMediaGateway, sender)
                if callable(getattr(sender, "call_api", None))
                else None
            )
            try:
                prepared = await self._native_images.prepare(message, runtime.vision, gateway)
                return VisualTurnState(images=prepared.images, attachment_text=prepared.documents)
            except Exception as exc:
                logger.warning(
                    "native_image_prepare_failed exception_category=%s code=%s",
                    type(exc).__name__,
                    getattr(exc, "code", "resource_unavailable"),
                )
                code = getattr(exc, "code", "resource_unavailable")
                allowed = {
                    "too_large",
                    "video_limit",
                    "unsupported_video",
                    "invalid_video",
                    "video_unavailable",
                    "document_unreadable",
                    "frame_budget",
                    "rate_limited",
                    "queue_full",
                    "image_capability_unavailable",
                    "encrypted_document",
                    "archive_expansion_limit",
                    "unsafe_archive",
                    "unsupported_archive",
                    "unsupported_document",
                    "binary_document",
                }
                safe_code = code if code in allowed else "resource_unavailable"
                return VisualTurnState(
                    failed=True,
                    error_code=safe_code,
                    attachment_text=(
                        f"[附件读取失败，原因={safe_code}。未获得附件内容；说明此限制，不能声称已经查看或根据附件猜测。]"
                        if has_video
                        else ""
                    ),
                )
        if self._vision is None or not self._settings.vision_enabled:
            return VisualTurnState(failed=True, error_code="not_configured")

        resolved_source_event_id = source_event_id
        if (
            not any(attachment.kind.value == "image" for attachment in message.attachments)
            and message.reply_to_message_id
        ):
            replied_event = await self._ledger.find_by_platform_message(
                bot_user_id=message.bot_user_id or "unknown-bot",
                platform_message_id=message.reply_to_message_id,
            )
            if replied_event is not None:
                resolved_source_event_id = replied_event.id
        gateway = (
            cast(OneBotMediaGateway, sender)
            if callable(getattr(sender, "call_api", None))
            else None
        )
        try:
            observation = await self._vision.analyze(
                message,
                question=question,
                runtime=runtime.vision,
                gateway=gateway,
                source_event_id=resolved_source_event_id,
                conversation_key=conversation_key,
            )
            await self._ledger.set_visual_summary(
                resolved_source_event_id,
                compact_visual_summary(observation),
            )
            return VisualTurnState(observation=observation)
        except VisionProcessingError as exc:
            logger.warning(
                "vision_turn_failed event_key=%s error_category=%s",
                event_key,
                exc.code,
            )
            return VisualTurnState(failed=True, error_code=exc.code)
        except Exception as exc:
            # Optional visual failures must not escape the OneBot event handler.
            # Exception text can contain signed media URLs, so only log its type.
            logger.error(
                "vision_turn_failed event_key=%s error_category=unexpected_%s",
                event_key,
                type(exc).__name__,
            )
            return VisualTurnState(failed=True, error_code="internal_error")

    async def _observe_group_metadata(
        self,
        message: InboundMessage,
        policy: EffectiveGroupPolicy | None,
        resolver: UserProfileResolver | None,
    ) -> None:
        if message.group_id is None or policy is None or not policy.enabled:
            return
        group_name = ""
        method = getattr(resolver, "resolve_group_name", None)
        now = time.monotonic()
        refresh_at = self._group_name_refreshes.get(message.group_id, float("-inf"))
        if callable(method) and now - refresh_at >= 300:
            # Reserve before awaiting: concurrent messages must not fan out lookups.
            # Bound both successful and failed refresh attempts; never erase a name
            # when a gateway returns no metadata. Network I/O precedes DB writes.
            self._group_name_refreshes[message.group_id] = now
            self._group_name_refreshes.move_to_end(message.group_id)
            while len(self._group_name_refreshes) > 256:
                self._group_name_refreshes.popitem(last=False)
            resolve_name = cast(Callable[[str], Awaitable[str]], method)
            try:
                async with asyncio.timeout(3):
                    group_name = sanitize_profile_name(await resolve_name(message.group_id))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning(
                    "group_name_resolve_failed exception_category=%s",
                    type(exc).__name__,
                )
        await self._groups.observe(
            message.group_id,
            name=group_name,
            enabled_if_new=policy.enabled,
        )

    async def _effective_group_policy(self, group_id: str | None) -> EffectiveGroupPolicy | None:
        if group_id is None:
            return None
        async with self._ledger._database.sessions() as session:
            return await canonical_group_policy(session, group_id)

    async def _effective_private_policy(
        self, message: InboundMessage
    ) -> EffectivePrivatePolicy | None:
        if message.scope_type is not ScopeType.PRIVATE:
            return None
        async with self._ledger._database.sessions() as session:
            return await canonical_private_policy(session, message.sender.user_id)

    async def _handle_ingress_drop(
        self, message: InboundMessage, sender: OutboundSender, reason: str
    ) -> ProcessResult:
        limited = await self._rate_limiter.check(
            user_id=message.sender.user_id, group_id=message.group_id, category="ingress_drop"
        )
        if limited.allowed and self._group_recovery is not None:
            hint = await self._group_recovery.hint(getattr(sender, "bot", None), message, reason)
            if hint is not None:
                await sender.send(OutboundMessage(text=hint))
                return ProcessResult(True, sent_messages=1, reason="group_route_paused")
        if limited.allowed:
            logger.info("canonical_ingress_dropped reason=%s", reason)
        return ProcessResult(False, reason=reason)

    async def _handle_command(
        self,
        command: CommandName,
        message: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        argument: str,
        sender: OutboundSender,
        event_key: str,
        started: float,
        turn_snapshot: ConversationTurnSnapshot | None = None,
    ) -> ProcessResult:
        async def execute() -> CommandExecution:
            return await self._commands.execute(
                command,
                message,
                identity,
                profile,
                argument,
                started,
                gateway=(
                    cast(OneBotMediaGateway, sender)
                    if callable(getattr(sender, "call_api", None))
                    else None
                ),
            )

        if turn_snapshot is None:
            execution = await execute()
            return await self._deliver_command_execution(
                execution=execution,
                message=message,
                identity=identity,
                sender=sender,
                event_key=event_key,
                started=started,
                handler=f"command_{command.value}",
                turn_snapshot=None,
            )
        try:
            async with self._effect_gate.permit(
                turn_snapshot,
                validate=self._validate_turn_snapshot,
                timeout_seconds=self._settings.conversation_effect_gate_timeout_seconds,
            ):
                execution = await execute()
                # A single issued permit linearizes the command mutation and its
                # confirmation. `/ai stop` intentionally advances coordinator
                # version while this already-issued permit remains valid.
                return await self._deliver_command_execution(
                    execution=execution,
                    message=message,
                    identity=identity,
                    sender=sender,
                    event_key=event_key,
                    started=started,
                    handler=f"command_{command.value}",
                    turn_snapshot=None,
                )
        except (EffectGateTimeoutError, EffectPermitRejectedError) as exc:
            raise TurnSupersededError("command effect permit was rejected") from exc

    async def _handle_privacy_command(
        self,
        message: InboundMessage,
        identity: ConversationScope,
        profile: UserProfileSnapshot,
        argument: str,
        sender: OutboundSender,
        event_key: str,
        started: float,
    ) -> ProcessResult:
        """Cancel old work, acquire sorted scope gates, then delete in one DB transaction."""

        scopes = await self._people.affected_conversation_scopes(message.sender.user_id)
        for scope in scopes:
            await self._turn_coordinator.cancel_interruptible(scope.key)
        try:
            async with AsyncExitStack() as stack:
                for scope in scopes:
                    await stack.enter_async_context(
                        self._effect_gate.hold(
                            scope.key,
                            timeout_seconds=(
                                self._settings.conversation_effect_gate_timeout_seconds
                            ),
                        )
                    )
                execution = await self._commands.execute(
                    CommandName.FORGETME,
                    message,
                    identity,
                    profile,
                    argument,
                    started,
                )
        except EffectGateTimeoutError:
            execution = CommandExecution(
                "隐私删除暂时无法安全取得全部会话边界，请稍后重试。",
                record_reply=False,
            )
        return await self._deliver_command_execution(
            execution=execution,
            message=message,
            identity=identity,
            sender=sender,
            event_key=event_key,
            started=started,
            handler="command_forgetme",
            turn_snapshot=None,
        )

    async def _handle_direct_plugin_command(
        self,
        match: DirectCommandMatch,
        message: InboundMessage,
        identity: ConversationScope,
        sender: OutboundSender,
        event_key: str,
        started: float,
        turn_snapshot: ConversationTurnSnapshot,
    ) -> ProcessResult:
        try:
            async with self._effect_gate.permit(
                turn_snapshot,
                validate=self._validate_turn_snapshot,
                timeout_seconds=self._settings.conversation_effect_gate_timeout_seconds,
            ):
                execution = await self._commands.execute_direct_plugin(message, identity, match)
                return await self._deliver_command_execution(
                    execution=execution,
                    message=message,
                    identity=identity,
                    sender=sender,
                    event_key=event_key,
                    started=started,
                    handler="command_plugin_direct",
                    turn_snapshot=None,
                )
        except (EffectGateTimeoutError, EffectPermitRejectedError) as exc:
            raise TurnSupersededError("plugin command effect permit was rejected") from exc

    async def _deliver_command_execution(
        self,
        *,
        execution: CommandExecution,
        message: InboundMessage,
        identity: ConversationScope,
        sender: OutboundSender,
        event_key: str,
        started: float,
        handler: str,
        turn_snapshot: ConversationTurnSnapshot | None,
    ) -> ProcessResult:
        sent = (
            await self._send_outbound(
                message,
                sender,
                execution.outbound,
                turn_snapshot=turn_snapshot,
            )
            if execution.outbound is not None
            else await self._send_text(
                message,
                sender,
                execution.text,
                record=execution.record_reply,
                turn_snapshot=turn_snapshot,
            )
        )
        if sent and execution.outbound is not None:
            if turn_snapshot is None:
                await self._commands.mark_media_sent(execution.outbound)
            else:
                try:
                    async with self._effect_gate.permit(
                        turn_snapshot,
                        validate=self._validate_turn_snapshot,
                        timeout_seconds=(self._settings.conversation_effect_gate_timeout_seconds),
                    ):
                        await self._commands.mark_media_sent(execution.outbound)
                except (EffectGateTimeoutError, EffectPermitRejectedError):
                    logger.warning("media_sent_followup_rejected_by_generation_fence")
        self._log_result(
            event_key,
            identity,
            message,
            handler=handler,
            started=started,
            success=sent,
            conversation_key=runtime_conversation_key(
                identity=identity,
                inbound=message,
                turn=turn_snapshot,
            ),
        )
        return ProcessResult(True, int(sent), handler)

    @staticmethod
    def _event_profile(message: InboundMessage) -> UserProfileSnapshot:
        return UserProfileSnapshot(
            user_id=message.sender.user_id,
            scope_type=message.scope_type,
            nickname=sanitize_profile_name(message.sender.nickname),
            group_id=message.group_id,
            group_card=sanitize_profile_name(message.sender.group_card),
        )

    async def _send_text(
        self,
        inbound: InboundMessage,
        sender: OutboundSender,
        text: str,
        *,
        record: bool = True,
        turn_snapshot: ConversationTurnSnapshot | None = None,
    ) -> bool:
        try:
            outbound = OutboundMessage(text=text)
            if turn_snapshot is None:
                await self._send_text_effect(inbound, sender, outbound, record=record)
            else:
                async with self._effect_gate.permit(
                    turn_snapshot,
                    validate=self._validate_turn_snapshot,
                    timeout_seconds=self._settings.conversation_effect_gate_timeout_seconds,
                ):
                    await self._send_text_effect(inbound, sender, outbound, record=record)
            return True
        except (
            EffectGateTimeoutError,
            EffectPermitRejectedError,
            OSError,
            RuntimeError,
            TypeError,
        ) as exc:
            logger.error("outbound_send_failed", exc_info=exc)
            return False

    async def _send_text_effect(
        self,
        inbound: InboundMessage,
        sender: OutboundSender,
        outbound: OutboundMessage,
        *,
        record: bool,
    ) -> None:
        receipt = await sender.send(outbound)
        if not isinstance(receipt, OutboundSendReceipt):
            raise TypeError("outbound sender returned no delivery receipt")
        if record:
            await self._chat.record_confirmed_outbound(inbound, outbound, receipt)
        else:
            await publish_notification(
                self._event_publisher,
                EventName.REPLY_SENT,
                {
                    "trigger_message_id": inbound.message_id,
                    "platform_message_id": receipt.platform_message_id,
                    "scope_type": inbound.scope_type.value,
                    "character_count": len(outbound.text),
                    "delivered": True,
                    "recorded": False,
                },
            )

    async def _send_outbound(
        self,
        inbound: InboundMessage,
        sender: OutboundSender,
        outbound: OutboundMessage,
        *,
        turn_snapshot: ConversationTurnSnapshot | None = None,
    ) -> bool:
        try:
            if turn_snapshot is None:
                await self._send_outbound_effect(inbound, sender, outbound)
            else:
                async with self._effect_gate.permit(
                    turn_snapshot,
                    validate=self._validate_turn_snapshot,
                    timeout_seconds=self._settings.conversation_effect_gate_timeout_seconds,
                ):
                    await self._send_outbound_effect(inbound, sender, outbound)
            return True
        except (
            EffectGateTimeoutError,
            EffectPermitRejectedError,
            OSError,
            RuntimeError,
            TypeError,
        ) as exc:
            logger.error("outbound_media_send_failed", exc_info=exc)
            return False

    async def _send_outbound_effect(
        self,
        inbound: InboundMessage,
        sender: OutboundSender,
        outbound: OutboundMessage,
    ) -> None:
        receipt = await sender.send(outbound)
        if not isinstance(receipt, OutboundSendReceipt):
            raise TypeError("outbound sender returned no delivery receipt")
        await self._chat.record_confirmed_outbound(inbound, outbound, receipt)

    async def _validate_turn_snapshot(self, snapshot: ConversationTurnSnapshot) -> bool:
        return self._turn_coordinator.version_matches(
            snapshot.scope_key,
            snapshot.coordinator_version,
        ) and await self._conversation_scopes.generation_matches(
            snapshot.scope_id,
            snapshot.generation,
            scope_key=snapshot.scope_key,
        )

    async def _publish_turn_rejected(self, message: InboundMessage, reason: str) -> None:
        await publish_notification(
            self._event_publisher,
            EventName.TURN_REJECTED,
            content_free_turn_payload(
                origin=TurnOrigin.USER_MESSAGE.value,
                scope_type=message.scope_type.value,
                conversation_key=runtime_conversation_key(
                    identity=message.scope(),
                    inbound=message,
                ),
                reason=reason,
            ),
        )

    @staticmethod
    def _log_result(
        event_key: str,
        identity: ConversationScope,
        message: InboundMessage,
        *,
        handler: str,
        started: float,
        success: bool,
        conversation_key: str | None = None,
    ) -> None:
        owner_key = conversation_key or identity.key
        logger.info(
            "message_handled",
            extra={
                "event_key": event_key,
                "conversation_hash": hashlib.sha256(owner_key.encode()).hexdigest()[:16],
                "message_type": message.scope_type.value,
                "handler": handler,
                "total_latency_seconds": round(time.perf_counter() - started, 4),
                "success": success,
            },
        )
