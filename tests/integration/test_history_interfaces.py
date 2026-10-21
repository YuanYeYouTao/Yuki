"""Real interface grants and projections over the common history query core."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.domain.conversations import ConversationScope
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from yuki.runtime.origin import TurnOrigin
from yuki.services.history_query import HistoryQuery
from yuki.social.db_models import SocialOperationModel
from yuki_plugin_sdk.models import CurrentMessage
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.testing.fake_services import FakeMessageFacade


async def test_plugin_history_has_no_old_caps_and_projects_real_asr_and_ids(database, tmp_path):
    env = await social_env(database, tmp_path)
    scope = ConversationScope.group("80001", "20001")
    rows = []
    for index in range(101):
        rows.append(
            (
                await seed_scoped_event(
                    env.service.writer,
                    scope=scope,
                    platform_message_id=f"plugin-history-{index}",
                    sender_user_id="10001",
                    direction="inbound",
                    content=f"row {index}",
                )
            ).event
        )
    spoken = (
        await seed_scoped_event(
            env.service.writer,
            scope=scope,
            platform_message_id="only-asr",
            sender_user_id="10001",
            direction="inbound",
            content="",
            audio_transcript=serialize_transcripts(
                (
                    AudioTranscript("current", 0, "乾坤 電動車"),
                    AudioTranscript("reply", 1, "北京"),
                )
            ),
            segments=(
                {"type": "unknown", "data": {}},
                {"type": "video", "data": {"name": "clip.mp4"}},
            ),
        )
    ).event
    host = HostPluginContext(
        plugin_id="test.history",
        approved_permissions=(PluginPermission.MESSAGE_HISTORY_READ,),
        services=PluginFacadeServices(ledger=EventLedgerRepository(database)),
    )
    invocation = PluginInvocation(
        plugin_id=host.plugin_id,
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_person_id=env.person,
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        space_id=env.space,
        presence_id=env.presence,
        platform="qq",
    )
    statements = []

    def observe(_c, _cur, statement, _params, _context, _many):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        with host.bind(invocation):
            recent = await host.messages.get_recent(limit=101)
            assert len(recent) == 101 and recent[-1].event_id == spoken.id
            found = await host.messages.search_history("乾 电动车", exclude=("北京",))
            assert len(found) == 1 and found[0].event_id == spoken.id
            assert found[0].message_id == spoken.platform_message_id
            assert found[0].person_id == spoken.author_person_id
            assert found[0].author_presence_id == spoken.author_presence_id
            assert found[0].presence_id == spoken.ingress_presence_id
            assert "電動車" in found[0].text and found[0].asr[0]["text"] == "乾坤 電動車"
            assert found[0].attachments[0]["attachment_index"] == 1
            assert found[0].occurred_at and found[0].observed_at
            fake = FakeMessageFacade()
            fake.recent = list(found)
            assert not await fake.search_history("转写")
            assert [item.event_id for item in await fake.search_history("乾 电动车")] == [spoken.id]
            assert not await host.messages.search_history("北京")
            assert not await host.messages.search_history("absent" * 101)
            older = await host.messages.search_history("", cursor=spoken.id, limit=1)
            assert [item.event_id for item in older] == [rows[-1].id]
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
        await host.close_host_resources()
    assert not set(statements) & {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    async with database.immediate_session() as writer:
        conversation = await writer.get(CanonicalConversationModel, env.context.conversation_id)
        conversation.starts_after_event_id = spoken.id
    host = HostPluginContext(
        plugin_id="test.history",
        approved_permissions=(PluginPermission.MESSAGE_HISTORY_READ,),
        services=PluginFacadeServices(ledger=EventLedgerRepository(database)),
    )
    try:
        with host.bind(invocation):
            assert await host.messages.get_recent() == ()
    finally:
        await host.close_host_resources()


async def test_operation_anchor_is_original_and_only_receipt_anchor_crosses_floor(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    first = await env.service.execute("send_message", {"text": "original effect"}, env.context)
    second = await env.service.execute(
        "send_message", {"text": "another effect"}, replace(env.context, call_id="second")
    )
    async with database.immediate_session() as writer:
        conversation = await writer.get(CanonicalConversationModel, env.context.conversation_id)
        conversation.starts_after_event_id = second["event_id"]
    request = {"view": "recent", "target": {"operation_id": first["operation_id"]}}
    original = await env.service.execute("query_chat_history", request, env.context)
    assert original["events"] == []
    assert original["receipt_anchor"]["event"]["event_id"] == first["event_id"]
    denied = await env.service.execute(
        "query_chat_history",
        {**request, "view": "around", "event_id": second["event_id"]},
        env.context,
    )
    assert denied["events"] == [] and denied["center_status"] == "unavailable"
    assert original.source_event_ids == (first["event_id"],)


async def test_sdk_fake_preserves_literal_all_any_phrase_exclude_and_cursor():
    now = datetime.now(UTC)
    fake = FakeMessageFacade()

    def row(identity, text):
        return CurrentMessage(
            event_id=identity,
            message_id=str(identity),
            sender_user_id="1001",
            scope_type="private",
            text=text,
            received_at=now,
        )

    fake.recent = [row(1, "乾坤 電動車"), row(2, "电动车"), row(3, "京"), row(4, "ab cd")]
    assert [item.event_id for item in await fake.search_history("乾 电动车")] == [1]
    assert [item.event_id for item in await fake.search_history("电动车 京", match="any")] == [
        1,
        2,
        3,
    ]
    assert not await fake.search_history("ab cd", match="phrase", exclude=("cd",))
    assert [item.event_id for item in await fake.search_history("", cursor=3, limit=1)] == [2]
    assert len(await fake.get_recent(1000)) == 4
    fake.recent = [
        row(5, "ab 展示标签 cd").model_copy(
            update={
                "content": "ab",
                "asr": ({"source": "current", "segment_index": 0, "text": "cd"},),
            }
        )
    ]
    assert not await fake.search_history("展示标签")
    assert not await fake.search_history("ab cd", match="phrase")


def test_query_selector_is_mutually_exclusive_and_dates_are_utc_eight():
    from uuid import uuid4

    with pytest.raises(ValueError):
        HistoryQuery.model_validate(
            {"target": {"person_id": str(uuid4()), "space_id": str(uuid4())}}
        )
    query = HistoryQuery.model_validate(
        {
            "view": "search",
            "start_at": "2026-10-22T00:00:00",
            "end_at": "2026-10-22T00:00:00Z",
            "limit": 1001,
        }
    )
    assert query.start_at == datetime(2026, 10, 21, 16, tzinfo=UTC)
    assert query.end_at == datetime(2026, 10, 22, tzinfo=UTC)
    assert query.limit == 1001


async def test_explicit_event_kind_preserves_external_source_without_payload_search(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    added = await env.service.writer.append_external(
        scope=ConversationScope.group(
            "80001", "20001", conversation_id=env.context.conversation_id, presence_id=env.presence
        ),
        platform_message_id="external-history",
        source_plugin_id="test.history",
        external_source="subscription",
        external_event_key="one",
        external_event_type="item.created",
        external_payload={"never-index-json-key": "private-payload"},
        external_target_id="20001",
        content="published original",
        occurred_at=datetime.now(UTC),
    )
    page = await env.service.execute(
        "query_chat_history",
        {"view": "search", "event_kind": "external_event", "keyword": "original"},
        env.context,
    )
    assert [item["id"] for item in page["events"]] == [added.event.id]
    assert page["events"][0]["external_source"] == "subscription"
    assert page["events"][0]["external_event_type"] == "item.created"
    assert "external_payload" not in page["events"][0]
    messages = await env.service.execute("query_chat_history", {"view": "recent"}, env.context)
    assert added.event.id not in {item["id"] for item in messages["events"]}
    unmatched = await env.service.execute(
        "query_chat_history",
        {"view": "search", "event_kind": "all", "keyword": "never-index-json-key"},
        env.context,
    )
    assert not unmatched["events"]


async def test_deleted_receipt_anchor_returns_original_identity_and_allows_only_normal_new_anchor(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send_message", {"text": "old confirmed body"}, env.context)
    async with database.immediate_session() as writer:
        original = await writer.get(ChatEventModel, sent["event_id"])
        original_conversation = original.canonical_conversation_id
        await writer.delete(original)
    async with database.sessions() as reader:
        receipt = await reader.get(SocialOperationModel, sent["operation_id"])
        assert receipt.event_id is None
        assert receipt.original_event_id == sent["event_id"]
        assert receipt.original_conversation_id == original_conversation
    target = {"operation_id": sent["operation_id"]}
    missing = await env.service.execute(
        "query_chat_history", {"view": "recent", "target": target}, env.context
    )
    assert missing["event_id"] == sent["event_id"]
    assert missing["anchor_status"] == "missing" and sent["event_id"] not in {
        row["id"] for row in missing["events"]
    }
    assert missing["receipt_status"] == "succeeded"
    newer = (
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="current-approved",
            sender_user_id="10001",
            direction="inbound",
            content="current approved fact",
        )
    ).event
    visible = await env.service.execute(
        "query_chat_history",
        {"view": "around", "target": target, "event_id": newer.id},
        env.context,
    )
    assert visible["center_event_id"] == newer.id
    assert visible["event_id"] == sent["event_id"] and visible["anchor_status"] == "missing"
    assert [row["content"] for row in visible["events"]] == ["current approved fact"]
    assert visible.source_event_ids == (newer.id,)


async def test_operation_search_and_paging_keep_query_semantics_separate_from_receipt_anchor(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    first = await env.service.execute("send_message", {"text": "original anchor"}, env.context)
    second = await env.service.execute(
        "send_message", {"text": "new current"}, replace(env.context, call_id="other")
    )
    target = {"operation_id": first["operation_id"]}
    empty = await env.service.execute(
        "query_chat_history",
        {"view": "search", "target": target, "keyword": "not-present"},
        env.context,
    )
    assert not empty["events"]
    assert empty["receipt_anchor"]["event"]["content"] == "original anchor"
    newest = await env.service.execute(
        "query_chat_history", {"view": "recent", "target": target, "limit": 1}, env.context
    )
    assert [row["id"] for row in newest["events"]] == [second["event_id"]]
    assert newest["has_more"] and newest["receipt_anchor"]["event"]["id"] == first["event_id"]
    next_page = await env.service.execute(
        "query_chat_history",
        {"view": "recent", "target": target, "limit": 1, "cursor": newest["next_cursor"]},
        env.context,
    )
    assert [row["id"] for row in next_page["events"]] == [first["event_id"]]
    assert "event" not in next_page["receipt_anchor"]
    assert next_page.source_event_ids == (first["event_id"],)
