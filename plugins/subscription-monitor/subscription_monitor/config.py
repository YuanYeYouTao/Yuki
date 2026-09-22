"""Subscription configuration; conditions are input to the existing Main Agent."""

from __future__ import annotations

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import NotificationTarget, StrictModel

JUDGE_TEMPLATES = {
    "any_update": "有新的订阅动态时，简短介绍与订阅有关的内容并附上来源链接。",
    "release": (
        "只在原文明确宣布产品、模型或软件版本已发布或上线时通知；"
        "预告、传闻、转述旧消息不通知。说明发布对象和动作。"
    ),
    "reset_time": (
        "只在原文明确说明额度、限额或服务的重置时间时通知。"
        "摘出重置时间和原文时区；没有时区时说明未知，不猜测。"
    ),
}


class Subscription(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    url: str = Field(min_length=1, max_length=2048)
    name: str = Field(default="", max_length=100)
    enabled: bool = True
    targets: tuple[NotificationTarget, ...] = Field(min_length=1, max_length=4)
    interval_seconds: int = Field(default=300, ge=30, le=86400)
    initial_sync: Literal["baseline", "replay_recent"] = "baseline"
    replay_recent_limit: int = Field(default=3, ge=1, le=20)
    include_any: tuple[str, ...] = Field(default=(), max_length=20)
    exclude_any: tuple[str, ...] = Field(default=(), max_length=20)
    judge_template: Literal["any_update", "release", "reset_time"] = "any_update"
    judge_prompt: str = Field(default="", max_length=700)

    @field_validator("url")
    @classmethod
    def public_feed_url(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("feed URL must use HTTP(S)")
        if parts.username or parts.password or parts.fragment:
            raise ValueError("feed URL cannot contain credentials or fragments")
        # DNS and redirect checks remain owned by the Host HTTP facade.
        _ = parts.port
        return value

    @field_validator("include_any", "exclude_any")
    @classmethod
    def bounded_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 100 for item in values):
            raise ValueError("keywords must contain 1-100 nonblank characters")
        return tuple(dict.fromkeys(item.strip().casefold() for item in values))

    @model_validator(mode="after")
    def unique_targets(self) -> Self:
        keys = [(target.target_type, target.target_id) for target in self.targets]
        if len(set(keys)) != len(keys):
            raise ValueError("subscription targets must be unique")
        return self

    def condition(self) -> str:
        return self.judge_prompt.strip() or JUDGE_TEMPLATES[self.judge_template]


class MonitorConfig(StrictModel):
    poll_interval_seconds: int = Field(default=30, ge=5, le=3600)
    max_notifications_per_poll: int = Field(default=10, ge=1, le=100)
    subscriptions: tuple[Subscription, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def unique_subscriptions(self) -> Self:
        ids = [item.id for item in self.subscriptions]
        if len(ids) != len(set(ids)):
            raise ValueError("subscription IDs must be unique")
        return self


async def load_config(context: PluginContext) -> MonitorConfig:
    values = {}
    for key in MonitorConfig.model_fields:
        value = await context.config.get(key)
        if value is not None:
            values[key] = value
    return MonitorConfig.model_validate(values)


async def save_subscriptions(context: PluginContext, rows: tuple[Subscription, ...]) -> None:
    MonitorConfig(subscriptions=rows)
    await context.config.set("subscriptions", [row.model_dump(mode="json") for row in rows])
