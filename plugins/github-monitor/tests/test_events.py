from __future__ import annotations

import pytest
from github_monitor.config import NotificationTargetConfig, RepositorySubscription
from github_monitor.events import (
    GitHubEventIdentityError,
    deduplicate_raw_events,
    event_allowed,
    event_key_for_boundary,
    normalize_event,
    raw_event_fingerprint,
    singleton_event_key,
)
from github_monitor.formatter import apply_compare


def test_push_event_is_bounded_stable_and_enriched() -> None:
    raw = {
        "id": "123",
        "type": "PushEvent",
        "actor": {"login": "alice", "type": "User"},
        "created_at": "2026-08-05T10:30:00Z",
        "payload": {
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "head": "b" * 40,
            "size": 2,
            "distinct_size": 2,
            "commits": [{"sha": "b" * 40, "message": "fix notifications"}],
        },
    }
    event = normalize_event("owner/repo", raw)
    assert event is not None
    assert event.branch == "main"
    assert event.event_key == singleton_event_key("owner/repo", "123")
    enriched = apply_compare(
        event,
        {
            "total_commits": 2,
            "ahead_by": 2,
            "status": "ahead",
            "files": [{"additions": 9, "deletions": 3}],
            "commits": [],
        },
    )
    assert enriched.payload["files_changed"] == 1
    assert "+9 / -3" in enriched.summary


def test_subscription_filters_bot_actor_and_branch() -> None:
    subscription = RepositorySubscription(
        repository="owner/repo",
        branches=frozenset({"main"}),
        targets=(NotificationTargetConfig(target_type="group", target_id="2001"),),
    )
    bot = normalize_event(
        "owner/repo",
        {
            "id": "1",
            "type": "PushEvent",
            "actor": {"login": "dependabot[bot]", "type": "Bot"},
            "created_at": "2026-08-05T10:30:00Z",
            "payload": {"ref": "refs/heads/main", "head": "a"},
        },
    )
    assert bot is not None
    assert not event_allowed(bot, subscription)


def test_release_event_preserves_bounded_card_details() -> None:
    event = normalize_event(
        "owner/repo",
        {
            "id": "124",
            "type": "ReleaseEvent",
            "actor": {"login": "alice", "type": "User"},
            "created_at": "2026-08-05T10:30:00Z",
            "payload": {
                "action": "published",
                "release": {
                    "id": 42,
                    "name": "Yuki 3.4.2",
                    "tag_name": "v3.4.2",
                    "target_commitish": "main",
                    "body": "新增 Release 通知卡片。",
                    "html_url": "https://github.com/owner/repo/releases/tag/v3.4.2",
                    "draft": False,
                    "prerelease": False,
                    "assets": [{"id": 1}, {"id": 2}],
                },
            },
        },
    )

    assert event is not None
    assert event.title == "Yuki 3.4.2"
    assert event.payload["tag"] == "v3.4.2"
    assert event.payload["target"] == "main"
    assert event.payload["assets_count"] == 2
    assert event.payload["prerelease"] is False
    assert event.payload["excerpt"] == "新增 Release 通知卡片。"


def test_comment_events_use_distinct_raw_source_ids() -> None:
    base = {
        "type": "IssueCommentEvent",
        "actor": {"login": "alice", "type": "User"},
        "created_at": "2026-08-05T10:30:00Z",
        "payload": {
            "action": "created",
            "issue": {"id": 42, "number": 7, "title": "same issue"},
        },
    }
    first = normalize_event("owner/repo", {**base, "id": "200"})
    second = normalize_event("owner/repo", {**base, "id": "201"})
    assert first is not None and second is not None
    assert first.legacy_event_key == second.legacy_event_key
    assert first.event_key == "github:owner/repo:event:200"
    assert second.event_key == "github:owner/repo:event:201"


def test_raw_ids_are_numeric_and_page_overlap_is_fingerprint_checked() -> None:
    event_99 = {"id": "99", "type": "WatchEvent", "payload": {"action": "started"}}
    event_100 = {"id": "100", "type": "WatchEvent", "payload": {"action": "started"}}
    rows = deduplicate_raw_events([event_100, event_99, dict(event_100)])
    assert [row["id"] for row in rows] == ["99", "100"]
    assert raw_event_fingerprint(event_100) == raw_event_fingerprint(dict(event_100))
    with pytest.raises(GitHubEventIdentityError, match="not_numeric"):
        deduplicate_raw_events([{"id": "release-1"}])
    with pytest.raises(GitHubEventIdentityError, match="payload_conflict"):
        deduplicate_raw_events([event_100, {**event_100, "payload": {"action": "stopped"}}])


def test_legacy_boundary_is_permanent() -> None:
    old = normalize_event(
        "owner/repo",
        {
            "id": "300",
            "type": "WatchEvent",
            "actor": {"login": "alice", "type": "User"},
            "created_at": "2026-08-05T10:30:00Z",
            "payload": {"action": "started"},
        },
    )
    new = normalize_event(
        "owner/repo",
        {
            "id": "301",
            "type": "WatchEvent",
            "actor": {"login": "bob", "type": "User"},
            "created_at": "2026-08-05T10:31:00Z",
            "payload": {"action": "started"},
        },
    )
    assert old is not None and new is not None
    assert event_key_for_boundary(old, "300") == old.legacy_event_key
    assert event_key_for_boundary(new, "300") == new.event_key
