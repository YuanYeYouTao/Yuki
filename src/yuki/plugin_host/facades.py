"""Trusted invocation-bound implementations of the public PluginContext Facades.

The classes in this module form the security boundary between trusted local
plugin code and Yuki's application services.  They are API isolation rather
than an operating-system sandbox: every turn-sensitive call re-derives its
authority from a Host-created :class:`PluginInvocation` stored in a ContextVar.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

from pydantic import PrivateAttr
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.adapters.onebot.sender import parse_onebot_send_receipt
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.control_resolution import ControlAccess
from yuki.admin.models import ControlAuditRef, RuntimeConfigSnapshot
from yuki.automation.authority import DelegatedAuthority
from yuki.automation.models import AutomationRecord, TurnOrigin
from yuki.automation.service import AutomationService
from yuki.conversation.scope import runtime_conversation_key
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import (
    ChatImage,
    InboundMessage,
    mention_snapshots,
    snapshot_mention_segments,
)
from yuki.domain.tool_actor import ToolActor
from yuki.gateway.media import PlatformMediaGateway
from yuki.llm.base import LLMEmptyResponseError
from yuki.memory.context import MemoryContextService
from yuki.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryInvalidationReason,
    MemoryScopeType,
    MemoryTargetRole,
)
from yuki.memory.models import MemoryEntityTarget, MemoryEvidenceCreate
from yuki.memory.service import MemoryFactService
from yuki.memory.validation import normalize_memory_text
from yuki.persistence.repositories import (
    EventLedgerRepository,
    GroupSettingsRepository,
    PeopleRepository,
)
from yuki.persistence.unit_of_work import optional_session
from yuki.plugin_host.audit import PluginAuditService
from yuki.plugin_host.canonical_projection import (
    projection_from_event,
    projection_from_inbound,
)
from yuki.plugin_host.config import BoundConfigFacade
from yuki.plugin_host.direct_delivery import (
    DirectDeliveryScope,
    delivery_unknown,
    prepare_delivery,
    validate_delivery_source,
)
from yuki.plugin_host.event_bus import PluginEventBus
from yuki.plugin_host.http_client import BoundHttpFacade
from yuki.plugin_host.main_turn import run_plugin_main_turn
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki.plugin_host.ownership import PluginOwnershipError
from yuki.plugin_host.secrets import BoundSecretsFacade
from yuki.plugin_host.session_facade import BoundAgentSessionFacade
from yuki.plugin_host.storage import BoundStorageFacade
from yuki.services.admin.memory_admin import MemoryAdminService, MemoryPreferenceTrigger
from yuki.services.agent_runner import (
    AgentRunner,
    AgentRunResult,
    AgentRuntime,
)
from yuki.services.native_media import NativeMediaPreparer
from yuki.services.renderer import sanitize_model_output
from yuki.services.vision_service import VisionProcessingError, VisionService
from yuki.social.models import OperationStatus, SocialError, SocialReceipt
from yuki.social.repository import SocialOperationRepository
from yuki.vision.models import VisualObservation
from yuki.web.base import WebSearchError, WebSearchProvider, normalize_public_url
from yuki.web.models import WebSearchRequest
from yuki_plugin_sdk.api import DEFAULT_FEATURES
from yuki_plugin_sdk.context import (
    AgentFacade,
    AutomationFacade,
    ConfigFacade,
    GroupFacade,
    HttpFacade,
    MediaFacade,
    MemoryFacade,
    MessageFacade,
    NotificationFacade,
    OneBotFacade,
    PeopleFacade,
    PluginContext,
    PluginEventPublisher,
    SchedulerFacade,
    SecretsFacade,
    StorageFacade,
    VisionFacade,
    WebFacade,
)
from yuki_plugin_sdk.errors import FeatureUnavailableError, PluginPermissionError
from yuki_plugin_sdk.events import EventEnvelope
from yuki_plugin_sdk.features import FeatureRegistry
from yuki_plugin_sdk.models import (
    BackgroundTargetGrantView,
    CurrentMessage,
    JsonValue,
    MediaArtifactHandle,
    NotificationPublishReceipt,
    NotificationTarget,
    PublishNotificationRequest,
)
from yuki_plugin_sdk.permissions import PluginPermission

if TYPE_CHECKING:
    from yuki.social.service import SocialService
from yuki_plugin_sdk.results import PluginResult
from yuki_plugin_sdk.sessions import (
    AgentSession,
    AgentSessionFacade,
    AgentSessionRunResult,
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
)

_QQ_ID = re.compile(r"^[1-9][0-9]{4,19}$")
_MUSIC_RESOURCE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TASK_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
_MUSIC_PROVIDERS = {
    "163": "163",
    "kugou": "kugou",
    "kuwo": "kuwo",
    "migu": "migu",
    "netease": "163",
    "qq": "qq",
}
_SENSITIVE_KEYS = frozenset(
    {"access_token", "authorization", "cookie", "password", "secret", "token"}
)
_READ_ONEBOT_ACTIONS = frozenset(
    {
        "can_send_image",
        "can_send_record",
        "check_url_safely",
        "get_cookies",
        "get_credentials",
        "get_csrf_token",
        "get_essence_msg_list",
        "get_forward_msg",
        "get_friend_list",
        "get_friend_msg_history",
        "get_group_at_all_remain",
        "get_group_file_system_info",
        "get_group_file_url",
        "get_group_honor_info",
        "get_group_info",
        "get_group_list",
        "get_group_member_info",
        "get_group_member_list",
        "get_group_msg_history",
        "get_group_root_files",
        "get_image",
        "get_login_info",
        "get_msg",
        "get_online_clients",
        "get_record",
        "get_status",
        "get_stranger_info",
        "get_unidirectional_friend_list",
        "get_version_info",
    }
)


class OneBotFacadeGateway(PlatformMediaGateway, Protocol):
    """The event-bound subset used without exposing the actual Bot object."""

    async def call_api(self, action: str, params: dict[str, Any]) -> Any: ...


class ToolRuntimeProjection(Protocol):
    """Minimal trusted fields consumed from the existing Agent ToolRuntime."""

    inbound: InboundMessage | None
    origin: TurnOrigin

    def require_actor(self) -> ToolActor: ...


ConfigFacadeFactory = Callable[[str | None, str | None], BoundConfigFacade]
AgentSessionFacadeFactory = Callable[["PluginInvocation"], BoundAgentSessionFacade]


class _HostWorkResult(PluginResult):
    """Keep the original Host result outside the public business payload."""

    _work: AgentRunResult = PrivateAttr()


class _HostSocialResult(PluginResult):
    _receipt: SocialReceipt = PrivateAttr()


def _agent_result(
    result: AgentRunResult, capabilities: Iterable[str], *, reason: str | None = None
) -> _HostWorkResult:
    projected = _HostWorkResult(
        data={
            "text": result.text,
            "tool_calls_used": result.tool_calls_used,
            "model_requests": result.model_requests,
            "capabilities": sorted(capabilities),
            "state": result.work_state or "completed",
            "work_id": result.work_id,
            "pending": result.work_state
            in {"queued", "running", "waiting_external", "waiting_user", "suspended"},
            "reason": reason,
        }
    )
    projected._work = result
    return projected


def _owned_work_result(row: Mapping[str, Any], source: Mapping[str, Any]) -> _HostWorkResult:
    checkpoint = json.loads(row["checkpoint_json"])
    result = _agent_result(
        AgentRunResult(
            text=checkpoint.get("sync_result") or "",
            tool_calls_used=0,
            model_requests=0,
            work_id=row["id"],
            work_state=row["state"],
        ),
        source.get("allowed_tools", ()),
        reason=checkpoint.get("reason", row.get("reason")),
    )
    # These are query displays, not new dispatches by this callback.
    result.data["model_requests"] = row["model_requests"]
    result.data["tool_calls_used"] = row["tool_calls"]
    return result


@dataclass(frozen=True, slots=True)
class PluginInvocation:
    """Host-only authority captured from one real turn.

    There is deliberately no ``actor_is_superuser`` input.  Privileged Facades
    prove its canonical Person against the Host's current granted identities.
    """

    plugin_id: str
    origin: TurnOrigin
    actor_user_id: str
    bot_user_id: str
    actor_person_id: str | None = None
    actor: ToolActor | None = field(default=None, repr=False)
    execution_source: dict[str, Any] | None = field(default=None, repr=False)
    inbound: InboundMessage | None = field(default=None, repr=False)
    gateway: OneBotFacadeGateway | None = field(default=None, repr=False)
    runtime_config: RuntimeConfigSnapshot | None = field(default=None, repr=False)
    delegated_authority: DelegatedAuthority | None = field(default=None, repr=False)
    source_event_id: int | None = None
    legacy_conversation_key: str | None = None
    person_id: str | None = None
    space_id: str | None = None
    conversation_id: str | None = None
    presence_id: str | None = None
    platform: str = ""
    # Host-only persisted callback key, never supplied through the public SDK.
    delivery_identity: str | None = None

    def __post_init__(self) -> None:
        if not self.plugin_id or not self.bot_user_id:
            raise ValueError("plugin invocation identity fields cannot be empty")
        if self.inbound is not None:
            object.__setattr__(self, "platform", self.inbound.platform)
            if self.inbound.sender.user_id != self.actor_user_id:
                raise ValueError("plugin invocation actor must match the real inbound sender")
            if self.inbound.bot_user_id and self.inbound.bot_user_id != self.bot_user_id:
                raise ValueError("plugin invocation bot must match the real inbound event")
        if self.inbound is not None:
            anchor = self.inbound.source_event_id
            if anchor is not None:
                if self.source_event_id is not None and self.source_event_id != anchor:
                    raise ValueError("plugin invocation event anchor mismatch")
                object.__setattr__(self, "source_event_id", anchor)
            projection = projection_from_inbound(self.inbound)
            object.__setattr__(self, "actor_person_id", self.inbound.person_id)
            object.__setattr__(self, "person_id", projection.person_id)
            object.__setattr__(self, "space_id", projection.space_id)
            object.__setattr__(self, "conversation_id", projection.conversation_id)
            object.__setattr__(self, "presence_id", projection.presence_id)
            if projection.conversation_key:
                object.__setattr__(self, "legacy_conversation_key", projection.conversation_key)
        elif self.actor is not None:
            object.__setattr__(self, "platform", self.actor.platform)

    def scope(self) -> ConversationScope:
        if self.current_group_id is not None:
            return ConversationScope.group(
                self.bot_user_id,
                self.current_group_id,
                platform=self.platform,
                presence_id=self.presence_id,
                conversation_id=self.conversation_id,
                thread_id=self.inbound.thread_id if self.inbound else None,
            )
        return ConversationScope.private(
            self.bot_user_id,
            self.actor_user_id,
            platform=self.platform,
            presence_id=self.presence_id,
            conversation_id=self.conversation_id,
        )

    @property
    def current_group_id(self) -> str | None:
        if self.actor is not None:
            return self.actor.group_id
        if self.inbound is not None:
            return self.inbound.group_id
        if self.delegated_authority is not None:
            return self.delegated_authority.current_group_id
        return None

    @property
    def has_visual_input(self) -> bool:
        inbound = self.inbound
        return bool(inbound and (inbound.attachments or inbound.reply_attachments))

    @property
    def conversation_key(self) -> str:
        if self.legacy_conversation_key:
            return self.legacy_conversation_key
        if self.inbound is not None:
            return runtime_conversation_key(
                identity=self.inbound.scope(),
                inbound=self.inbound,
            )
        if self.conversation_id:
            return f"conversation:{self.conversation_id}"
        return self.scope().key


@dataclass(slots=True)
class PluginFacadeServices:
    """Private dependency bundle; never returned through PluginContext."""

    approval_revision: str = ""
    bot_display_name: str = "Yuki"
    ledger: EventLedgerRepository | None = None
    social: SocialService | None = None
    people: PeopleRepository | None = None
    groups: GroupSettingsRepository | None = None
    memories: MemoryFactService | None = None
    memory_context: MemoryContextService | None = None
    memory_admin: MemoryAdminService | None = None
    runtime_config: RuntimeConfigService | None = None
    agent_runner: AgentRunner | None = None
    web_provider: WebSearchProvider | None = None
    vision: VisionService | None = None
    automation: AutomationService | None = None
    storage: BoundStorageFacade | None = None
    config_factory: ConfigFacadeFactory | None = None
    secrets: BoundSecretsFacade | None = None
    http: BoundHttpFacade | None = None
    agent_sessions_factory: AgentSessionFacadeFactory | None = None
    events: PluginEventBus | None = None
    audit: PluginAuditService | None = None
    notifications: PluginNotificationRepository | None = None
    notification_wake: Callable[[], None] | None = None
    media_artifacts: PluginMediaArtifactStore | None = None
    native_media: NativeMediaPreparer | None = None
    media_storage_mb: int = 10


@dataclass(frozen=True, slots=True)
class _OutboundLedgerMessage:
    """Sanitized outbound message metadata persisted after a confirmed send."""

    scope_type: ScopeType
    content: str
    segments: tuple[dict[str, Any], ...]
    group_id: str | None = None
    private_peer_user_id: str | None = None


_CURRENT_INVOCATION: ContextVar[PluginInvocation | None] = ContextVar(
    "yuki_plugin_invocation",
    default=None,
)
_CURRENT_DELIVERY: ContextVar[DirectDeliveryScope | None] = ContextVar(
    "yuki_plugin_delivery", default=None
)


class _InvocationBinding:
    """A binding usable by both synchronous and asynchronous context managers."""

    def __init__(self, context: HostPluginContext, invocation: PluginInvocation) -> None:
        self._context = context
        self._invocation = invocation
        self._token: Token[PluginInvocation | None] | None = None
        self._delivery_token: Token[DirectDeliveryScope | None] | None = None

    def __enter__(self) -> HostPluginContext:
        if self._token is not None:
            raise RuntimeError("plugin invocation binding cannot be entered twice")
        self._context._validate_binding(self._invocation)
        delivery = DirectDeliveryScope(_delivery_identity(self._context, self._invocation))
        self._token = _CURRENT_INVOCATION.set(self._invocation)
        self._delivery_token = _CURRENT_DELIVERY.set(delivery)
        return self._context

    def __exit__(self, *_exc: object) -> None:
        self._reset()

    async def __aenter__(self) -> HostPluginContext:
        return self.__enter__()

    async def __aexit__(self, *_exc: object) -> None:
        self._reset()

    def _reset(self) -> None:
        if self._token is None:
            raise RuntimeError("plugin invocation binding was not entered")
        _CURRENT_INVOCATION.reset(self._token)
        assert self._delivery_token is not None
        _CURRENT_DELIVERY.reset(self._delivery_token)
        self._delivery_token = None
        self._token = None


class HostPluginContext:
    """Concrete PluginContext with no public core-object escape hatch."""

    __slots__ = (
        "_agent",
        "_agent_sessions",
        "_approved_permissions",
        "_automation",
        "_closed",
        "_config",
        "_events",
        "_features",
        "_groups",
        "_http",
        "_logger",
        "_media",
        "_memory",
        "_messages",
        "_notifications",
        "_onebot",
        "_people",
        "_plugin_id",
        "_scheduler",
        "_secrets",
        "_services",
        "_storage",
        "_vision",
        "_web",
    )

    def __init__(
        self,
        *,
        plugin_id: str,
        approved_permissions: Iterable[PluginPermission],
        services: PluginFacadeServices | None = None,
        features: Iterable[str] = DEFAULT_FEATURES,
        scheduler_task_limit: int = 4,
    ) -> None:
        if not plugin_id:
            raise ValueError("plugin_id cannot be empty")
        if scheduler_task_limit < 0:
            raise ValueError("scheduler_task_limit must be non-negative")
        self._plugin_id = plugin_id
        self._closed = False
        self._approved_permissions = frozenset(approved_permissions)
        self._services = services or PluginFacadeServices()
        self._logger = logging.getLogger(f"yuki.plugin.{plugin_id}")
        self._features = FeatureRegistry(features)
        self._messages = _MessageFacade(self)
        self._people = _PeopleFacade(self)
        self._groups = _GroupFacade(self)
        self._memory = _MemoryFacade(self)
        self._agent = _AgentFacade(self)
        self._agent_sessions = _AgentSessionsFacade(self)
        self._web = _WebFacade(self)
        self._http = _HttpFacade(self)
        self._vision = _VisionFacade(self)
        self._media = _MediaFacade(self)
        self._notifications = _NotificationFacade(self)
        self._automation = _AutomationFacade(self)
        self._config = _ConfigFacade(self)
        self._secrets = _SecretsFacade(self)
        self._storage = _StorageFacade(self)
        self._scheduler = _SchedulerFacade(self, task_limit=scheduler_task_limit)
        self._onebot = _OneBotFacade(self)
        self._events = _EventPublisher(self)

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    @property
    def current(self) -> CurrentMessage | None:
        if PluginPermission.MESSAGE_CURRENT_READ not in self._approved_permissions:
            return None
        invocation = self._invocation(required=False)
        return _current_message(invocation.inbound) if invocation is not None else None

    @property
    def features(self) -> FeatureRegistry:
        return self._features

    @property
    def messages(self) -> MessageFacade:
        return self._messages

    @property
    def people(self) -> PeopleFacade:
        return self._people

    @property
    def groups(self) -> GroupFacade:
        return self._groups

    @property
    def memory(self) -> MemoryFacade:
        return self._memory

    @property
    def agent(self) -> AgentFacade:
        return self._agent

    @property
    def agent_sessions(self) -> AgentSessionFacade:
        return self._agent_sessions

    @property
    def web(self) -> WebFacade:
        return self._web

    @property
    def http(self) -> HttpFacade:
        return self._http

    @property
    def vision(self) -> VisionFacade:
        return self._vision

    @property
    def media(self) -> MediaFacade:
        return self._media

    @property
    def notifications(self) -> NotificationFacade:
        return self._notifications

    @property
    def automation(self) -> AutomationFacade:
        return self._automation

    @property
    def config(self) -> ConfigFacade:
        return self._config

    @property
    def secrets(self) -> SecretsFacade:
        return self._secrets

    @property
    def storage(self) -> StorageFacade:
        return self._storage

    @property
    def scheduler(self) -> SchedulerFacade:
        return self._scheduler

    @property
    def onebot(self) -> OneBotFacade:
        return self._onebot

    @property
    def events(self) -> PluginEventPublisher:
        return self._events

    def bind(self, invocation: PluginInvocation) -> _InvocationBinding:
        """Bind one trusted invocation for the duration of a plugin callback."""

        return _InvocationBinding(self, invocation)

    async def close_host_resources(self) -> None:
        """Stop tasks created through the Host scheduler during plugin shutdown."""

        self._closed = True
        from yuki.plugin_host.main_turn import close_plugin_main_tasks

        await close_plugin_main_tasks(self.plugin_id)
        await self._scheduler.stop()

    def invocation_scope(
        self, plugin_id: str, runtime: ToolRuntimeProjection
    ) -> _InvocationBinding:
        """Adapter helper accepting the existing ToolRuntime without importing it."""

        inbound = runtime.inbound
        actor = runtime.require_actor()
        invocation = PluginInvocation(
            plugin_id=plugin_id,
            origin=TurnOrigin(runtime.origin),
            actor_user_id=actor.user_id,
            actor_person_id=actor.person_id,
            actor=actor,
            execution_source=cast(dict[str, Any] | None, getattr(runtime, "sandbox_source", None)),
            bot_user_id=actor.bot_user_id,
            inbound=inbound,
            legacy_conversation_key=getattr(runtime, "conversation_key", None),
            person_id=getattr(runtime, "person_id", None),
            space_id=getattr(runtime, "space_id", None),
            conversation_id=actor.conversation_id,
            presence_id=actor.presence_id,
            source_event_id=actor.event_id,
            runtime_config=cast(
                RuntimeConfigSnapshot | None,
                getattr(runtime, "runtime_config", None),
            ),
            delegated_authority=cast(
                DelegatedAuthority | None,
                getattr(runtime, "delegated_authority", None),
            ),
        )
        return self.bind(invocation)

    def _validate_binding(self, invocation: PluginInvocation) -> None:
        if invocation.plugin_id != self._plugin_id:
            raise PluginPermissionError("plugin invocation belongs to another plugin")

    def _invocation(self, *, required: bool = True) -> PluginInvocation | None:
        invocation = _CURRENT_INVOCATION.get()
        if invocation is not None and invocation.plugin_id != self._plugin_id:
            invocation = None
        if required and invocation is None:
            raise PluginPermissionError("plugin facade requires a trusted invocation")
        return invocation

    async def _prepare_selected_media(
        self, handles: tuple[MediaArtifactHandle, ...], *, tool_name: str
    ) -> tuple[ChatImage, ...]:
        """Host bridge for an explicitly returned SDK handle under this invocation."""
        invocation = self._require(PluginPermission.MEDIA_ARTIFACT_CREATE)
        assert invocation is not None
        store = _require_service(self._services.media_artifacts, "plugin media artifacts")
        preparer = _require_service(self._services.native_media, "native media")
        images: list[ChatImage] = []
        for handle in dict.fromkeys(handles):
            original, data = await store.read_owned(
                plugin_id=self.plugin_id, handle_id=handle.handle_id
            )
            if (
                original.sha256 != handle.sha256
                or original.byte_size != handle.byte_size
                or original.content_type != handle.content_type
                or _aware_media(original.expires_at) != _aware_media(handle.expires_at)
            ):
                raise PluginPermissionError("selected media artifact version does not match")
            prepared = await asyncio.to_thread(preparer.prepare_image, data, source="tool")
            images.extend(
                replace(
                    image,
                    plugin_id=self.plugin_id,
                    plugin_media_handle=original.handle_id,
                    plugin_tool_name=tool_name,
                    plugin_approval_revision=self._services.approval_revision,
                    content_hash=original.sha256,
                    expires_at=_aware_media(original.expires_at).isoformat(),
                )
                for image in prepared
            )
            preparer.check_budget(tuple(images))
        await self._validate_selected_media(tuple(images))
        return tuple(images)

    async def _validate_selected_media(self, images: tuple[ChatImage, ...]) -> None:
        """Recheck the original owned dependency, never an archived derivative alone."""
        self._require(PluginPermission.MEDIA_ARTIFACT_CREATE)
        store = _require_service(self._services.media_artifacts, "plugin media artifacts")
        versions = {}
        for image in images:
            if image.plugin_id != self.plugin_id or not image.plugin_media_handle:
                raise PluginPermissionError("selected media belongs to another plugin")
            if image.plugin_media_handle not in versions:
                original, _data = await store.read_owned(
                    plugin_id=self.plugin_id, handle_id=image.plugin_media_handle
                )
                versions[image.plugin_media_handle] = original
            original = versions[image.plugin_media_handle]
            if (
                original.sha256 != image.content_hash
                or _aware_media(original.expires_at).isoformat() != image.expires_at
                or image.plugin_approval_revision != self._services.approval_revision
            ):
                raise PluginPermissionError("selected media artifact version does not match")

    def _require(
        self,
        permission: PluginPermission,
        *,
        mutation: bool = False,
        send: bool = False,
        require_invocation: bool = True,
    ) -> PluginInvocation | None:
        if self._closed:
            raise PluginPermissionError("plugin context is closed")
        if permission not in self._approved_permissions:
            raise PluginPermissionError(f"plugin lacks {permission.value} permission")
        invocation = self._invocation(required=require_invocation)
        if invocation is None:
            return None
        return invocation

    async def _require_privileged(self, invocation: PluginInvocation) -> None:
        direct_user_event = (
            invocation.inbound is not None and invocation.source_event_id is not None
        )
        delegated_task = bool(
            invocation.actor is not None
            and invocation.actor.principal_kind == "person"
            and invocation.actor.person_id == invocation.actor_person_id
            and invocation.actor.execution_id
        )
        if not await self._is_real_superuser(invocation) or not (
            direct_user_event or delegated_task
        ):
            raise PluginPermissionError(
                "operation requires direct or delegated Person administrator authority"
            )

    def _require_any(
        self,
        permissions: tuple[PluginPermission, ...],
        *,
        mutation: bool = False,
        send: bool = False,
        require_invocation: bool = True,
    ) -> tuple[PluginPermission, PluginInvocation | None]:
        permission = next(
            (item for item in permissions if item in self._approved_permissions),
            None,
        )
        if permission is None:
            names = ", ".join(item.value for item in permissions)
            raise PluginPermissionError(f"plugin lacks one of: {names}")
        return permission, self._require(
            permission,
            mutation=mutation,
            send=send,
            require_invocation=require_invocation,
        )

    async def _is_real_superuser(
        self, invocation: PluginInvocation, *, session: AsyncSession | None = None
    ) -> bool:
        from yuki.identity.authority import person_is_superuser

        ledger = self._services.ledger
        if ledger is None:
            return False
        async with optional_session(ledger._database, session, write=False) as reader:
            return await person_is_superuser(reader, invocation.actor_person_id)

    def _control_access(self) -> ControlAccess:
        people = self._services.people
        if people is None:
            raise PluginPermissionError("control principal cannot be resolved")
        return ControlAccess(people._database)

    async def _require_user_scope(
        self,
        invocation: PluginInvocation,
        user_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> str:
        normalized = str(UUID(user_id))
        mentioned = (
            invocation.inbound.mentioned_person_ids
            if invocation.inbound
            else invocation.actor.mentioned_person_ids
            if invocation.actor
            else ()
        )
        ledger = self._services.ledger
        same_actor = normalized == invocation.actor_person_id
        same_mentioned = normalized in mentioned
        if ledger is not None and not same_actor and not same_mentioned:
            from yuki.identity.person_identity import same_person_id

            async with optional_session(ledger._database, session, write=False) as reader:
                same_actor = bool(
                    invocation.actor_person_id
                    and await same_person_id(reader, normalized, invocation.actor_person_id)
                )
                for person_id in mentioned:
                    if await same_person_id(reader, normalized, person_id):
                        same_mentioned = True
                        break
        if (
            not same_actor
            and not same_mentioned
            and not await self._is_real_superuser(invocation, session=session)
        ):
            raise PluginPermissionError("target user is outside the current real turn")
        return normalized

    async def _require_group_scope(
        self,
        invocation: PluginInvocation,
        group_id: str,
        *,
        session: AsyncSession | None = None,
    ) -> str:
        normalized = str(UUID(group_id))
        if normalized != invocation.space_id and not await self._is_real_superuser(
            invocation, session=session
        ):
            raise PluginPermissionError("target group is outside the current real turn")
        return normalized

    async def _audit(
        self,
        invocation: PluginInvocation,
        *,
        operation: str,
        permission: PluginPermission,
        success: bool,
        error_category: str | None = None,
    ) -> None:
        audit = self._services.audit
        if audit is None:
            return
        await audit.record(
            plugin_id=self._plugin_id,
            actor_user_id=invocation.actor_user_id,
            operation=operation,
            permission=permission.value,
            success=success,
            error_category=error_category,
        )

    async def _run_audited(
        self,
        invocation: PluginInvocation,
        *,
        operation: str,
        permission: PluginPermission,
        runner: Callable[[], Awaitable[PluginResult]],
    ) -> PluginResult:
        """Run one Facade operation and emit exactly one redacted audit event."""

        try:
            result = await runner()
        except Exception as exc:
            await self._audit(
                invocation,
                operation=operation,
                permission=permission,
                success=False,
                error_category=_facade_error_category(exc),
            )
            raise
        try:
            await self._audit(
                invocation,
                operation=operation,
                permission=permission,
                success=result.ok,
                error_category=_result_error_category(result),
            )
        except Exception as exc:
            if not isinstance(result, _HostSocialResult):
                raise
            self._logger.warning("plugin_send_audit_failed category=%s", type(exc).__name__)
        return result


class _MessageFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def get_current(self) -> CurrentMessage | None:
        invocation = self._host._require(PluginPermission.MESSAGE_CURRENT_READ)
        assert invocation is not None
        return _current_message(invocation.inbound)

    async def get_reply(self) -> CurrentMessage | None:
        invocation = self._host._require(PluginPermission.MESSAGE_REPLY_READ)
        assert invocation is not None
        inbound = invocation.inbound
        ledger = self._host._services.ledger
        if (
            inbound is None
            or inbound.reply_to_event_id is None
            or invocation.conversation_id is None
            or inbound.conversation_id != invocation.conversation_id
            or ledger is None
        ):
            return None
        record = await ledger.get_reply_event(
            inbound.reply_to_event_id,
            conversation_id=invocation.conversation_id,
            current_generation_only=True,
        )
        return _record_message(record) if record is not None else None

    async def get_recent(self, limit: int = 20) -> tuple[CurrentMessage, ...]:
        invocation = self._host._require(PluginPermission.MESSAGE_HISTORY_READ)
        assert invocation is not None
        ledger = _require_service(self._host._services.ledger, "message history")
        scope = invocation.scope()
        rows = await ledger.list_scope_recent(
            scope,
            limit=_bounded_limit(limit),
            message_only=True,
        )
        return tuple(_record_message(row) for row in rows)

    async def search_history(
        self,
        query: str,
        limit: int = 20,
    ) -> tuple[CurrentMessage, ...]:
        invocation = self._host._require(PluginPermission.MESSAGE_HISTORY_READ)
        assert invocation is not None
        ledger = _require_service(self._host._services.ledger, "message history")
        keyword = _bounded_text(query, maximum=400, field_name="query")
        rows = await ledger.search(
            keyword=keyword,
            scope=invocation.scope(),
            limit=_bounded_limit(limit),
            message_only=True,
        )
        return tuple(_record_message(row) for row in rows)

    async def send_text(self, text: str) -> PluginResult:
        invocation = self._host._invocation()
        assert invocation is not None
        return await self._send(
            "group" if invocation.space_id else "private",
            invocation.space_id or cast(str, invocation.actor_person_id),
            text=text,
            permission=PluginPermission.MESSAGE_GROUP_SEND
            if invocation.space_id
            else PluginPermission.MESSAGE_PRIVATE_SEND,
            operation="message.send_text",
        )

    async def send_private(self, user_id: str, text: str) -> PluginResult:
        return await self._send(
            "private",
            user_id,
            text=text,
            permission=PluginPermission.MESSAGE_PRIVATE_SEND,
            operation="message.send_private",
        )

    async def send_group(self, group_id: str, text: str) -> PluginResult:
        return await self._send(
            "group",
            group_id,
            text=text,
            permission=PluginPermission.MESSAGE_GROUP_SEND,
            operation="message.send_group",
        )

    async def send_image(
        self,
        *,
        target_type: str,
        target_id: str,
        media_reference: str,
    ) -> PluginResult:
        return await self._send(
            target_type,
            target_id,
            media_reference=media_reference,
            permission=PluginPermission.MESSAGE_MEDIA_SEND,
            operation="message.send_image",
        )

    async def _send(
        self,
        target_type: str,
        target_id: str,
        *,
        permission: PluginPermission,
        operation: str,
        text: str = "",
        media_reference: str | None = None,
    ) -> PluginResult:
        invocation = self._host._invocation()
        assert invocation is not None

        async def send() -> PluginResult:
            checked = self._host._require(permission, send=True)
            assert checked is not None
            if target_type == "group":
                target = await self._host._require_group_scope(checked, target_id)
            elif target_type == "private":
                target = await self._host._require_user_scope(checked, target_id)
            else:
                raise ValueError("target_type must be private or group")
            return await _send_social(
                self._host,
                checked,
                target_type,
                target,
                text=_outbound_text(text) if text else "",
                media_reference=media_reference,
            )

        return await self._host._run_audited(
            invocation,
            operation=operation,
            permission=permission,
            runner=send,
        )


class _PeopleFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def get_current(self) -> Mapping[str, JsonValue] | None:
        invocation = self._host._require(PluginPermission.PERSON_CURRENT_READ)
        assert invocation is not None
        return await self._get_profile(invocation, cast(str, invocation.actor_person_id))

    async def get(self, user_id: str) -> Mapping[str, JsonValue] | None:
        invocation = self._host._require(PluginPermission.PERSON_READ)
        assert invocation is not None
        target = await self._host._require_user_scope(invocation, user_id)
        return await self._get_profile(invocation, target)

    async def list_aliases(self, user_id: str) -> tuple[str, ...]:
        invocation = self._host._require(PluginPermission.PERSON_ALIAS_READ)
        assert invocation is not None
        target = await self._host._require_user_scope(invocation, user_id)
        people = _require_service(self._host._services.people, "people")
        return await people.aliases(target, limit=20)

    async def add_alias(self, user_id: str, alias: str) -> PluginResult:
        invocation = self._host._require(
            PluginPermission.PERSON_ALIAS_WRITE,
            mutation=True,
        )
        assert invocation is not None
        await self._host._require_user_scope(invocation, user_id)
        _bounded_text(alias, maximum=128, field_name="alias")
        return _unavailable("person alias writes require a reviewed alias business service")

    async def _get_profile(
        self,
        invocation: PluginInvocation,
        user_id: str,
    ) -> Mapping[str, JsonValue] | None:
        people = _require_service(self._host._services.people, "people")
        row = await people.get(person_id=user_id, space_id=invocation.space_id)
        if row is None:
            return None
        return {
            "user_id": row.user_id,
            "nickname": row.nickname,
            "group_id": row.group_id,
            "group_card": row.group_card,
            "display_name": row.display_name,
        }


class _GroupFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def get_current(self) -> Mapping[str, JsonValue] | None:
        invocation = self._host._require(PluginPermission.GROUP_CURRENT_READ)
        assert invocation is not None
        if invocation.space_id is None:
            return None
        return await self._get(invocation.space_id)

    async def get(self, group_id: str) -> Mapping[str, JsonValue] | None:
        invocation = self._host._require(PluginPermission.GROUP_READ)
        assert invocation is not None
        target = await self._host._require_group_scope(invocation, group_id)
        return await self._get(target)

    async def list_members(
        self,
        group_id: str,
        limit: int = 100,
    ) -> tuple[Mapping[str, JsonValue], ...]:
        invocation = self._host._require(PluginPermission.GROUP_MEMBERS_READ)
        assert invocation is not None
        target = await self._host._require_group_scope(invocation, group_id)
        social = _require_service(self._host._services.social, "social")
        assert invocation.conversation_id is not None
        actor = _invocation_actor(invocation)
        from yuki.social.service import SocialContext

        result = await social.execute(
            "get_group_members",
            {"target_id": target, "limit": _bounded_limit(limit, maximum=100)},
            SocialContext(
                f"plugin:{self._host.plugin_id}",
                "members",
                invocation.conversation_id,
                space_id=invocation.space_id,
                actor=actor,
                inbound=invocation.inbound,
                trigger_event_id=invocation.source_event_id,
                origin=invocation.origin.value,
                presence_id=invocation.presence_id,
            ),
        )
        return tuple(result.get("items", ()))

    async def get_settings(self, group_id: str) -> Mapping[str, JsonValue]:
        invocation = self._host._require(PluginPermission.GROUP_READ)
        assert invocation is not None
        target = await self._host._require_group_scope(invocation, group_id)
        row = await self._get(target)
        return row or {}

    async def set_setting(
        self,
        group_id: str,
        key: str,
        value: JsonValue,
    ) -> PluginResult:
        invocation = self._host._require(
            PluginPermission.GROUP_SETTINGS_WRITE,
            mutation=True,
        )
        assert invocation is not None
        await self._host._require_privileged(invocation)
        target = await self._host._require_group_scope(invocation, group_id)
        groups = _require_service(self._host._services.groups, "group settings")
        if not isinstance(value, bool):
            raise ValueError("group setting value must be boolean")
        if key == "enabled":
            row = await groups.set_enabled(target, value)
        elif key == "autonomous_enabled":
            row = await groups.set_autonomous_enabled(target, value)
        else:
            return _unavailable("only enabled and autonomous_enabled are reviewed")
        await self._host._audit(
            invocation,
            operation=f"group.set.{key}",
            permission=PluginPermission.GROUP_SETTINGS_WRITE,
            success=True,
        )
        return PluginResult(data={"group": _group_record(row)})

    async def _get(self, group_id: str) -> Mapping[str, JsonValue] | None:
        groups = _require_service(self._host._services.groups, "group settings")
        row = await groups.get(group_id)
        return _group_record(row) if row is not None else None


class _MemoryFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def list_person(
        self,
        user_id: str,
        limit: int = 20,
    ) -> tuple[Mapping[str, JsonValue], ...]:
        invocation = self._host._require(PluginPermission.MEMORY_PERSON_READ)
        assert invocation is not None
        memories = _require_service(self._host._services.memories, "memory")
        async with memories.repository.transaction(read_snapshot=True) as session:
            target = await self._host._require_user_scope(invocation, user_id, session=session)
            rows = await memories.list_person(
                person_id=target, limit=_bounded_limit(limit, maximum=100), session=session
            )
        return tuple(_memory_record(row, "person") for row in rows)

    async def list_group(
        self,
        group_id: str,
        limit: int = 20,
    ) -> tuple[Mapping[str, JsonValue], ...]:
        invocation = self._host._require(PluginPermission.MEMORY_GROUP_READ)
        assert invocation is not None
        memories = _require_service(self._host._services.memories, "memory")
        async with memories.repository.transaction(read_snapshot=True) as session:
            target = await self._host._require_group_scope(invocation, group_id, session=session)
            rows = await memories.list_group(
                space_id=target, limit=_bounded_limit(limit, maximum=100), session=session
            )
        return tuple(_memory_record(row, "group") for row in rows)

    async def search(
        self,
        query: str,
        *,
        scope_type: str,
        subject_id: str,
        limit: int = 20,
    ) -> tuple[Mapping[str, JsonValue], ...]:
        invocation = self._host._require(PluginPermission.MEMORY_SEARCH)
        assert invocation is not None
        keyword = _bounded_text(query, maximum=400, field_name="query")
        if scope_type == "person":
            target_id = await self._host._require_user_scope(invocation, subject_id)
            target = MemoryEntityTarget(
                role=MemoryTargetRole.CURRENT_PERSON,
                scope_type=MemoryScopeType.PERSON,
                canonical_subject_person_id=target_id,
                block_id="plugin_person",
            )
        elif scope_type == "group":
            target_id = await self._host._require_group_scope(invocation, subject_id)
            target = MemoryEntityTarget(
                role=MemoryTargetRole.CURRENT_GROUP,
                scope_type=MemoryScopeType.GROUP,
                canonical_subject_space_id=target_id,
                block_id="plugin_group",
            )
        else:
            raise ValueError("memory search scope_type must be person or group")
        service = _require_service(self._host._services.memory_context, "memory retrieval")
        from yuki.memory.runtime.query_plane import (
            MemoryQueryPlane,
            MemoryReadConsumer,
            MemoryReadRequest,
            ResolvedReadScope,
        )

        result = await MemoryQueryPlane(service).read(
            MemoryReadConsumer.PLUGIN,
            MemoryReadRequest(
                text=keyword,
                resolved_scope=ResolvedReadScope(targets=(target,)),
                requested_limit=_bounded_limit(limit, maximum=100),
            ),
            runtime=await _runtime_snapshot(self._host, invocation),
        )
        self._host._require(PluginPermission.MEMORY_SEARCH)
        if scope_type == "person":
            await self._host._require_user_scope(invocation, subject_id)
        else:
            await self._host._require_group_scope(invocation, subject_id)
        return tuple(
            {
                **_memory_record(hit.fact, scope_type),
                "retrieval_reason": hit.selection_reason,
            }
            for hit in result.hits
        )

    async def add(
        self,
        *,
        scope_type: str,
        subject_id: str,
        content: str,
        source_type: str,
        confidence: float,
        source_event_ids: tuple[str, ...] = (),
    ) -> PluginResult:
        invocation = self._host._require(
            PluginPermission.MEMORY_WRITE,
            mutation=True,
        )
        assert invocation is not None
        if scope_type != "person":
            return _unavailable("group memory writes need a reviewed group-memory service")
        target = str(UUID(subject_id))
        memories = _require_service(self._host._services.memories, "memory")
        if await self._host._is_real_superuser(invocation):
            target = await self._host._require_user_scope(invocation, target)
        else:
            actor_person_id = await memories.resolve_person_id(person_id=invocation.actor_person_id)
            target_person_id = await memories.resolve_person_id(person_id=target)
            if actor_person_id is None or actor_person_id != target_person_id:
                raise PluginPermissionError("plugins may only write the current person's memory")
        normalized = _bounded_text(content, maximum=4_000, field_name="content")
        _validate_memory_metadata(invocation, source_type, confidence, source_event_ids)
        evidence = (
            MemoryEvidenceCreate(
                event_id=invocation.source_event_id,
                source_speaker_user_id=invocation.actor_user_id,
                relation=MemoryEvidenceRelation.EXPLICIT_COMMAND,
                authority=MemoryAuthority.EXPLICIT,
                excerpt=normalize_memory_text(
                    invocation.inbound.text if invocation.inbound is not None else "",
                ),
            )
            if source_event_ids and invocation.source_event_id is not None
            else None
        )
        service = _require_service(self._host._services.memory_admin, "memory mutation")
        row = await service.add_memory(
            _memory_mutation_subject(
                invocation,
                is_superuser=await self._host._is_real_superuser(invocation),
            ),
            target,
            normalized,
            evidence=evidence,
            target_person_id=target,
        )
        await self._host._audit(
            invocation,
            operation="memory.add",
            permission=PluginPermission.MEMORY_WRITE,
            success=True,
        )
        return PluginResult(data={"memory": _memory_record(row, "person")})

    async def update(
        self,
        memory_id: str,
        *,
        content: str,
        confidence: float | None = None,
    ) -> PluginResult:
        invocation = self._host._require(
            PluginPermission.MEMORY_WRITE,
            mutation=True,
        )
        assert invocation is not None
        numeric_id = _person_memory_id(memory_id)
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between zero and one")
        memories = _require_service(self._host._services.memories, "memory")
        current = await memories.get_fact(numeric_id)
        visible = None
        if current is not None and await memories.person_owns_fact(
            current,
            actor_person_id=invocation.actor_person_id,
        ):
            expected = _bounded_text(content, maximum=4_000, field_name="content")
            service = _require_service(self._host._services.memory_admin, "memory mutation")
            corrected = await service.correct_fact(
                _memory_mutation_subject(
                    invocation,
                    is_superuser=await self._host._is_real_superuser(invocation),
                ),
                numeric_id,
                expected,
            )
            visible = await _visible_person_projection_fact(
                memories,
                user_id=cast(str, invocation.actor_person_id),
                fact_id=corrected.id if corrected is not None else None,
                content=expected,
            )
        changed = visible is not None
        result_id = f"person:{visible.id}" if visible is not None else memory_id
        await self._host._audit(
            invocation,
            operation="memory.update",
            permission=PluginPermission.MEMORY_WRITE,
            success=changed,
            error_category=None if changed else "not_found",
        )
        return PluginResult(
            ok=changed,
            data={"memory_id": result_id},
            error_code=None if changed else "memory.not_found",
            detail="" if changed else "memory is not owned by the current user",
        )

    async def delete(self, memory_id: str) -> PluginResult:
        invocation = self._host._require(
            PluginPermission.MEMORY_DELETE,
            mutation=True,
        )
        assert invocation is not None
        numeric_id = _person_memory_id(memory_id)
        memories = _require_service(self._host._services.memories, "memory")
        current = await memories.get_fact(numeric_id)
        if current is None or not await memories.person_owns_fact(
            current,
            actor_person_id=invocation.actor_person_id,
        ):
            changed = False
        else:
            service = _require_service(self._host._services.memory_admin, "memory mutation")
            changed = await service.invalidate_fact(
                _memory_mutation_subject(
                    invocation,
                    is_superuser=await self._host._is_real_superuser(invocation),
                ),
                numeric_id,
                MemoryInvalidationReason.PLUGIN_EXPLICIT_INVALIDATION.value,
            )
        await self._host._audit(
            invocation,
            operation="memory.delete",
            permission=PluginPermission.MEMORY_DELETE,
            success=changed,
            error_category=None if changed else "not_found",
        )
        return PluginResult(
            ok=changed,
            data={"memory_id": memory_id},
            error_code=None if changed else "memory.not_found",
            detail="" if changed else "memory is not owned by the current user",
        )


class _AgentFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def result(self, work_id: str) -> PluginResult:
        """Read an owned durable result without keeping the old callback alive."""
        if self._host._closed:
            raise PluginPermissionError("plugin Host is closed")
        from yuki.runtime.work_repository import WorkRepository

        ledger = _require_service(self._host._services.ledger, "event ledger")
        row = await WorkRepository(ledger._database).get(work_id)
        if row is None:
            return PluginResult(ok=False, error_code="work_not_found_or_archived")
        source = json.loads(row["source_json"])
        if (
            source.get("owner") != "plugin_invocation"
            or source.get("plugin_id") != self._host.plugin_id
        ):
            raise PluginPermissionError("plugin does not own this work")
        if source.get("approval_revision") != self._host._services.approval_revision:
            raise PluginPermissionError("plugin approved version changed")
        permission = PluginPermission(source["permission"])
        self._host._require(permission, require_invocation=False)
        from yuki.conversation.canonical_db_models import CanonicalConversationModel

        async with ledger._database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, row["conversation_id"])
        if conversation is None or conversation.generation != row["generation"]:
            return _agent_result(
                AgentRunResult(
                    text="",
                    tool_calls_used=0,
                    model_requests=0,
                    work_id=work_id,
                    work_state="cancelled",
                ),
                source.get("allowed_tools", ()),
            )
        checkpoint = json.loads(row["checkpoint_json"])
        if checkpoint.get("archived"):
            return PluginResult(ok=False, error_code="work_archived", data={"work_id": work_id})
        return _owned_work_result(row, source)

    async def resume(self, work_id: str, text: str, *, request_id: str) -> PluginResult:
        """Append an owned answer to the same work; never reset its budget/history."""
        current = await self.result(work_id)
        if not current.ok or current.data.get("state") in {"completed", "cancelled", "failed"}:
            return current
        text = _bounded_text(text, maximum=12_000, field_name="text")
        request_id = _bounded_text(request_id, maximum=128, field_name="request_id")
        from yuki.runtime.work_repository import WorkConflict, WorkRepository

        ledger = _require_service(self._host._services.ledger, "event ledger")
        repository = WorkRepository(ledger._database)
        row = await repository.get(work_id)
        if row is None:
            return PluginResult(ok=False, error_code="work_not_found_or_archived")
        lease = await repository.acquire(row["conversation_id"], row["generation"])
        if lease is None:
            return PluginResult(ok=False, error_code="work_busy", data={"work_id": work_id})
        try:
            key = hashlib.sha256(
                f"{self._host.plugin_id}:{work_id}:{request_id}".encode()
            ).hexdigest()
            await repository.enqueue(
                row["conversation_id"],
                row["generation"],
                f"plugin-answer:{key}",
                kind="control",
                work_id=work_id,
                resume=(lease, {"text": "[原插件补充资料；不改变执行权限]\n" + text}),
            )
        except WorkConflict as exc:
            return PluginResult(ok=False, error_code="work_input_conflict", detail=str(exc))
        finally:
            await repository.release(lease)
        return await self.result(work_id)

    async def run(
        self,
        instruction: str,
        *,
        context_profile: str = "none",
        max_tool_calls: int | None = None,
        max_model_requests: int | None = None,
    ) -> PluginResult:
        invocation = self._host._require(PluginPermission.AGENT_RUN)
        assert invocation is not None
        _, base_runtime = await _agent_dependencies(self._host, invocation)
        tool_limit = min(
            (
                limit
                for limit in (max_tool_calls, base_runtime.runtime_config.agent.max_tool_calls)
                if limit is not None
            ),
            default=None,
        )
        request_limit = min(
            (
                limit
                for limit in (
                    max_model_requests,
                    base_runtime.runtime_config.agent.max_model_requests,
                )
                if limit is not None
            ),
            default=None,
        )
        runtime = replace(
            base_runtime,
            max_tool_calls=max(0, tool_limit) if tool_limit is not None else None,
            max_model_requests=max(1, request_limit) if request_limit is not None else None,
        )
        result = await run_plugin_main_turn(
            self._host,
            invocation,
            instruction=instruction.strip(),
            context_data=await _agent_context(self._host, invocation, context_profile),
            runtime=runtime,
            tools=None,
            permission=PluginPermission.AGENT_RUN,
            context_profile=context_profile,
        )
        return _agent_result(result, ())


class _AgentSessionsFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def create(self, request: CreateAgentSessionRequest) -> AgentSession:
        return await self._bound().create(request)

    async def run(self, request: RunAgentSessionRequest) -> AgentSessionRunResult:
        return await self._bound().run(request)

    async def reset(self, session_id: UUID) -> AgentSession:
        return await self._bound().reset(session_id)

    async def close(self, session_id: UUID) -> AgentSession:
        return await self._bound().close(session_id)

    def _bound(self) -> BoundAgentSessionFacade:
        invocation = self._host._require(PluginPermission.AGENT_SESSION)
        assert invocation is not None
        factory = _require_service(
            self._host._services.agent_sessions_factory,
            "plugin Agent sessions",
        )
        return factory(invocation)


class _WebFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def search(self, query: str) -> PluginResult:
        invocation = self._host._require(PluginPermission.WEB_SEARCH)
        assert invocation is not None
        provider = _require_service(self._host._services.web_provider, "web search")
        runtime = await _runtime_snapshot(self._host, invocation)
        try:
            response = await provider.search(
                WebSearchRequest(
                    query=query,
                    max_results=runtime.web.search_max_results,
                )
            )
        except WebSearchError as exc:
            return PluginResult(ok=False, error_code=f"web.{exc.code}", detail=exc.detail)
        data = {
            "query": response.query,
            "sources": [_web_source(item) for item in response.sources],
            "partial_failure": response.partial_failure,
        }
        if response.provider_summary:
            data["provider_summary"] = response.provider_summary
            data["provider_summary_instruction"] = (
                "provider_summary 是上游模型生成的总结，不是网页原文或逐来源摘录。"
                "总结中的 URL 不证明来源，只有 sources 中的真实搜索来源可作为证据。"
            )
        return PluginResult(data=data)

    async def read(self, url: str, question: str = "") -> PluginResult:
        self._host._require(PluginPermission.WEB_READ)
        provider = _require_service(self._host._services.web_provider, "web read")
        try:
            source = await provider.extract(
                normalize_public_url(url),
                question,
            )
        except WebSearchError as exc:
            return PluginResult(ok=False, error_code=f"web.{exc.code}", detail=exc.detail)
        return PluginResult(
            data={
                "source": _web_source(source),
            }
        )


class _HttpFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        auth_secret: str | None = None,
    ) -> PluginResult:
        self._host._require_any(
            (
                PluginPermission.NETWORK_HTTP_UNRESTRICTED,
                PluginPermission.NETWORK_HTTP_ALLOWLISTED,
            ),
            require_invocation=False,
        )
        facade = _require_service(self._host._services.http, "plugin HTTP")
        return await facade.request(
            method,
            url,
            headers=headers,
            body=body,
            auth_secret=auth_secret,
        )


class _VisionFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def analyze_current_media(self, question: str = "") -> PluginResult:
        invocation = self._host._require(PluginPermission.VISION_ANALYZE)
        assert invocation is not None
        inbound = invocation.inbound
        if inbound is None or not VisionService.has_visual_input(inbound):
            return PluginResult(
                ok=False,
                error_code="vision.no_current_media",
                detail="the trusted current event contains no image",
            )
        service = _require_service(self._host._services.vision, "vision")
        runtime = await _runtime_snapshot(self._host, invocation)
        try:
            observation = await service.analyze(
                inbound,
                question=_bounded_optional_text(question, maximum=1_000),
                runtime=runtime.vision,
                gateway=invocation.gateway,
                source_event_id=invocation.source_event_id,
                conversation_key=invocation.conversation_key,
            )
        except VisionProcessingError as exc:
            return PluginResult(
                ok=False,
                error_code=f"vision.{exc.code}",
                detail=exc.detail,
            )
        return PluginResult(data={"observation": dict(_visual_observation(observation) or {})})


class _MediaFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def get_current(self) -> tuple[Mapping[str, JsonValue], ...]:
        invocation = self._host._require(PluginPermission.MEDIA_CURRENT_READ)
        assert invocation is not None
        inbound = invocation.inbound
        if inbound is None:
            return ()
        return tuple(
            {
                "kind": attachment.kind.value,
                "label": attachment.label[:256],
                "segment_index": attachment.segment_index,
                "source": attachment.source,
                "sub_type": attachment.sub_type,
                "file_size": attachment.file_size,
                "emoji_id": attachment.emoji_id,
                "emoji_package_id": attachment.emoji_package_id,
                "is_sticker": attachment.is_sticker,
                "sticker_format": attachment.sticker_format,
                "mime_type": attachment.mime_type,
                "filename": attachment.filename,
                "width": attachment.width,
                "height": attachment.height,
                "preview_available": attachment.preview is not None,
            }
            for attachment in (*inbound.attachments, *inbound.reply_attachments)
        )

    async def create_artifact(
        self,
        *,
        data: bytes,
        content_type: str,
        filename: str,
        ttl_seconds: int = 86_400,
    ) -> MediaArtifactHandle:
        self._host._require(
            PluginPermission.MEDIA_ARTIFACT_CREATE,
            mutation=True,
            require_invocation=False,
        )
        store = _require_service(self._host._services.media_artifacts, "plugin media artifacts")
        return await store.create(
            plugin_id=self._host.plugin_id,
            data=data,
            content_type=content_type,
            filename=filename,
            ttl_seconds=ttl_seconds,
            storage_mb=self._host._services.media_storage_mb,
        )


class _NotificationFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def publish(
        self,
        request: PublishNotificationRequest,
    ) -> NotificationPublishReceipt:
        self._host._require(
            PluginPermission.NOTIFICATION_PUBLISH,
            send=True,
            require_invocation=False,
        )
        if request.ask_agent or request.resume_waiting_work:
            self._host._require(
                PluginPermission.NOTIFICATION_AGENT,
                require_invocation=False,
            )
        repository = _require_service(
            self._host._services.notifications,
            "plugin notifications",
        )
        receipt = await repository.publish(plugin_id=self._host.plugin_id, request=request)
        if self._host._services.notification_wake is not None:
            self._host._services.notification_wake()
        return receipt

    async def grant_target(
        self,
        target: NotificationTarget,
        *,
        bot_user_id: str,
    ) -> BackgroundTargetGrantView:
        invocation = self._host._require(
            PluginPermission.NOTIFICATION_PUBLISH,
            mutation=True,
        )
        assert invocation is not None
        await self._host._require_privileged(invocation)
        if bot_user_id and bot_user_id != invocation.bot_user_id:
            raise PluginPermissionError("grant bot must match the current connected bot")
        repository = _require_service(
            self._host._services.notifications,
            "plugin notifications",
        )
        selected = (
            target.model_copy(update={"conversation_id": invocation.conversation_id})
            if target.conversation_id is None
            and target.target_id in {invocation.actor_person_id, invocation.space_id}
            else target
        )
        return await repository.grant_target(
            plugin_id=self._host.plugin_id,
            target=selected,
            bot_user_id=invocation.bot_user_id,
            created_by_user_id=invocation.actor_person_id or "",
            presence_id=(
                invocation.presence_id
                if selected.conversation_id == invocation.conversation_id
                else None
            ),
        )

    async def revoke_target(self, target: NotificationTarget) -> bool:
        invocation = self._host._require(
            PluginPermission.NOTIFICATION_PUBLISH,
            mutation=True,
        )
        assert invocation is not None
        await self._host._require_privileged(invocation)
        repository = _require_service(
            self._host._services.notifications,
            "plugin notifications",
        )
        return await repository.revoke_target(plugin_id=self._host.plugin_id, target=target)

    async def list_grants(self) -> tuple[BackgroundTargetGrantView, ...]:
        self._host._require(
            PluginPermission.NOTIFICATION_PUBLISH,
            require_invocation=False,
        )
        repository = _require_service(
            self._host._services.notifications,
            "plugin notifications",
        )
        return await repository.list_grants(self._host.plugin_id)

    async def status(self) -> Mapping[str, int]:
        self._host._require(
            PluginPermission.NOTIFICATION_PUBLISH,
            require_invocation=False,
        )
        repository = _require_service(
            self._host._services.notifications,
            "plugin notifications",
        )
        return await repository.counts(self._host.plugin_id)


class _AutomationFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def list_current_owner(self) -> tuple[Mapping[str, JsonValue], ...]:
        invocation = self._host._require(PluginPermission.AUTOMATION_READ)
        assert invocation is not None
        service = _require_service(self._host._services.automation, "automation")
        rows = await service.list_for_actor(_invocation_actor(invocation))
        return tuple(_automation_record(row) for row in rows)

    async def create_task(
        self,
        task: Mapping[str, JsonValue],
        *,
        request_id: str,
    ) -> PluginResult:
        invocation = self._host._require(
            PluginPermission.AUTOMATION_MANAGE_SELF,
            mutation=True,
        )
        assert invocation is not None
        service = _require_service(self._host._services.automation, "automation")
        actor = _invocation_actor(invocation)
        request_id = _bounded_text(request_id, maximum=128, field_name="request_id")
        identity = (
            "plugin-task:"
            + hashlib.sha256(
                json.dumps([invocation.plugin_id, actor.source_key, request_id]).encode()
            ).hexdigest()
        )
        row = await service.create_task(
            dict(task),
            actor=actor,
            conversation_key=invocation.conversation_key,
            creation_source_key=identity,
        )
        return PluginResult(data={"automation": _automation_record(row)})

    async def update_task(self, task_id: str, task: Mapping[str, JsonValue]) -> PluginResult:
        invocation = self._host._require(PluginPermission.AUTOMATION_MANAGE_SELF, mutation=True)
        assert invocation is not None
        service = _require_service(self._host._services.automation, "automation")
        row = await service.update_task(
            int(task_id),
            dict(task),
            actor=_invocation_actor(invocation),
            conversation_key=invocation.conversation_key,
        )
        return PluginResult(data={"automation": _automation_record(row)})

    async def pause(self, task_id: str) -> PluginResult:
        return await self._manage(task_id, "pause")

    async def resume(self, task_id: str) -> PluginResult:
        return await self._manage(task_id, "resume")

    async def cancel(self, task_id: str) -> PluginResult:
        return await self._manage(task_id, "cancel")

    async def _manage(self, task_id: str, operation: str) -> PluginResult:
        invocation = self._host._require(
            PluginPermission.AUTOMATION_MANAGE_SELF,
            mutation=True,
        )
        assert invocation is not None
        try:
            numeric_id = int(task_id)
        except ValueError as exc:
            raise ValueError("task_id must be an integer") from exc
        service = _require_service(self._host._services.automation, "automation")
        method = {"pause": service.pause, "resume": service.resume, "cancel": service.cancel}[
            operation
        ]
        changed = await method(
            numeric_id,
            actor=_invocation_actor(invocation),
            conversation_key=invocation.conversation_key,
        )
        return PluginResult(
            ok=changed,
            data={"task_id": task_id, "operation": operation},
            error_code=None if changed else "automation.not_changed",
            detail="" if changed else "automation state was not changed",
        )


class _ConfigFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def get(
        self,
        key: str,
        *,
        scope_type: str = "global",
        scope_id: str = "",
    ) -> JsonValue:
        invocation = self._host._require(
            PluginPermission.PLUGIN_CONFIG_READ,
            require_invocation=scope_type != "global",
        )
        facade = self._bound(invocation)
        return await facade.get(key, scope_type=scope_type, scope_id=scope_id)

    async def set(
        self,
        key: str,
        value: JsonValue,
        *,
        scope_type: str = "global",
        scope_id: str = "",
    ) -> None:
        invocation = self._host._require(
            PluginPermission.PLUGIN_CONFIG_WRITE,
            mutation=True,
            require_invocation=scope_type != "global",
        )
        facade = self._bound(invocation)
        await facade.set(key, value, scope_type=scope_type, scope_id=scope_id)

    def _bound(self, invocation: PluginInvocation | None) -> BoundConfigFacade:
        factory = _require_service(self._host._services.config_factory, "plugin config")
        return factory(
            invocation.actor_person_id if invocation is not None else None,
            invocation.space_id if invocation is not None else None,
        )


class _SecretsFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    def configured(self, name: str) -> bool:
        return _require_service(self._host._services.secrets, "plugin secrets").configured(name)

    def get(self, name: str) -> str:
        return _require_service(self._host._services.secrets, "plugin secrets").get(name)


class _StorageFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    def _bound(self) -> BoundStorageFacade:
        self._host._require(
            PluginPermission.STORAGE_PRIVATE,
            require_invocation=False,
        )
        return _require_service(self._host._services.storage, "plugin storage")

    async def get(self, namespace: str, key: str) -> JsonValue:
        return await self._bound().get(namespace, key)

    async def set(self, namespace: str, key: str, value: JsonValue) -> None:
        await self._bound().set(namespace, key, value)

    async def delete(self, namespace: str, key: str) -> bool:
        return await self._bound().delete(namespace, key)

    async def list(self, namespace: str) -> Mapping[str, JsonValue]:
        return await self._bound().list(namespace)

    async def compare_and_set(
        self,
        namespace: str,
        key: str,
        expected: JsonValue,
        value: JsonValue,
    ) -> bool:
        return await self._bound().compare_and_set(namespace, key, expected, value)


class _SchedulerFacade:
    def __init__(self, host: HostPluginContext, *, task_limit: int) -> None:
        self._host = host
        self._task_limit = task_limit
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopped = asyncio.Event()

    def create_task(self, name: str, runner: Callable[[], Awaitable[None]]) -> str:
        self._host._require(
            PluginPermission.BACKGROUND_WORKER,
            require_invocation=False,
        )
        if _TASK_NAME.fullmatch(name) is None:
            raise ValueError("managed task name is invalid")
        self._discard_finished()
        if len(self._tasks) >= self._task_limit:
            raise PluginPermissionError("plugin background task limit reached")
        task_id = str(uuid.uuid4())

        async def managed_runner() -> None:
            await runner()

        task: asyncio.Task[None] = asyncio.create_task(
            managed_runner(),
            name=f"yuki-plugin:{self._host.plugin_id}:{name}:{task_id}",
        )
        self._tasks[task_id] = task

        def remove_completed(_task: asyncio.Task[None], *, key: str = task_id) -> None:
            self._tasks.pop(key, None)

        task.add_done_callback(remove_completed)
        return task_id

    async def cancel(self, task_id: str) -> bool:
        self._host._require(
            PluginPermission.BACKGROUND_WORKER,
            require_invocation=False,
        )
        task = self._tasks.pop(task_id, None)
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def sleep_until_stopped(self) -> None:
        self._host._require(
            PluginPermission.BACKGROUND_WORKER,
            require_invocation=False,
        )
        await self._stopped.wait()

    async def stop(self) -> None:
        """Host-only cooperative shutdown hook."""

        self._stopped.set()
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _discard_finished(self) -> None:
        for key, task in tuple(self._tasks.items()):
            if task.done():
                self._tasks.pop(key, None)


class _OneBotFacade:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def send_music_card(self, *, provider: str, resource_id: str) -> PluginResult:
        """Send one provider-native music card to this invocation's real scene."""

        invocation = self._host._invocation()
        assert invocation is not None

        async def send() -> PluginResult:
            checked = self._host._require(PluginPermission.ONEBOT_SEND, send=True)
            assert checked is not None
            card_provider = _validated_music_provider(provider)
            card_resource_id = _validated_music_resource_id(resource_id)
            message = [
                {
                    "type": "music",
                    "data": {"type": card_provider, "id": card_resource_id},
                }
            ]
            outbound_segments = (
                {
                    "type": "music",
                    "data": {"provider": card_provider, "id": card_resource_id},
                },
            )
            if checked.current_group_id is not None:
                target = checked.current_group_id
                return await _send_onebot(
                    self._host,
                    checked,
                    "send_group_msg",
                    {"group_id": target, "message": message},
                    outbound=_group_music_outbound(target, outbound_segments),
                )
            target = checked.actor_user_id
            return await _send_onebot(
                self._host,
                checked,
                "send_private_msg",
                {"user_id": target, "message": message},
                outbound=_private_music_outbound(target, outbound_segments),
            )

        return await self._host._run_audited(
            invocation,
            operation="onebot.send_music_card",
            permission=PluginPermission.ONEBOT_SEND,
            runner=send,
        )

    async def send_custom_music_card(
        self,
        *,
        url: str,
        image: str,
        title: str,
        singer: str = "",
        content: str = "",
    ) -> PluginResult:
        """Send one bounded custom music-style card to the current real scene."""

        invocation = self._host._invocation()
        assert invocation is not None

        async def send() -> PluginResult:
            checked = self._host._require(PluginPermission.ONEBOT_SEND, send=True)
            assert checked is not None
            card_data = {
                "type": "custom",
                "url": normalize_public_url(url),
                "image": normalize_public_url(image),
                "title": _bounded_text(title, maximum=200, field_name="title"),
                "singer": _bounded_optional_text(singer, maximum=200),
                "content": _bounded_optional_text(content, maximum=500),
            }
            message = [{"type": "music", "data": card_data}]
            outbound_segments = ({"type": "music", "data": dict(card_data)},)
            if checked.current_group_id is not None:
                target = checked.current_group_id
                return await _send_onebot(
                    self._host,
                    checked,
                    "send_group_msg",
                    {"group_id": target, "message": message},
                    outbound=_group_music_outbound(target, outbound_segments),
                )
            target = checked.actor_user_id
            return await _send_onebot(
                self._host,
                checked,
                "send_private_msg",
                {"user_id": target, "message": message},
                outbound=_private_music_outbound(target, outbound_segments),
            )

        return await self._host._run_audited(
            invocation,
            operation="onebot.send_custom_music_card",
            permission=PluginPermission.ONEBOT_SEND,
            runner=send,
        )

    async def send_private(self, user_id: str, text: str) -> PluginResult:
        invocation = self._host._invocation()
        assert invocation is not None

        async def send() -> PluginResult:
            checked = self._host._require(PluginPermission.ONEBOT_SEND, send=True)
            assert checked is not None
            target = user_id
            content = _outbound_text(text)
            return await _send_onebot(
                self._host,
                checked,
                "send_private_msg",
                {"user_id": target, "message": content},
                outbound=_private_outbound(target, content),
            )

        return await self._host._run_audited(
            invocation,
            operation="onebot.send_private",
            permission=PluginPermission.ONEBOT_SEND,
            runner=send,
        )

    async def send_group(self, group_id: str, text: str) -> PluginResult:
        invocation = self._host._invocation()
        assert invocation is not None

        async def send() -> PluginResult:
            checked = self._host._require(PluginPermission.ONEBOT_SEND, send=True)
            assert checked is not None
            target = group_id
            content = _outbound_text(text)
            return await _send_onebot(
                self._host,
                checked,
                "send_group_msg",
                {"group_id": target, "message": content},
                outbound=_group_outbound(target, content),
            )

        return await self._host._run_audited(
            invocation,
            operation="onebot.send_group",
            permission=PluginPermission.ONEBOT_SEND,
            runner=send,
        )

    async def call_read_action(
        self,
        action: str,
        params: Mapping[str, JsonValue],
    ) -> PluginResult:
        invocation = self._host._invocation()
        assert invocation is not None

        async def call() -> PluginResult:
            checked = self._host._require(PluginPermission.ONEBOT_READ)
            assert checked is not None
            normalized = action.strip()
            if normalized not in _READ_ONEBOT_ACTIONS:
                raise PluginPermissionError("action is not classified as a reviewed read action")
            await _validate_onebot_scope(self._host, checked, params)
            return await _call_onebot(self._host, checked, normalized, params)

        return await self._host._run_audited(
            invocation,
            operation="onebot.call_read_action",
            permission=PluginPermission.ONEBOT_READ,
            runner=call,
        )

    async def call_mutating_action(
        self,
        action: str,
        params: Mapping[str, JsonValue],
    ) -> PluginResult:
        invocation = self._host._invocation()
        assert invocation is not None

        async def call() -> PluginResult:
            checked = self._host._require(
                PluginPermission.ONEBOT_MUTATE,
                mutation=True,
            )
            assert checked is not None
            normalized = action.strip()
            if not normalized or len(normalized) > 128:
                raise ValueError("OneBot action is invalid")
            if normalized in {
                "send_private_forward_msg",
                "send_group_forward_msg",
                "send_forward_msg",
            }:
                raise ValueError(
                    "forward message delivery is unsupported by the SDK receipt contract"
                )
            if normalized == "send_msg":
                message_type = params.get("message_type")
                has_group = params.get("group_id") not in (None, "", 0)
                has_user = params.get("user_id") not in (None, "", 0)
                if message_type == "group" and has_group and not has_user:
                    normalized = "send_group_msg"
                elif message_type == "private" and has_user and not has_group:
                    normalized = "send_private_msg"
                elif message_type in (None, "") and has_group != has_user:
                    normalized = "send_group_msg" if has_group else "send_private_msg"
                else:
                    raise ValueError("send_msg requires one unambiguous message target")
            if normalized in {"send_private_msg", "send_group_msg"}:
                self._host._require(PluginPermission.ONEBOT_MUTATE, mutation=True, send=True)
                message, content, segments = _outbound_onebot_message(params.get("message"))
                if normalized == "send_group_msg":
                    target = str(params.get("group_id", ""))
                    outbound = _group_outbound(target, content)
                    target_key = "group_id"
                else:
                    target = str(params.get("user_id", ""))
                    outbound = _private_outbound(target, content)
                    target_key = "user_id"
                return await _send_onebot(
                    self._host,
                    checked,
                    normalized,
                    {
                        **{key: value for key, value in params.items() if key != "message_type"},
                        target_key: target,
                        "message": message,
                    },
                    outbound=replace(outbound, segments=segments),
                    privileged=True,
                )
            await self._host._require_privileged(checked)
            return await _call_onebot(self._host, checked, normalized, params)

        return await self._host._run_audited(
            invocation,
            operation="onebot.call_mutating_action",
            permission=PluginPermission.ONEBOT_MUTATE,
            runner=call,
        )


