"""JEV receives the same original mention facts as Main, without routing authority."""

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.support.semantic_participation_host_helpers import (
    _event_and_route,
    _host,
    _item,
    _observation,
)
from yuki_participation.models import Snapshot

from yuki.conversation.initiative_sources import source_revision
from yuki.domain.messages import MENTION_SNAPSHOT_FIELDS
from yuki.identity.canonical_repository import ensure_person
from yuki.identity.person_identity import merge_persons
from yuki.services.participation_trace import TracedSemanticObserver


def segment(account, stage, name, start, end, *, person=None, presence=None):
    return {
        "type": "mention",
        "data": {
            "kind": "yuki" if presence else "member",
            "display_name": name,
            "account_id": account,
            "assignment_id": stage,
            "person_id": person,
            "presence_id": presence,
            "text_start": start,
            "text_end": end,
            "source": {"platform": "qq", "qq": "native-not-a-business-id"},
        },
    }


@pytest.mark.asyncio
async def test_host_passes_ordered_original_mentions_without_relabeling_target(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    original = await _event_and_route(database, host.app.ledger, content="前😀@Ice 后@Yuki @Ice")
    item = await _item(host, original)
    account, stage, person, own_account, own_stage = (str(uuid4()) for _ in range(5))
    segments = (
        segment(account, stage, "Ice", 2, 6, person=person),
        segment(own_account, own_stage, "Yuki", 8, 13, presence=own_stage),
        segment(account, stage, "Ice", 14, 18, person=person),
    )
    record = replace(original, segments=segments)
    try:
        event = host._event(record, item, actor_identity=lambda _: "current-family")
        assert event.target == original.author_person_id == event.author
        assert event.text == record.perceived_content
        assert [mention.account_id for mention in event.mentions] == [account, own_account, account]
        for actual, expected in zip(event.mentions, segments, strict=True):
            assert actual.model_dump() == {
                key: expected["data"][key] for key in MENTION_SNAPSHOT_FIELDS
            }
            assert event.text[actual.text_start : actual.text_end] == "@" + actual.display_name
        changed = replace(
            record,
            segments=(segment(str(uuid4()), stage, "Ice", 2, 6, person=person), *segments[1:]),
        )
        assert source_revision(changed) != source_revision(record)
        corrected = host._event(changed, item)
        assert corrected.ref.revision == event.ref.revision + 1
        assert corrected.target == event.target
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_plain_at_has_no_native_relation_and_account_author_is_not_human_or_self(
    database, tmp_path
):
    host, _ = await _host(database, tmp_path, observer=False)
    original = await _event_and_route(database, host.app.ledger, content="普通文字@Ice")
    item = await _item(host, original)
    try:
        event = host._event(original, item)
        assert event.mentions == () and event.text == original.content
        unknown = replace(
            original, author_kind="account", author_person_id=None, author_account_id=str(uuid4())
        )
        assert host._event(unknown, item) is None
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_mentioned_person_current_projection_uses_the_existing_root_batch(database, tmp_path):
    host, _ = await _host(database, tmp_path, observer=False)
    original = await _event_and_route(database, host.app.ledger, content="看看@Ice")
    item = await _item(host, original)
    async with database.sessions() as session, session.begin():
        old_person = await ensure_person(session, "1002")
        current_person = await ensure_person(session, "1003")
        await merge_persons(session, old_person, current_person)
    record = replace(
        original,
        segments=(segment(str(uuid4()), str(uuid4()), "Ice", 2, 6, person=old_person),),
    )
    try:
        event = host._event(record, item)
        snapshot = Snapshot(
            scope=item.scene.scope, focus=event, context=(), sequence=1, issued_at=time.time()
        )
        before = snapshot.model_dump_json()

        async def observe(actual, *, actor_identity):
            assert actor_identity(old_person) == current_person
            assert actual.focus.author == original.author_person_id
            return _observation(actual)

        observer = SimpleNamespace(evaluate=AsyncMock(side_effect=observe))
        result = await TracedSemanticObserver(observer, None, database=database).evaluate(snapshot)
        assert result.snapshot.model_dump_json() == before == snapshot.model_dump_json()
        assert result.snapshot.focus.mentions[0].person_id == old_person
        observer.evaluate.assert_awaited_once()
    finally:
        await host.close()
