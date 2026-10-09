"""Canonical relationship management uses original scores, audits and CAS receipts."""

import dataclasses
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.support.control_plane_foundation_helpers import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.domain.identity import PersonId, RequestId
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import PersonRelationshipModel, RelationshipEventModel


@pytest.fixture
async def relationship_scene(database):
    people = [PersonId.new(), PersonId.new()]
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        for person in people:
            session.add(
                CanonicalPersonModel(
                    id=person.text, revision=1, enabled=True, created_at=now, updated_at=now
                )
            )
        await session.flush()
        for person in people:
            session.add(
                PersonRelationshipModel(
                    canonical_person_id=person.text,
                    affection_score=50,
                    trust_score=55,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
        for index in range(35):
            session.add(
                RelationshipEventModel(
                    canonical_person_id=people[0 if index < 34 else 1].text,
                    actor_user_id="private-platform-actor",
                    change_type="manual",
                    affection_before=50,
                    affection_delta=0,
                    affection_after=50,
                    trust_before=55,
                    trust_delta=0,
                    trust_after=55,
                    reason_code="manual_set_trust",
                    created_at=now,
                )
            )
    return people


async def test_relationships_pages_and_history_stay_with_original_person(
    database, relationship_scene
):
    first_person, other = relationship_scene
    q = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.relationship.read")
    first = await q.list_relationships(ctx, PageRequest(limit=1))
    second = await q.list_relationships(ctx, PageRequest(limit=1, cursor=first.next_cursor))
    assert {row.resource_id for row in (*first.items, *second.items)} == {
        first_person.text,
        other.text,
    }
    assert second.next_cursor is None
    detail = await q.read_relationship(ctx, first_person)
    assert detail.fields["affection_score"] == 50 and detail.fields["stage"] == "friendly"
    assert "private-platform-actor" not in repr(detail)
    history = await q.list_relationship_history(
        ctx, PageRequest(limit=30), person_id=first_person, section="events"
    )
    tail = await q.list_relationship_history(
        ctx,
        PageRequest(limit=30, cursor=history.next_cursor),
        person_id=first_person,
        section="events",
    )
    assert len(history.items) == 30 and len(tail.items) == 4 and tail.next_cursor is None
    assert "actor_user_id" not in repr(history)
    with pytest.raises(ControlQueryError):
        await q.list_relationship_history(
            ctx, PageRequest(cursor=history.next_cursor), person_id=other, section="events"
        )
    with pytest.raises(ControlQueryError):
        await q.list_relationship_history(
            ctx, PageRequest(cursor=history.next_cursor), person_id=first_person, section="jobs"
        )
    with pytest.raises(ControlQueryError) as exc:
        await q.read_relationship(context(), first_person)
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED


async def test_manual_canonical_change_without_platform_binding_replays_original_receipt(
    database, relationship_scene
):
    person, _ = relationship_scene
    q = ControlQueryService(ControlQueryAdapter(database))
    c = ControlCommandService(ControlCommandAdapter(database))
    ctx = context("control.relationship.read", "control.relationship.mutate")
    before = await q.read_relationship(ctx, person)
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=before.fields["revision"],
        payload={"action": "set_affection", "resource_id": person.text, "spec": {"value": 87}},
    )
    saved = await c.mutate_relationship(ctx, command)
    assert await c.mutate_relationship(ctx, command) == saved
    after = await q.read_relationship(ctx, person)
    assert after.fields["affection_score"] == 87 and after.fields["trust_score"] == 55
    assert after.fields["stage"] == "affectionate" and after.fields["revision"] == saved.revision
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RelationshipEventModel)) == 36
        audit = await session.scalar(
            select(RelationshipEventModel).order_by(RelationshipEventModel.id.desc()).limit(1)
        )
        assert audit.affection_delta == 37 and audit.source_event_id is None
        assert (
            audit.canonical_person_id == person.text and audit.reason_code == "manual_set_affection"
        )
    stale = dataclasses.replace(ctx, request_id=RequestId.new())
    with pytest.raises(ControlCommandError) as exc:
        await c.mutate_relationship(
            stale, dataclasses.replace(command, request_id=stale.request_id)
        )
    assert exc.value.problem.code is ProblemCode.VERSION_CONFLICT
    with pytest.raises(ControlCommandError) as exc:
        await c.mutate_relationship(context(), command)
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED


@pytest.mark.parametrize(
    "action,value",
    [("set_affection", True), ("set_trust", 101), ("adjust_affection", -21), ("arbitrary", 2)],
)
async def test_relationship_invalid_changes_leave_scores_and_audits(
    database, relationship_scene, action, value
):
    person, _ = relationship_scene
    q = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.relationship.read", "control.relationship.mutate")
    before = await q.read_relationship(ctx, person)
    with pytest.raises(ControlCommandError) as exc:
        await ControlCommandService(ControlCommandAdapter(database)).mutate_relationship(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=before.fields["revision"],
                payload={"action": action, "resource_id": person.text, "spec": {"value": value}},
            ),
        )
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
    assert await q.read_relationship(ctx, person) == before