class _EventPublisher:
    def __init__(self, host: HostPluginContext) -> None:
        self._host = host

    async def publish(self, event: EventEnvelope) -> None:
        self._host._require(
            PluginPermission.EVENT_SUBSCRIBE,
            require_invocation=False,
        )
        bus = _require_service(self._host._services.events, "plugin events")
        payload = {"source_plugin_id": self._host.plugin_id, **dict(event.payload)}
        await bus.publish(event.model_copy(update={"payload": payload}))


async def _runtime_snapshot(
    host: HostPluginContext,
    invocation: PluginInvocation,
) -> RuntimeConfigSnapshot:
    if invocation.runtime_config is not None:
        return invocation.runtime_config
    service = _require_service(host._services.runtime_config, "runtime configuration")
    return await service.snapshot(
        user_id=invocation.actor_person_id,
        group_id=invocation.space_id,
    )


async def _agent_dependencies(
    host: HostPluginContext,
    invocation: PluginInvocation,
) -> tuple[AgentRunner, AgentRuntime]:
    runner = _require_service(host._services.agent_runner, "LLM/Agent")
    runtime = await _runtime_snapshot(host, invocation)
    return runner, AgentRuntime(
        origin=TurnOrigin.PLUGIN_SESSION,
        actor_user_id=invocation.actor_user_id,
        actor_is_superuser=await host._is_real_superuser(invocation),
        delegated_authority=invocation.delegated_authority,
        conversation_key=invocation.conversation_key,
        current_group_id=invocation.current_group_id,
        bot_user_id=invocation.bot_user_id,
        gateway=invocation.gateway,
        runtime_config=runtime,
        allowed_capabilities=(
            frozenset({"web"})
            if PluginPermission.WEB_SEARCH in host._approved_permissions
            else frozenset()
        ),
        max_tool_calls=runtime.agent.max_tool_calls,
        max_model_requests=runtime.agent.max_model_requests,
        canonical_conversation_id=invocation.conversation_id,
    )


