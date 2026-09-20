"""Explicit capability registry for the automation DSL."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.automation.authority import AuthorityContext, PermissionLevel
from qq_ai_bot.automation.models import AutomationContext, RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.task_spec import TaskSpec


class CapabilityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerateArguments(CapabilityArguments):
    instruction: str = Field(min_length=1, max_length=4000)
    context_profile: Literal["none", "creator_private", "current_group"] = "none"
    max_characters: int = Field(default=200, ge=1, le=4000)


class AgentArguments(CapabilityArguments):
    instruction: str = Field(min_length=1, max_length=4000)
    context_profile: Literal["none", "creator_private", "current_group"] = "none"
    max_tool_calls: int = Field(default=32, ge=0, le=160)
    max_model_requests: int = Field(default=24, ge=1, le=120)
    allowed_capabilities: tuple[str, ...] = Field(default=(), max_length=128)


class AutomationCreateTaskArguments(CapabilityArguments):
    task: TaskSpec
    max_runs: int | None = Field(default=None, ge=1, le=10000)


class AutomationUpdateTaskArguments(CapabilityArguments):
    automation_id: int = Field(ge=1)
    task: TaskSpec


class AutomationIdArguments(CapabilityArguments):
    automation_id: int = Field(ge=1)


class AutomationListArguments(CapabilityArguments):
    include_completed: bool = False
    match_task: TaskSpec | None = None
    max_runs: int | None = Field(default=None, ge=1, le=10000)


class SendPrivateArguments(CapabilityArguments):
    user_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=12000)


class SendGroupArguments(CapabilityArguments):
    group_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=12000)


class SpeechSendPrivateArguments(SendPrivateArguments):
    style_hint: str = Field(default="", max_length=128)
    profile_id: str = Field(default="", max_length=64)


class SpeechSendGroupArguments(SendGroupArguments):
    style_hint: str = Field(default="", max_length=128)
    profile_id: str = Field(default="", max_length=64)


class EmojiSendArguments(CapabilityArguments):
    emotion: str = Field(default="", max_length=100)
    intended_tone: str = Field(default="", max_length=300)
    group_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_id: str | None = Field(default=None, min_length=1, max_length=64)
    placement: Literal["before_text", "after_text", "only"] = "only"

    @model_validator(mode="after")
    def _one_target(self) -> EmojiSendArguments:
        if (self.group_id is None) == (self.user_id is None):
            raise ValueError("group_id 和 user_id 必须且只能提供一个")
        return self


class EmojiSendByIdArguments(EmojiSendArguments):
    emoji_id: str = Field(min_length=8, max_length=64)


class OneBotCallArguments(CapabilityArguments):
    action: str = Field(min_length=1, max_length=128)
    params: dict[str, Any]


class ConfigGetArguments(CapabilityArguments):
    key: str = Field(min_length=1, max_length=128)
    scope_type: Literal["global", "group", "user"] = "global"
    scope_id: str = Field(default="", max_length=64)


class ConfigSetArguments(ConfigGetArguments):
    value: Any


class WebSearchArguments(CapabilityArguments):
    query: str = Field(min_length=1, max_length=400)
    topic: Literal["general", "news"] = "general"
    time_range: Literal["day", "week", "month", "year"] | None = None


class WebReadArguments(CapabilityArguments):
    url: str = Field(min_length=1, max_length=2048)
    question: str = Field(default="", max_length=1000)


class PersonMemoryArguments(CapabilityArguments):
    user_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)


class GroupMemoryArguments(CapabilityArguments):
    group_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)


class HistorySearchArguments(CapabilityArguments):
    keyword: str = Field(min_length=1, max_length=400)
    user_id: str | None = Field(default=None, max_length=64)
    group_id: str | None = Field(default=None, max_length=64)
    after: str | None = Field(default=None, max_length=64)
    before: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=20, ge=1, le=100)


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    data: dict[str, Any]
    llm_calls: int = 0
    tool_calls: int = 1
    messages_sent: int = 0
    pending_work_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityExecutionContext:
    authority: AuthorityContext
    automation_id: int
    automation_run_id: int
    step_id: str
    creator_user_id: str
    bot_user_id: str
    current_group_id: str | None
    scheduled_for: datetime
    actual_started_at: datetime
    local_time: datetime
    timezone: str
    automation_context: AutomationContext
    conversation_key: str
    web_was_used: bool = False
    gateway: object | None = None
    canonical_target_person_id: str | None = None
    canonical_target_space_id: str | None = None
    canonical_conversation_id: str | None = None
    conversation_generation: int | None = None
    automation_script_hash: str = ""
    source_step_id: str = ""
    agent_instruction: str | None = None
    agent_context_profile: str = "none"
    revalidate_authority: Callable[[str | None], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )


CapabilityHandler = Callable[
    [dict[str, Any], CapabilityExecutionContext], Awaitable[CapabilityResult]
]
CapabilityArgumentValidator = Callable[[object, bool], dict[str, Any]]
CapabilitySchemaVersion = int | str


@dataclass(frozen=True, slots=True)
class AutomationCapability:
    name: str
    description: str
    argument_model: type[BaseModel]
    output_schema: dict[str, object]
    required_permission: PermissionLevel
    risk_class: RiskClass
    retry_policy: RetryPolicy
    allowed_origins: frozenset[TurnOrigin]
    schema_version: CapabilitySchemaVersion = 1
    argument_schema: dict[str, object] | None = None
    argument_validator: CapabilityArgumentValidator | None = field(default=None, repr=False)
    provider_plugin_id: str | None = None
    provider_version: str | None = None
    provider_manifest_hash: str | None = None
    handler: CapabilityHandler | None = field(default=None, repr=False)
    result_cacheable: bool = True
    model_tool_name: str = ""

    @property
    def input_schema(self) -> dict[str, object]:
        return self.argument_schema or self.argument_model.model_json_schema()

    def validate_arguments(
        self,
        value: object,
        *,
        allow_templates: bool = False,
    ) -> dict[str, Any]:
        """Validate arguments through the capability's native schema contract."""

        if self.argument_validator is not None:
            return self.argument_validator(value, allow_templates)
        return self.argument_model.model_validate(value).model_dump()

    def permits(self, permission: PermissionLevel) -> bool:
        return not (
            self.required_permission is PermissionLevel.SUPERUSER
            and permission is not PermissionLevel.SUPERUSER
        )


