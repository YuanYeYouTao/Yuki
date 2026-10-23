"""Frozen 0109 facts upgrade without importing a retired runtime or account ORM."""

import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config


def insert_row(connection, table, values):
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(values.values())
    )


def rows(connection, table):
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]


def seed_conversation(connection, person, presence, space, now, *, private):
    conversation, alias = str(uuid4()), str(uuid4())
    insert_row(
        connection,
        "canonical_conversations",
        {
            "id": conversation,
            "kind": "private" if private else "space",
            "person_id": person if private else None,
            "space_id": None if private else space,
            "primary_alias_id": alias,
            "primary_marker": 1,
            "generation": 1,
            "starts_after_event_id": 0,
            "last_event_id": 0,
            "last_generation_change_event_id": 0,
            "covered_through_event_id": 0,
            "uncovered_event_count": 0,
            "uncovered_character_count": 0,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        },
    )
    insert_row(
        connection,
        "conversation_legacy_aliases",
        {
            "id": alias,
            "conversation_id": conversation,
            "scope_key": "bot:80001:private:10001" if private else "bot:80001:group:20001",
            "is_primary": 1,
            "created_at": now,
            "updated_at": now,
        },
    )
    return conversation


@pytest.mark.parametrize(
    "read_scope", ["current_group", "creator_private", "creator_private_pause"]
)
def test_legacy_publication_keeps_hash_receipts_and_archives_original_main_work(
    tmp_path, monkeypatch, read_scope
):
    path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = Config("alembic.ini")
    command.upgrade(config, "0109")
    now = "2026-10-14 00:00:00.000000"
    person, presence, space = (str(uuid4()) for _ in range(3))
    reminder = "完整旧提醒：" + "文字资料" * 1800 + "ORIGINAL_REMINDER_TAIL"
    goal = "原始高层目标" + "完整要求" * 1500
    instruction = json.dumps(
        {"goal": goal, "constraints": ["完整约束尾部"], "delivery": "none"}, ensure_ascii=False
    )
    json_instruction = json.dumps(
        {
            "goal": "完整 JSON 指令",
            "constraints": ["保留正文中的全部字段"],
            "delivery": {"target": "none"},
            "additional_requirement": "必须保留的附加要求",
        },
        ensure_ascii=False,
    )
    steps = (
        {
            "id": "original_agent",
            "call": "yuki.agent",
            "arguments": {
                "instruction": instruction,
                "delivery_target": "none",
                "context_profile": "creator_private",
            },
        },
        {"id": "literal_reminder", "call": "social.send_message", "arguments": {"text": reminder}},
        {
            "id": "unknown_plugin",
            "call": "plugin.original_action",
            "arguments": {"text": "${prior.output}"},
        },
        {
            "id": "literal_json_instruction",
            "call": "yuki.agent",
            "arguments": {"instruction": json_instruction, "delivery_target": "none"},
        },
        {
            "id": "opaque_template",
            "call": "yuki.agent",
            "arguments": {"instruction": "${missing.output}"},
        },
        {
            "id": "send_with_extra_effect",
            "call": "social.send_message",
            "arguments": {"text": "提醒", "mentions": ["current_speaker"]},
        },
    )
    scripts = []
    work_id = str(uuid4())
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT INTO persons VALUES(?,1,1,?,?)", (person, now, now))
        connection.execute(
            "INSERT INTO presences VALUES(?,'qq','80001',1,1,1,?,?)", (presence, now, now)
        )
        connection.execute("INSERT INTO spaces VALUES(?,'群',1,0,1,1,?,?)", (space, now, now))
        for table, values in (
            (
                "identity_bindings",
                {"id": str(uuid4()), "person_id": person, "external_account_id": "10001"},
            ),
            (
                "space_bindings",
                {"id": str(uuid4()), "space_id": space, "external_space_id": "20001"},
            ),
        ):
            insert_row(
                connection,
                table,
                {
                    **values,
                    "platform": "qq",
                    "display_name": "原名",
                    "status": "active",
                    "revision": 1,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        conversation = seed_conversation(connection, person, presence, space, now, private=False)
        private = seed_conversation(connection, person, presence, space, now, private=True)
        for index, step in enumerate(steps, 1):
            script = {
                "version": 1,
                "name": f"original-{index}",
                "timezone": "UTC",
                "schedule": {"type": "daily", "hour": 9, "minute": 0, "timezone": None}
                if index == 2
                else {"type": "interval", "seconds": 60},
                "context": {"scene": "creator_private", "history_limit": 1},
                "steps": [step],
                "limits": {"max_steps": 1, "max_llm_calls": 2, "timeout_seconds": 600},
            }
            raw = json.dumps(script, ensure_ascii=False)
            scripts.append(raw)
            authority = json.dumps(
                {
                    "creator_user_id": "10001",
                    "bot_user_id": "80001",
                    "canonical_creator_person_id": person,
                    "canonical_conversation_id": conversation,
                    "canonical_presence_id": presence,
                    "canonical_space_id": space,
                    "granted_capabilities": [step["call"]],
                    "capability_schema_versions": {step["call"]: 1},
                }
            )
            insert_row(
                connection,
                "automations",
                {
                    "id": index,
                    "creator_user_id": "10001",
                    "bot_user_id": "80001",
                    "name": script["name"],
                    "status": "active",
                    "timezone": "UTC",
                    "schedule_json": json.dumps(script["schedule"]),
                    "script_json": raw,
                    "script_hash": f"original-frozen-hash-{index}",
                    "required_capabilities_json": json.dumps([step["call"]]),
                    "authority_snapshot_json": authority,
                    "created_from_message_id": "old-native-source",
                    "creation_source_key": f"original:{index}",
                    "next_run_at": now,
                    "run_count": 1 if index == 1 else 0,
                    "consecutive_failures": 0,
                    "created_at": now,
                    "updated_at": now,
                    "canonical_creator_person_id": person,
                    "canonical_target_space_id": space,
                    "canonical_presence_id": presence,
                },
            )
            insert_row(
                connection,
                "automation_versions",
                {
                    "id": index,
                    "automation_id": index,
                    "version": 1,
                    "script_json": raw,
                    "script_hash": f"original-frozen-hash-{index}",
                    "authority_snapshot_json": authority,
                    "canonical_target_space_id": space,
                    "canonical_presence_id": presence,
                    "updated_by": "10001",
                    "created_at": now,
                },
            )
        insert_row(
            connection,
            "automation_runs",
            {
                "id": 1,
                "automation_id": 1,
                "version_id": 1,
                "scheduled_for": now,
                "actual_started_at": now,
                "status": "running",
                "idempotency_key": "original-run",
                "llm_calls": 2,
                "tool_calls": 1,
                "messages_sent": 1,
                "created_at": now,
            },
        )
        source = {
            "owner": "automation",
            "origin": "scheduled_automation",
            "automation_id": 1,
            "automation_run_id": 1,
            "step_id": "original_agent",
            "principal_kind": "person",
            "actor_person_id": person,
            "actor_user_id": "10001",
            "presence_id": presence,
            "space_id": space,
            "bot_user_id": "80001",
            "conversation_id": conversation,
            "generation": 1,
            "instruction": instruction,
            "context_profile": "creator_private",
            "delivery_target": "none",
        }
        insert_row(
            connection,
            "runtime_work",
            {
                "id": work_id,
                "conversation_id": conversation,
                "generation": 1,
                "source_key": "original-paid-work",
                "source_json": json.dumps(source),
                "goal": instruction,
                "state": "queued" if read_scope.endswith("pause") else "waiting_external",
                "model_requests": 2,
                "tool_calls": 1,
                "active_seconds": 42,
                "sent_messages": 1,
                "checkpoint_json": "{}",
                "created": 1.0,
                "updated": 2.0,
            },
        )
        payload = json.dumps(
            {
                "transcript": {
                    "paid_raw": "ORIGINAL_PRIVATE_READSET_ONLY",
                    "opaque": "original-paid-server-tool",
                },
                "metadata": {
                    "source_guard": {
                        "version": {
                            "conversation_id": private
                            if read_scope != "current_group"
                            else conversation
                        }
                    },
                    "progress": {"provider_pause_replay": read_scope.endswith("pause")},
                },
                "pending": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        insert_row(
            connection,
            "runtime_work_journal",
            {
                "work_id": work_id,
                "chain_id": str(uuid4()),
                "contract": "retired-original-contract",
                "source_revision": 0,
                "phase": "paired",
                "payload_json": payload,
                "updated": 2.0,
            },
        )
        insert_row(
            connection,
            "runtime_work_effects",
            {
                "effect_key": "original-effect",
                "work_id": work_id,
                "kind": "tool",
                "state": "accepted",
                "receipt_json": '{"outcome":"success","message_id":"original-ack"}',
                "created": 1.0,
                "updated": 2.0,
            },
        )
        insert_row(
            connection,
            "runtime_work_budgets",
            {"root_id": work_id, "models": 2, "tools": 1, "model_limit": 2},
        )
        insert_row(
            connection,
            "runtime_automation_cursors",
            {
                "run_id": 1,
                "script_hash": "original-frozen-hash-1",
                "phase": "agent",
                "payload_json": json.dumps({"work_id": work_id, "llm_calls": 2}),
                "updated": 2.0,
            },
        )
        originals = {
            name: rows(connection, name)
            for name in (
                "automation_versions",
                "automation_runs",
                "runtime_work",
                "runtime_work_journal",
                "runtime_work_effects",
                "runtime_work_budgets",
                "runtime_automation_cursors",
            )
        }

    command.upgrade(config, "0110")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        current = rows(connection, "automations")
        assert json.loads(current[0]["script_json"])["goal"] == goal
        assert json.loads(current[0]["script_json"])["constraints"] == ["完整约束尾部"]
        assert (
            json.loads(current[1]["script_json"])["goal"]
            == "在安排到期时发送以下提醒原文：\n" + reminder
        )
        assert json.loads(current[3]["script_json"])["goal"] == json_instruction
        for index in (0, 1, 3):
            task = json.loads(current[index]["script_json"])
            assert (
                current[index]["script_hash"]
                == hashlib.sha256(
                    json.dumps(
                        task, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                ).hexdigest()
            )
            authority = json.loads(current[index]["authority_snapshot_json"])
            assert "granted_capabilities" not in authority
            assert authority["canonical_creator_person_id"] == person
        for index in (2, 4, 5):
            assert current[index]["status"] == "blocked" and current[index]["next_run_at"] is None
            assert current[index]["script_json"] == scripts[index]
        assert rows(connection, "automation_versions")[:6] == originals["automation_versions"]
        assert len(rows(connection, "automation_versions")) == 9
        for table, original in originals.items():
            if table != "automation_versions":
                assert rows(connection, table) == original
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    # Later upgrades keep original Work/source/budget/paid evidence. The retired
    # private payload is an archive; current-chain execution has its own tests.
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        for table in (
            "automation_runs",
            "runtime_work",
            "runtime_work_effects",
            "runtime_work_budgets",
            "runtime_automation_cursors",
        ):
            assert rows(connection, table) == originals[table]
        saved = rows(connection, "runtime_work_journal")[0]
        assert (
            json.loads(saved.pop("archived_journal_json")) == originals["runtime_work_journal"][0]
        )
        assert saved == originals["runtime_work_journal"][0]
        assert rows(connection, "automation_versions")[:6] == originals["automation_versions"]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