async def _agent_context(
    host: HostPluginContext,
    invocation: PluginInvocation,
    profile: str,
) -> str:
    if profile == "none":
        return ""
    if profile not in {"current_user", "current_group"}:
        raise ValueError("context_profile must be none, current_user, or current_group")
    if profile == "current_group" and invocation.current_group_id is None:
        raise PluginPermissionError("current_group context requires a real group turn")
    if not invocation.actor_person_id:
        return f" Current Space id={invocation.space_id}." if profile == "current_group" else ""
    people = _require_service(host._services.people, "people")
    person = await people.get(
        person_id=invocation.actor_person_id,
        space_id=invocation.space_id if profile == "current_group" else None,
    )
    if person is None:
        return ""
    context = (
        f"\nTrusted current user metadata: display_name={person.display_name!r}; "
        "use only for this request."
    )
    if profile == "current_group":
        context += f" Current Space id={invocation.space_id}."
    return context


async def _send_social(
    host: HostPluginContext,
    invocation: PluginInvocation,
    target_type: str,
    target_id: str,
    *,
    text: str,
    media_reference: str | None,
) -> PluginResult:
    """Use the shared Social effect with the original callback/call identity."""
    from yuki.social.models import SocialError
    from yuki.social.service import SocialContext

    social = _require_service(host._services.social, "social delivery")
    state = _CURRENT_DELIVERY.get()
    if state is None or invocation.conversation_id is None:
        return _unavailable("SDK delivery requires a durable canonical invocation")
    actor = _invocation_actor(invocation)
    async with state.lock:
        call_id = state.next_call()
        context = SocialContext(
            state.identity,
            call_id,
            invocation.conversation_id,
            person_refs={"current_speaker": actor.person_id} if actor.person_id else {},
            space_id=invocation.space_id,
            trigger_event_id=invocation.source_event_id,
            caused_by_event_id=(
                invocation.source_event_id
                if invocation.origin in {TurnOrigin.PLUGIN_BACKGROUND, TurnOrigin.SYSTEM_TASK}
                else None
            ),
            origin=invocation.origin.value,
            actor=actor,
            conversation_key=invocation.conversation_key,
            inbound=invocation.inbound,
            initiative_run_id=actor.initiative_run_id,
            automation_run_id=actor.automation_run_id,
            presence_id=invocation.presence_id,
            reply_presence_id=invocation.presence_id,
        )
        args: dict[str, Any] = {"text": text}
        result = None
        current_target = invocation.space_id if target_type == "group" else invocation.person_id
        if target_id != current_target:
            args["target"] = {
                "kind": "space" if target_type == "group" else "person",
                "target_id": target_id,
            }
        if media_reference is not None:
            artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{state.identity}:{call_id}:media"))
            args.update(artifact_id=artifact_id, attachment_kind="image")
            import tempfile

            store = _require_service(social.workspace, "artifact delivery")
            prior = await social.receipts.find(state.identity, call_id)
            if prior is not None:
                source = await asyncio.to_thread(store.snapshot_source, artifact_id)
                if source is None or not source.startswith(
                    f"plugin-media:{host.plugin_id}:{media_reference}:"
                ):
                    raise SocialError("idempotency_conflict")
                result = await social.replay_send(args, context)
            if result is None:
                media = _require_service(host._services.media_artifacts, "plugin media artifacts")
                original, data = await media.read_owned(
                    plugin_id=host.plugin_id,
                    handle_id=media_reference,
                )

                def snapshot() -> dict[str, Any]:
                    with tempfile.TemporaryFile() as stream:
                        stream.write(data)
                        stream.flush()
                        return store.snapshot(
                            stream.fileno(),
                            original.filename,
                            artifact_id=artifact_id,
                            source_key=f"plugin-media:{host.plugin_id}:{original.handle_id}:{original.sha256}",
                        )

                await asyncio.to_thread(snapshot)
        try:
            if result is None:
                result = await social.execute("send_message", args, context)
        except SocialError as exc:
            prior = await social.receipts.find(state.identity, call_id)
            if str(exc) != "idempotency_conflict" or prior is None:
                raise
            return PluginResult(
                ok=False,
                error_code="social.idempotency_conflict",
                detail=(
                    "Original call payload differs; the persisted effect was not dispatched again."
                ),
                data={"operation_id": prior.operation_id, "status": prior.status.value},
            )
        operation_id = result.get("operation_id")
        if not isinstance(operation_id, str):
            return PluginResult(
                ok=False,
                data=_safe_json(result),
                error_code=str(result.get("error") or "social.delivery_unconfirmed"),
            )
        status = str(result.get("status", "unknown"))
        unknown = status in {"executing", "uncertain", "unknown"}
        projected = _HostSocialResult(
            ok=status == "succeeded",
            data={
                "operation_id": operation_id,
                "status": "unknown" if unknown else status,
                "uncertain": unknown,
                "retryable": False,
                "result": _safe_json(result),
            },
            error_code=None if status == "succeeded" else "social.delivery_unconfirmed",
            detail=str(result.get("error") or ""),
        )
        projected._receipt = await social.receipts.get(operation_id)
        return projected


