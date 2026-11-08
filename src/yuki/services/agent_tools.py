"""Fixed model tools over authenticated runtime services and canonical memory."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Set
from contextlib import AbstractContextManager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import RuntimeConfigSnapshot
from yuki.capabilities.results import ToolExecutionResult
from yuki.config import Settings
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import ChatTool, InboundMessage
from yuki.domain.tool_actor import ToolActor
from yuki.external_agents.errors import ExternalAgentArgumentError
from yuki.external_agents.tools import (
    EXTERNAL_AGENT_TOOLS,
    coding_agent_arguments,
    coding_agent_view,
)
from yuki.memory.authorized_scope import AuthorizedMemoryScope
from yuki.memory.context import MemoryContextService
from yuki.memory.enums import (
    MemoryKind,
    MemoryScopeType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from yuki.memory.errors import MemoryRetrievalError
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.models import MemoryEntityTarget
from yuki.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationResult,
    MemoryMutationTarget,
)
from yuki.memory.mutation.service import MemoryMutationService
from yuki.memory.query import MemoryQueryBuilder
from yuki.memory.read_scope import MemoryReadScopeResolver
from yuki.memory.retrieval import MemoryRetriever
from yuki.memory.runtime.query_plane import (
    MemoryQueryPlane,
    MemoryReadConsumer,
    MemoryReadRequest,
    ResolvedReadScope,
)
from yuki.memory.service import MemoryFactService
from yuki.memory.subjects import ResolvedSubject
from yuki.memory.targets import MemoryTargetResolver
from yuki.memory.tool_intent import parse_memory_tool_intent
from yuki.persistence.models import MembershipModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import (
    EventLedgerRepository,
)
from yuki.runtime.errors import InvalidTurnContextError
from yuki.runtime.origin import TurnOrigin
from yuki.sandbox.environment_tools import SANDBOX_TOOLS, command_arguments, command_view
from yuki.services.context_boundary import ContextBoundaryReader
from yuki.services.context_compaction import PublicCompactContext

if TYPE_CHECKING:
    from yuki.external_agents.service import ExternalAgentService
    from yuki.sandbox.client import SandboxClient
    from yuki.social.service import SocialService
    from yuki.workspace.service import WorkspaceService
from yuki.services.turn_coordinator import TurnToken
from yuki.web.base import WebSearchError, WebSearchProvider, normalize_public_url
from yuki.web.models import (
    WebMode,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchTimeRange,
    WebSearchTopic,
)
from yuki.workspace.tools import WORKSPACE_TOOLS

_MEMORY_CHANGE_ORIGINS = frozenset(
    {
        TurnOrigin.USER_MESSAGE,
        TurnOrigin.AUTONOMOUS_GROUP,
        TurnOrigin.SCHEDULED_AUTOMATION,
        TurnOrigin.SELF_INITIATIVE,
    }
)
_MEMORY_TARGET: dict[str, object] = {
    "type": "object",
    "description": "恰含一个键：人物 person_id、群 space_id，或自己 self=true",
    "properties": {
        "person_id": {"type": "string", "minLength": 1},
        "space_id": {"type": "string", "minLength": 1},
        "self": {"const": True},
    },
    "oneOf": [{"required": ["person_id"]}, {"required": ["space_id"]}, {"required": ["self"]}],
    "additionalProperties": False,
}
_MEMORY_EVIDENCE: dict[str, object] = {
    "type": ["integer", "string"],
    "description": "证据：真实事件 ID，或本轮工具结果 tool_N；省略为当前消息",
}
_RUNTIME_SNAPSHOT: ContextVar[RuntimeConfigSnapshot | None] = ContextVar(
    "agent_tool_runtime_snapshot",
    default=None,
)
_MEMORY_READ_TOOL: ContextVar[str] = ContextVar("memory_read_tool", default="")
_OBSERVED_MEMORY_READS = frozenset({"search_memory"})

logger = logging.getLogger(__name__)
_CODE = re.compile(r"[a-z][a-z0-9_]{2,80}")


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """Authorization and scene data that cannot be supplied by the model."""

    inbound: InboundMessage | None
    declaration_only: bool = False
    conversation_key: str = ""
    trigger_event_id: int | None = None
    actor_context: ToolActor | None = None
    actor_is_superuser: bool = False
    runtime_config: RuntimeConfigSnapshot | None = None
    origin: TurnOrigin = TurnOrigin.USER_MESSAGE
    tools_closed: bool = False
    read_scope: ConversationScope | None = None
    read_target_id: str | None = None
    turn_token: TurnToken | None = None
    turn_snapshot: ConversationTurnSnapshot | None = None
    visible_event_ids: Set[int] = frozenset()
    selection_query: str = ""
    sandbox_source: dict[str, Any] | None = None
    execution_id: str = ""
    initiative_run_id: str | None = None
    memory_session: object | None = None
    memory_allowed_scopes: tuple[MemoryScopeType, ...] | None = None
    preparation_model_requests: int = 0
    before_model_request: Callable[[], Awaitable[None]] | None = None
    observation_boundary: ContextBoundaryReader | None = None
    public_compaction: PublicCompactContext | None = None
    compact_focus: str | None = None
    scope_type: ScopeType | None = None
    conversation_id: str | None = None
    person_id: str | None = None
    space_id: str | None = None
    external_target_id: str | None = None
    target_presence_id: str | None = None
    context_plugin_id: str | None = None
    context_read_contract: str | None = None
    activation_context: Callable[[], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.inbound is not None:
            incoming = replace(
                ToolActor.from_inbound(self.inbound),
                event_id=self.effective_trigger_event_id,
                origin=self.origin,
            )
            if self.actor_context is not None and self.actor_context != incoming:
                raise PermissionError("tool_actor_context_mismatch")
            object.__setattr__(self, "actor_context", incoming)

    @property
    def actor_person_id(self) -> str | None:
        return self.actor_context.person_id if self.actor_context is not None else None

    @property
    def actor_user_id(self) -> str:
        return self.actor_context.user_id if self.actor_context is not None else ""

    @property
    def current_group_id(self) -> str | None:
        if self.actor_context is not None:
            return self.actor_context.group_id
        return self.read_scope.group_id if self.read_scope is not None else None

    @property
    def mentioned_user_ids(self) -> tuple[str, ...]:
        return self.actor_context.mentioned_user_ids if self.actor_context is not None else ()

    @property
    def bot_user_id(self) -> str | None:
        if self.actor_context is not None:
            return self.actor_context.bot_user_id
        return self.read_scope.bot_user_id if self.read_scope is not None else None

    @property
    def presence_id(self) -> str | None:
        return (
            self.actor_context.presence_id
            if self.actor_context is not None
            else self.target_presence_id
        )

    @property
    def effective_trigger_event_id(self) -> int | None:
        if self.turn_snapshot is not None:
            return self.turn_snapshot.trigger_event_id
        if self.trigger_event_id is not None:
            return self.trigger_event_id
        return self.inbound.source_event_id if self.inbound is not None else None

    @property
    def effective_execution_id(self) -> str:
        if self.execution_id:
            return self.execution_id
        if self.effective_trigger_event_id is not None:
            return f"event:{self.effective_trigger_event_id}"
        if self.inbound is not None and self.inbound.source_execution_id:
            return self.inbound.source_execution_id
        raise ValueError("missing_internal_execution_anchor")

    @property
    def effective_scope_type(self) -> ScopeType:
        if self.inbound is not None:
            return self.inbound.scope_type
        if self.scope_type is None:
            raise RuntimeError("tool runtime scope is unavailable")
        return self.scope_type

    @property
    def effective_bot_user_id(self) -> str | None:
        return self.inbound.bot_user_id if self.inbound is not None else self.bot_user_id

    @property
    def effective_conversation_id(self) -> str | None:
        return self.inbound.conversation_id if self.inbound is not None else self.conversation_id

    @property
    def effective_presence_id(self) -> str | None:
        return self.inbound.presence_id if self.inbound is not None else self.presence_id

    def require_actor(self) -> ToolActor:
        if self.inbound is not None:
            if self.actor_context is None:
                raise PermissionError("tool_actor_unavailable")
            return self.actor_context
        actor: ToolActor | None = self.actor_context
        if self.origin is TurnOrigin.SELF_INITIATIVE or (
            self.origin is TurnOrigin.SCHEDULED_AUTOMATION
            and actor is not None
            and actor.principal_kind == "self"
        ):
            if (
                actor is None
                or actor.principal_kind != "self"
                or actor.origin is not self.origin
                or actor.user_id
                or self.actor_is_superuser
                or actor.person_id is not None
                or self.person_id is not None
                or (self.origin is TurnOrigin.SELF_INITIATIVE and not self.initiative_run_id)
                or (
                    self.origin is TurnOrigin.SCHEDULED_AUTOMATION
                    and self.initiative_run_id is not None
                )
                or actor.initiative_run_id != self.initiative_run_id
                or (
                    self.origin is TurnOrigin.SCHEDULED_AUTOMATION
                    and (
                        self.sandbox_source is None
                        or actor.automation_run_id != self.sandbox_source.get("automation_run_id")
                    )
                )
                or actor.execution_id != self.execution_id
                or actor.conversation_id != self.effective_conversation_id
                or self.effective_trigger_event_id is not None
            ):
                raise PermissionError("self_actor_context_mismatch")
            return actor
        if (
            actor is None
            or actor.origin is not self.origin
            or not actor.execution_id
            or actor.execution_id != self.execution_id
            or actor.conversation_id != self.effective_conversation_id
        ):
            raise PermissionError("tool_actor_unavailable")
        return actor

    def validate_source(self) -> None:
        """Reject an inconsistent source before a real execution backend is installed.

        Declaration-only runtimes never execute and do not call this entry.
        """
        scope = self.effective_scope_type  # RuntimeError when the scope is unavailable.
        if scope is ScopeType.GROUP and not self.current_group_id:
            raise InvalidTurnContextError("group scene requires a group id")
        actor = self.actor_context
        if actor is not None and actor.principal_kind == "self":
            if (
                self.origin not in {TurnOrigin.SELF_INITIATIVE, TurnOrigin.SCHEDULED_AUTOMATION}
                or (self.origin is TurnOrigin.SELF_INITIATIVE and not self.initiative_run_id)
                or (
                    self.origin is TurnOrigin.SCHEDULED_AUTOMATION
                    and self.initiative_run_id is not None
                )
            ):
                raise InvalidTurnContextError("invalid self turn authority")
        elif self.initiative_run_id is not None or self.origin is TurnOrigin.SELF_INITIATIVE:
            raise InvalidTurnContextError("invalid turn principal")

    def require_inbound(self) -> InboundMessage:
        if self.inbound is None:
            raise RuntimeError("tool requires a direct message actor")
        return self.inbound

    def conversation_scope(self) -> ConversationScope:
        """Resolve the authenticated conversation without inventing a message actor."""

        if self.read_scope is not None:
            if self.effective_conversation_id is not None:
                return replace(
                    self.read_scope,
                    key=f"conversation:{self.effective_conversation_id}",
                    conversation_id=self.effective_conversation_id,
                )
            return self.read_scope
        if self.inbound is not None:
            return self.inbound.scope()
        bot_user_id = (self.bot_user_id or "").strip()
        target_id = (self.external_target_id or "").strip()
        if not bot_user_id or not target_id:
            raise RuntimeError("tool runtime conversation is unavailable")
        platform = self.require_actor().platform
        if self.effective_scope_type is ScopeType.GROUP:
            group_id = (self.current_group_id or target_id).strip()
            return ConversationScope.group(
                bot_user_id,
                group_id,
                platform=platform,
                presence_id=self.effective_presence_id,
                conversation_id=self.effective_conversation_id,
            )
        return ConversationScope.private(
            bot_user_id,
            target_id,
            platform=platform,
            presence_id=self.effective_presence_id,
            conversation_id=self.effective_conversation_id,
        )


@dataclass(frozen=True, slots=True)
class _ToolFailure:
    code: str
    detail: str


# Real causes of common memory-write rejections, said the way the model can act on.
_MEMORY_REJECTIONS = {
    "self_memory_requires_self_subject_ref": (
        'SELF 自主工作只能写自己的记忆：target 用 {"self": true}'
    ),
    "evidence_quote_not_in_tool_receipt": "quote 必须逐字出自本轮工具结果原文",
    "initiative_tool_evidence_required": "SELF 写记忆须给出本轮工具结果中的 quote，不能用事件 ID",
    "evidence_quote_not_in_current_event": "quote 必须逐字出自证据消息原文",
    "unsupported_evidence_reference": "有当前消息时 evidence 用事件 ID，不用 tool_N",
    "private_self_fact_cannot_be_global": (
        "私聊中的经历不能设为 global；只有偏好（kind=preference）可以"
    ),
    "operation_not_allowed_for_target": "不能对他人的个人记忆执行该操作",
    "fact_id_required": "没有这条记忆；先用 search_memory 找到 fact_id",
    "fact_target_mismatch": "该 fact_id 不属于可编辑的记忆",
    "self_memory_disabled": "自我记忆功能未启用",
    "memory_operation_payload_conflict": "同一调用已以不同内容执行过，不会重复写入",
}


def _object_schema(
    properties: dict[str, object],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def database_failure_result(
    error: BaseException, *, provider_id: str = "", tool_name: str = ""
) -> ToolExecutionResult:
    from yuki.persistence.database import (
        write_failure_committed,
        write_failure_phase,
        write_failure_retryable,
    )

    committed = write_failure_committed(error)
    return ToolExecutionResult(
        ok=False,
        error_code="database_failure",
        public_message="数据库操作失败",
        retryable=write_failure_retryable(error),
        uncertain=committed is None,
        mutation_committed=committed,
        provider_id=provider_id,
        tool_name=tool_name,
        metadata={"database_write_phase": write_failure_phase(error)},
    )


class AgentToolService:
    """Define and execute tools without granting authority through prompt text."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedgerRepository,
        memories: MemoryFactService,
        social_service: SocialService,
        workspace_service: WorkspaceService,
        sandbox_client: SandboxClient,
        external_agent_service: ExternalAgentService,
        memory_context: MemoryContextService | None = None,
        memory_mutations: MemoryMutationService | None = None,
        web_provider: WebSearchProvider | None = None,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._ledger = ledger
        self._memories = memories
        self._memory_repository = memories.repository
        self._people = PeopleRepository(ledger._database)
        self._memory_reads = MemoryReadScopeResolver(memories.repository.database)
        if memory_context is None:
            memory_context = MemoryContextService(
                query_builder=MemoryQueryBuilder(MemoryTargetResolver(self._people)),
                retriever=MemoryRetriever(
                    repository=self._memory_repository,
                    lexical_index=SQLiteMemoryFTSIndex(ledger._database),
                ),
            )
        self._memory_context = memory_context
        self._memory_mutations = memory_mutations
        self._web_provider = web_provider
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=ledger._database,
        )
        self.social_service = social_service
        self.workspace_service = workspace_service
        self.sandbox_client = sandbox_client
        self.external_agent_service = external_agent_service

    @staticmethod
    def _work_source() -> dict[str, str]:
        from yuki.runtime.work_activation import current_work_control

        control = current_work_control.get()
        return {"work_id": control.current["id"]} if control and control.current else {}

    def definitions(self, runtime: ToolRuntime) -> tuple[ChatTool, ...]:
        tools = [
            ChatTool(
                name="search_memory",
                namespace="memory.search",
                effect="read_state",
                description=(
                    "搜索有来源的长期记忆，或用 fact_id 读取一条。省略 target 时搜索你有权读取的"
                    "全部人物、群与自己的记忆，当前群或提及不会自动缩小范围。结果有界、可能不全，"
                    "缺证据时换个说法再查；原始聊天用 read_chat。"
                ),
                parameters=_object_schema(
                    {
                        "query": {"type": "string", "minLength": 1},
                        "target": _MEMORY_TARGET,
                        "since": {"type": "string", "description": "带时区 ISO-8601 起点（含）"},
                        "until": {"type": "string", "description": "带时区 ISO-8601 终点（不含）"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "fact_id": {"type": "integer", "minimum": 1},
                    }
                ),
            ),
        ]
        if self._memory_mutations is not None:
            tools.extend(
                (
                    ChatTool(
                        name="save_memory",
                        namespace="memory.state.write",
                        effect="write_state",
                        description=(
                            "保存一条长期记忆到 target 指定的人物、群或自己。证据须是真实消息或"
                            "本轮工具结果，不能用自己的输出或编造事件；SELF 自主工作只能写自己的"
                            "记忆并给出 quote。以结果中的 applied 与 outcome 为准，"
                            "未写入不要说已记住。"
                        ),
                        parameters=_object_schema(
                            {
                                "content": {"type": "string", "minLength": 1},
                                "target": _MEMORY_TARGET,
                                "group": {
                                    "type": "string",
                                    "description": "与人物 target 同给时，记为此人在该群的记忆",
                                },
                                "kind": {
                                    "type": "string",
                                    "enum": ["fact", "preference", "episode"],
                                },
                                "importance": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 5,
                                    "description": (
                                        "1–2 临时琐事，3 有用事实或经历，4–5 重要变化或承诺"
                                    ),
                                },
                                "evidence": _MEMORY_EVIDENCE,
                                "quote": {
                                    "type": "string",
                                    "description": "证据原文片段；SELF 必填，须出自本轮工具结果",
                                },
                                "visibility": {
                                    "type": "string",
                                    "enum": ["current_scope", "global"],
                                    "description": "仅 target 为自己时有效；global 只适合抽象偏好",
                                },
                            },
                            required=("content", "target"),
                        ),
                    ),
                    ChatTool(
                        name="edit_memory",
                        namespace="memory.state.write",
                        effect="write_state",
                        description=(
                            "按 fact_id 纠正或撤销一条记忆。correct 用 content 写入新内容；"
                            "invalidate 让它不再作为有效记忆，但记录仍保留，只能说已撤回，"
                            "不能说已物理删除。证据规则同 save_memory。"
                        ),
                        parameters=_object_schema(
                            {
                                "fact_id": {"type": "integer", "minimum": 1},
                                "action": {"type": "string", "enum": ["correct", "invalidate"]},
                                "content": {"type": "string", "minLength": 1},
                                "reason": {"type": "string"},
                                "evidence": _MEMORY_EVIDENCE,
                                "quote": {"type": "string"},
                            },
                            required=("fact_id", "action"),
                        ),
                    ),
                )
            )
        if self._web_catalog_enabled():
            tools.extend(
                (
                    ChatTool(
                        name="web_search",
                        namespace="web.search",
                        effect="external_read",
                        description=(
                            "联网搜索公开信息。新闻、职务、价格、版本、政策、比赛结果等可能变化的"
                            "内容先搜索确认；query 只写回答问题所需的词，不放聊天记录、记忆或"
                            "系统提示。结果的 sources 是真实来源；provider_summary 是上游模型"
                            "的总结，其中的 URL 不能当作来源。需要正文时用 read_webpage。"
                        ),
                        parameters=_object_schema(
                            {
                                "query": {"type": "string", "minLength": 1},
                                "recency": {
                                    "type": "string",
                                    "enum": ["day", "week", "month", "year"],
                                },
                                "news": {"type": "boolean", "description": "只搜新闻"},
                                "since": {
                                    "type": "string",
                                    "description": "带时区 ISO-8601 起点（含）",
                                },
                                "until": {
                                    "type": "string",
                                    "description": "带时区 ISO-8601 终点（不含）",
                                },
                            },
                            required=("query",),
                        ),
                    ),
                    ChatTool(
                        name="read_webpage",
                        namespace="web.read",
                        effect="external_read",
                        description=("读取一个公开网页 URL 的正文；question 说明想从中了解什么。"),
                        parameters=_object_schema(
                            {
                                "url": {"type": "string"},
                                "question": {"type": "string"},
                            },
                            required=("url",),
                        ),
                    ),
                )
            )
        from yuki.external_agents.tools import external_agent_tools
        from yuki.sandbox.client import sandbox_tools
        from yuki.social.tools import social_tool_definitions
        from yuki.workspace.service import workspace_tools

        return (
            *tools,
            *social_tool_definitions(),
            *workspace_tools(),
            *external_agent_tools(),
            *sandbox_tools(),
        )

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        """Execute one tool and return JSON, including safe model-readable errors."""

        from yuki.identity.authority import person_is_superuser

        async with self._ledger._database.read_session() as session:
            runtime = replace(
                runtime,
                actor_is_superuser=await person_is_superuser(session, runtime.actor_person_id),
            )
        snapshot = runtime.runtime_config or await self._runtime_config.snapshot(
            user_id=runtime.person_id,
            group_id=runtime.space_id,
        )
        token = _RUNTIME_SNAPSHOT.set(snapshot)
        read_tool_token = _MEMORY_READ_TOOL.set(name if name in _OBSERVED_MEMORY_READS else "")
        try:
            try:
                arguments = json.loads(arguments_json)
            except json.JSONDecodeError:
                return self._result(error="invalid_json", detail="工具参数不是有效 JSON")
            if not isinstance(arguments, dict):
                return self._result(error="invalid_arguments", detail="工具参数必须是对象")
            try:
                from yuki.social.tools import SOCIAL_TOOL_NAMES

                if name in SANDBOX_TOOLS or name in EXTERNAL_AGENT_TOOLS:
                    from yuki.capabilities.invocation import current_invocation

                    invocation = current_invocation.get()
                    if runtime.tools_closed:
                        return self._result(error="permission_denied", detail="本轮未授权执行操作")
                    if invocation is None:
                        return self._result(
                            error="external_agent_unavailable"
                            if name in EXTERNAL_AGENT_TOOLS
                            else "sandbox_unavailable",
                            detail="执行服务未连接",
                        )
                    from hashlib import sha256

                    request_id = sha256(
                        (
                            f"{runtime.conversation_id}:"
                            f"{runtime.effective_execution_id}:"
                            f"{invocation.invocation_key}"
                        ).encode()
                    ).hexdigest()
                    from yuki.runtime.work_activation import current_work_control

                    work_control = current_work_control.get()
                    if work_control is not None and work_control.session is not None:
                        request_id = sha256(invocation.invocation_key.encode()).hexdigest()
                    source = (
                        {**runtime.sandbox_source, **self._work_source()}
                        if runtime.sandbox_source
                        else {
                            **self._work_source(),
                            "conversation_id": runtime.effective_conversation_id,
                            "origin": runtime.origin.value,
                            "actor_is_superuser": runtime.actor_is_superuser,
                            "actor_person_id": runtime.actor_person_id,
                            "actor_user_id": runtime.actor_user_id,
                            "bot_user_id": runtime.effective_bot_user_id,
                            "presence_id": runtime.effective_presence_id,
                            "generation": runtime.turn_snapshot.generation
                            if runtime.turn_snapshot
                            else None,
                            "trigger_event_id": runtime.effective_trigger_event_id,
                            "parent_execution_id": runtime.effective_execution_id,
                        }
                    )
                    source["actor_person_id"] = runtime.actor_person_id
                    view: Callable[[dict[str, Any]], dict[str, Any]] | None
                    try:
                        if name in EXTERNAL_AGENT_TOOLS:
                            execution_result = await self.external_agent_service.execute(
                                coding_agent_arguments(name, arguments),
                                request_id=request_id,
                                source=source,
                            )
                            view = coding_agent_view
                        else:
                            from yuki.sandbox.client import MODEL_TOOL_RPC_METHODS

                            execution_result = await self.sandbox_client.execute(
                                MODEL_TOOL_RPC_METHODS[name],
                                command_arguments(name, arguments),
                                request_id=request_id,
                                source=source,
                            )
                            view = None if name == "get_environment" else command_view
                    except ExternalAgentArgumentError:
                        raise
                    except ValueError as exc:
                        from yuki.persistence.database import write_failure_phase

                        if write_failure_phase(exc) is not None or not _CODE.fullmatch(str(exc)):
                            raise
                        # Pre-dispatch refusals of the service name the failing argument.
                        return self._result(
                            error=str(exc), detail="目标或参数无效，检查 target、id 与 device"
                        )
                    uncertain = bool(execution_result.get("uncertain"))
                    shown = view(execution_result) if view is not None else None
                    if execution_result.get("error"):
                        failed = self._result(
                            data=execution_result,
                            error=str(execution_result["error"]),
                            detail="结果未知，不要重新提交；按 id 或 operation_id 查询原执行。"
                            if uncertain
                            else "执行没有完成",
                            uncertain=uncertain,
                        )
                        return replace(failed, view=shown)
                    return replace(self._result(data=execution_result), view=shown)

                if name in WORKSPACE_TOOLS:
                    from yuki.workspace.store import WorkspaceError

                    if runtime.tools_closed:
                        return self._result(
                            error="permission_denied", detail="本轮未授权工作区操作"
                        )
                    try:
                        from yuki.capabilities.invocation import current_invocation

                        invocation = current_invocation.get()
                        workspace_result = await self.workspace_service.execute(
                            name,
                            arguments,
                            runtime=runtime,
                            source={"actor_person_id": runtime.actor_person_id},
                            request_id=invocation.invocation_key
                            if invocation is not None
                            else None,
                        )
                        return self._result(
                            data=workspace_result,
                            # An unconfirmed copy is reported as unknown, never as done.
                            error=str(workspace_result["error"])
                            if workspace_result.get("error")
                            else "copy_unconfirmed"
                            if workspace_result.get("uncertain")
                            else None,
                            detail=str(workspace_result.get("message") or ""),
                            uncertain=bool(workspace_result.get("uncertain")),
                        )
                    except (WorkspaceError, ValueError, OSError) as exc:
                        from yuki.persistence.database import write_failure_phase

                        if write_failure_phase(exc) is not None:
                            raise
                        category = (
                            str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
                        )
                        detail = {
                            "attachment_not_found": "这条消息没有该附件；先读聊天记录，"
                            "用真实 event_id 与从 0 开始的 attachment",
                            "choose_one_source": "来源三选一：path（可带 device）、"
                            "artifact_id，或 event_id 加 attachment",
                            "source_not_supported_for_destination": "暂不支持从本人电脑复制，"
                            "也不支持把 artifact_id 复制进 Yuki 环境",
                        }.get(category, "文件操作没有完成")
                        return self._result(error=category, detail=detail)

                if name == "read_chat":
                    return await self._query_history(arguments, runtime)
                if name in SOCIAL_TOOL_NAMES:
                    from yuki.social.agent_adapter import (
                        SOCIAL_ERROR_MESSAGES,
                        invoke_social,
                        members_view,
                        receipt_view,
                    )
                    from yuki.social.models import SocialError

                    try:
                        social_result = await invoke_social(
                            self.social_service, name, arguments, runtime
                        )
                    except SocialError as exc:
                        from yuki.persistence.database import (
                            write_failure_committed,
                            write_failure_phase,
                        )

                        if (
                            write_failure_phase(exc) is not None
                            and write_failure_committed(exc) is not False
                        ):
                            return await self._database_failure(exc, name=name, runtime=runtime)
                        return self._result(
                            error=str(exc),
                            detail=SOCIAL_ERROR_MESSAGES.get(
                                str(exc), "操作未执行；检查参数后再决定是否调用"
                            ),
                        )
                    social_view = (
                        receipt_view(social_result)
                        if name in {"send", "recall_message"}
                        else members_view(social_result)
                        if name == "get_members"
                        else None
                    )
                    if social_result.get("error") or social_result.get("status") in {
                        "failed",
                        "uncertain",
                    }:
                        uncertain = social_result.get("status") == "uncertain"
                        sent_parts = int(social_result.get("sent_messages") or 0)
                        error = str(
                            social_result.get("error")
                            or ("delivery_uncertain" if uncertain else "delivery_failed")
                        )
                        return replace(
                            self._result(
                                error=error,
                                detail=SOCIAL_ERROR_MESSAGES.get(error)
                                or (
                                    "部分分条已发送；不要重发已成功的分条"
                                    if sent_parts
                                    else "发送结果未知，不要重发；按 event_id 查证后再说明"
                                    if uncertain
                                    else "发送前或发送时明确失败，没有消息送达"
                                ),
                                data=social_result,
                                uncertain=uncertain,
                            ),
                            view=social_view,
                        )
                    return replace(self._result(data=social_result), view=social_view)
                if name == "search_memory":
                    result = await self._search_memory(arguments, runtime)
                    return await self._capture_memory_tool_result(result, runtime)
                if name == "save_memory":
                    return await self._save_memory(arguments, runtime)
                if name == "edit_memory":
                    return await self._edit_memory(arguments, runtime)
                if name == "web_search":
                    return await self._web_search(arguments, runtime)
                if name == "read_webpage":
                    return await self._read_webpage(arguments, runtime)
                return self._result(error="unknown_tool", detail=f"未知工具：{name}")
            except WebSearchError as exc:
                return self._result(error=exc.code, detail=exc.detail)
            except MemoryRetrievalError as exc:
                await self._record_memory_tool_outcome(runtime, "infrastructure_failure")
                return self._result(error=exc.code, detail="记忆检索失败", retryable=True)
            except SQLAlchemyError as exc:
                return await self._database_failure(exc, name=name, runtime=runtime)
            except ExternalAgentArgumentError as exc:
                return self._result(error=exc.code, detail=exc.detail, retryable=False)
            except (TypeError, ValueError) as exc:
                from yuki.persistence.database import write_failure_committed, write_failure_phase

                if (
                    write_failure_phase(exc) is not None
                    and write_failure_committed(exc) is not False
                ):
                    return await self._database_failure(exc, name=name, runtime=runtime)
                if name in _OBSERVED_MEMORY_READS:
                    return self._result(error="invalid_arguments", detail=str(exc))
                return self._result(error=type(exc).__name__, detail="工具执行失败")
            except (OSError, RuntimeError) as exc:
                from yuki.persistence.database import write_failure_phase

                if write_failure_phase(exc) is not None:
                    return await self._database_failure(exc, name=name, runtime=runtime)
                return self._result(error=type(exc).__name__, detail="工具执行失败")
        finally:
            _MEMORY_READ_TOOL.reset(read_tool_token)
            _RUNTIME_SNAPSHOT.reset(token)

    async def _database_failure(
        self,
        error: BaseException,
        *,
        name: str,
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        logger.exception("agent_tool_database_failure tool=%s", name)
        await self._record_memory_tool_outcome(runtime, "infrastructure_failure")
        return database_failure_result(error)

    async def _query_history(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        from pydantic import ValidationError

        from yuki.social.agent_adapter import SOCIAL_ERROR_MESSAGES, invoke_social, validation_code
        from yuki.social.models import SocialError

        selected = dict(arguments)
        if "around" in selected:
            selected.setdefault("context", self._settings.conversation_history_around_context)
        else:
            selected.setdefault("limit", self._settings.recent_history_tool_limit)
        try:
            if runtime.origin is not TurnOrigin.PLUGIN_BACKGROUND:
                data = await invoke_social(self.social_service, "read_chat", selected, runtime)
            else:
                from yuki.social.history import read_history
                from yuki.social.service import SocialContext

                context = SocialContext(
                    turn_id=runtime.effective_execution_id,
                    call_id="history",
                    conversation_id=runtime.effective_conversation_id or "",
                    actor=runtime.actor_context,
                    space_id=runtime.space_id,
                    origin=runtime.origin.value,
                )
                try:
                    data = await read_history(
                        self._ledger._database,
                        selected,
                        context,
                        router=self.social_service.router,
                        runtime=runtime,
                    )
                except ValidationError as exc:
                    raise SocialError(validation_code(exc)) from None
        except SocialError as exc:
            return self._result(
                error=str(exc),
                detail=SOCIAL_ERROR_MESSAGES.get(str(exc), "无法读取所选聊天范围"),
            )
        # Every outer forward failed: the real first error, with each group's state.
        failed = [group["error"] for group in data.get("forwards", ()) if "error" in group]
        result = (
            self._result(
                error=failed[0],
                detail=SOCIAL_ERROR_MESSAGES.get(failed[0], "合并转发没有读取到"),
                data=data,
            )
            if failed and len(failed) == len(data["forwards"])
            else self._result(data=data)
        )
        return replace(
            result,
            source_event_ids=getattr(data, "source_event_ids", ()),
            source_privacy_generation=getattr(data, "source_privacy_generation", None),
        )

    @staticmethod
    def _memory_target(value: Any) -> tuple[str, str] | _ToolFailure | None:
        """One internal selector: ("person_id"|"space_id", ID) or ("self", "")."""
        if value is None:
            return None
        if not isinstance(value, dict) or len(value) != 1:
            return _ToolFailure(
                "invalid_target", "target 恰含一个键：person_id、space_id 或 self=true"
            )
        ((key, item),) = value.items()
        if key == "self" and item is True:
            return "self", ""
        if key in {"person_id", "space_id"} and isinstance(item, str) and item.strip():
            return key, item.strip()
        return _ToolFailure("invalid_target", "target 恰含一个键：person_id、space_id 或 self=true")

    async def _search_memory(
        self,
        arguments: dict[str, Any],
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        unknown = set(arguments) - {"query", "target", "since", "until", "limit", "fact_id"}
        if unknown:
            return self._result(
                error="invalid_arguments",
                detail=f"search_memory 不接受参数：{', '.join(sorted(unknown))}",
            )
        query = arguments.get("query")
        if "fact_id" in arguments:
            if set(arguments) != {"fact_id"}:
                return self._result(
                    error="invalid_arguments",
                    detail="fact_id 读取单条记忆时不要再给 query、target 或时间范围",
                )
            return await self._memory_fact(arguments, runtime)
        if not isinstance(query, str) or not query.strip():
            return self._result(
                error="invalid_arguments", detail="提供非空 query 搜索，或用 fact_id 读取一条记忆"
            )
        selected = self._memory_target(arguments.get("target"))
        if isinstance(selected, _ToolFailure):
            return self._result(error=selected.code, detail=selected.detail)
        try:
            intent = parse_memory_tool_intent(arguments)
        except ValueError as exc:
            return self._result(error="invalid_time", detail=str(exc))
        scope: ResolvedReadScope
        authorized_scope: AuthorizedMemoryScope | None = None
        if selected is None:
            requester = self._social_requester(runtime)
            if requester is None:
                try:
                    actor = runtime.require_actor()
                except PermissionError:
                    return self._result(
                        error="permission_denied", detail="记忆搜索需要可信执行主体"
                    )
                # SELF work has only its authenticated scene, not an inherited
                # human identity or an arbitrary historical social scope.
                if actor.principal_kind != "self":
                    return self._result(error="permission_denied", detail="无法确定记忆读取主体")
            authorized_scope = await self._memory_reads.authorized(
                requester,
                current_space_id=self._runtime_space_id(runtime),
                private_scene=runtime.effective_scope_type is ScopeType.PRIVATE,
                self_actor=requester is None,
                allowed_scopes=self._readable_scopes(runtime),
            )
            scope = ResolvedReadScope(targets=())
        else:
            kind, identity = selected
            if runtime.memory_allowed_scopes is not None:
                requested_scope = {
                    "person_id": MemoryScopeType.PERSON,
                    "space_id": MemoryScopeType.GROUP,
                    "self": MemoryScopeType.SELF,
                }[kind]
                if requested_scope not in runtime.memory_allowed_scopes:
                    return self._result(
                        error="permission_denied", detail="插件未获该记忆范围读取权限"
                    )
            if kind == "person_id":
                selection = await self._person_memory_selection(identity, runtime)
                if isinstance(selection, _ToolFailure):
                    return self._result(error=selection.code, detail=selection.detail)
                scope = ResolvedReadScope(targets=selection)
            elif kind == "space_id":
                requester = self._social_requester(runtime)
                if requester is not None:
                    targets = (await self._memory_reads.group(requester, identity)).targets
                elif (
                    runtime.effective_scope_type is ScopeType.GROUP
                    and self._runtime_space_id(runtime) == identity
                    and runtime.actor_context is not None
                    and runtime.require_actor().principal_kind == "self"
                ):
                    targets = (
                        MemoryEntityTarget(
                            role=MemoryTargetRole.CURRENT_GROUP,
                            scope_type=MemoryScopeType.GROUP,
                            canonical_subject_space_id=identity,
                            block_id="current_group",
                        ),
                    )
                else:
                    targets = ()
                if not targets:
                    return self._result(error="permission_denied", detail="没有该群记忆的读取授权")
                scope = ResolvedReadScope(targets=targets)
            else:
                if not self._settings.self_memory_enabled:
                    return self._result(
                        error="self_memory_unavailable", detail="自我记忆功能未启用"
                    )
                self_target = await self._visible_self_memory_target(runtime)
                if self_target is None:
                    return self._result(
                        error="self_memory_unavailable", detail="当前会话不能读取自我记忆"
                    )
                scope = ResolvedReadScope(targets=(self_target,))
            requester = self._social_requester(runtime)
            if requester is not None:
                authorized_scope = await self._memory_reads.authorized(
                    requester,
                    current_space_id=self._runtime_space_id(runtime),
                    private_scene=runtime.effective_scope_type is ScopeType.PRIVATE,
                    allowed_scopes=(
                        tuple(MemoryScopeType)
                        if runtime.memory_allowed_scopes is None
                        else runtime.memory_allowed_scopes
                    ),
                )
                authorized_scope = authorized_scope.model_copy(
                    update={"resolved_targets": scope.targets}
                )
        limit = arguments.get("limit")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            return self._result(error="invalid_arguments", detail="limit 必须是 1～100 的整数")
        result = await MemoryQueryPlane(self._memory_context).read(
            MemoryReadConsumer.AGENT_TOOL,
            MemoryReadRequest(
                text=query.strip(),
                intent=intent,
                requested_limit=limit,
                resolved_scope=scope,
                authorized_scope=authorized_scope,
            ),
            runtime=self._runtime(),
        )
        data: dict[str, Any] = {
            "memories": [self._memory_json(hit.fact) for hit in result.hits],
            # Bounded retrieval: say whether this page is everything that matched.
            "exhaustive": result.exhaustive,
        }
        if result.truncated:
            data["truncated"] = True
        if result.partial_failure:
            data["partial_failure"] = result.partial_reason or True
        if result.semantic_degraded:
            data["semantic_degraded"] = True
        return self._result(data=data)

    def _readable_scopes(self, runtime: ToolRuntime) -> tuple[MemoryScopeType, ...]:
        allowed = runtime.memory_allowed_scopes
        if allowed is None:
            allowed = (
                MemoryScopeType.PERSON,
                MemoryScopeType.PERSON_GROUP,
                MemoryScopeType.GROUP,
                MemoryScopeType.SELF,
            )
        if not self._settings.self_memory_enabled:
            allowed = tuple(item for item in allowed if item is not MemoryScopeType.SELF)
        return allowed

    async def _person_memory_selection(
        self, person_id: str, runtime: ToolRuntime
    ) -> tuple[MemoryEntityTarget, ...] | _ToolFailure:
        requester = self._social_requester(runtime)
        if requester is not None:
            scope = await self._memory_reads.person(requester, person_id, group_id=None)
            if scope.targets:
                return scope.targets
            return _ToolFailure("permission_denied", "没有本人或历史共同空间关系授权")
        actor = runtime.require_actor()
        space_id = self._runtime_space_id(runtime)
        if actor.principal_kind != "self" or space_id is None:
            return _ToolFailure("permission_denied", "当前执行没有该人物的读取授权")
        from yuki.identity.person_identity import same_person_condition

        async with self._people.database.read_session() as session:
            membership = await session.scalar(
                select(MembershipModel.canonical_person_id).where(
                    same_person_condition(MembershipModel.canonical_person_id, person_id),
                    MembershipModel.canonical_space_id == space_id,
                )
            )
        if membership is None:
            return _ToolFailure("permission_denied", "只能读取当前空间真实成员在本空间的记忆")
        return (
            MemoryEntityTarget(
                role=MemoryTargetRole.REFERENCED_PERSON_GROUP,
                scope_type=MemoryScopeType.PERSON_GROUP,
                canonical_subject_person_id=person_id,
                canonical_subject_space_id=space_id,
                block_id=f"tool_person_group:{person_id}:{space_id}",
            ),
        )

    @staticmethod
    def _runtime_space_id(runtime: ToolRuntime) -> str | None:
        return (
            runtime.inbound.space_id
            if runtime.inbound is not None
            else runtime.space_id
            or (runtime.actor_context.space_id if runtime.actor_context is not None else None)
        )

    async def _visible_self_memory_target(self, runtime: ToolRuntime) -> MemoryEntityTarget | None:
        space_id = self._runtime_space_id(runtime)
        person_id = self._social_requester(runtime)
        if runtime.effective_scope_type is ScopeType.GROUP and space_id is not None:
            return MemoryEntityTarget(
                role=MemoryTargetRole.CURRENT_SELF,
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.GROUP,
                canonical_visibility_space_id=space_id,
                block_id="current_self",
            )
        if runtime.effective_scope_type is ScopeType.PRIVATE and person_id is not None:
            return MemoryEntityTarget(
                role=MemoryTargetRole.CURRENT_SELF,
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.PRIVATE,
                canonical_visibility_person_id=person_id,
                block_id="current_self",
            )
        return None

    async def _memory_fact(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        fact_id = arguments.get("fact_id")
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id <= 0:
            return self._result(error="invalid_arguments", detail="fact_id 必须是正整数")
        fact = await self._authorized_memory_fact(fact_id, runtime)
        if fact is None:
            return self._result(error="memory_not_found", detail="没有这条记忆，或你无权读取它")
        return self._result(data={"memories": [self._memory_json(fact)]})

    @staticmethod
    def _memory_evidence(arguments: dict[str, Any]) -> dict[str, Any] | _ToolFailure:
        """Map the model's evidence/quote onto the mutation request's source fields."""
        fields: dict[str, Any] = {}
        evidence = arguments.get("evidence")
        if isinstance(evidence, int) and not isinstance(evidence, bool) and evidence > 0:
            fields["evidence_event_id"] = evidence
        elif (
            isinstance(evidence, str)
            and evidence.startswith("tool_")
            and evidence[5:].isdigit()
            and int(evidence[5:]) > 0
        ):
            fields["evidence_refs"] = (evidence,)
        elif evidence is not None:
            return _ToolFailure(
                "invalid_evidence", "evidence 必须是真实事件 ID（正整数）或本轮工具结果 tool_N"
            )
        quote = arguments.get("quote")
        if quote is not None:
            if not isinstance(quote, str) or not quote.strip():
                return _ToolFailure("invalid_quote", "quote 必须是非空的证据原文片段")
            fields["evidence_quote"] = quote
        return fields

    async def _save_memory(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        unknown = set(arguments) - {
            "content",
            "target",
            "group",
            "kind",
            "importance",
            "evidence",
            "quote",
            "visibility",
        }
        if unknown:
            return self._result(
                error="invalid_arguments",
                detail=f"save_memory 不接受参数：{', '.join(sorted(unknown))}",
            )
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            return self._result(error="invalid_arguments", detail="content 必须是非空字符串")
        selected = self._memory_target(arguments.get("target"))
        if selected is None:
            return self._result(error="invalid_target", detail="save_memory 必须提供 target")
        if isinstance(selected, _ToolFailure):
            return self._result(error=selected.code, detail=selected.detail)
        kind_value = arguments.get("kind", "fact")
        if kind_value not in {"fact", "preference", "episode"}:
            return self._result(
                error="invalid_arguments", detail="kind 必须是 fact、preference 或 episode"
            )
        importance = arguments.get("importance")
        if importance is not None and (
            isinstance(importance, bool)
            or not isinstance(importance, int)
            or not 1 <= importance <= 5
        ):
            return self._result(error="invalid_arguments", detail="importance 必须是 1～5 的整数")
        visibility = arguments.get("visibility")
        if visibility not in {None, "current_scope", "global"}:
            return self._result(
                error="invalid_arguments", detail="visibility 必须是 current_scope 或 global"
            )
        group = arguments.get("group")
        if group is not None and (
            selected[0] != "person_id" or not isinstance(group, str) or not group.strip()
        ):
            return self._result(
                error="invalid_arguments", detail="group 只能与人物 target 同给，值为群的 space_id"
            )
        evidence = self._memory_evidence(arguments)
        if isinstance(evidence, _ToolFailure):
            return self._result(error=evidence.code, detail=evidence.detail)
        kind, identity = selected
        scope_type = (
            MemoryScopeType.SELF
            if kind == "self"
            else MemoryScopeType.GROUP
            if kind == "space_id"
            else MemoryScopeType.PERSON_GROUP
            if group is not None
            else MemoryScopeType.PERSON
        )
        override: ResolvedSubject | None = None
        if kind != "self":
            person_id = identity if kind == "person_id" else None
            space_id = identity if kind == "space_id" else group.strip() if group else None
            missing = await self._missing_memory_owner(person_id, space_id)
            if missing is not None:
                return self._result(error="target_not_found", detail=missing)
            override = ResolvedSubject(
                scope_type,
                None,
                None,
                canonical_subject_person_id=person_id,
                canonical_subject_space_id=space_id,
            )
        # The fact label is the kind; SELF labels keep the self_ prefix that
        # decides which SELF facts may be global.
        label = f"self_{kind_value}" if kind == "self" else str(kind_value)
        request = MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(
                subject_ref="self" if kind == "self" else kind, scope_type=scope_type
            ),
            visibility=visibility,
            new_content=content.strip(),
            memory_key=label,
            category=label,
            kind=MemoryKind(kind_value),
            importance=importance,
            **evidence,
        )
        return await self._mutate_memory(request, runtime, target=override)

    async def _missing_memory_owner(
        self, person_id: str | None, space_id: str | None
    ) -> str | None:
        """An internal ID must name a real Person/Space; this is not an authorization gate."""
        from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel

        async with self._people.database.read_session() as session:
            if person_id is not None and await session.get(CanonicalPersonModel, person_id) is None:
                return "没有这个 person_id；用 find_contacts 查询人物的内部 ID"
            if space_id is not None and await session.get(CanonicalSpaceModel, space_id) is None:
                return "没有这个群 space_id；用 find_contacts 查询群的内部 ID"
        return None

    async def _edit_memory(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        unknown = set(arguments) - {"fact_id", "action", "content", "reason", "evidence", "quote"}
        if unknown:
            return self._result(
                error="invalid_arguments",
                detail=f"edit_memory 不接受参数：{', '.join(sorted(unknown))}",
            )
        fact_id = arguments.get("fact_id")
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id <= 0:
            return self._result(error="invalid_arguments", detail="fact_id 必须是正整数")
        action = arguments.get("action")
        content = arguments.get("content")
        if action == "correct":
            if not isinstance(content, str) or not content.strip():
                return self._result(
                    error="invalid_arguments", detail="correct 需要非空 content 作为新内容"
                )
        elif action == "invalidate":
            if content is not None:
                return self._result(error="invalid_arguments", detail="invalidate 不接受 content")
        else:
            return self._result(
                error="invalid_arguments", detail="action 必须是 correct 或 invalidate"
            )
        reason = arguments.get("reason")
        if reason is not None and not isinstance(reason, str):
            return self._result(error="invalid_arguments", detail="reason 必须是字符串")
        evidence = self._memory_evidence(arguments)
        if isinstance(evidence, _ToolFailure):
            return self._result(error=evidence.code, detail=evidence.detail)
        request = MemoryMutationRequest(
            operation=MemoryMutationOperation(action),
            fact_id=fact_id,
            new_content=content.strip() if isinstance(content, str) else None,
            **({"reason": reason} if reason else {}),
            **evidence,
        )
        return await self._mutate_memory(request, runtime, target=None)

    async def _mutate_memory(
        self,
        request: MemoryMutationRequest,
        runtime: ToolRuntime,
        *,
        target: ResolvedSubject | None,
    ) -> ToolExecutionResult:
        service = self._memory_mutations
        if service is None:
            return self._result(error="memory_change_unavailable", detail="当前轮不能变更记忆")
        actor = runtime.require_actor()
        from yuki.capabilities.invocation import current_invocation

        invocation = current_invocation.get()
        if actor.principal_kind == "self":
            # SELF writes only its own memories, sourced from this round's tool results.
            context = MemoryMutationContext(
                event=None,
                conversation_key=runtime.conversation_key,
                turn_origin=runtime.origin.value,
                delegation_mode="main_agent",
                trigger_actor_user_id="",
                decision_actor_type=MemoryDecisionActorType.AGENT,
                decision_actor_id=runtime.effective_execution_id or "",
                executed_by_bot_user_id=runtime.effective_bot_user_id or "",
                initiative_run_id=runtime.initiative_run_id,
                source_group_id=runtime.current_group_id,
                operation_id=invocation.invocation_key if invocation is not None else None,
                actor_person_id=None,
            )
            return self._memory_change_result(await service.mutate(request, context))
        trigger_event_id = request.evidence_event_id or runtime.effective_trigger_event_id
        event = await self._ledger.get_event(trigger_event_id) if trigger_event_id else None
        if event is None:
            return self._result(
                error="evidence_event_not_found",
                detail="evidence 不是真实事件 ID；省略它即以当前消息为证据"
                if request.evidence_event_id is not None
                else "当前没有可作证据的入站消息；用 evidence 指定真实事件 ID",
            )
        # The evidence is a real inbound message of this actor in this conversation.
        if (
            event.author_person_id is None
            or event.author_person_id != actor.person_id
            or event.group_id != runtime.current_group_id
            or event.direction != "inbound"
            or event.bot_user_id != (runtime.effective_bot_user_id or "bot")
            or event.suppression_status not in {None, "keeper"}
            or (
                runtime.effective_conversation_id is not None
                and event.canonical_conversation_id != runtime.effective_conversation_id
            )
            or (
                runtime.effective_presence_id is not None
                and event.ingress_presence_id != runtime.effective_presence_id
            )
        ):
            return self._result(
                error="untrusted_evidence_event",
                detail="evidence 须是当前发言人在本会话发来的真实消息",
            )
        context = MemoryMutationContext(
            event=event,
            conversation_key=runtime.conversation_key,
            turn_origin=runtime.origin.value,
            delegation_mode="main_agent",
            trigger_actor_user_id=event.sender_user_id,
            decision_actor_type=MemoryDecisionActorType.AGENT,
            decision_actor_id=actor.execution_id or "main_agent",
            executed_by_bot_user_id=runtime.effective_bot_user_id or "bot",
            actor_is_superuser=runtime.actor_is_superuser,
            operation_id=invocation.invocation_key if invocation is not None else None,
            actor_person_id=event.author_person_id,
        )
        if target is None:
            result = await service.mutate(request, context)
        else:
            result = await service.mutate_resolved(request, context, target=target)
        return self._memory_change_result(result)

    def _memory_change_result(self, result: MemoryMutationResult) -> ToolExecutionResult:
        payload = result.tool_payload()
        if not result.ok:
            return self._result(
                data=payload,
                error=result.reason_code or "memory_change_rejected",
                detail=_MEMORY_REJECTIONS.get(
                    result.reason_code, "记忆未写入；按 error 调整请求，或如实说明未能记住"
                ),
            )
        return self._result(data=payload)

    @staticmethod
    def _social_requester(runtime: ToolRuntime) -> str | None:
        # An origin or arbitrary actor_user_id is not proof of a real user.
        if runtime.inbound is not None and runtime.origin in _MEMORY_CHANGE_ORIGINS:
            return runtime.inbound.person_id
        if runtime.inbound is not None and runtime.origin is TurnOrigin.PLUGIN_SESSION:
            try:
                actor = runtime.require_actor()
                return actor.person_id
            except PermissionError:
                return None
        if runtime.actor_context is not None:
            try:
                actor = runtime.require_actor()
                return actor.person_id
            except PermissionError:
                return None
        return None

    async def _authorized_memory_fact(self, fact_id: int, runtime: ToolRuntime) -> Any | None:
        """Hydrate details under the same current SQL ACL as no-target search."""
        requester = self._social_requester(runtime)
        if requester is None:
            try:
                if runtime.require_actor().principal_kind != "self":
                    return None
            except PermissionError:
                return None
        scope = await self._memory_reads.authorized(
            requester,
            current_space_id=self._runtime_space_id(runtime),
            private_scene=runtime.effective_scope_type is ScopeType.PRIVATE,
            self_actor=requester is None,
            allowed_scopes=self._readable_scopes(runtime),
        )
        facts = await self._memories.repository.get_active_authorized(scope, (fact_id,))
        return facts[0] if facts else None

    @staticmethod
    def _memory_json(row: Any) -> dict[str, Any]:
        """One fact as the model reads it: no aliases, internals or null fields."""
        payload: dict[str, Any] = {
            "fact_id": row.id,
            "scope": row.scope_type.value,
            "kind": row.kind.value,
            "content": row.content,
            "importance": row.importance,
            "confidence": row.confidence,
            "status": row.status.value,
            "last_confirmed_at": row.last_confirmed_at.isoformat(),
        }
        if row.scope_type is not MemoryScopeType.SELF:
            payload.update(
                {
                    key: value
                    for key, value in (
                        ("person_id", row.canonical_subject_person_id),
                        ("space_id", row.canonical_subject_space_id),
                    )
                    if value is not None
                }
            )
        if row.valid_from is not None:
            payload["occurred_at"] = row.valid_from.isoformat()
        if row.conflict_state.value != "clear":
            payload["conflict"] = row.conflict_state.value
        return payload

    async def _capture_memory_tool_result(
        self,
        result: ToolExecutionResult,
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        memories = result.data.get("memories") if isinstance(result.data, dict) else None
        count = len(memories) if isinstance(memories, list) else 0
        if result.ok:
            outcome = "success" if count else "empty"
        elif result.error_code == "permission_denied":
            outcome = "permission_denied"
        else:
            outcome = "unavailable"
        await self._record_memory_tool_outcome(runtime, outcome, result_count=count)
        return result

    async def _record_memory_tool_outcome(
        self, runtime: ToolRuntime, outcome: str, *, result_count: int = 0
    ) -> None:
        if not _MEMORY_READ_TOOL.get():
            return
        from yuki.runtime.observability import current_runtime_turn_correlation

        correlation = current_runtime_turn_correlation()
        logger.info(
            "memory_tool_read correlation_id=%s tool=%s outcome=%s result_count=%d",
            correlation.turn_id if correlation else "unbound",
            _MEMORY_READ_TOOL.get(),
            outcome,
            result_count,
        )

    async def _web_search(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        provider = self._web_dependencies()
        query = arguments.get("query")
        if not isinstance(query, str):
            return self._result(error="invalid_query", detail="query 必须是字符串")
        query = " ".join(query.split())
        if not query:
            return self._result(error="invalid_query", detail="query 不能为空")
        news = arguments.get("news", False)
        if type(news) is not bool:
            return self._result(error="invalid_arguments", detail="news 必须是布尔值")
        topic: WebSearchTopic = "news" if news else "general"
        time_range_value = arguments.get("recency")
        if time_range_value not in {None, "day", "week", "month", "year"}:
            return self._result(
                error="invalid_arguments", detail="recency 必须是 day、week、month 或 year"
            )
        time_range = cast(WebSearchTimeRange | None, time_range_value)
        start_date = self._search_day(arguments.get("since"), "since", end=False)
        end_date = self._search_day(arguments.get("until"), "until", end=True)
        if start_date is not None and end_date is not None and start_date > end_date:
            return self._result(error="invalid_time_range", detail="since 必须早于 until")
        response = await provider.search(
            WebSearchRequest(
                query=query,
                topic=topic,
                time_range=time_range,
                start_date=start_date,
                end_date=end_date,
                max_results=self._runtime().web.search_max_results,
            )
        )
        return self._result(data=self._web_response_json(response))

    async def _read_webpage(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        provider = self._web_dependencies()
        raw_url = arguments.get("url")
        if not isinstance(raw_url, str):
            return self._result(error="invalid_url", detail="url 必须是字符串")
        normalized = normalize_public_url(raw_url)
        question_value = arguments.get("question")
        if question_value is not None and not isinstance(question_value, str):
            return self._result(error="invalid_question", detail="question 必须是字符串")
        question = " ".join((question_value or "读取用户指定的网页").split())
        if not question:
            return self._result(error="invalid_question", detail="question 不能为空")
        source = await provider.extract(normalized, question)
        response = WebSearchResponse(
            query=question,
            sources=(source,),
            provider_request_id=None,
            latency_seconds=0,
            partial_failure=False,
            provider=source.provider,
        )
        return self._result(data=self._web_response_json(response))

    def pin_web_provider(self) -> AbstractContextManager[None]:
        """Hold the web backend chosen when a model/tool Runner starts."""
        pin = getattr(self._web_provider, "pin", None)
        return pin() if callable(pin) else nullcontext()

    def _web_catalog_enabled(self) -> bool:
        """Put web tools in the requestable catalog without choosing a provider.

        Catalog membership is not first-round exposure. Deployment mode and
        backend authorization control availability; no failure-driven switching.
        """

        return (
            self._settings.web.mode in {WebMode.NATIVE, WebMode.TAVILY, WebMode.BOTH}
            and self._web_provider is not None
        )

    def _web_dependencies(self) -> WebSearchProvider:
        if (
            self._settings.web.mode
            not in {
                WebMode.NATIVE,
                WebMode.TAVILY,
                WebMode.BOTH,
            }
            or self._web_provider is None
        ):
            raise WebSearchError("web_disabled", "联网搜索尚未启用")
        return self._web_provider

    @staticmethod
    def _web_response_json(response: WebSearchResponse) -> dict[str, Any]:
        result: dict[str, Any] = {
            "query": response.query,
            "sources": [
                {
                    key: value
                    for key, value in {
                        "source_id": source.source_id,
                        "title": source.title,
                        "url": source.url,
                        "snippet": source.snippet,
                        "relevant_content": source.relevant_content,
                        "published_at": (
                            source.published_at.isoformat() if source.published_at else None
                        ),
                    }.items()
                    if value not in (None, "")
                }
                for source in response.sources
            ],
        }
        if response.partial_failure:
            result["partial_failure"] = True
        if response.provider_summary:
            result["provider_summary"] = response.provider_summary
        return result

    @staticmethod
    def _search_day(value: Any, name: str, *, end: bool) -> date | None:
        """A timezone-qualified boundary as the provider's UTC+8 search day."""
        if value is None:
            return None
        message = f"{name} 必须是带时区的 ISO-8601 时间"
        if not isinstance(value, str):
            raise WebSearchError("invalid_time", message)
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WebSearchError("invalid_time", message) from exc
        if moment.tzinfo is None:
            raise WebSearchError("invalid_time", message)
        local = moment.astimezone(timezone(timedelta(hours=8)))
        # until is exclusive; the provider's end date is inclusive.
        return (local - timedelta(microseconds=1)).date() if end else local.date()

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    def _result(
        self,
        *,
        data: Any = None,
        error: str | None = None,
        detail: str = "",
        retryable: bool = False,
        uncertain: bool = False,
        mutation_committed: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        from yuki.capabilities.media import result_images

        return ToolExecutionResult(
            ok=error is None,
            data=data,
            error_code=error,
            public_message=detail or None,
            retryable=retryable,
            uncertain=uncertain,
            mutation_committed=mutation_committed
            if mutation_committed is not None
            else None
            if uncertain or error is None
            else False,
            metadata=metadata,
            images=result_images(data) if error is None else (),
        )

    @staticmethod
    def _runtime() -> RuntimeConfigSnapshot:
        runtime = _RUNTIME_SNAPSHOT.get()
        if runtime is None:
            raise RuntimeError("agent tool runtime snapshot is missing")
        return runtime
