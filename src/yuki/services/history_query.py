"""One public history query shape; limits are defaults, never hidden caps."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HistoryTarget(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=lambda schema: schema.update(
            oneOf=[{"required": [name]} for name in HistoryTarget.model_fields]
        ),
    )
    conversation_id: UUID | None = None
    person_id: UUID | None = None
    space_id: UUID | None = None
    binding_id: UUID | None = None
    operation_id: UUID | None = None

    @model_validator(mode="after")
    def one_selector(self) -> "HistoryTarget":
        if len(self.model_dump(exclude_none=True)) != 1:
            raise ValueError("target requires exactly one internal selector")
        return self


class HistorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str | None = None
    scope: Literal["private", "group"] | None = None


class HistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["recent", "search", "around"] = "recent"
    target: HistoryTarget | None = None
    source: HistorySource | None = None
    order: Literal["recent", "oldest"] = "recent"
    keyword: str = ""
    match: Literal["all", "any", "phrase"] = "all"
    exclude: tuple[str, ...] = ()
    person_id: UUID | None = None
    author_kind: Literal["person", "yuki", "external_bot", "system"] | None = None
    event_kind: Literal["message", "external_event", "all"] = "message"
    start_at: datetime | None = None
    end_at: datetime | None = None
    has: tuple[Literal["image", "video", "file", "audio"], ...] = ()
    limit: int | None = Field(default=None, gt=0, strict=True)
    cursor: int | None = Field(default=None, gt=0, strict=True)
    event_id: int | None = Field(default=None, gt=0, strict=True)
    before: int | None = Field(default=None, ge=0, strict=True)
    after: int | None = Field(default=None, ge=0, strict=True)
    context_before: int = Field(default=0, ge=0, strict=True)
    context_after: int = Field(default=0, ge=0, strict=True)

    @field_validator("start_at", "end_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return (
            value if value.tzinfo else value.replace(tzinfo=timezone(timedelta(hours=8)))
        ).astimezone(UTC)


def history_schema() -> dict[str, object]:
    return HistoryQuery.model_json_schema()


HISTORY_TOOL_DESCRIPTION = (
    "查询已观察到的聊天账本。recent 读取近期，search 按原文/本人语音、作者、日期或附件筛选，"
    "around 按内部 event_id 读取原会话邻文。target 省略时 recent 浏览当前会话，"
    "search 搜索所有获准的聊天。source可按平台及private/group筛选，"
    "与target取交集；recent仅给source时浏览该来源的会话集合。"
    "显式目标使用一个内部 ID，多入口合并检索并分别标明平台、私聊/群聊、会话名称与话题来源。"
    "可查重开上下文前的永久账本。target.person_id 选择私聊入口，顶层 person_id 筛选原作者。"
    "operation_id 沿原发送回执定位会话和原锚点。cursor 按 order 续查 ID 页；"
    "before/after 是 around 条数；around按锚点取邻文，不使用关键词、作者、日期、附件筛选。"
    "search可用context_before/context_after附带各命中的邻文，默认不读。"
    "日期未带时区按 UTC+8，起点包含、终点不含。"
    "不调用平台补查，不接受平台消息号。"
)