async def _send_onebot(
    host: HostPluginContext,
    invocation: PluginInvocation,
    action: str,
    params: Mapping[str, JsonValue] | Mapping[str, object],
    *,
    outbound: _OutboundLedgerMessage,
    privileged: bool = False,
) -> PluginResult:
    ledger = host._services.ledger
    state = _CURRENT_DELIVERY.get()
    if ledger is None or state is None or not invocation.conversation_id:
        return _unavailable("SDK delivery requires a durable canonical invocation")
    if invocation.platform != "qq":
        raise PluginPermissionError("OneBot extensions require an original QQ entry")
    # Keep per-callback call ordinals ordered even when plugin code uses gather().
    async with state.lock:
        call_id = state.next_call()
        prior = await SocialOperationRepository(ledger._database).find(state.identity, call_id)
        if prior is None or prior.status is OperationStatus.PREPARED:
            if privileged:
                await host._require_privileged(invocation)
            await _validate_onebot_scope(host, invocation, cast(Mapping[str, JsonValue], params))
        repository, receipt, presence_id = await prepare_delivery(
            ledger,
            source_conversation_id=invocation.conversation_id,
            source_event_id=invocation.source_event_id,
            actor_user_id=invocation.actor_user_id,
            actor=invocation.actor,
            origin=invocation.origin.value,
            bot_user_id=invocation.bot_user_id,
            group_id=outbound.group_id,
            user_id=outbound.private_peer_user_id,
            source_turn_id=state.identity,
            call_id=call_id,
            action=action,
            params=dict(params),
        )
        if receipt.status is not OperationStatus.PREPARED:
            return _delivery_result(receipt)
        assert receipt.prepared is not None
        entry_binding_id = receipt.prepared["binding_id"]
        gateway = await _qq_gateway(host, invocation)
        claimed = False
        if receipt.status is OperationStatus.PREPARED:
            async with ledger._database.immediate_session() as session:
                target, current_presence, current_binding = await validate_delivery_source(
                    session,
                    source_conversation_id=invocation.conversation_id,
                    source_event_id=invocation.source_event_id,
                    actor_user_id=invocation.actor_user_id,
                    actor=invocation.actor,
                    bot_user_id=invocation.bot_user_id,
                    group_id=outbound.group_id,
                    user_id=outbound.private_peer_user_id,
                )
                if (
                    target != receipt.target
                    or current_presence != presence_id
                    or current_binding != entry_binding_id
                ):
                    raise SocialError("plugin_send_source_mismatch")
                claimed = await repository.claim(
                    receipt.operation_id,
                    presence_id=presence_id,
                    session=session,
                    expected_payload_hash=receipt.payload_hash,
                )
        if not claimed:
            receipt = await repository.get(receipt.operation_id)
            return _delivery_result(receipt)
        confirmed = None
        try:
            raw = await gateway.call_api(action, dict(params))
            confirmed = parse_onebot_send_receipt(raw)
            scope = (
                ConversationScope.group(
                    invocation.bot_user_id,
                    outbound.group_id,
                    presence_id=presence_id,
                    conversation_id=invocation.conversation_id
                    if outbound.group_id == invocation.current_group_id
                    else None,
                )
                if outbound.group_id is not None
                else ConversationScope.private(
                    invocation.bot_user_id,
                    outbound.private_peer_user_id or "",
                    presence_id=presence_id,
                    conversation_id=invocation.conversation_id
                    if invocation.current_group_id is None
                    and outbound.private_peer_user_id == invocation.actor_user_id
                    else None,
                )
            )
            async with ledger._database.immediate_session() as session:
                from yuki.social.confirmed_content import content_sources_retained

                retained = await content_sources_retained(
                    session,
                    conversation_id=invocation.conversation_id,
                    target=receipt.target,
                    event_ids=(invocation.source_event_id,),
                    person_id=invocation.actor_person_id,
                )
                appended = (
                    await ledger._writer.append(
                        scope=scope,
                        platform_message_id=confirmed.platform_message_id,
                        sender_user_id=invocation.bot_user_id,
                        direction="outbound",
                        content=outbound.content,
                        segments=outbound.segments,
                        origin=invocation.origin.value,
                        session=session,
                        identity_binding_id=entry_binding_id
                        if receipt.target.kind == "person"
                        else None,
                        space_binding_id=entry_binding_id
                        if receipt.target.kind == "space"
                        else None,
                    )
                    if retained
                    else None
                )
                await repository.finish(
                    receipt.operation_id,
                    status=OperationStatus.SUCCEEDED,
                    platform_reference=confirmed.platform_message_id,
                    event_id=appended.event.id if appended is not None else None,
                    session=session,
                )
        except BaseException as exc:
            # A parsed platform confirmation remains success if its body can no
            # longer be published. Without that confirmation the effect is unknown.
            try:
                persisted = await repository.get(receipt.operation_id)
                if persisted.status is OperationStatus.SUCCEEDED:
                    if not isinstance(exc, Exception):
                        raise
                    return _delivery_result(persisted)
                async with ledger._database.sessions() as session, session.begin():
                    await repository.finish(
                        receipt.operation_id,
                        status=OperationStatus.SUCCEEDED
                        if confirmed is not None
                        else OperationStatus.UNCERTAIN,
                        platform_reference=confirmed.platform_message_id
                        if confirmed is not None
                        else None,
                        error_category=None if confirmed is not None else type(exc).__name__[:64],
                        session=session,
                    )
                receipt = await repository.get(receipt.operation_id)
            except Exception as secondary:
                exc.add_note(f"SDK delivery reconciliation deferred: {type(secondary).__name__}")
                raise exc from secondary
            if not isinstance(exc, Exception):
                raise
            return _delivery_result(receipt)
        receipt = await repository.get(receipt.operation_id)
        result = _delivery_result(receipt)
        return result.model_copy(update={"data": {**result.data, "result": _safe_json(raw)}})


