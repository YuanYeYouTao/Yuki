"""A genuine old shared private entry resumes on its saved bot after migrations."""

import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.gateway import builtin_registry
from tests.support.runtime_execution import make_work_resumer
from tests.unit.test_migration_0119 import _seed_0118

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.runtime.work_journal import encode_transcript
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.execution_sources import recover_source
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.social.service import SocialContext, SocialService


class Bot:
    def __init__(self, account):
        self.self_id = account
        self.calls = []

    async def call_api(self, action, **params):
        self.calls.append((action, params))
        return {"message_id": f"reply-{self.self_id}-{len(self.calls)}"}


def test_migrated_old_b_work_finishes_without_merging_new_b_history(tmp_path, monkeypatch):
    path = tmp_path / "old-entries.db"
    url, conversations = _seed_0118(path, monkeypatch)
    command.upgrade(Config("alembic.ini"), "0121")
    conversation_id = conversations[0]
    second_presence = str(uuid4())
    now = "2026-10-14 00:00:00"
    with sqlite3.connect(path) as connection:
        person_id = connection.execute(
            "SELECT person_id FROM canonical_conversations WHERE id=?", (conversation_id,)
        ).fetchone()[0]
        original_presence = connection.execute(
            "SELECT id FROM presences WHERE external_account_id='test'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO presences VALUES(?,'qq','testB',1,1,1,?,?)", (second_presence, now, now)
        )
        connection.execute(
            "INSERT INTO conversation_legacy_aliases VALUES(?,?,?,0,?,?)",
            (str(uuid4()), conversation_id, "bot:testB:private:0", now, now),
        )
        connection.execute(
            """INSERT INTO chat_events(id,bot_user_id,platform_message_id,scope_type,
            private_peer_user_id,sender_user_id,direction,content,visual_summary,segments_json,origin,
            occurred_at,observed_at,canonical_event_id,canonical_conversation_id,author_kind,
            author_person_id,ingress_presence_id,suppression_status)
            VALUES(9,'testB','original-b','private','0','0','inbound',
            'old B request','','[]','user_message',
            ?,?,?,?,'person',?,?,'keeper')""",
            (now, now, str(uuid4()), conversation_id, person_id, second_presence),
        )
        # Existing A events make the migration choose A as the old Conversation entry.
        connection.execute(
            "UPDATE chat_events SET ingress_presence_id=? WHERE id=2", (original_presence,)
        )
        source = {
            "origin": "user_message",
            "trigger_event_id": 9,
            "generation": 1,
            "bot_user_id": "testB",
            "presence_id": second_presence,
            "actor_person_id": person_id,
            "conversation_key": "bot:testB:private:0",
        }
        connection.execute(
            "UPDATE runtime_work SET source_json=? WHERE conversation_id=?",
            (json.dumps(source), conversation_id),
        )
        work_id = connection.execute(
            "SELECT id FROM runtime_work WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0]
        budgets = connection.execute(
            "SELECT model_requests,tool_calls FROM runtime_work WHERE id=?", (work_id,)
        ).fetchone()
        transcript = TurnTranscript((ChatMessage("user", "old B request"),))
        payload = {
            "transcript": encode_transcript(transcript),
            "pending": [],
            "metadata": {
                "sequence": 7,
                "event_ids": [9],
                "source_keys": [],
                "input_ids": [],
                "progress": {},
            },
            "file_media": [],
        }
        revision = connection.execute(
            "SELECT prompt_source_revision FROM canonical_conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO runtime_work_journal(work_id,chain_id,contract,source_revision,"
            "phase,payload_json,updated) VALUES(?,?,?,?,?,?,1)",
            (
                work_id,
                transcript.chain_id,
                "original-pre-entry-contract",
                revision,
                "paired",
                json.dumps(payload),
            ),
        )

        # Original B Job/outbox also predate private-entry isolation. The grant
        # must follow those persisted children through 0124, not entry A.
        def insert_row(table, values):
            columns = ",".join(values)
            marks = ",".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO {table}({columns}) VALUES({marks})", tuple(values.values())
            )

        insert_row(
            "plugin_installations",
            dict(
                plugin_id="legacy.notification",
                name="Legacy",
                version="1.0",
                plugin_api="3.5",
                yuki_requires=">=3.0",
                manifest_hash="b" * 64,
                entrypoint="fixture:Plugin",
                status="running",
                enabled=1,
                failure_count=0,
                approved_permissions_json="[]",
                requested_permissions_json="[]",
                discovered_at=now,
                started_at=now,
                updated_at=now,
            ),
        )
        insert_row(
            "chat_events",
            dict(
                id=10,
                bot_user_id="testB",
                platform_message_id="external-original-B",
                scope_type="private",
                private_peer_user_id="0",
                sender_user_id="testB",
                direction="external",
                content="original plugin update",
                visual_summary="",
                segments_json="[]",
                origin="plugin_background",
                event_kind="external_event",
                source_plugin_id="legacy.notification",
                external_source="fixture",
                external_event_key="original-B-event",
                external_event_type="fixture.update",
                external_payload_json="{}",
                external_target_id="0",
                external_resume_wait=0,
                occurred_at=now,
                observed_at=now,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_id,
                author_kind="system",
                ingress_presence_id=second_presence,
                suppression_status="keeper",
            ),
        )
        insert_row(
            "plugin_background_target_grants",
            dict(
                plugin_id="legacy.notification",
                target_type="private",
                target_id="0",
                bot_user_id="testB",
                enabled=1,
                created_by_user_id="0",
                created_at=now,
                updated_at=now,
                canonical_target_person_id=person_id,
                canonical_created_by_person_id=person_id,
                canonical_presence_id=second_presence,
            ),
        )
        child = dict(
            source_event_id=10,
            plugin_id="legacy.notification",
            target_type="private",
            target_id="0",
            bot_user_id="testB",
            status="pending",
            attempts=0,
            max_attempts=3,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
            canonical_target_person_id=person_id,
            canonical_conversation_id=conversation_id,
            canonical_presence_id=second_presence,
        )
        insert_row(
            "plugin_background_turn_jobs",
            dict(
                child,
                id=1,
                agent_intent="original B purpose",
                generated_text="",
                tool_calls_used=0,
                model_requests=0,
            ),
        )
        insert_row(
            "plugin_notification_outbox",
            dict(
                child,
                id=1,
                notification_id="retained-notification",
                part_key="text",
                part_type="text",
                text="original queued effect",
            ),
        )

    command.upgrade(Config("alembic.ini"), "0124")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT entry_presence_id FROM canonical_conversations WHERE id=?",
            (conversation_id,),
        ).fetchone() == (original_presence,)
        assert connection.execute(
            "SELECT canonical_conversation_id,ingress_presence_id FROM chat_events WHERE id=9"
        ).fetchone() == (conversation_id, second_presence)
        assert connection.execute(
            "SELECT canonical_conversation_id,canonical_presence_id "
            "FROM plugin_background_target_grants WHERE plugin_id='legacy.notification'"
        ).fetchone() == (conversation_id, second_presence)
    # Exercise today's consumers against the real forward-upgraded schema, while
    # keeping the original private-entry migration assertions above.
    command.upgrade(Config("alembic.ini"), "head")

    async def check():
        database = Database(url)
        try:
            repository = WorkRepository(database)
            provider = FakeLLMProvider(
                lambda _: ChatResponse(
                    "",
                    0,
                    tool_calls=(
                        ToolCall(
                            "finish-old", ToolFunction("task_control", '{"action":"complete"}')
                        ),
                    ),
                )
            )
            settings = make_settings(url)
            harness = build_harness(database, settings, provider)
            chat = harness.processor._chat
            bind_main_contract(harness, tmp_path)
            registry = builtin_registry()
            router = PresenceRouter(database, registry)
            bot_a, bot_b = Bot("test"), Bot("testB")
            registry.connect(bot_a, provider_id="snowluma", presence_id=original_presence)
            registry.connect(bot_b, provider_id="snowluma", presence_id=second_presence)
            social = SocialService(database, router, harness.scoped_events)
            chat._tools.social_service = social
            original = await repository.get(work_id)
            recovered = await recover_source(database, conversation_id, source, request_id=work_id)
            assert recovered.presence_id == second_presence
            resumer = make_work_resumer(
                repository,
                ledger=harness.ledger,
                scopes=harness.conversation_scopes,
                turns=chat._turn_coordinator,
                router=router,
                config=chat._runtime_config,
                generate_self=chat.generate_self_initiative,
                generate_wakeup=chat.generate_main_agent_wakeup,
                bindings=chat.runtime.bindings,
                settings=settings,
            )
            assert await resumer.resume(original) is None
            completed = await repository.get(work_id)
            assert completed["state"] == "completed"
            assert completed["model_requests"] == budgets[0] + 1
            assert (
                completed["tool_calls"] == budgets[1]
            )  # typed control consumes no business effect budget
            assert json.loads(completed["source_json"]) == source
            resolved = await router.resolve_send_for_conversation(
                conversation_id, presence_id=second_presence, source_event_id=9
            )
            async with database.sessions() as session:
                await router.validate_prepared_send(
                    session, resolved, target_kind="person", target_id=person_id
                )
                conversation = await session.get(CanonicalConversationModel, conversation_id)
                assert conversation.entry_presence_id == original_presence
            inbound = recovered.inbound(await harness.ledger.get_event(9))
            sent = await social.execute(
                "send_message",
                {"text": "old original effect"},
                SocialContext(
                    "legacy-work-tail",
                    "send",
                    conversation_id,
                    person_refs={"current_speaker": person_id},
                    actor=ToolActor.from_inbound(inbound),
                    inbound=inbound,
                    trigger_event_id=9,
                    presence_id=second_presence,
                    reply_presence_id=second_presence,
                ),
            )
            assert sent["status"] == "succeeded", sent
            event = await harness.ledger.get_event(sent["event_id"])
            assert (
                event.canonical_conversation_id == conversation_id
                and event.ingress_presence_id == second_presence
            )
            assert len(bot_b.calls) == 1 and bot_a.calls == []
            from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
            from qq_ai_bot.plugin_host.db_models import (
                PluginBackgroundTargetGrantModel,
                PluginBackgroundTurnJobModel,
                PluginNotificationOutboxModel,
            )
            from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
            from qq_ai_bot.plugin_host.notification_delivery import (
                PlatformNotificationTransport,
                PluginNotificationOutboxWorker,
            )
            from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
            from qq_ai_bot.services.effect_gate import ConversationEffectGate

            notifications = PluginNotificationRepository(database)
            async with database.sessions() as reader:
                grant = await reader.scalar(select(PluginBackgroundTargetGrantModel))
                assert grant.canonical_conversation_id == conversation_id
                assert grant.canonical_presence_id == second_presence
            job = await notifications.claim_turn()
            assert job is not None and job.id == 1 and job.source_event_id == 10
            before_job = len(provider.requests)
            provider._responder = lambda _: ChatResponse("original job completed internally", 0)
            worker = PluginBackgroundTurnWorker(
                repository=notifications,
                ledger=harness.ledger,
                runtime_config=chat._runtime_config,
                chat=chat,
                turns=chat._turn_coordinator,
                conversation_scopes=chat._conversation_scopes,
            )
            await worker._execute(job)
            async with database.sessions() as reader:
                retained = await reader.get(PluginBackgroundTurnJobModel, 1)
                assert retained.status == "completed", retained.last_error_category
                assert (
                    retained.source_event_id == 10
                    and retained.canonical_presence_id == second_presence
                )
            assert len(provider.requests) == before_job + 1
            outbox = await notifications.claim_outbox()
            assert (
                outbox is not None
                and outbox.id == 1
                and outbox.notification_id == "retained-notification"
            )
            delivery = PluginNotificationOutboxWorker(
                repository=notifications,
                ledger=harness.ledger,
                artifacts=PluginMediaArtifactStore(database, tmp_path / "media"),
                transport=PlatformNotificationTransport(router=router),
                effect_gate=ConversationEffectGate(),
            )
            await delivery._deliver(outbox)
            async with database.sessions() as reader:
                queued_effect = await reader.get(PluginNotificationOutboxModel, 1)
                assert queued_effect.status == "sent", queued_effect.last_error_category
                assert (
                    queued_effect.notification_id == "retained-notification"
                    and queued_effect.source_event_id == 10
                )
            assert len(bot_b.calls) == 2 and bot_a.calls == []

            with pytest.raises(RouteSendError):
                await router.resolve_send_for_conversation(
                    conversation_id, presence_id=second_presence
                )
            fresh = await ScopedEventLedgerUnitOfWork(database).append(
                scope=ConversationScope.private("testB", "0"),
                platform_message_id="new-b",
                sender_user_id="0",
                direction="inbound",
                content="new B request",
            )
            assert fresh.event.canonical_conversation_id != conversation_id
            assert (await harness.ledger.get_event(9)).canonical_conversation_id == conversation_id
        finally:
            await database.close()

    asyncio.run(check())
