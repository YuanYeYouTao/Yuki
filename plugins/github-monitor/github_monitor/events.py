"""Normalize and filter GitHub's untrusted event payloads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .client import as_mapping
from .config import RepositorySubscription
from .models import NormalizedGitHubEvent

SUPPORTED_EVENT_TYPES = frozenset(
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


class GitHubEventIdentityError(ValueError):
    """The upstream event stream cannot be advanced safely."""


def normalize_event(repository: str, raw: object) -> NormalizedGitHubEvent | None:
    item = as_mapping(raw)
    event_id = numeric_event_id(item.get("id"))
    event_type = _text(item.get("type"), 128)
    if event_type not in SUPPORTED_EVENT_TYPES:
        return None
    actor_row = as_mapping(item.get("actor"))
    actor = _text(actor_row.get("login"), 128) or "unknown"
    created_at = datetime.fromisoformat(_text(item.get("created_at"), 64).replace("Z", "+00:00"))
    payload = as_mapping(item.get("payload"))
    normalized = _extract(repository, event_id, event_type, actor, created_at, payload)
    return normalized.model_copy(update={"is_bot": _is_bot(actor, actor_row)})


def event_allowed(event: NormalizedGitHubEvent, subscription: RepositorySubscription) -> bool:
    if event.event_type not in subscription.event_types:
        return False
    if event.actor.casefold() in {actor.casefold() for actor in subscription.ignored_actors}:
        return False
    if subscription.ignore_bots and event.is_bot:
        return False
    if subscription.ignore_draft_pull_requests and event.is_draft:
        return False
    if subscription.branches and event.branch not in subscription.branches:
        return False
    return not (subscription.default_branch_only and event.branch not in {"", "main", "master"})


def _extract(
    repository: str,
    event_id: str,
    event_type: str,
    actor: str,
    created_at: datetime,
    payload: dict[str, Any],
) -> NormalizedGitHubEvent:
    action = _text(payload.get("action"), 64)
    number: int | None = _number(payload.get("number"))
    branch = _text(payload.get("ref"), 255).removeprefix("refs/heads/")
    title = ""
    url = ""
    is_draft = False
    business_id = event_id
    safe_payload: dict[str, Any] = {"repository": repository, "actor": actor}
    for key in ("pull_request", "issue", "comment", "review", "release", "discussion"):
        row = as_mapping(payload.get(key))
        if not row:
            continue
        number = _number(row.get("number")) or number
        title = _text(row.get("title"), 300)
        url = _text(row.get("html_url"), 1000)
        is_draft = bool(row.get("draft", False))
        business_id = _text(row.get("id"), 128) or str(number or event_id)
        safe_payload.update(
            {
                "number": number,
                "title": title,
                "url": url,
                "excerpt": _text(row.get("body"), 500),
                "merged": bool(row.get("merged", False)),
            }
        )
        break
    before = _text(payload.get("before"), 64)
    head = _text(payload.get("head"), 64)
    deleted = bool(payload.get("deleted", False))
    if event_type == "PushEvent":
        business_id = head or event_id
        safe_payload.update(
            {
                "before": before,
                "head": head,
                "ref": branch,
                "size": _number(payload.get("size")) or 0,
                "distinct_size": _number(payload.get("distinct_size")) or 0,
                "forced": bool(payload.get("forced", False)),
                "deleted": deleted,
                "commits": _commits(payload.get("commits")),
            }
        )
    elif event_type == "ReleaseEvent":
        release = as_mapping(payload.get("release"))
        title = _text(release.get("name"), 300) or _text(release.get("tag_name"), 128)
        assets = release.get("assets") if isinstance(release.get("assets"), list) else []
        safe_payload.update(
            {
                "tag": _text(release.get("tag_name"), 128),
                "name": title,
                "target": _text(release.get("target_commitish"), 255),
                "prerelease": bool(release.get("prerelease", False)),
                "draft": bool(release.get("draft", False)),
                "assets_count": len(assets),
            }
        )
    elif event_type in {"CreateEvent", "DeleteEvent"}:
        branch = _text(payload.get("ref"), 255)
        safe_payload.update({"ref": branch, "ref_type": _text(payload.get("ref_type"), 32)})
    old_event_key = legacy_event_key(repository, event_type, business_id, action)
    event_key = singleton_event_key(repository, event_id)
    summary = _basic_summary(repository, event_type, action, number, title, branch, safe_payload)
    safe_payload.update({"event_type": event_type, "action": action})
    return NormalizedGitHubEvent(
        github_event_id=event_id,
        repository=repository,
        event_type=event_type,
        actor=actor,
        created_at=created_at,
        action=action,
        branch=branch,
        number=number,
        title=title,
        url=url,
        summary=summary,
        payload=safe_payload,
        event_key=event_key,
        is_draft=is_draft,
        push_before=before,
        push_head=head,
        push_deleted=deleted,
        legacy_event_key=old_event_key,
    )


def stable_event_key(repository: str, event_type: str, identity: str, action: str = "") -> str:
    """Compatibility name for the pre-C4 business-object key."""

    return legacy_event_key(repository, event_type, identity, action)


def legacy_event_key(repository: str, event_type: str, identity: str, action: str = "") -> str:
    raw = f"github:{repository}:{event_type}:{identity}:{action}".rstrip(":")
    if len(raw) <= 255:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return f"{raw[:190]}:{digest}"


def singleton_event_key(repository: str, github_event_id: str) -> str:
    event_id = numeric_event_id(github_event_id)
    key = f"github:{repository}:event:{event_id}"
    if len(key) > 255:
        raise GitHubEventIdentityError("github_event_key_too_long")
    return key


def event_key_for_boundary(event: NormalizedGitHubEvent, legacy_boundary: str) -> str:
    if legacy_boundary:
        boundary = numeric_event_id(legacy_boundary)
        if int(event.github_event_id) <= int(boundary):
            if not event.legacy_event_key:
                raise GitHubEventIdentityError("legacy_event_key_missing")
            return event.legacy_event_key
    return singleton_event_key(event.repository, event.github_event_id)


def numeric_event_id(value: object) -> str:
    event_id = _text(value, 128)
    if not event_id or not event_id.isdecimal():
        raise GitHubEventIdentityError("github_event_id_not_numeric")
    if str(int(event_id)) != event_id:
        raise GitHubEventIdentityError("github_event_id_not_canonical")
    return event_id


def raw_event_fingerprint(raw: object) -> str:
    item = as_mapping(raw)
    numeric_event_id(item.get("id"))
    try:
        encoded = json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise GitHubEventIdentityError("github_event_payload_invalid") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def deduplicate_raw_events(raw_events: Sequence[object]) -> tuple[dict[str, Any], ...]:
    """Collapse page overlap and fail closed when one source id changes payload."""

    by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    for raw in raw_events:
        item = as_mapping(raw)
        event_id = numeric_event_id(item.get("id"))
        fingerprint = raw_event_fingerprint(item)
        existing = by_id.get(event_id)
        if existing is not None and existing[0] != fingerprint:
            raise GitHubEventIdentityError("github_event_payload_conflict")
        by_id[event_id] = (fingerprint, item)
    return tuple(by_id[event_id][1] for event_id in sorted(by_id, key=int))


def _basic_summary(
    repository: str,
    event_type: str,
    action: str,
    number: int | None,
    title: str,
    branch: str,
    payload: dict[str, Any],
) -> str:
    suffix = f"：{title}" if title else ""
    if event_type == "PushEvent":
        size = int(payload.get("distinct_size", 0) or payload.get("size", 0) or 0)
        forced = "强制" if payload.get("forced") else ""
        return f"{repository} 的 {branch or '默认'} 分支{forced}推送了 {size} 个提交"
    if event_type == "PullRequestEvent":
        verb = "已合并" if action == "closed" and payload.get("merged") else _action(action, "更新")
        return f"{repository} 的 PR #{number or '?'} {verb}{suffix}"
    if event_type == "IssuesEvent":
        return f"{repository} 的 Issue #{number or '?'} {_action(action, '更新')}{suffix}"
    if event_type in {"IssueCommentEvent", "PullRequestReviewCommentEvent"}:
        return f"{repository} 的 #{number or '?'} 有一条新评论"
    if event_type == "PullRequestReviewEvent":
        return f"{repository} 的 PR #{number or '?'} 收到新的 Review"
    if event_type == "ReleaseEvent":
        return f"{repository} 发布了 {payload.get('tag') or title or '新版本'}"
    if event_type == "CreateEvent":
        return f"{repository} 创建了 {branch or '新引用'}"
    if event_type == "DeleteEvent":
        return f"{repository} 删除了 {branch or '一个引用'}"
    if event_type == "ForkEvent":
        return f"{repository} 被 Fork 了"
    if event_type == "WatchEvent":
        return f"{repository} 收到一个新的 Star"
    if event_type == "DiscussionEvent":
        return f"{repository} 的 Discussion {_action(action, '更新')}{suffix}"
    if event_type == "DiscussionCommentEvent":
        return f"{repository} 的 Discussion 有一条新评论"
    return f"{repository} 发生了 {event_type}"


def _action(action: str, fallback: str) -> str:
    return {
        "opened": "已创建",
        "closed": "已关闭",
        "reopened": "已重新打开",
        "published": "已发布",
        "created": "已创建",
        "edited": "已编辑",
        "deleted": "已删除",
    }.get(action, fallback)


def _commits(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "sha": _text(as_mapping(item).get("sha"), 40),
            "message": _text(as_mapping(item).get("message"), 240),
        }
        for item in value[:6]
    ]


def _is_bot(actor: str, row: dict[str, Any]) -> bool:
    return _text(row.get("type"), 32).casefold() == "bot" or actor.casefold().endswith("[bot]")


def _text(value: object, maximum: int) -> str:
    return str(value).strip()[:maximum] if value is not None else ""


def _number(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
