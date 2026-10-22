"""QQ discovers and reads a same-named TG group, then replies in its original QQ entry."""

from dataclasses import replace
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.domain.conversations import ConversationScope
from yuki.domain.tool_actor import ToolActor
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.identity.db_models import CanonicalSpaceModel, SpaceBindingModel
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.social.db_models import SocialOperationModel


async def test_qq_discovers_telegram_group_reads_all_authors_and_replies_to_original_entry(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    wire = []

    def telegram(request):
        wire.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as transport:
        client = TelegramClient("offline-test-token", client=transport)
        client.self_id = "88001"
        now = datetime.now(UTC)
        async with database.immediate_session() as session:
            (await session.get(CanonicalSpaceModel, env.space)).name = "雷达讨论"
            qq_binding = await session.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.space_id == env.space)
            )
            qq_binding.display_name = "雷达讨论"
            presence = await create_presence(
                session, external_account_id=client.self_id, platform="telegram", now=now
            )
            group = await create_space_binding(
                session,
                external_space_id="-10088001",
                name="雷达讨论",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                platform="telegram",
                now=now,
            )
            people = [
                await create_person_binding(
                    session,
                    external_account_id=f"9100{index}",
                    display_name=f"TG群友{index}",
                    platform="telegram",
                    now=now,
                )
                for index in range(2)
            ]
        env.registry.connect(client, provider_id="telegram", presence_id=presence.id)
        tg_scope = ConversationScope.group(
            client.self_id,
            group.external_space_id,
            platform="telegram",
            presence_id=presence.id,
            thread_id="17",
        )
        originals = [
            (
                await seed_scoped_event(
                    env.service.writer,
                    scope=tg_scope,
                    platform_message_id=f"tg-radar-{index}",
                    sender_user_id=person.external_account_id,
                    direction="inbound",
                    content=f"雷达站方案{index}",
                )
            ).event
            for index, person in enumerate(people)
        ]
        incoming = await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="qq-radar-decoy",
            sender_user_id="10001",
            direction="inbound",
            content="雷达站在QQ说过的另一件事",
        )
        context = replace(
            env.context,
            trigger_event_id=incoming.event.id,
            actor=ToolActor(
                user_id="10001",
                bot_user_id="80001",
                group_id="20001",
                origin=TurnOrigin.USER_MESSAGE,
                instruction="查看TG同名群的雷达站记录",
                event_id=incoming.event.id,
                person_id=env.person,
                conversation_id=env.context.conversation_id,
                presence_id=env.presence,
                space_id=env.space,
            ),
        )
        env.bot.calls.clear()
        directory = await env.service.execute(
            "find_contacts",
            {
                "view": "entries",
                "source": {"platform": "telegram", "scope": "group"},
                "display_name": "雷达讨论",
            },
            replace(context, call_id="discover-tg"),
        )
        assert len(directory["items"]) == 1
        entry = directory["items"][0]
        assert entry["conversation_id"] == originals[0].canonical_conversation_id
        assert entry["conversation_id"] != env.context.conversation_id
        history = await env.service.execute(
            "query_chat_history",
            {
                "view": "search",
                "target": {"conversation_id": entry["conversation_id"]},
                "source": {"platform": "telegram", "scope": "group"},
                "keyword": "雷达站",
            },
            replace(context, call_id="read-tg"),
        )
        assert {row["event_id"] for row in history["events"]} == {row.id for row in originals}
        assert {row["person_id"] for row in history["events"]} == {
            person.person_id for person in people
        }
        assert set(history.source_event_ids) == {row.id for row in originals}
        assert all(
            row["platform"] == "telegram" and row["scope"] == "group" and row["thread_id"] == "17"
            for row in history["events"]
        )
        assert history["sources"][0]["conversation_name"] == "雷达讨论"
        assert env.bot.calls == [] and wire == []
        text = "TG记录：" + "；".join(row["content"] for row in history["events"])
        receipt = await env.service.execute(
            "send", {"text": text}, replace(context, call_id="reply-in-qq")
        )
        assert receipt["status"] == "succeeded"
        assert env.bot.calls[-1][0] == "send_group_msg"
        assert str(env.bot.calls[-1][1]["group_id"]) == "20001"
        assert wire == []
        async with database.read_session() as session:
            operation = await session.get(SocialOperationModel, receipt["operation_id"])
            event = await session.get(ChatEventModel, receipt["event_id"])
            assert operation.status == "succeeded"
            assert operation.source_conversation_id == env.context.conversation_id
            assert operation.original_conversation_id == env.context.conversation_id
            assert event.canonical_conversation_id == env.context.conversation_id
            assert event.author_presence_id == env.presence and event.content == text
