from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from github_monitor.aggregation import (
    BATCH_EVENT_TYPE,
    MAX_HOST_PAYLOAD_BYTES,
    build_aggregate_projection,
    plan_adjacent_members,
)
from github_monitor.models import (
    NormalizedGitHubEvent,
    QueuedSourceEvent,
    TargetPolicySnapshot,
    canonical_json,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)
TARGET = TargetPolicySnapshot(
    target_type="group",
    target_id="2001",
    send_text=True,
    send_card=True,
    ask_agent=True,
)


def _queued(
    event_id: int,
    *,
    event_type: str = "DeleteEvent",
    actor: str = "alice",
    ref_type: str = "branch",
    ref: str | None = None,
    targets: tuple[TargetPolicySnapshot, ...] = (TARGET,),
) -> QueuedSourceEvent:
    branch = ref or f"feature/{event_id}"
    event = NormalizedGitHubEvent(
        github_event_id=str(event_id),
        repository="owner/repo",
        event_type=event_type,
        actor=actor,
        created_at=NOW + timedelta(seconds=event_id),
        action="deleted" if event_type == "DeleteEvent" else "started",
        branch=branch,
        summary=f"owner/repo event {event_id}",
        payload={
            "repository": "owner/repo",
            "actor": actor,
            "event_type": event_type,
            "action": "deleted" if event_type == "DeleteEvent" else "started",
            "ref": branch,
            "ref_type": ref_type,
            "excerpt": "must-not-survive",
        },
        event_key=f"github:owner/repo:event:{event_id}",
        legacy_event_key=f"github:owner/repo:legacy:{event_id}",
    )
    return QueuedSourceEvent(
        github_event_id=str(event_id),
        source_fingerprint=f"{event_id:064x}",
        source_created_at=event.created_at,
        normalized=event,
        target_snapshot=targets,
    )


def test_delete_batch_is_adjacent_bounded_and_actor_specific() -> None:
    pending = (
        _queued(1),
        _queued(2),
        _queued(3, actor="bob"),
        _queued(4),
    )

    assert [
        item.github_event_id for item in plan_adjacent_members(pending, coalesce=True, limit=10)
    ] == ["1", "2"]
    assert [
        item.github_event_id for item in plan_adjacent_members(pending, coalesce=True, limit=1)
    ] == ["1"]


@pytest.mark.parametrize(
    ("middle", "expected"),
    [
        (_queued(2, ref_type="tag"), ["1"]),
        (_queued(2, event_type="ReleaseEvent"), ["1"]),
        (_queued(2, event_type="CreateEvent"), ["1"]),
        (
            _queued(
                2,
                targets=(TARGET.model_copy(update={"ask_agent": False}),),
            ),
            ["1"],
        ),
    ],
)
def test_incompatible_middle_event_is_a_hard_batch_boundary(
    middle: QueuedSourceEvent,
    expected: list[str],
) -> None:
    pending = (_queued(1), middle, _queued(3))
    assert [
        item.github_event_id for item in plan_adjacent_members(pending, coalesce=True, limit=10)
    ] == expected


@pytest.mark.parametrize("event_type", ["WatchEvent", "ForkEvent"])
def test_watch_and_fork_batch_without_actor_equality(event_type: str) -> None:
    pending = (
        _queued(1, event_type=event_type, actor="alice"),
        _queued(2, event_type=event_type, actor="bob"),
    )
    assert len(plan_adjacent_members(pending, coalesce=True, limit=10)) == 2


@pytest.mark.parametrize(
    "event_type",
    [
        "PushEvent",
        "ReleaseEvent",
        "PullRequestEvent",
        "IssueCommentEvent",
        "PullRequestReviewEvent",
    ],
)
def test_unsafe_event_types_are_always_singletons(event_type: str) -> None:
    pending = (_queued(1, event_type=event_type), _queued(2, event_type=event_type))
    assert len(plan_adjacent_members(pending, coalesce=True, limit=10)) == 1


def test_coalesce_kill_switch_only_selects_a_singleton() -> None:
    pending = (_queued(1), _queued(2))
    assert plan_adjacent_members(pending, coalesce=False, limit=10) == (pending[0],)


def test_projection_is_deterministic_body_free_and_keeps_all_source_ids() -> None:
    members = (_queued(1, ref="feature/a"), _queued(2, ref="feature/b"))

    first = build_aggregate_projection(members, legacy_boundary="1")
    second = build_aggregate_projection(members, legacy_boundary="1")

    assert first == second
    assert first.event_key.startswith("github:owner/repo:batch:v1:")
    assert first.payload["source_event_ids"] == ["1", "2"]
    without_legacy_boundary = build_aggregate_projection(members, legacy_boundary="")
    assert first.event_key != without_legacy_boundary.event_key
    encoded = json.dumps(first.payload, ensure_ascii=False)
    assert "must-not-survive" not in encoded
    assert "excerpt" not in encoded
    assert "feature/a" in first.text and "feature/b" in first.text
    assert BATCH_EVENT_TYPE == "github_event_batch"


def test_projection_rejects_a_non_adjacent_compatible_set() -> None:
    with pytest.raises(ValueError, match="not mutually compatible"):
        build_aggregate_projection(
            (_queued(1), _queued(2, actor="bob")),
            legacy_boundary="",
        )


def test_batch_key_is_bounded_for_maximum_configured_repository_name() -> None:
    repository = f"{'o' * 100}/{'r' * 100}"
    members = []
    for source in (_queued(1), _queued(2)):
        assert source.normalized is not None
        event = source.normalized.model_copy(
            update={
                "repository": repository,
                "event_key": f"github:{repository}:event:{source.github_event_id}",
            }
        )
        members.append(source.model_copy(update={"normalized": event}))

    projection = build_aggregate_projection(tuple(members), legacy_boundary="")

    assert len(projection.event_key) <= 255
    assert projection.event_key.startswith("github:batch:v1:")


def test_large_batch_text_is_bounded_but_payload_keeps_every_source_id() -> None:
    members = tuple(_queued(index, ref="x" * 255) for index in range(1, 201))

    projection = build_aggregate_projection(members, legacy_boundary="")

    assert len(projection.text) <= 12_000
    assert projection.payload["source_event_ids"] == [str(index) for index in range(1, 201)]
    assert "source event IDs 已完整保留" in projection.text


def test_maximum_batch_fits_host_payload_limit_with_128_digit_source_ids() -> None:
    members = []
    for index in range(200):
        event_id = str(10**127 + index)
        source = _queued(index + 1, event_type="WatchEvent")
        assert source.normalized is not None
        event = source.normalized.model_copy(
            update={
                "github_event_id": event_id,
                "event_key": f"github:owner/repo:event:{event_id}",
            }
        )
        members.append(
            source.model_copy(
                update={
                    "github_event_id": event_id,
                    "source_fingerprint": hashlib.sha256(event_id.encode()).hexdigest(),
                    "normalized": event,
                }
            )
        )

    projection = build_aggregate_projection(tuple(members), legacy_boundary="")
    payload_bytes = len(canonical_json(projection.payload).encode("utf-8"))

    assert payload_bytes <= MAX_HOST_PAYLOAD_BYTES
    assert len(projection.payload["source_event_ids"]) == 200
