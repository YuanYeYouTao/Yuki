"""Run with the archived 755/820 PYTHONPATH to create lawful legacy facts.

No current ORM, schema shortcut or synthetic dispatch metadata is used. All
domain records go through that historical version's actual repositories.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, update

from qq_ai_bot.automation.authority import DelegatedAuthority, PermissionLevel
from qq_ai_bot.automation.models import AutomationScript
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.validator import AutomationValidator, CreationProvenance
from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
from qq_ai_bot.identity.write_settings import configure_identity_write_settings
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.runtime.work_budget import budgets
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.social.models import OperationStatus, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository


async def seed(url, output):
    configure_identity_write_settings(Settings(database_url=url))
    database = Database(url)
    try:
        async with database.immediate_session() as writer:
            person = await ensure_person(writer, "10001")
            presence = await ensure_presence(writer, "80001")
            space = await ensure_space(writer, "20001")
        ledger = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
        await ledger.append(
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id="migration-fixture",
            sender_user_id="10001",
            direction="inbound",
            content="synthetic legacy task",
        )
        async with database.sessions() as reader:
            event = await reader.scalar(select(ChatEventModel))
            conversation, event_id = event.canonical_conversation_id, event.id
        repo = WorkRepository(database)
        lease = await repo.acquire(conversation, 1)

        async def validate():
            assert await repo.valid(lease)

        source = {"trigger_event_id": event_id}
        control = WorkControl(repo, lease, "legacy-fixture", source, validate)
        control.current = await repo.accept(
            lease, source_key="legacy-fixture", source=source, goal="preserve this original task"
        )
        owner = WorkSession(control, "historical-contract")
        await owner.restore(TurnTranscript((ChatMessage("user", "original task"),)))
        await repo.checkpoint(lease, control.current["id"], None, models=2, tools=1)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(budgets)
                .where(budgets.c.root_id == control.current["id"])
                .values(model_limit=7, tool_limit=9)
            )
        await owner.save("paired")
        social = SocialOperationRepository(database)
        receipt = await social.prepare(
            source_turn_id="legacy-send",
            tool_call_id="original-call",
            source_conversation_id=conversation,
            action="send_message",
            target=SocialTarget(kind="space", id=space),
            payload={"text": "original payload"},
        )
        assert await social.claim(receipt.operation_id, presence_id=presence)
        async with database.immediate_session() as writer:
            await social.finish(
                receipt.operation_id,
                status=OperationStatus.UNCERTAIN,
                error_category="lost_response",
                session=writer,
            )
        manager = SandboxTaskRepository(database)
        run = str(uuid4())
        await manager.prepare(
            "original-manager-request",
            {"command": "synthetic command"},
            {
                "conversation_id": conversation,
                "origin": "user_message",
                "actor_user_id": "10001",
                "trigger_event_id": event_id,
            },
        )
        await manager.bind_run("original-manager-request", run)
        await manager.receive(
            {
                "request_id": "original-manager-request",
                "run_id": run,
                "result": {"run_id": run, "status": "succeeded", "pending": False},
            }
        )

        async def original_result():
            row = await manager.by_run(run)
            return row.completion_json

        call = ToolCall(
            "original-manager-call",
            ToolFunction("terminal_exec", '{"command":"synthetic command"}'),
        )
        owner.transcript.append(ChatMessage("assistant", None, tool_calls=(call,)))
        await owner.save("response", (call,))
        result = await owner.execute(call, original_result)
        owner.transcript.append_result(call.id, result)
        await owner.save("paired")
        plugins = PluginInstallationRepository(database)
        await plugins.upsert_discovered(
            plugin_id="migration.fixture",
            name="migration fixture",
            version="1.0.0",
            plugin_api="3.0",
            yuki_requires=">=3.0",
            manifest_hash="a" * 64,
            entrypoint="unused:Plugin",
            requested_permissions=("social.send",),
        )
        await plugins.approve("migration.fixture")
        await plugins.set_enabled("migration.fixture", enabled=True)
        now = datetime(2026, 10, 1, tzinfo=UTC)
        script = AutomationScript.model_validate(
            {
                "version": 1,
                "name": "original reminder",
                "schedule": {"type": "after", "seconds": 120},
                "steps": [
                    {
                        "id": "remind",
                        "call": "social.send_message",
                        "arguments": {
                            "target": {"kind": "person", "subject_ref": "current_speaker"},
                            "text": "literal reminder",
                        },
                    }
                ],
            }
        )
        validated = AutomationValidator(
            settings=Settings(database_url=url), registry=build_capability_registry()
        ).validate(
            script,
            CreationProvenance(
                creator_user_id="10001",
                bot_user_id="80001",
                message_id="migration-fixture",
                original_text="remind me",
                current_group_id="20001",
                mentioned_user_ids=(),
                permission=PermissionLevel.USER,
            ),
            now_utc=now,
        )
        authority = DelegatedAuthority(
            creator_user_id="10001",
            bot_user_id="80001",
            created_from_message_id="migration-fixture",
            created_at=now.isoformat(),
            permission_level=PermissionLevel.USER,
            granted_capabilities=validated.required_capabilities,
            capability_schema_versions={"social.send_message": 1},
        )
        automation = AutomationRepository(database)
        original = await automation.create(
            validated,
            authority,
            creator_person_id=person,
            max_runs=2,
            misfire_grace_seconds=300,
            now=now,
        )
        original_run = await automation.create_run(
            original.id,
            scheduled_for=validated.next_run_at,
            actual_started_at=validated.next_run_at,
        )
        output.write_text(
            json.dumps(
                {
                    "work_id": control.current["id"],
                    "conversation_id": conversation,
                    "social_id": receipt.operation_id,
                    "manager_run": run,
                    "automation_id": original.id,
                    "automation_run": original_run.id,
                }
            )
        )
        await repo.release(lease)
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(seed(sys.argv[1], Path(sys.argv[2])))
