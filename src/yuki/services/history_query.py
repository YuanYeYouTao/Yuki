"""One public history query shape; limits are defaults, never hidden caps."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HistoryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


class HistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["recent", "search", "around"] = "recent"
    target: HistoryTarget | None = None
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
    schema = HistoryQuery.model_json_schema()
    schema["required"] = ["view"]
    target = schema["$defs"]["HistoryTarget"]
    target["oneOf"] = [{"required": [name]} for name in HistoryTarget.model_fields]
    return schema


HISTORY_TOOL_DESCRIPTION = (
    "查询已观察到的聊天账本。recent 读取近期，search 按原文/本人语音、作者、日期或附件筛选，"
    "around 按内部 event_id 读取邻文。target 省略为当前 Conversation；跨入口用一个内部 ID，"
    "有多个会话时先选择候选。target.person_id 选择入口，顶层 person_id 筛选原作者。"
    "operation_id 沿原发送回执定位会话和原锚点。cursor 按 order 续查 ID 页；"
    "before/after 是 around 条数。日期未带时区按 UTC+8，起点包含、终点不含。"
    "不调用平台补查，不接受平台消息号。"
)
