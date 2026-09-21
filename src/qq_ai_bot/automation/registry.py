"""Explicit capability registry for the automation DSL."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from qq_ai_bot.automation.authority import AuthorityContext, PermissionLevel
from qq_ai_bot.automation.models import AutomationContext, RetryPolicy, RiskClass, TurnOrigin


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
    # Historical persisted DSL metadata only; never an execution allowlist.
    allowed_capabilities: tuple[str, ...] = Field(default=(), max_length=128)


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
    start_date: str | None = None
    end_date: str | None = None


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
    canonical_creator_person_id: str | None = None
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
            "调用主 Agent 完成生成目标，复用完整工具。",
            GenerateArguments,
            PermissionLevel.USER,
            RiskClass.GENERATE,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "yuki.agent",
            "以创建者当前权限运行主 Agent。",
            AgentArguments,
            PermissionLevel.USER,
            RiskClass.GENERATE,
            RetryPolicy.TRANSIENT_ONCE,
        ),
        (
            "onebot.send_private_message",
            "向已授权私聊发送文本。",
            SendPrivateArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "onebot.send_group_message",
            "向已授权群发送文本。",
            SendGroupArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "speech.send_private",
            "自动化语音发送：用 user_id 和 text 向任务所有者发送指定文本"
            "。profile_id 可省略；此项是显式 DSL 步骤，Agent 发送使用 send_message。",
            SpeechSendPrivateArguments,
            PermissionLevel.USER,
            RiskClass.SEND,
            RetryPolicy.NONE,
        ),
        (
            "speech.send_group",
            "自动化语音发送：用 group_id 和 text 向创建时授权群发送指定文"
            "本。profile_id 可省略；此项是显式 DSL 步骤，Agent 发送使用 send_message。",
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
            "读取授权范围内的配置。",
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