def _delivery_identity(host: HostPluginContext, invocation: PluginInvocation) -> str:
    from yuki.capabilities.invocation import current_invocation
    from yuki.runtime.work_activation import current_work_control

    identity = invocation.delivery_identity
    tool = current_invocation.get()
    if identity is None and tool is not None and tool.call_id:
        control = current_work_control.get()
        identity = (
            tool.call_id
            if control is not None and control.session is not None
            else f"{tool.execution_key}:{tool.call_id}"
        )
    # A direct callback without a persisted Host identity is a NEW invocation,
    # not an alleged replay inferred from its source event or message content.
    identity = identity or f"callback:{uuid.uuid4()}"
    encoded = json.dumps(
        [host.plugin_id, identity],
        separators=(",", ":"),
    )
    return "plugin-send:" + hashlib.sha256(encoded.encode()).hexdigest()


def _delivery_result(receipt: SocialReceipt) -> PluginResult:
    unknown = delivery_unknown(receipt)
    result = _HostSocialResult(
        ok=receipt.status is OperationStatus.SUCCEEDED,
        data={
            "operation_id": receipt.operation_id,
            "status": "unknown" if unknown else receipt.status.value,
            "uncertain": unknown,
            "retryable": False,
            "result": {"message_id": receipt.platform_reference}
            if receipt.platform_reference is not None
            else None,
        },
        error_code=(None if receipt.status is OperationStatus.SUCCEEDED else "onebot.call_failed"),
        detail=receipt.error_category or "",
    )
    result._receipt = receipt
    return result


