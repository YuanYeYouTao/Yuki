"""Strict, JSON-storable plugin configuration."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from yuki_plugin_sdk.models import StrictModel

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

DEFAULT_EVENT_TYPES = frozenset(
    {
        "PushEvent",
        "PullRequestEvent",
        "IssuesEvent",
        "IssueCommentEvent",
        "PullRequestReviewEvent",
        "PullRequestReviewCommentEvent",
        "ReleaseEvent",
        "CreateEvent",
        "DeleteEvent",
        "ForkEvent",
        "WatchEvent",
        "DiscussionEvent",
        "DiscussionCommentEvent",
    }
)


class NotificationTargetConfig(StrictModel):
    target_type: Literal["group", "private"]
    target_id: str = Field(min_length=1, max_length=64)
    ask_agent: bool = True
    send_text: bool = True
    send_card: bool = True


class RepositorySubscription(StrictModel):
    repository: str
    enabled: bool = True
    event_types: frozenset[str] = DEFAULT_EVENT_TYPES
    branches: frozenset[str] = frozenset()
    ignored_actors: frozenset[str] = frozenset()
    ignore_bots: bool = True
    ignore_draft_pull_requests: bool = False
    default_branch_only: bool = False
    targets: tuple[NotificationTargetConfig, ...] = Field(min_length=1)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        normalized = value.strip()
        if _REPOSITORY.fullmatch(normalized) is None:
            raise ValueError("repository must use owner/name")
        return normalized

    @model_validator(mode="after")
    def unique_targets(self) -> Self:
        keys = [(target.target_type, target.target_id) for target in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("repository targets must be unique")
        return self


class GitHubMonitorConfig(StrictModel):
    poll_interval_seconds: int = Field(default=60, ge=30, le=3600)
    initial_sync_mode: Literal["baseline", "replay_recent"] = "baseline"
    replay_recent_limit: int = Field(default=5, ge=1, le=20)
    events_per_repository: int = Field(default=100, ge=1, le=100)
    max_events_per_poll: int = Field(default=50, ge=1, le=200)
    coalesce: bool = True
    request_timeout_seconds: int = Field(default=20, ge=3, le=60)
    repositories: tuple[RepositorySubscription, ...] = ()

    @model_validator(mode="after")
    def unique_repositories(self) -> Self:
        names = [item.repository.casefold() for item in self.repositories]
        if len(names) != len(set(names)):
            raise ValueError("repositories must be unique")
        return self


CONFIG_KEYS = tuple(GitHubMonitorConfig.model_fields)


async def load_config(context: object) -> GitHubMonitorConfig:
    config = context.config
    values: dict[str, object] = {}
    for key in CONFIG_KEYS:
        value = await config.get(key)
        if value is not None:
            values[key] = value
    return GitHubMonitorConfig.model_validate(values)


async def save_repositories(
    context: object, repositories: tuple[RepositorySubscription, ...]
) -> None:
    await context.config.set(
        "repositories",
        [item.model_dump(mode="json") for item in repositories],
    )
