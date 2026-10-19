"""Fixed model tools over authenticated runtime services and canonical memory."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Set
from contextlib import AbstractContextManager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import RuntimeConfigSnapshot
from yuki.admin.permission_catalog import CapabilityReport, PermissionCatalogService
from yuki.automation.models import TurnOrigin
from yuki.capabilities.results import ToolExecutionResult
from yuki.client_api.errors import ClientError
from yuki.config import Settings
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import ChatTool, InboundMessage
from yuki.domain.tool_actor import ToolActor
from yuki.executor.service import LocalExecutors
from yuki.memory.authorized_scope import AuthorizedMemoryScope
from yuki.memory.context import MemoryContextService
from yuki.memory.enums import (
    MemoryScopeType,
    MemoryTargetRole,
    SelfMemoryVisibility,
)
from yuki.memory.errors import MemoryRetrievalError
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.models import MemoryEntityTarget, MemoryQueryIntent
from yuki.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationAppliedOperation,
    MemoryMutationContext,
    MemoryMutationRequest,
    MemoryMutationResult,
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
from yuki.memory.tool_intent import effective_query_summary, parse_memory_tool_intent
from yuki.persistence.models import MembershipModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import (
    EventLedgerRepository,
)
from yuki.runtime.errors import InvalidTurnContextError
from yuki.sandbox.environment_tools import SANDBOX_TOOLS, is_execution
from yuki.services.context_boundary import ContextBoundaryReader
from yuki.services.context_compaction import PublicCompactContext
from yuki.services.evidence_state import evidence_state
from yuki.services.turn_coordinator import TurnToken
from yuki.time.formatting import local_iso
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
_MEMORY_INTENT_PROPERTIES = {
    "purpose": {
        "type": "string",
        "enum": ["background", "continuation", "recall", "verify", "correct"],
    },
    "start_at": {
        "type": "string",
        "description": (
            "带时区 ISO-8601 起点（包含）。当地某一天从当地00:00开始；"
            "转换为 UTC 后不要保留原时区偏移。今天/昨天按当前时间上下文换算。"
        ),
    },
    "end_at": {"type": "string", "description": "带时区 ISO-8601 终点（不包含），例如次日零点"},
    "temporal_constraint": {
        "type": "string",
        "enum": ["strict", "soft"],
        "description": (
            "有时间边界默认 strict；明确日期必须严格，不混入旧经历或时间未知项。"
            "soft 仅用于宽泛偏好，空结果不得自动放宽。"
        ),
    },
}
_RUNTIME_SNAPSHOT: ContextVar[RuntimeConfigSnapshot | None] = ContextVar(
    "agent_tool_runtime_snapshot",
    default=None,
)
_MEMORY_READ_TOOL: ContextVar[str] = ContextVar("memory_read_tool", default="")
_OBSERVED_MEMORY_READS = frozenset(
    {
        "search_memory",
        "read_memory",
    }
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """Authorization and scene data that cannot be supplied by the model."""

    inbound: InboundMessage | None
    declaration_only: bool = False
    conversation_key: str = ""
    trigger_message_id: str = ""
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
class _PersonMemorySelection:
    user_id: str
    targets: tuple[MemoryEntityTarget, ...]
    resolved_by: str
    subject_ref: str | None = None


@dataclass(frozen=True, slots=True)
class _ToolFailure:
    code: str
    detail: str
    data: Any = None


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


class AgentToolService:
    """Define and execute tools without granting authority through prompt text."""

    def __init__(
        self,
        *,
        settings: Settings,
        ledger: EventLedgerRepository,
        memories: MemoryFactService,
        memory_context: MemoryContextService | None = None,
        memory_mutations: MemoryMutationService | None = None,
        web_provider: WebSearchProvider | None = None,
        runtime_config: RuntimeConfigService | None = None,
        permission_catalog: PermissionCatalogService | None = None,
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
                facts=memories,
            )
        self._memory_context = memory_context
        self._memory_mutations = memory_mutations
        self._web_provider = web_provider
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=ledger._database,
        )
        self._permission_catalog = permission_catalog or PermissionCatalogService(
            settings=settings,
            config_registry=self._runtime_config.registry,
        )
        self.social_service: Any = None
        self.short_state: Any = None
        self.workspace_service: Any = None
        self.sandbox_client: Any = None
        self.local_executors: LocalExecutors | None = None

    @staticmethod
    def _work_source() -> dict[str, str]:
        from yuki.runtime.work_activation import current_work_control

        control = current_work_control.get()
        return {"work_id": control.current["id"]} if control and control.current else {}

    def definitions(self, runtime: ToolRuntime) -> tuple[ChatTool, ...]:
        bot_name = self._settings.bot_display_name
        tools = [
            ChatTool(
                name="local_device",
                description=(
                    "操作本次可信請求人自己的本機執行器。list 查能力與授權；"
                    "invoke 登記並等待最多20秒；"
                    "get 以原op_id查證。離線新請求不排隊。pending 時保留原ID，"
                    "Work可time_due稍後查詢。"
                    "unknown表示結果未知，不可當作失敗而重做；新操作須新invoke。"
                    "folder.read {path,mode:list|read}，"
                    "folder.write {path,content_base64|artifact_id+version,if_absent?}，"
                    "open.url {url}，open.file {path}，notify {title,body}。"
                    "path使用授權alias與相對路徑。"
                    "folder讀寫為confirmed；open與notify僅submitted，不代表使用者已看到或程式已載入。"
                ),
                parameters=_object_schema(
                    {
                        "action": {"type": "string", "enum": ["list", "invoke", "get"]},
                        "executor_id": {"type": "string"},
                        "capability": {"type": "string"},
                        "params": {"type": "object"},
                        "op_id": {"type": "string"},
                    },
                    required=("action",),
                ),
            ),
            ChatTool(
                name="get_my_capabilities",
                description=(
                    f"给 {bot_name} 当前模型轮内部查询原执行主体能够修改、管理和读取的权限。"
                    "用于查询可修改的配置、管理操作及读取范围；"
                    "按问题整理返回内容；"
                    "默认 summary，具体问题用 focused+category/query，需要完整目录用 full。"
                    "不能查询他人，也不能用权限目录代替工具执行回执。"
                ),
                parameters=_object_schema(
                    {
                        "mode": {
                            "type": "string",
                            "enum": ["summary", "focused", "full"],
                        },
                        "category": {"type": "string"},
                        "query": {"type": "string", "maxLength": 64},
                    }
                ),
            ),
            ChatTool(
                name="query_chat_history",
                result_cacheable=False,
                description=(
                    "读取当前 Conversation 的授权账本：recent 取近期消息，search 按 keyword、"
                    "内部 Person/Space 和 start_at/end_at 时间查原话；"
                    "around 按内部 event_id 取前后记录。"
                    "不调用平台补查，不接受平台消息号。before/after 是 around 的消息条数。"
                ),
                parameters=_object_schema(
                    {
                        "view": {"type": "string", "enum": ["recent", "search", "around"]},
                        "keyword": {"type": "string"},
                        "person_id": {"type": "string"},
                        "space_id": {"type": "string"},
                        "start_at": {"type": "string", "description": "ISO 8601 起点"},
                        "end_at": {"type": "string", "description": "ISO 8601 终点"},
                        "limit": {"type": "integer", "minimum": 1},
                        "event_id": {"type": "integer", "minimum": 1},
                        "before": {"type": "integer", "minimum": 0},
                        "after": {"type": "integer", "minimum": 0},
                    },
                    required=("view",),
                ),
            ),
            ChatTool(
                name="search_memory",
                description=(
                    "按自然语言搜索有来源的长期记忆。无 target 时搜索本次主体有权读取的全部历史"
                    "人物、人物群、群及可见 SELF；当前群或提及不自动缩小范围。"
                    "仅明确限定目标时填写 target，姓名和群名须精确；歧义先澄清，拒绝不换范围重试。"
                    "结果有界且可能非穷尽，缺证据可改用不同查询补查。"
                    "原始聊天用 query_chat_history(view=search)。"
                ),
                parameters=_object_schema(
                    {
                        "query": {"type": "string", "minLength": 1},
                        "target": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "scope": {"type": "string", "enum": ["person", "group", "self"]},
                                "subject_ref": {
                                    "type": "string",
                                    "description": (
                                        "本轮事件人物引用：current_speaker、"
                                        "member_{person_id} 或 replied_message_author"
                                    ),
                                },
                                "display_name": {"type": "string"},
                                "person_id": {"type": "string"},
                                "space_id": {"type": "string"},
                                "space_name": {"type": "string"},
                            },
                            "required": ["scope"],
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        **_MEMORY_INTENT_PROPERTIES,
                    },
                    required=("query",),
                ),
            ),
            ChatTool(
                name="read_memory",
                description=(
                    "按内部 fact_id 读取记忆。view=fact 返回有权查看的事实；"
                    "view=evidence 只返回本人记忆的有界证据摘要，"
                    "不暴露他人来源身份，也不记作已读事实。"
                ),
                parameters=_object_schema(
                    {
                        "view": {"type": "string", "enum": ["fact", "evidence"]},
                        "fact_id": {"type": "integer", "minimum": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    required=("view", "fact_id"),
                ),
            ),
        ]
        if self._memory_mutations is not None:
            tools.append(
                ChatTool(
                    name="memory_change",
                    description=(
                        "依据真实入站证据或 SELF 原执行本轮工具回执，创建、纠正、撤销、恢复、"
                        "争议、合并、改归属或更新长期记忆。SELF 只能写自己的记忆；"
                        f"不能把 {bot_name} 自己的输出当证据，也不能编造真人事件。"
                        "target.subject_ref 可使用 current_speaker、current_group、"
                        "member_{person_id} 等本轮可验证成员引用，或"
                        "replied_message_author；正文中的当前群姓名使用 named_member 并填写"
                        f" subject_name；{bot_name} 自我记忆使用 self + self。"
                        "自我记忆仅在功能开启时，依据本轮真实用户消息或 SELF 工具回执，"
                        f"记录 {bot_name} 自己的判断和经历。SELF 的 visibility"
                        "只能用 current_scope 或 global；global 只适合抽象偏好、反思和原则，"
                        "私聊原始经历只能保存为当前私聊可见，不能提升为 global。"
                        "工具回执中的 applied_operation 和 outcome"
                        "才是真实结果，回复用户时必须以回执为准；被降级为 contest 或 noop"
                        "时不得声称已经覆盖、删除或纠正成功。create 必须提供 target、"
                        "new_content、memory_key 和 category；correct 可通过 fact_id 继承目标、"
                        "memory_key 和 category；invalidate、restore、contest、merge 和"
                        "update_metadata 可通过 fact_id 直接定位，不必重复 target。"
                        "fact_id 缺失时，除 reassign 外可提供 target 和 selector，由后端在当前合法"
                        "作用域内定位；只有唯一精确命中才执行，否则按候选或未找到结果处理。"
                        "但 SELF 不支持 reassign 或 update_metadata。reassign 仍必须提供新 target。"
                        "invalidate 成功表示事实保留审计记录但不再作为有效记忆；"
                        "只能称为已撤回、已失效或不再记住，不得声称数据库记录已物理删除。"
                    ),
                    parameters=_object_schema(
                        {
                            "operation": {
                                "type": "string",
                                "enum": [
                                    "create",
                                    "correct",
                                    "invalidate",
                                    "restore",
                                    "contest",
                                    "merge",
                                    "reassign",
                                    "update_metadata",
                                ],
                            },
                            "fact_id": {"type": "integer", "minimum": 1},
                            "merge_fact_id": {"type": "integer", "minimum": 1},
                            "selector": _object_schema(
                                {
                                    "memory_key": {"type": "string"},
                                    "old_content": {"type": "string"},
                                    "category": {"type": "string"},
                                }
                            )
                            | {
                                "description": (
                                    "没有 fact_id 时使用；至少提供 memory_key 或 old_content，"
                                    "并同时提供合法 target。memory_key 是内部稳定键；不知道时"
                                    "不要根据用户说法自行编造，应把用户可见标签或原陈述放入"
                                    "old_content。仅唯一精确命中时执行；返回候选后使用其 fact_id"
                                    "重试。"
                                )
                            },
                            "merge_selector": _object_schema(
                                {
                                    "memory_key": {"type": "string"},
                                    "old_content": {"type": "string"},
                                    "category": {"type": "string"},
                                }
                            )
                            | {
                                "description": (
                                    "merge 没有 merge_fact_id 时使用；"
                                    "至少提供 memory_key 或 old_content。"
                                )
                            },
                            "target": _object_schema(
                                {
                                    "subject_ref": {
                                        "type": "string",
                                        "description": (
                                            "本轮提供的主体引用，如 current_speaker、"
                                            "member_{person_id}、replied_message_author、"
                                            "named_member、self 或 current_group"
                                        ),
                                    },
                                    "scope_type": {
                                        "type": "string",
                                        "enum": ["person", "person_group", "group", "self"],
                                    },
                                    "subject_name": {
                                        "type": "string",
                                        "description": "subject_ref=named_member 时填写当前群姓名",
                                    },
                                    "candidate_ref": {
                                        "type": "string",
                                        "description": "姓名歧义后从工具返回候选中选择",
                                    },
                                },
                                required=("subject_ref", "scope_type"),
                            ),
                            "visibility": {
                                "type": "string",
                                "enum": ["current_scope", "global"],
                                "description": (
                                    "仅 target.scope_type=self 时生效；其他目标误填合法值会被"
                                    "后端忽略。current_scope 表示当前私聊或群，global 仅适合"
                                    "抽象偏好、反思和原则。"
                                ),
                            },
                            "request_basis": {
                                "type": "string",
                                "enum": ["user_requested", "agent_initiated"],
                                "description": (
                                    "用户要求变更时用 user_requested；自主决定时用 agent_initiated"
                                ),
                            },
                            "new_content": {"type": "string"},
                            "memory_key": {"type": "string"},
                            "category": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["fact", "preference", "episode"],
                            },
                            "reason": {
                                "type": "string",
                                "description": "简述记忆变更理由，不写思考过程。",
                            },
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "importance": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                                "description": (
                                    "1–2 是临时琐事，3 是未来有用的"
                                    "事实或有意义的单次经历，4–5 是重要变化/承诺/里程碑。"
                                ),
                            },
                            "evidence_event_id": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "真实内部证据事件 ID。无入站消息时，"
                                    "先查询创建者在当前会话的原始记录；"
                                    "省略使用当前入站事件，不可编造。"
                                ),
                            },
                            "evidence_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "description": (
                                    "入站轮使用 current_event。主动 SELF 可省略，"
                                    "由 evidence_quote 定位本轮最近匹配的真实工具回执，"
                                    "或使用已有工具回执 tool_N；不可编造事件。"
                                ),
                            },
                            "evidence_quote": {"type": "string"},
                            "expected_fact_state": {
                                "type": "string",
                                "enum": ["active", "contested", "superseded", "invalidated"],
                            },
                            "valid_from": {"type": "string"},
                            "valid_until": {"type": "string"},
                        },
                        required=("operation",),
                    ),
                )
            )
        if self._web_catalog_enabled():
            tools.extend(
                (
                    ChatTool(
                        name="web_search",
                        description=(
                            "受控联网搜索。最新新闻、当前人物职务、价格、软件版本、政策、"
                            "比赛结果等时效内容应使用此工具确认；闲聊中遇到不熟悉的公开事实"
                            "也可查证。复杂问题可重新组织搜索词再次搜索。搜索词只"
                            "包含回答当前问题所需的信息，禁止放入完整聊天记录、人物记忆或"
                            "系统提示词。搜索与网页提取沿本次明确请求执行。"
                        ),
                        parameters=_object_schema(
                            {
                                "query": {
                                    "type": "string",
                                    "description": "必填，回答当前问题所需的搜索词",
                                },
                                "topic": {
                                    "type": "string",
                                    "enum": ["general", "news"],
                                },
                                "time_range": {
                                    "type": "string",
                                    "enum": ["day", "week", "month", "year"],
                                },
                                "start_date": {
                                    "type": "string",
                                    "description": "YYYY-MM-DD",
                                },
                                "end_date": {
                                    "type": "string",
                                    "description": "YYYY-MM-DD",
                                },
                            },
                            required=("query",),
                        ),
                    ),
                    ChatTool(
                        name="read_webpage",
                        description=(
                            "读取一个明确的公开网页 URL，可来自本轮或历史。question 指定所需内容；"
                            "搜索不自动下载页面，需正文时显式调用此工具。"
                        ),
                        parameters=_object_schema(
                            {
                                "url": {"type": "string"},
                                "question": {
                                    "type": "string",
                                    "description": "用户希望从网页了解的问题，可省略",
                                },
                            },
                            required=("url",),
                        ),
                    ),
                )
            )
        from yuki.sandbox.client import sandbox_tools
        from yuki.services.personal_entry_tool import ENTRY_TOOL
        from yuki.social.tools import social_tool_definitions
        from yuki.workspace.service import workspace_tools

        return (
            *tools,
            *social_tool_definitions(),
            ENTRY_TOOL,
            *workspace_tools(),
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

        async with self._ledger._database.sessions() as session:
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
                from yuki.social.tools import social_tool_definitions

                if name == "local_device":
                    from yuki.capabilities.invocation import current_invocation
                    from yuki.runtime.work_activation import current_work_control

                    invocation = current_invocation.get()
                    control = current_work_control.get()
                    if self.local_executors is None or invocation is None:
                        return self._result(error="unavailable")
                    if runtime.tools_closed:
                        return self._result(error="permission_denied")
                    try:
                        local_result = await self.local_executors.tools(
                            runtime.actor_person_id,
                            arguments.get("action", ""),
                            arguments,
                            invocation.call_id,
                            control.current["id"] if control and control.current else None,
                        )
                    except ClientError as exc:
                        return self._result(error=exc.code)
                    return self._result(
                        data=local_result, uncertain=local_result.get("uncertain", False)
                    )
                if name in SANDBOX_TOOLS:
                    from yuki.capabilities.invocation import current_invocation

                    invocation = current_invocation.get()
                    if runtime.tools_closed:
                        return self._result(error="permission_denied", detail="本轮未授权沙箱操作")
                    if self.sandbox_client is None or invocation is None:
                        return self._result(error="sandbox_unavailable", detail="沙箱未连接")
                    from hashlib import sha256

                    request_id = sha256(
                        (
                            f"{runtime.conversation_id}:"
                            f"{runtime.effective_execution_id}:"
                            f"{invocation.call_id}"
                        ).encode()
                    ).hexdigest()
                    from yuki.runtime.work_activation import current_work_control

                    work_control = current_work_control.get()
                    if work_control is not None and work_control.session is not None:
                        request_id = sha256(invocation.call_id.encode()).hexdigest()
                    result = await self.sandbox_client.execute(
                        name,
                        arguments,
                        request_id=request_id,
                        source={**runtime.sandbox_source, **self._work_source()}
                        if runtime.sandbox_source
                        and (is_execution(name, arguments) or name == "external_agent")
                        else {
                            **self._work_source(),
                            "conversation_id": runtime.effective_conversation_id,
                            "origin": runtime.origin.value,
                            "actor_is_superuser": runtime.actor_is_superuser,
                            "actor_user_id": runtime.actor_user_id,
                            "bot_user_id": runtime.effective_bot_user_id,
                            "presence_id": runtime.effective_presence_id,
                            "generation": runtime.turn_snapshot.generation
                            if runtime.turn_snapshot
                            else None,
                            "trigger_event_id": runtime.effective_trigger_event_id,
                            "parent_execution_id": runtime.effective_execution_id,
                        }
                        if is_execution(name, arguments) or name == "external_agent"
                        else None,
                    )
                    if result.get("error"):
                        return self._result(
                            data=result,
                            error=str(result["error"]),
                            detail="沙箱提交结果未确认；核对原执行回执，不能重新提交。"
                            if result.get("uncertain")
                            else "沙箱操作未完成",
                            uncertain=bool(result.get("uncertain")),
                        )
                    return self._result(data=result)

                if name in WORKSPACE_TOOLS:
                    from yuki.workspace.store import WorkspaceError

                    if runtime.tools_closed:
                        return self._result(
                            error="permission_denied", detail="本轮未授权工作区操作"
                        )
                    if self.workspace_service is None:
                        return self._result(error="workspace_unavailable", detail="工作区未连接")
                    try:
                        from yuki.capabilities.invocation import current_invocation

                        invocation = current_invocation.get()
                        workspace_result = await self.workspace_service.execute(
                            name,
                            arguments,
                            runtime=runtime,
                            request_id=invocation.call_id if invocation is not None else None,
                        )
                        return self._result(
                            data=workspace_result,
                            error=str(workspace_result["error"])
                            if workspace_result.get("error")
                            else None,
                            uncertain=bool(workspace_result.get("uncertain")),
                        )
                    except (WorkspaceError, ValueError, OSError) as exc:
                        category = (
                            str(exc) if isinstance(exc, WorkspaceError) else type(exc).__name__
                        )
                        detail = (
                            "所选消息没有这个附件。先查询当前会话历史，使用真实 event_id"
                            " 和从 0 开始的"
                            " attachment_index 导入；新收到的附件不会自动加入已开始的一轮。"
                            if category == "attachment_not_found"
                            else "工作区操作未完成"
                        )
                        return self._result(error=category, detail=detail)

                if name == "read_personal_entry":
                    from yuki.services.personal_entry_tool import read_entry
                    from yuki.social.models import SocialError

                    try:
                        data = await read_entry(self._ledger._database, runtime, arguments)
                        return self._result(data=data, error=data.get("error"))
                    except SocialError as exc:
                        return self._result(
                            error=str(exc), detail="仅 App/ACP 个人会话可读取本人其他私聊入口"
                        )

                if name in {tool.name for tool in social_tool_definitions()}:
                    from yuki.social.agent_adapter import invoke_social
                    from yuki.social.models import SocialError

                    if self.social_service is None:
                        return self._result(error="social_unavailable", detail="社交服务尚未连接")
                    try:
                        social_result = await invoke_social(
                            self.social_service, name, arguments, runtime
                        )
                        if social_result.get("error") or social_result.get("status") in {
                            "failed",
                            "uncertain",
                        }:
                            uncertain = social_result.get("status") == "uncertain"
                            sent_parts = int(social_result.get("sent_messages") or 0)
                            return self._result(
                                error=str(
                                    social_result.get("error")
                                    or ("delivery_uncertain" if uncertain else "delivery_failed")
                                ),
                                detail=(
                                    "从 bindings 选择明确的 binding_id 后再调用；未派发消息"
                                    if social_result.get("error") == "binding_required"
                                    else "读取账号不明确；从 presences 选择 presence_id，勿换号试读"
                                    if social_result.get("error") == "presence_ambiguous"
                                    else "网关读取失败，保留已取得的分组状态"
                                    if name == "read_conversation_history"
                                    else "文件已发送成功，附带文字未确认发送；不要重发文件"
                                    if social_result.get("error") == "file_sent_caption_unconfirmed"
                                    else "部分分条已确认发送；不要重发已成功的分条"
                                    if sent_parts
                                    else "发送结果未知，不要重发；请根据回执说明情况"
                                    if uncertain
                                    else "发送前或发送时明确失败，未确认有消息送达"
                                ),
                                data=social_result,
                                uncertain=uncertain,
                            )
                        return self._result(data=social_result)
                    except SocialError as exc:
                        detail = {
                            "history_receipt_unavailable": (
                                "该回执不属于当前来源会话，或没有可核验的发送账号"
                            ),
                            "history_anchor_unavailable": (
                                "原事件或定位不可用，使用当前会话的真实 event_id 或原回执"
                            ),
                            "history_source_changed": (
                                "来源授权、代次或隐私状态已变化，未发布旧读取正文"
                            ),
                            "invalid_history_event_id": (
                                "event_id 须是当前会话的正整数内部事件 ID"
                            ),
                            "history_presence_unavailable": "原账号不可用，未换号读取",
                            "history_provider_failed": "网关读取失败，不表示没有消息",
                            "invalid_history_result": "网关历史返回格式无效，不能据此判断会话内容",
                            "artifact_transfer_unavailable": (
                                "文件已存在工作区，但中转不可用，尚未发送；本轮不要重复发送"
                            ),
                            "invalid_target_id": (
                                "target_id 使用联系人查询返回的内部 UUID；当前会话可省略 target"
                            ),
                            "invalid_message_arguments": (
                                "检查 text、artifact_id 和 attachment_kind；"
                                "文件必须指定 file 类型。"
                                "已生成的工作区文件仍保留"
                            ),
                            "route_paused": "该操作所需路由已暂停，未执行；不会自动换路或解暂停",
                            "binding_ambiguous": (
                                "目标有多个有效平台 Binding；查询联系人后明确指定 Binding，未执行"
                            ),
                            "binding_unavailable": "指定的平台 Binding 无效或不属于目标，未执行",
                            "original_presence_unavailable": "原消息发送账号当前不可用，未撤回",
                            "group_unavailable": "当前没有可访问该群的连接，未执行",
                            "group_target_required": (
                                "请选择群的内部目标；当前群可省略。提及在正文原位置写 @名字"
                            ),
                            "subject_ref_unavailable": (
                                "该人物引用不在当前事件中；当前发言人用 current_speaker，"
                                "其他人物先 find_contacts，不要猜测引用或映射故障"
                            ),
                            "target_not_found": (
                                "名称未精确匹配；先 find_contacts。群发送顶层是群目标，"
                                "提及在正文写 @名字；未找到不代表账号映射损坏"
                            ),
                            "invalid_space_id": ("space_id 必须是内部 Space 编号；当前空间可省略"),
                        }.get(str(exc), "社交操作未执行或结果不确定，请勿盲重试")
                        return self._result(error=str(exc), detail=detail)
                if name in {
                    "search_memory",
                }:
                    self._log_memory_read_intent(arguments, parse_memory_tool_intent(arguments))
                if name == "get_my_capabilities":
                    return self._my_capabilities(arguments, runtime)
                if name == "query_chat_history":
                    view = arguments.get("view")
                    if view == "recent":
                        return await self._recent_history(runtime)
                    if view == "search":
                        return await self._search(arguments, runtime)
                    if view == "around":
                        return await self._history_around(arguments, runtime)
                    return self._result(error="invalid_arguments", detail="未知 history view")
                if name == "search_memory":
                    result = await self._search_memory(arguments, runtime)
                    return await self._capture_memory_tool_result(result, runtime)
                if name == "read_memory":
                    if arguments.get("view") == "fact":
                        result = await self._memory_fact(arguments, runtime)
                        return await self._capture_memory_tool_result(result, runtime)
                    if arguments.get("view") == "evidence":
                        return await self._memory_evidence(arguments, runtime)
                    return self._result(error="invalid_arguments", detail="未知 memory view")
                if name == "memory_change":
                    return await self._memory_change(arguments, runtime)
                if name == "web_search":
                    return await self._web_search(arguments, runtime)
                if name == "read_webpage":
                    return await self._read_webpage(arguments, runtime)
                return self._result(error="unknown_tool", detail=f"未知工具：{name}")
            except WebSearchError as exc:
                return self._web_result(error=exc.code, detail=exc.detail)
            except MemoryRetrievalError as exc:
                await self._record_memory_tool_outcome(runtime, "infrastructure_failure")
                return self._result(error=exc.code, detail="记忆检索失败", retryable=True)
            except SQLAlchemyError:
                logger.exception("memory_tool_database_failure tool=%s", name)
                await self._record_memory_tool_outcome(runtime, "infrastructure_failure")
                return self._result(
                    error="database_failure", detail="数据库事务未提交", retryable=True
                )
            except (TypeError, ValueError) as exc:
                if name in _OBSERVED_MEMORY_READS:
                    return self._result(
                        error="invalid_arguments",
                        detail="记忆查询参数无效，请检查枚举及带时区日期区间",
                        retryable=False,
                    )
                return self._result(error=type(exc).__name__, detail="工具执行失败")
            except (OSError, RuntimeError) as exc:
                return self._result(error=type(exc).__name__, detail="工具执行失败")
        finally:
            _MEMORY_READ_TOOL.reset(read_tool_token)
            _RUNTIME_SNAPSHOT.reset(token)

    def _my_capabilities(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        """Return only the report derived from this authoritative inbound event."""

        try:
            mode, category, query = self._capability_options(arguments)
            report = self._capability_report(runtime, category=category, query=query)
        except PermissionError:
            return self._result(
                error="permission_context_mismatch",
                detail="权限查询没有绑定到当前真实消息发送者",
            )
        except ValueError as exc:
            return self._result(error="invalid_arguments", detail=str(exc))
        return self._result(data=report.to_model_dict(mode))

    @staticmethod
    def _capability_options(
        arguments: dict[str, Any],
    ) -> tuple[Literal["summary", "focused", "full"], str | None, str | None]:
        extra = set(arguments) - {"mode", "category", "query"}
        if extra:
            raise ValueError("权限查询只接受 mode、category、query")
        raw_mode = arguments.get("mode", "summary")
        if raw_mode not in {"summary", "focused", "full"}:
            raise ValueError("mode 必须是 summary、focused 或 full")
        category = arguments.get("category")
        query = arguments.get("query")
        if category is not None and not isinstance(category, str):
            raise ValueError("category 必须是字符串")
        if query is not None and not isinstance(query, str):
            raise ValueError("query 必须是字符串")
        if raw_mode == "focused" and not (category or query):
            raise ValueError("focused 模式必须提供 category 或 query")
        return cast(Literal["summary", "focused", "full"], raw_mode), category, query

    def _capability_report(
        self,
        runtime: ToolRuntime,
        *,
        category: str | None = None,
        query: str | None = None,
    ) -> CapabilityReport:
        """Resolve the current sender after validating all event-bound fields."""

        actor = runtime.require_actor()
        return self._permission_catalog.report_for_actor(
            actor,
            category=category,
            query=query,
            actor_is_superuser=runtime.actor_is_superuser,
        )

    async def _recent_history(self, runtime: ToolRuntime) -> ToolExecutionResult:
        rows = await self._ledger.list_scope_recent(
            runtime.conversation_scope(),
            limit=self._settings.recent_history_tool_limit,
            message_only=True,
        )
        return self._result(
            data={"source": "ledger", "events": [self._event_json(row) for row in rows]}
        )

    async def _search(self, arguments: dict[str, Any], runtime: ToolRuntime) -> ToolExecutionResult:
        keyword = arguments.get("keyword")
        if not isinstance(keyword, str) or not keyword.strip():
            return self._result(error="invalid_keyword", detail="keyword 必须是非空字符串")
        rows = await self._ledger.search(
            keyword=keyword,
            scope=runtime.conversation_scope(),
            person_id=self._optional_string(arguments.get("person_id")),
            space_id=self._optional_string(arguments.get("space_id")),
            after=self._parse_time(arguments.get("start_at")),
            before=self._parse_time(arguments.get("end_at")),
            limit=int(arguments.get("limit", 20)),
            message_only=True,
        )
        events = [self._event_json(row) for row in rows]
        return self._result(
            data={"events": events, "returned_count": len(events), "truncated": False}
        )

    async def _history_around(
        self,
        arguments: dict[str, Any],
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        event_id = arguments.get("event_id")
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            return self._result(error="invalid_event_id", detail="event_id 必须是正整数")
        if "platform_message_id" in arguments:
            return self._result(error="invalid_target", detail="本地账本只接受 event_id")
        scope = runtime.conversation_scope()
        max_before = self._settings.conversation_history_around_before
        max_after = self._settings.conversation_history_around_after
        total_limit = self._settings.conversation_history_around_limit
        try:
            before = self._optional_bounded_int(
                arguments.get("before"), default=max_before, maximum=max_before
            )
            after = self._optional_bounded_int(
                arguments.get("after"), default=max_after, maximum=max_after
            )
        except ValueError:
            return self._result(error="invalid_radius", detail="before/after 必须是整数")
        if before + after + 1 > total_limit:
            extra = before + after + 1 - total_limit
            reduce_before = min(before, extra)
            before -= reduce_before
            extra -= reduce_before
            after = max(0, after - extra)
        center, earlier, later = await self._ledger.list_scope_around(
            scope,
            event_id=event_id,
            before=before,
            after=after,
            message_only=True,
        )
        if center is None:
            return self._result(error="not_found", detail="当前会话找不到这条消息")
        events = (*earlier, center, *later)
        return self._result(
            data={
                "source": "ledger",
                "center_event_id": center.id,
                "before": len(earlier),
                "after": len(later),
                "events": [self._event_json(row) for row in events],
            }
        )

    async def _search_memory(
        self,
        arguments: dict[str, Any],
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 必须是非空字符串")
        allowed_arguments = {
            "query",
            "target",
            "limit",
            "purpose",
            "start_at",
            "end_at",
            "temporal_constraint",
        }
        if set(arguments) - allowed_arguments:
            raise ValueError("search_memory 包含未知字段")
        target = arguments.get("target")
        if target is not None and not isinstance(target, dict):
            raise ValueError("target 必须是对象")
        scope: ResolvedReadScope
        authorized_scope: AuthorizedMemoryScope | None = None
        if target is None:
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
            allowed_scopes = runtime.memory_allowed_scopes
            if allowed_scopes is None:
                allowed_scopes = (
                    MemoryScopeType.PERSON,
                    MemoryScopeType.PERSON_GROUP,
                    MemoryScopeType.GROUP,
                    MemoryScopeType.SELF,
                )
            if not self._settings.self_memory_enabled:
                allowed_scopes = tuple(
                    item for item in allowed_scopes if item is not MemoryScopeType.SELF
                )
            authorized_scope = await self._memory_reads.authorized(
                requester,
                current_space_id=self._runtime_space_id(runtime),
                private_scene=runtime.effective_scope_type is ScopeType.PRIVATE,
                self_actor=requester is None,
                allowed_scopes=allowed_scopes,
            )
            scope = ResolvedReadScope(targets=())
        else:
            if not isinstance(target.get("scope"), str):
                raise ValueError("target.scope 必须是 person、group 或 self")
            kind = target["scope"]
            allowed = {
                "person": {
                    "scope",
                    "subject_ref",
                    "display_name",
                    "person_id",
                    "space_id",
                    "space_name",
                },
                "group": {"scope", "space_id", "space_name"},
                "self": {"scope"},
            }.get(kind)
            if allowed is None or set(target) - allowed:
                raise ValueError("target 字段与范围不匹配")
            if runtime.memory_allowed_scopes is not None:
                requested_scope = {
                    "person": MemoryScopeType.PERSON,
                    "group": MemoryScopeType.GROUP,
                    "self": MemoryScopeType.SELF,
                }[kind]
                if requested_scope not in runtime.memory_allowed_scopes:
                    return self._result(
                        error="permission_denied", detail="插件未获该记忆范围读取权限"
                    )
            if kind == "person":
                selection = await self._resolve_person_memory_selection(target, runtime)
                if isinstance(selection, _ToolFailure):
                    return self._result(
                        error=selection.code, detail=selection.detail, data=selection.data
                    )
                scope = ResolvedReadScope(targets=selection.targets)
            elif kind == "group":
                group_id = await self._read_group_selector(target, runtime, default_current=False)
                if isinstance(group_id, _ToolFailure):
                    return self._result(
                        error=group_id.code, detail=group_id.detail, data=group_id.data
                    )
                if group_id is None:
                    raise ValueError("指定 group 范围须提供 space_id 或 space_name")
                requester = self._social_requester(runtime)
                if requester is not None:
                    targets = (await self._memory_reads.group(requester, group_id)).targets
                elif (
                    runtime.effective_scope_type is ScopeType.GROUP
                    and self._runtime_space_id(runtime) == group_id
                    and runtime.actor_context is not None
                    and runtime.require_actor().principal_kind == "self"
                ):
                    targets = (
                        MemoryEntityTarget(
                            role=MemoryTargetRole.CURRENT_GROUP,
                            scope_type=MemoryScopeType.GROUP,
                            canonical_subject_space_id=group_id,
                            block_id="current_group",
                        ),
                    )
                else:
                    targets = ()
                if not targets:
                    return self._result(error="permission_denied", detail="没有该群的读取授权")
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
        if target is not None and self._social_requester(runtime) is not None:
            authorized_scope = await self._memory_reads.authorized(
                self._social_requester(runtime),
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
        result = await self._read_memories(
            arguments,
            runtime=runtime,
            text=query.strip(),
            targets=scope.targets,
            authorized_scope=authorized_scope,
            requested_limit=self._memory_requested_limit(arguments),
        )
        return self._memory_list_result(
            data={
                "effective_query": effective_query_summary(parse_memory_tool_intent(arguments)),
                "semantic_status": result.semantic_status,
                "semantic_degraded": result.semantic_degraded,
                "candidate_count": result.candidate_count,
                "target_count": len({hit.target.block_id for hit in result.hits}),
                "result_scope": "authorized_maximum" if target is None else "explicit_targets",
                "exhaustive": result.exhaustive,
                "truncated": result.truncated,
                "partial_failure": not result.exhaustive,
                "partial_reason": result.partial_reason,
                "memories": [
                    {
                        **(
                            self._self_memory_json(hit.fact, retrieval_reason=hit.selection_reason)
                            if hit.fact.scope_type is MemoryScopeType.SELF
                            else self._memory_json(hit.fact, retrieval_reason=hit.selection_reason)
                        ),
                    }
                    for hit in result.hits
                ],
            }
        )

    async def _resolve_person_memory_selection(
        self,
        arguments: dict[str, Any],
        runtime: ToolRuntime,
    ) -> _PersonMemorySelection | _ToolFailure:
        selectors = [
            key
            for key in ("subject_ref", "display_name", "person_id")
            if arguments.get(key) is not None
        ]
        if len(selectors) != 1:
            return _ToolFailure(
                "invalid_person_selector", "subject_ref、display_name、person_id 必须且只能提供一个"
            )
        space_id = await self._read_group_selector(arguments, runtime, default_current=False)
        if isinstance(space_id, _ToolFailure):
            return space_id
        key = selectors[0]
        value = arguments[key]
        if not isinstance(value, str) or not value.strip():
            return _ToolFailure("invalid_person_selector", "人物目标必须是非空字符串")
        value = value.strip()
        if key == "subject_ref":
            person_id = await self._person_id_for_subject_ref(value, runtime)
            if isinstance(person_id, _ToolFailure):
                return person_id
        elif key == "display_name":
            requester = self._social_requester(runtime)
            if requester is not None:
                matches = await self._memory_reads.people_named(requester, value)
            elif self._runtime_space_id(runtime) is not None:
                actor = runtime.require_actor()
                matches = tuple(
                    item.person_id
                    for item in await self._people.search_group_member_names(
                        value,
                        runtime.current_group_id or "",
                        platform=actor.platform,
                        space_id=self._runtime_space_id(runtime),
                    )
                    if item.exact and item.person_id is not None
                )
            else:
                matches = ()
            if not matches:
                return _ToolFailure("person_not_found", "授权范围内没有精确匹配的昵称或别名")
            if len(matches) > 1:
                return _ToolFailure(
                    "ambiguous_person",
                    "存在多个同名人物，请澄清目标或提供内部 Person 编号",
                    {"candidates": [{"person_id": item} for item in matches], "has_more": False},
                )
            person_id = matches[0]
        else:
            person_id = value
        return await self._person_memory_selection(
            person_id,
            runtime,
            resolved_by=key,
            subject_ref=value if key == "subject_ref" else None,
            selected_space_id=space_id,
        )

    async def _person_id_for_subject_ref(
        self, subject_ref: str, runtime: ToolRuntime
    ) -> str | _ToolFailure:
        if subject_ref == "current_speaker":
            person_id = self._social_requester(runtime)
            if person_id is not None:
                return person_id
        inbound = runtime.inbound
        if inbound is not None:
            if subject_ref == "replied_message_author" and inbound.reply_to_event_id is not None:
                reply = await self._ledger.get_event(inbound.reply_to_event_id)
                if reply is not None and reply.author_person_id is not None:
                    return reply.author_person_id
            if subject_ref.startswith("member_"):
                person_id = subject_ref.removeprefix("member_")
                if person_id in inbound.mentioned_person_ids:
                    return person_id
        return _ToolFailure("subject_not_found", "该人物引用在本轮真实事件中不存在")

    async def _person_memory_selection(
        self,
        person_id: str,
        runtime: ToolRuntime,
        *,
        resolved_by: str,
        subject_ref: str | None = None,
        selected_space_id: str | None = None,
    ) -> _PersonMemorySelection | _ToolFailure:
        requester = self._social_requester(runtime)
        if requester is not None:
            scope = await self._memory_reads.person(
                requester, person_id, group_id=selected_space_id
            )
            if scope.targets:
                return _PersonMemorySelection(
                    user_id=person_id,
                    targets=scope.targets,
                    resolved_by=resolved_by,
                    subject_ref=subject_ref,
                )
            return _ToolFailure("permission_denied", "没有本人或历史共同空间关系授权")
        actor = runtime.require_actor()
        space_id = self._runtime_space_id(runtime)
        if (
            actor.principal_kind != "self"
            or space_id is None
            or (selected_space_id is not None and selected_space_id != space_id)
        ):
            return _ToolFailure("permission_denied", "当前执行没有该人物的读取授权")
        from yuki.identity.person_identity import same_person_condition

        async with self._people.database.sessions() as session:
            membership = await session.scalar(
                select(MembershipModel.canonical_person_id).where(
                    same_person_condition(MembershipModel.canonical_person_id, person_id),
                    MembershipModel.canonical_space_id == space_id,
                )
            )
        if membership is None:
            return _ToolFailure("permission_denied", "只能读取当前空间真实成员在本空间的记忆")
        return _PersonMemorySelection(
            user_id=person_id,
            targets=(
                MemoryEntityTarget(
                    role=MemoryTargetRole.REFERENCED_PERSON_GROUP,
                    scope_type=MemoryScopeType.PERSON_GROUP,
                    canonical_subject_person_id=person_id,
                    canonical_subject_space_id=space_id,
                    block_id=f"tool_person_group:{person_id}:{space_id}",
                ),
            ),
            resolved_by=resolved_by,
            subject_ref=subject_ref,
        )

    async def _read_group_selector(
        self,
        arguments: dict[str, Any],
        runtime: ToolRuntime,
        *,
        default_current: bool,
    ) -> str | None | _ToolFailure:
        selectors = [key for key in ("space_id", "space_name") if arguments.get(key) is not None]
        if not selectors:
            return self._runtime_space_id(runtime) if default_current else None
        if len(selectors) != 1:
            return _ToolFailure("invalid_group_selector", "space_id 与 space_name 只能提供一个")
        key = selectors[0]
        value = arguments[key]
        if not isinstance(value, str) or not value.strip():
            return _ToolFailure("invalid_group_selector", "空间目标必须是非空字符串")
        if key == "space_id":
            return value.strip()
        requester = self._social_requester(runtime)
        if requester is None:
            return _ToolFailure("permission_denied", "历史空间名查询需要真实用户主体")
        matches = await self._memory_reads.groups_named(requester, value)
        if not matches:
            return _ToolFailure("group_not_found", "授权历史空间中没有精确匹配的名称")
        if len(matches) > 1:
            return _ToolFailure(
                "ambiguous_group",
                "存在多个同名空间，请澄清目标",
                {"candidates": [{"space_id": item} for item in matches], "has_more": False},
            )
        return matches[0]

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

    def _memory_list_result(self, *, data: dict[str, Any]) -> ToolExecutionResult:
        """Fit ranked whole facts into the existing response budget, never fake an empty search."""
        remaining = list(data["memories"])
        payload = {
            **data,
            "memories": remaining,
            "returned_count": len(remaining),
            "result_scope": data.get("result_scope", "bounded_query"),
            "exhaustive": data.get("exhaustive", False),
        }
        limit = self._runtime().agent.tool_result_max_characters
        while True:
            wire = {"ok": True, "data": payload}
            wire["evidence_state"] = evidence_state(wire, "memory_tool")
            model_wire = ToolExecutionResult(
                ok=True,
                data=payload,
                mutation_committed=False,
                evidence_state=cast(dict[str, Any], wire["evidence_state"]),
                provider_id="core",
                tool_name=_MEMORY_READ_TOOL.get() or "search_memory",
            ).model_payload()
            if len(json.dumps(model_wire, ensure_ascii=False, default=str)) <= limit:
                return self._result(data=payload)
            if len(remaining) <= 1:
                return self._result(
                    error="result_too_large",
                    detail="首条完整记忆超过本轮结果预算；不能把它裁成片段或报告为空",
                )
            remaining.pop()
            payload["truncated"] = True
            payload["exhaustive"] = False
            payload["partial_failure"] = True
            payload["partial_reason"] = "response_character_budget"
            payload["returned_count"] = len(remaining)
            payload["truncation_reason"] = "response_character_budget"

    @staticmethod
    def _memory_requested_limit(arguments: dict[str, Any]) -> int | None:
        if "limit" not in arguments or arguments.get("limit") is None:
            return None
        value = arguments.get("limit")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise ValueError("limit 必须是 1～100 的整数")
        return int(value)

    async def _read_memories(
        self,
        arguments: dict[str, Any],
        *,
        runtime: ToolRuntime,
        text: str,
        targets: tuple[MemoryEntityTarget, ...],
        authorized_scope: AuthorizedMemoryScope | None = None,
        requested_limit: int | None,
        default_overview: bool = False,
    ) -> Any:
        intent = self._memory_tool_intent(arguments, default_overview=default_overview)
        request = MemoryReadRequest(
            text=text,
            intent=intent,
            requested_limit=requested_limit,
            resolved_scope=ResolvedReadScope(targets=targets),
            authorized_scope=authorized_scope,
        )
        runtime_config = self._runtime()
        return await MemoryQueryPlane(self._memory_context).read(
            MemoryReadConsumer.AGENT_TOOL,
            request,
            runtime=runtime_config,
        )

    @staticmethod
    def _log_memory_read_intent(arguments: dict[str, Any], intent: MemoryQueryIntent) -> None:
        from yuki.runtime.observability import current_runtime_turn_correlation

        correlation = current_runtime_turn_correlation()
        logger.info(
            "memory_read_intent correlation_id=%s tool=%s mode=%s purpose=%s "
            "explicit_fields=%s temporal_constraint=%s",
            correlation.turn_id if correlation else "unbound",
            _MEMORY_READ_TOOL.get(),
            intent.mode.value,
            intent.purpose.value,
            ",".join(
                name
                for name in (
                    "query",
                    "mode",
                    "purpose",
                    "start_at",
                    "end_at",
                    "temporal_constraint",
                    "target",
                    "subject_ref",
                    "display_name",
                    "user_id",
                    "group_id",
                    "group_name",
                )
                if name in arguments
            ),
            intent.temporal.constraint.value,
        )

    @staticmethod
    def _memory_tool_intent(
        arguments: dict[str, Any],
        *,
        default_overview: bool = False,
    ) -> MemoryQueryIntent:
        del default_overview
        return parse_memory_tool_intent(arguments)

    async def _memory_fact(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        fact_id = arguments.get("fact_id")
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id <= 0:
            raise ValueError("fact_id 必须是正整数")
        fact = await self._authorized_memory_fact(fact_id, runtime)
        if fact is None:
            return self._result(error="memory_not_found", detail="没有找到可查看的事实")
        projection = (
            self._self_memory_json(fact, retrieval_reason="fact_id")
            if fact.scope_type is MemoryScopeType.SELF
            else self._memory_json(fact, retrieval_reason="fact_id")
        )
        return self._result(data={"memory": projection})

    async def _memory_evidence(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        fact_id = arguments.get("fact_id")
        limit = arguments.get("limit", 10)
        if isinstance(fact_id, bool) or not isinstance(fact_id, int) or fact_id <= 0:
            raise ValueError("fact_id 必须是正整数")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1～20 的整数")
        if (
            runtime.memory_allowed_scopes is not None
            and MemoryScopeType.PERSON not in runtime.memory_allowed_scopes
        ):
            return self._result(error="memory_not_found", detail="没有找到可查看的本人事实")
        fact = await self._memories.get_fact(fact_id)
        if fact is None or not await self._can_read_own_person_fact(fact, runtime):
            return self._result(error="memory_not_found", detail="没有找到可查看的本人事实")
        rows = await self._memories.list_evidence(fact_id, limit=limit)
        return self._result(
            data={
                "fact_id": fact_id,
                "evidence": [
                    {
                        "relation": row.relation.value,
                        "confidence": row.confidence,
                        "authority": row.authority.value,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in rows
                ],
            }
        )

    async def _memory_change(
        self,
        arguments: dict[str, Any],
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        service = self._memory_mutations
        if service is None:
            return self._result(error="memory_change_unavailable", detail="当前轮不能变更记忆")
        normalized_arguments = {key: value for key, value in arguments.items() if value is not None}
        try:
            request = MemoryMutationRequest.model_validate(normalized_arguments)
        except ValidationError as exc:
            first = exc.errors(include_url=False)[0]
            location = ".".join(str(item) for item in first.get("loc", ())) or "request"
            logger.warning(
                "memory_change_validation_failed location=%s error_type=%s",
                location,
                first.get("type", "validation_error"),
            )
            return self._result(
                error="invalid_memory_change",
                detail=(f"记忆变更参数无效：{location}:{first.get('type', 'validation_error')}"),
            )
        actor = runtime.require_actor()
        from yuki.capabilities.invocation import current_invocation

        invocation = current_invocation.get()
        if actor.principal_kind == "self":
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
                operation_id=invocation.call_id if invocation is not None else None,
                actor_person_id=None,
            )
            result = await service.mutate(request, context)
            return self._memory_change_result(result)
        trigger_event_id = request.evidence_event_id or runtime.effective_trigger_event_id
        event = await self._ledger.get_event(trigger_event_id) if trigger_event_id else None
        if event is None:
            return self._result(
                error="trigger_event_not_found",
                detail="无法从永久账本核验当前入站消息",
            )
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
                error="untrusted_trigger_event",
                detail="工具运行时与真实入站消息不一致",
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
            operation_id=invocation.call_id if invocation is not None else None,
            actor_person_id=event.author_person_id,
        )
        named_target: ResolvedSubject | None = None
        if request.target is not None and request.target.subject_ref == "named_member":
            group_id = event.group_id
            subject_name = request.target.subject_name or ""
            if group_id is None:
                return self._result(
                    error="named_subject_requires_group",
                    detail="普通姓名只能在当前群成员中解析",
                )
            matches = await self._people.search_group_member_names(
                subject_name,
                group_id,
                platform=actor.platform,
                space_id=self._runtime_space_id(runtime),
            )
            exact = tuple(item for item in matches if item.exact)
            chosen = exact[0] if len(exact) == 1 and request.target.candidate_ref is None else None
            if request.target.candidate_ref is not None:
                position = int(request.target.candidate_ref.removeprefix("member_candidate_")) - 1
                if 0 <= position < len(matches):
                    chosen = matches[position]
            if chosen is None:
                candidates = [
                    {
                        "candidate_ref": f"member_candidate_{index}",
                        "display_name": item.display_name,
                        "nickname": item.nickname,
                        "group_card": item.group_card,
                        "matched_alias": item.matched_alias,
                        "person_id": item.person_id,
                        "similarity": round(item.score, 4),
                        "exact": item.exact,
                    }
                    for index, item in enumerate(matches, start=1)
                ]
                return self._result(
                    data={
                        "reason_code": "subject_resolution_required",
                        "subject_name": subject_name,
                        "candidates": candidates,
                        "requires_user_decision": True,
                    },
                    error="subject_resolution_required",
                    detail=(
                        "当前群姓名不能唯一确定；可以选择 candidate_ref 重试，也可以自行询问用户"
                    ),
                    retryable=True,
                )
            named_target = ResolvedSubject(
                MemoryScopeType.PERSON_GROUP,
                chosen.user_id,
                group_id,
                canonical_subject_person_id=chosen.person_id,
                canonical_subject_space_id=self._runtime_space_id(runtime),
            )
        if named_target is None:
            result = await service.mutate(request, context)
        else:
            result = await service.mutate_resolved(request, context, target=named_target)
        return self._memory_change_result(result)

    def _memory_change_result(self, result: MemoryMutationResult) -> ToolExecutionResult:
        payload: dict[str, Any] = {
            "ok": result.ok,
            "mutation_id": result.mutation_id,
            "requested_operation": result.requested_operation.value,
            "applied_operation": result.applied_operation.value,
            "outcome": result.outcome.value,
            "old_fact_id": result.old_fact_id,
            "new_fact_id": result.new_fact_id,
            "reason_code": result.reason_code,
            "deduplicated": result.deduplicated,
            "candidates": [
                {
                    "fact_id": candidate.fact_id,
                    "memory_ref": candidate.memory_ref,
                    "key": candidate.memory_key,
                    "category": candidate.category,
                    "kind": candidate.kind.value,
                    "content": candidate.content,
                    "status": candidate.status.value,
                }
                for candidate in result.candidates
            ],
        }
        if result.ok and result.applied_operation is MemoryMutationAppliedOperation.INVALIDATE:
            payload["persistence_semantics"] = "invalidated_not_deleted"
        if not result.ok:
            retryable = result.reason_code in {
                "memory_candidate_ambiguous",
                "memory_candidate_not_found",
            }
            return self._result(
                data=payload,
                error=result.reason_code or "memory_change_rejected",
                detail=(
                    "记忆定位未唯一命中；请选择候选 fact_id，或按需请求记忆读取工具后重试"
                    if retryable
                    else "记忆变更未执行，请根据 reason_code 调整请求"
                ),
                retryable=retryable,
            )
        return self._result(data=payload)

    async def _can_read_own_person_fact(self, fact: Any, runtime: ToolRuntime) -> bool:
        from yuki.memory.partition import canonical_fact_owner_complete

        owners = await self._runtime_canonical_owners(runtime)
        person_id = owners[0]
        if person_id is None or not canonical_fact_owner_complete(fact):
            return False
        from yuki.identity.person_identity import same_person_id

        if fact.canonical_subject_person_id is None:
            return False
        async with self._people.database.sessions() as session:
            return await same_person_id(session, fact.canonical_subject_person_id, person_id)

    async def _runtime_canonical_owners(
        self, runtime: ToolRuntime
    ) -> tuple[str | None, str | None]:
        return self._social_requester(runtime), self._runtime_space_id(runtime)

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
        allowed_scopes = runtime.memory_allowed_scopes
        if allowed_scopes is None:
            allowed_scopes = (
                MemoryScopeType.PERSON,
                MemoryScopeType.PERSON_GROUP,
                MemoryScopeType.GROUP,
                MemoryScopeType.SELF,
            )
        if not self._settings.self_memory_enabled:
            allowed_scopes = tuple(
                item for item in allowed_scopes if item is not MemoryScopeType.SELF
            )
        scope = await self._memory_reads.authorized(
            requester,
            current_space_id=self._runtime_space_id(runtime),
            private_scene=runtime.effective_scope_type is ScopeType.PRIVATE,
            self_actor=requester is None,
            allowed_scopes=allowed_scopes,
        )
        facts = await self._memories.repository.get_active_authorized(scope, (fact_id,))
        return facts[0] if facts else None

    @staticmethod
    def _memory_json(
        row: Any,
        *,
        retrieval_reason: str,
    ) -> dict[str, Any]:
        payload = {
            "fact_id": row.id,
            "memory_ref": f"M{row.id}",
            "scope": row.scope_type.value,
            "subject": {
                "person_id": row.canonical_subject_person_id,
                "space_id": row.canonical_subject_space_id,
            },
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
            "retrieval_reason": retrieval_reason,
        }
        payload["occurred_at"] = row.valid_from.isoformat() if row.valid_from is not None else None
        return payload

    @staticmethod
    def _self_memory_json(row: Any, *, retrieval_reason: str) -> dict[str, Any]:
        """Project SELF facts without visibility identities or evidence internals."""

        payload = {
            "fact_id": row.id,
            "memory_ref": f"M{row.id}",
            "kind": row.kind.value,
            "category": row.category,
            "content": row.content,
            "importance": row.importance,
            "confidence": row.confidence,
            "status": row.status.value,
            "retrieval_reason": retrieval_reason,
        }
        payload["occurred_at"] = row.valid_from.isoformat() if row.valid_from is not None else None
        return payload

    async def _capture_memory_tool_result(
        self,
        result: ToolExecutionResult,
        runtime: ToolRuntime,
    ) -> ToolExecutionResult:
        payload = result.model_payload()
        fact_ids: list[int] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                ref = value.get("memory_ref")
                if (
                    isinstance(ref, str)
                    and ref.startswith("M")
                    and len(ref) <= 20
                    and ref[1:].isdigit()
                    and int(ref[1:]) > 0
                ):
                    fact_ids.append(int(ref[1:]))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        unique_ids = tuple(dict.fromkeys(fact_ids))
        if payload.get("ok"):
            outcome = "success" if unique_ids else "empty"
        elif payload.get("error_code") in {
            "ambiguous_person",
            "ambiguous_subject",
            "ambiguous_group",
        }:
            outcome = "ambiguous"
        elif payload.get("error_code") == "permission_denied":
            outcome = "permission_denied"
        else:
            outcome = "unavailable"
        payload["evidence_state"] = evidence_state(payload, "memory_tool")
        rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) > self._runtime().agent.tool_result_max_characters:
            await self._record_memory_tool_outcome(runtime, "unavailable", result_count=0)
            return self._result(error="result_too_large", detail="完整结果与证据元数据超过本轮预算")
        await self._record_memory_tool_outcome(runtime, outcome, result_count=len(unique_ids))
        return replace(
            result,
            evidence_state=payload["evidence_state"],
        )

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
            return self._web_result(error="invalid_query", detail="query 必须是字符串")
        query = " ".join(query.split())
        if not query:
            return self._web_result(
                error="invalid_query",
                detail="query 不能为空",
            )
        topic_value = arguments.get("topic", "general")
        if topic_value not in {"general", "news"}:
            return self._web_result(error="invalid_topic", detail="topic 必须是 general 或 news")
        topic = cast(WebSearchTopic, topic_value)
        time_range_value = arguments.get("time_range")
        if time_range_value not in {None, "day", "week", "month", "year"}:
            return self._web_result(error="invalid_time_range", detail="time_range 无效")
        time_range = cast(WebSearchTimeRange | None, time_range_value)
        start_date = self._parse_date(arguments.get("start_date"), "start_date")
        end_date = self._parse_date(arguments.get("end_date"), "end_date")
        if start_date is not None and end_date is not None and start_date > end_date:
            return self._web_result(
                error="invalid_date_range",
                detail="start_date 不能晚于 end_date",
            )
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
        return self._web_result(data=self._web_response_json(response))

    async def _read_webpage(
        self, arguments: dict[str, Any], runtime: ToolRuntime
    ) -> ToolExecutionResult:
        provider = self._web_dependencies()
        raw_url = arguments.get("url")
        if not isinstance(raw_url, str):
            return self._web_result(error="invalid_url", detail="url 必须是字符串")
        normalized = normalize_public_url(raw_url)
        question_value = arguments.get("question")
        if question_value is not None and not isinstance(question_value, str):
            return self._web_result(error="invalid_question", detail="question 必须是字符串")
        question = " ".join((question_value or "读取用户指定的网页").split())
        if not question:
            return self._web_result(
                error="invalid_question",
                detail="question 不能为空",
            )
        source = await provider.extract(normalized, question)
        response = WebSearchResponse(
            query=question,
            sources=(source,),
            provider_request_id=None,
            latency_seconds=0,
            partial_failure=False,
            provider=source.provider,
        )
        return self._web_result(data=self._web_response_json(response))

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
        result = {
            "query": response.query,
            "partial_failure": response.partial_failure,
            "sources": [
                {
                    "source_id": source.source_id,
                    "title": source.title,
                    "url": source.url,
                    "domain": source.domain,
                    "snippet": source.snippet,
                    "relevant_content": source.relevant_content,
                    "published_at": (
                        source.published_at.isoformat() if source.published_at else None
                    ),
                    "provider_score": source.provider_score,
                }
                for source in response.sources
            ],
        }
        if response.provider_summary:
            result["provider_summary"] = response.provider_summary
            result["provider_summary_instruction"] = (
                "provider_summary 是上游模型生成的总结，不是网页原文或逐来源摘录。"
                "总结中的 URL 不证明来源，只有 sources 中的真实搜索来源可作为证据。"
            )
        return result

    @staticmethod
    def _parse_date(value: Any, name: str) -> date | None:
        if value in {None, ""}:
            return None
        if not isinstance(value, str):
            raise WebSearchError("invalid_date", f"{name} 必须是 YYYY-MM-DD")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise WebSearchError("invalid_date", f"{name} 必须是 YYYY-MM-DD") from exc

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError("time must be an ISO string")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return None

    @staticmethod
    def _bounded_int(value: Any, *, default: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("limit must be an integer")
        return max(1, min(int(value), maximum))

    @staticmethod
    def _optional_bounded_int(value: Any, *, default: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("count must be an integer")
        return max(0, min(int(value), maximum))

    def _event_json(self, row: Any) -> dict[str, Any]:
        display_name = row.sender_display_name
        if (
            row.author_is_yuki()
            and not row.sender_group_card.strip()
            and not row.sender_nickname.strip()
        ):
            display_name = self._settings.bot_display_name
        return {
            "id": row.id,
            "event_kind": row.event_kind,
            "author_kind": row.author_kind,
            "source": row.origin,
            "person_id": row.author_person_id,
            "presence_id": row.author_presence_id,
            "sender_nickname": row.sender_nickname,
            "sender_group_card": row.sender_group_card,
            "sender_display_name": display_name,
            "scope": row.scope_type.value,
            "conversation_id": row.canonical_conversation_id,
            "direction": row.direction,
            "content": row.perceived_content,
            "attachments": [
                {
                    "attachment_index": index,
                    "kind": segment["type"],
                    "name": str(segment["data"].get("name", ""))[:200],
                }
                for index, segment in enumerate(
                    segment
                    for segment in row.segments
                    if isinstance(segment, dict)
                    and segment.get("type") in {"image", "video", "file", "audio", "record"}
                    and isinstance(segment.get("data"), dict)
                )
            ],
            "occurred_at": local_iso(row.occurred_at, self._settings.default_timezone),
        }

    def _result(
        self,
        *,
        data: Any = None,
        error: str | None = None,
        detail: str = "",
        retryable: bool = False,
        uncertain: bool = False,
    ) -> ToolExecutionResult:
        from yuki.capabilities.media import result_images

        return ToolExecutionResult(
            ok=error is None,
            data=data,
            error_code=error,
            public_message=detail or None,
            retryable=retryable,
            uncertain=uncertain,
            mutation_committed=None if uncertain or error is None else False,
            images=result_images(data) if error is None else (),
        )

    def _web_result(
        self,
        *,
        data: Any = None,
        error: str | None = None,
        detail: str = "",
    ) -> ToolExecutionResult:
        result = self._result(data=data, error=error, detail=detail)
        return replace(result, evidence_state=evidence_state(result.model_payload(), "web_tool"))

    @staticmethod
    def _runtime() -> RuntimeConfigSnapshot:
        runtime = _RUNTIME_SNAPSHOT.get()
        if runtime is None:
            raise RuntimeError("agent tool runtime snapshot is missing")
        return runtime