async def _call_onebot(
    host: HostPluginContext,
    invocation: PluginInvocation,
    action: str,
    params: Mapping[str, object],
) -> PluginResult:
    gateway = await _qq_gateway(host, invocation)
    try:
        raw = await gateway.call_api(action, dict(params))
    except Exception as exc:
        return PluginResult(
            ok=False,
            error_code="onebot.call_failed",
            detail=type(exc).__name__,
        )
    return PluginResult(data={"result": _safe_json(raw)})


def _private_outbound(
    user_id: str,
    content: str,
    *,
    image: bool = False,
) -> _OutboundLedgerMessage:
    return _OutboundLedgerMessage(
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id=user_id,
        content=content,
        segments=_outbound_segments(content, image=image),
    )


def _group_outbound(
    group_id: str,
    content: str,
    *,
    image: bool = False,
) -> _OutboundLedgerMessage:
    return _OutboundLedgerMessage(
        scope_type=ScopeType.GROUP,
        group_id=group_id,
        content=content,
        segments=_outbound_segments(content, image=image),
    )


def _private_music_outbound(
    user_id: str,
    segments: tuple[dict[str, Any], ...],
) -> _OutboundLedgerMessage:
    return _OutboundLedgerMessage(
        scope_type=ScopeType.PRIVATE,
        private_peer_user_id=user_id,
        content="",
        segments=segments,
    )


