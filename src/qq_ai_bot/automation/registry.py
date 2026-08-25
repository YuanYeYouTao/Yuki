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
    max_tool_calls: int = Field(default=6, ge=0, le=16)
    max_model_requests: int = Field(default=10, ge=1, le=10)
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


class AdminActionArguments(CapabilityArguments):
    action: str = Field(min_length=1, max_length=128)
    target: str | None = Field(default=None, max_length=32)
    user_id: str | None = Field(default=None, max_length=64)
    group_id: str | None = Field(default=None, max_length=64)
    value: Any = None
    delta: int | None = None
    memory_id: int | None = None
    max_importance: int | None = Field(default=None, ge=1, le=5)
    older_than_days: int | None = Field(default=None, ge=1, le=3650)
    content: str | None = Field(default=None, max_length=4000)
    key: str | None = Field(default=None, max_length=128)


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
            "主动发送一条普通私聊消息。",
            SendPrivateArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "onebot.send_group_message",
            "主动发送一条普通群消息。",
            SendGroupArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "speech.send_private",
            "生成本地语音并发送给任务所有者本人。",
            SpeechSendPrivateArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "speech.send_group",
            "生成本地语音并发送到任务创建时的当前群。",
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
            "admin.execute_action",
            "调用已登记的后端管理员业务 action。",
            AdminActionArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "config.get",
            "读取已登记运行时配置。",
            ConfigGetArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.READ,
            RetryPolicy.NONE,
        ),
        (
            "config.set",
            "修改已登记运行时配置。",
            ConfigSetArguments,
            PermissionLevel.SUPERUSER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "web.search",
            "通过受控 Tavily Provider 搜索公开网页。",
            WebSearchArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "web.read_page",
            "通过受控 Provider 读取一个公开网页。",
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
            "在明确范围内搜索本地永久聊天账本。",
            HistorySearchArguments,
            PermissionLevel.USER,
            RiskClass.READ,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "automation.create_task",
            "为当前任务创建者新建后续自动化；task 使用与用户会话相同的 TaskSpec。",
            AutomationCreateTaskArguments,
            PermissionLevel.USER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "automation.update_task",
            "更新当前创建者拥有的自动化任务。",
            AutomationUpdateTaskArguments,
            PermissionLevel.USER,
            RiskClass.MUTATE,
            RetryPolicy.NONE,
        ),
        (
            "automation.cancel_task",
            "取消当前创建者拥有的自动化任务。",
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
    return registry