class AutomationCapabilityRegistry:
    """Reviewed capability allowlist; never reflects arbitrary Python functions."""

    def __init__(self) -> None:
        self._items: dict[str, AutomationCapability] = {}

    def register(self, definition: AutomationCapability) -> None:
        if definition.name in self._items:
            raise ValueError(f"duplicate automation capability: {definition.name}")
        self._items[definition.name] = definition

    def get(self, name: str) -> AutomationCapability | None:
        return self._items.get(name)

    def unregister(self, name: str) -> bool:
        return self._items.pop(name, None) is not None

    def unregister_plugin(self, plugin_id: str) -> int:
        names = [name for name, item in self._items.items() if item.provider_plugin_id == plugin_id]
        for name in names:
            self._items.pop(name, None)
        return len(names)

    def require(self, name: str) -> AutomationCapability:
        definition = self.get(name)
        if definition is None:
            raise ValueError(f"未登记的自动化 capability：{name}")
        return definition

    def list(self) -> tuple[AutomationCapability, ...]:
        return tuple(self._items[name] for name in sorted(self._items))

    def names_for(self, permission: PermissionLevel) -> tuple[str, ...]:
        return tuple(item.name for item in self.list() if item.permits(permission))

    def agent_tool_name(self, capability_name: str) -> str:
        """Return a provider-neutral model-safe name for one registered capability."""

        self.require(capability_name)
        normalized = re.sub(r"[^a-zA-Z0-9_]", "_", capability_name.replace(".", "__"))
        normalized = re.sub(r"_+", "_", normalized).strip("_") or "capability"
        digest = hashlib.sha256(capability_name.encode("utf-8")).hexdigest()[:8]
        return f"{normalized[:51]}_{digest}"

    def resolve_agent_reference(self, reference: str) -> str:
        """Resolve exact names or model-safe aliases without guessing ambiguous tools."""

        if reference in self._items:
            return reference
        matches = [name for name in self._items if self.agent_tool_name(name) == reference]
        if len(matches) == 1:
            return matches[0]
        normalized = _loose_capability_key(reference)
        loose_matches = [name for name in self._items if _loose_capability_key(name) == normalized]
        if len(loose_matches) == 1:
            return loose_matches[0]
        if len(loose_matches) > 1:
            raise ValueError(f"自动化能力引用不明确：{reference}")
        raise ValueError(f"未登记的自动化 capability：{reference}")

    def delegatable(self) -> tuple[AutomationCapability, ...]:
        """List capabilities that a scheduled Agent may call itself."""

        return tuple(item for item in self.list() if _is_agent_delegatable(item))


def _loose_capability_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _is_agent_delegatable(item: AutomationCapability) -> bool:
    # yuki.agent/yuki.generate would recursively start another model loop. All
    # concrete READ/SEND/MUTATE capabilities remain delegatable; identity,
    # ownership and per-run quotas are enforced by their handlers/executor.
    return not item.name.startswith("yuki.")