def _group_music_outbound(
    group_id: str,
    segments: tuple[dict[str, Any], ...],
) -> _OutboundLedgerMessage:
    return _OutboundLedgerMessage(
        scope_type=ScopeType.GROUP,
        group_id=group_id,
        content="",
        segments=segments,
    )


def _outbound_segments(content: str, *, image: bool) -> tuple[dict[str, Any], ...]:
    if image:
        # A permanent chat event records the media kind, never its file id or URL.
        return ({"type": "image", "data": {}},)
    return ({"type": "text", "data": {"text": content}},)


def _result_error_category(result: PluginResult) -> str | None:
    if result.ok:
        return None
    if result.error_code == "onebot.call_failed" and result.detail:
        return result.detail[:64]
    return (result.error_code or "operation_failed")[:64]


def _facade_error_category(exc: Exception) -> str:
    if isinstance(exc, PluginOwnershipError):
        return exc.category[:64]
    if isinstance(exc, PluginPermissionError):
        return "permission_denied"
    if isinstance(exc, FeatureUnavailableError):
        return "feature_unavailable"
    if isinstance(exc, ValueError | TypeError):
        return "validation_error"
    return type(exc).__name__[:64]


def _current_message(inbound: InboundMessage | None) -> CurrentMessage | None:
    if inbound is None:
        return None
    projection = projection_from_inbound(inbound)
    return CurrentMessage(
        platform=inbound.platform,
        author_kind=inbound.author_kind,
        author_account_id=inbound.author_account_id,
        message_id=inbound.message_id,
        sender_user_id=inbound.sender.user_id,
        scope_type=inbound.scope_type.value,
        group_id=inbound.group_id,
        text=inbound.text,
        mentions=mention_snapshots(snapshot_mention_segments(inbound.segments, inbound.mentions)),
        received_at=inbound.received_at,
        **projection.sdk_fields(),
    )


def _record_message(record: Any) -> CurrentMessage:
    projection = projection_from_event(record)
    return CurrentMessage(
        platform=record.platform,
        author_kind=record.author_kind,
        author_account_id=record.author_account_id,
        message_id=record.platform_message_id,
        sender_user_id=record.sender_user_id,
        scope_type=record.scope_type.value,
        group_id=record.group_id,
        text=record.content,
        mentions=mention_snapshots(record.segments),
        received_at=record.occurred_at,
        **projection.sdk_fields(),
    )


def _control_audit_ref(invocation: PluginInvocation) -> ControlAuditRef:
    inbound = invocation.inbound
    return ControlAuditRef(
        user_id=invocation.actor_user_id,
        trigger_message_id=inbound.message_id if inbound else "plugin-task",
        trigger_event_id=invocation.source_event_id,
        canonical_conversation_id=invocation.conversation_id,
        ingress_presence_id=invocation.presence_id,
        conversation_key=invocation.conversation_key,
        bot_user_id=invocation.bot_user_id,
        decision_actor_type="plugin",
        decision_actor_id=invocation.plugin_id,
    )


def _invocation_actor(invocation: PluginInvocation) -> ToolActor:
    if invocation.actor is not None:
        return invocation.actor
    if invocation.inbound is not None:
        return ToolActor.from_inbound(invocation.inbound)
    raise PluginPermissionError("plugin invocation has no execution actor")


def _memory_mutation_subject(
    invocation: PluginInvocation,
    *,
    is_superuser: bool,
) -> MemoryPreferenceTrigger:
    inbound = invocation.inbound
    state = _CURRENT_DELIVERY.get()
    assert state is not None
    return MemoryPreferenceTrigger(
        user_id=invocation.actor_user_id,
        bot_user_id=invocation.bot_user_id,
        trigger_message_id=inbound.message_id if inbound else "plugin-task",
        trigger_event_id=invocation.source_event_id,
        canonical_conversation_id=invocation.conversation_id,
        ingress_presence_id=invocation.presence_id,
        conversation_key=invocation.conversation_key,
        decision_actor_type="plugin",
        decision_actor_id=invocation.plugin_id,
        # HostPluginContext resolves this from the current Person authority;
        # plugin code never supplies it.
        actor_is_superuser=is_superuser,
        person_id=invocation.actor_person_id,
        operation_id=state.next_memory_call(),
    )


def _group_record(row: Any) -> dict[str, JsonValue]:
    return {
        "group_id": row.group_id,
        "name": row.name,
        "enabled": row.enabled,
        "require_mention": row.require_mention,
        "autonomous_enabled": row.autonomous_enabled,
    }


async def _visible_person_projection_fact(
    memories: MemoryFactService,
    *,
    user_id: str,
    fact_id: int | None,
    content: str,
) -> Any | None:
    if fact_id is None:
        return None
    expected = normalize_memory_text(content)
    return next(
        (
            row
            for row in await memories.list_person(person_id=user_id)
            if row.id == fact_id and row.content == expected
        ),
        None,
    )


def _memory_record(row: Any, scope: str) -> dict[str, JsonValue]:
    return {
        "memory_id": f"{scope}:{row.id}",
        "fact_id": row.id,
        "scope_type": scope,
        "kind": row.kind.value,
        "category": row.category,
        "content": row.content,
        "importance": row.importance,
        "confidence": row.confidence,
        "source_type": row.source_type.value,
        "status": row.status.value,
        "authority": row.authority.value,
        "conflict_state": row.conflict_state.value,
        "evidence_count": row.evidence_count,
        "last_confirmed_at": row.last_confirmed_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "user_id": row.canonical_subject_person_id,
        "group_id": row.canonical_subject_space_id or row.canonical_visibility_space_id,
        "subject_user_id": row.canonical_subject_person_id,
    }


def _automation_record(row: AutomationRecord) -> dict[str, JsonValue]:
    return {
        "task_id": str(row.id),
        "name": row.name,
        "status": row.status.value,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "run_count": row.run_count,
    }


def _visual_observation(observation: VisualObservation | None) -> Mapping[str, JsonValue] | None:
    if observation is None:
        return None
    return cast(Mapping[str, JsonValue], observation.model_dump(mode="json"))


def _web_source(source: Any) -> dict[str, JsonValue]:
    return {
        "title": source.title,
        "url": source.url,
        "domain": source.domain,
        "snippet": source.snippet,
        "relevant_content": source.relevant_content,
        "published_at": source.published_at.isoformat() if source.published_at else None,
    }


def _validate_memory_metadata(
    invocation: PluginInvocation,
    source_type: str,
    confidence: float,
    source_event_ids: tuple[str, ...],
) -> None:
    if source_type not in {"automatic", "plugin"}:
        raise ValueError("plugin memory source_type must be automatic or plugin")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    allowed = {str(invocation.source_event_id)} if invocation.source_event_id is not None else set()
    if set(source_event_ids) - allowed:
        raise PluginPermissionError("memory source events must belong to the current real turn")


def _person_memory_id(value: str) -> int:
    prefix, separator, raw_id = value.partition(":")
    if prefix != "person" or not separator:
        raise PluginPermissionError("only current-person memory ids can be mutated")
    try:
        result = int(raw_id)
    except ValueError as exc:
        raise ValueError("memory_id is invalid") from exc
    if result <= 0:
        raise ValueError("memory_id is invalid")
    return result


async def _require_qq_user_scope(
    host: HostPluginContext, invocation: PluginInvocation, user_id: str
) -> str:
    if invocation.platform != "qq":
        raise PluginPermissionError("OneBot extensions require an original QQ entry")
    from yuki.identity.canonical_repository import find_identity_binding

    ledger = _require_service(host._services.ledger, "ledger")
    async with ledger._database.sessions() as session:
        binding = await find_identity_binding(session, user_id)
        if binding is None or binding.status != "active":
            raise PluginPermissionError("QQ target is not an active Binding")
        person_id = binding.person_id
    await host._require_user_scope(invocation, person_id)
    return user_id


async def _require_qq_group_scope(
    host: HostPluginContext, invocation: PluginInvocation, group_id: str
) -> str:
    if invocation.platform != "qq":
        raise PluginPermissionError("OneBot extensions require an original QQ entry")
    from yuki.identity.canonical_repository import find_space_binding

    ledger = _require_service(host._services.ledger, "ledger")
    async with ledger._database.sessions() as session:
        binding = await find_space_binding(session, group_id)
        if binding is None or binding.status != "active":
            raise PluginPermissionError("QQ target is not an active SpaceBinding")
        space_id = binding.space_id
    await host._require_group_scope(invocation, space_id)
    return group_id


async def _qq_gateway(host: HostPluginContext, invocation: PluginInvocation) -> OneBotFacadeGateway:
    if invocation.platform != "qq":
        raise PluginPermissionError("OneBot extensions require an original QQ entry")
    if invocation.gateway is not None:
        return invocation.gateway
    from yuki.adapters.onebot.sender import OneBotRouteSender

    social = _require_service(host._services.social, "social")
    if invocation.presence_id is None:
        raise PluginPermissionError("OneBot extension requires its original Presence")
    resolved = await social.router.resolve_presence(invocation.presence_id)
    if resolved.platform != "qq":
        raise PluginPermissionError("OneBot extensions require an original QQ Presence")
    return OneBotRouteSender(
        resolved.connection.bot,
        group=invocation.current_group_id is not None,
        target_id=invocation.current_group_id or invocation.actor_user_id,
    )


async def _validate_onebot_scope(
    host: HostPluginContext,
    invocation: PluginInvocation,
    params: Mapping[str, JsonValue],
) -> None:
    user_id = params.get("user_id")
    group_id = params.get("group_id")
    if isinstance(user_id, str | int):
        await _require_qq_user_scope(host, invocation, str(user_id))
    if isinstance(group_id, str | int):
        await _require_qq_group_scope(host, invocation, str(group_id))


def _safe_json(value: object, *, depth: int = 0) -> JsonValue:
    if depth > 6:
        return "[truncated]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:32_000]
    if isinstance(value, list | tuple):
        return [_safe_json(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in list(value.items())[:200]:
            name = str(key)[:128]
            if name.casefold() in _SENSITIVE_KEYS:
                result[name] = "[redacted]"
            else:
                result[name] = _safe_json(item, depth=depth + 1)
        return result
    return str(value)[:1_000]


def _bounded_limit(value: int, *, maximum: int = 100) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    return max(1, min(value, maximum))


def _bounded_text(value: str, *, maximum: int, field_name: str) -> str:
    normalized = " ".join(value.split()).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1 to {maximum} characters")
    return normalized


def _bounded_optional_text(value: str, *, maximum: int) -> str:
    normalized = " ".join(value.split()).strip()
    if len(normalized) > maximum:
        raise ValueError(f"text cannot exceed {maximum} characters")
    return normalized


def _outbound_text(value: str) -> str:
    return sanitize_model_output(value)


def _outbound_onebot_message(
    value: JsonValue,
) -> tuple[JsonValue, str, tuple[dict[str, Any], ...]]:
    """Clean only text segments; retain explicit at/reply/media structure."""
    if isinstance(value, str):
        text = _outbound_text(value)
        return text, text, _outbound_segments(text, image=False)
    if not isinstance(value, list):
        raise ValueError("OneBot message must be text or a segment list")
    segments: list[dict[str, Any]] = []
    texts: list[str] = []
    for raw in value:
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            raise ValueError("OneBot message segment is invalid")
        segment = json.loads(json.dumps(raw, allow_nan=False))
        if segment.get("type") == "text":
            text = segment["data"].get("text")
            if not isinstance(text, str):
                raise ValueError("OneBot text segment is invalid")
            try:
                cleaned = _outbound_text(text)
            except LLMEmptyResponseError:
                continue
            segment["data"]["text"] = cleaned
            texts.append(cleaned)
        segments.append(segment)
    if not any(segment.get("type") != "reply" for segment in segments):
        raise ValueError("OneBot message is empty after sanitization")
    return cast(JsonValue, segments), "".join(texts), tuple(segments)


def _validated_qq(value: str) -> str:
    normalized = value.strip()
    if _QQ_ID.fullmatch(normalized) is None:
        raise ValueError("QQ or group id is invalid")
    return normalized


def _validated_music_provider(value: str) -> str:
    normalized = value.strip().casefold()
    provider = _MUSIC_PROVIDERS.get(normalized)
    if provider is None:
        raise ValueError("music provider must be qq, netease, kugou, kuwo, or migu")
    return provider


def _validated_music_resource_id(value: str) -> str:
    normalized = value.strip()
    if _MUSIC_RESOURCE_ID.fullmatch(normalized) is None:
        raise ValueError("music resource id is invalid")
    return normalized


def _unavailable(detail: str) -> PluginResult:
    return PluginResult(ok=False, error_code="feature.unavailable", detail=detail)


def _require_service[T](value: T | None, name: str) -> T:
    if value is None:
        raise FeatureUnavailableError(f"{name} service is unavailable")
    return value


def _aware_media(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _assert_plugin_context_contract(context: HostPluginContext) -> PluginContext:
    """Keep the Host implementation structurally checked against Plugin API v1."""

    return context


__all__ = [
    "AgentSessionFacadeFactory",
    "ConfigFacadeFactory",
    "HostPluginContext",
    "OneBotFacadeGateway",
    "PluginFacadeServices",
    "PluginInvocation",
]