def build_capability_registry(
    handlers: dict[str, CapabilityHandler] | None = None,
) -> AutomationCapabilityRegistry:
    """Build the versioned 1.5 registry with optionally bound handlers."""

    bound = handlers or {}
    scheduled = frozenset({TurnOrigin.SCHEDULED_AUTOMATION, TurnOrigin.SYSTEM_TASK})
    definitions: Iterable[
        tuple[
            str,
            str,
            type[BaseModel],
            PermissionLevel,
            RiskClass,
            RetryPolicy,
        ]
    ] = (
        (
            "yuki.generate",
            "调用主模型生成文字，不开放工具。",
            GenerateArguments,
            PermissionLevel.USER,
            RiskClass.GENERATE,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "yuki.agent",
            "运行受委托能力约束的 Agent。",
            AgentArguments,
            PermissionLevel.USER,
            RiskClass.GENERATE,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "onebot.send_private_message",
            "自动化委托发送：用 user_id 和 text 向已授权私聊发送文本；不生成当前会话最终回复。",
            SendPrivateArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "onebot.send_group_message",
            "自动化委托发送：用 group_id 和 text 向已授权群发送文本；不支持结构化 mentions。",
            SendGroupArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "speech.send_private",
            "自动化语音发送：用 user_id 和 text 向任务所有者发送指定文本"
            "。profile_id 可省略；不同于本轮回复布局 send_voice。",
            SpeechSendPrivateArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "speech.send_group",
            "自动化语音发送：用 group_id 和 text 向创建时授权群发送指定文"
            "本。profile_id 可省略；不同于本轮回复布局 send_voice。",
            SpeechSendGroupArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "emoji.send",
            "按语气和情绪选择已采用表情，并发送到已授权的本人私聊或当前群。",
            EmojiSendArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "emoji.send_by_id",
            "发送任务创建时明确指定、且当前作用域可用的已采用表情。",
            EmojiSendByIdArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "onebot.call_api",
            "调用任意公开 QQ/OneBot Provider action。",
            OneBotCallArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "config.get",
            "自动化管理员委托读取单个配置 key；scope_type 和 scope_id "
            "指定授权范围。批量当前请求读取使用 admin_get_config 的 keys。",
            ConfigGetArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.READ,
            RetryPolicy.NONE,
        ),
        (
            "config.set",
            "自动化管理员委托修改单个配置 key/value；指定 scope_type/scope_id，"
            "不允许修改 automation.*。不能替代当前请求的 admin_set_config 授权。",
            ConfigSetArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "web.search",
            "通过已配置的搜索服务查询公开网页；query 为问题，topic 为 gen"
            "eral/news，time_range 可省略。仅在自动化委托范围内执行。",
            WebSearchArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "web.read_page",
            "读取已授权的公开网页 url，question 可用于聚焦内容；遵守自动化委托和 URL 校验。",
            WebReadArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "memory.get_person",
            "读取创建者本人或已授权人物记忆。",
            PersonMemoryArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "memory.get_group",
            "读取当前群或已授权群记忆。",
            GroupMemoryArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "history.search",
            "在自动化授权范围内按 keyword、可选 user_id/group_id "
            "及时间搜索本地账本；绑定 canonical 会话时搜索该会话，不代表全库检索。",
            HistorySearchArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "automation.create_task",
            "为已授权任务创建者登记后续自动化，task 填写结构化 TaskSpec；不同于 au"
            "tomation_create 的会话创建参数。以持久化 ID 确认创建，不重复登记。",
            AutomationCreateTaskArguments,
            PermissionLevel.USER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "automation.update_task",
            "按 automation_id 和 task 更新委托创建者拥有的自动化；"
            "task 是结构化 TaskSpec，仍核验所有权。",
            AutomationUpdateTaskArguments,
            PermissionLevel.USER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "automation.cancel_task",
            "按 automation_id 取消委托创建者拥有的自动化；只取消指定任务，不撤销已完成效果。",
            AutomationIdArguments,
            PermissionLevel.USER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "automation.run_task_now",
            "立即调度当前创建者拥有的自动化任务。",
            AutomationIdArguments,
            PermissionLevel.USER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "automation.list_tasks",
            "列出当前创建者自己的自动化任务和稳定 ID。",
            AutomationListArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.NONE,
        ),
    )
    registry = AutomationCapabilityRegistry()
    for name, description, model, permission, risk, retry in definitions:
        registry.register(
            AutomationCapability(
                name=name,
                description=description,
                argument_model=model,
                output_schema={"type": "object"},
                required_permission=permission,
                risk_class=risk,
                retry_policy=retry,
                allowed_origins=scheduled,
                handler=bound.get(name),
            )
        )
    from qq_ai_bot.social.automation import register_social_automation

    register_social_automation(registry, bound)
    return registry
