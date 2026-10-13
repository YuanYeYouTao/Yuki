"""Public material retirement preserves real expressions and private sources."""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from tests.support.projection_sql_counts import capture_sql
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.observation_schema import PROJECTION_TRIGGERS_0089
from qq_ai_bot.conversation.observations import (
    ContextObservationRepository,
    encode,
    validate_observations,
)
from qq_ai_bot.conversation.projection_current_schema import PROJECTION_TRIGGERS_0109
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    RuntimeConfigOverrideModel,
    ToolArtifactModel,
)
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

HEADER = "本轮运行资料（按 trust 字段区分可信度）："
VIEW = "a" * 64


def note(text_value, refs=()):
    return {
        "version": 1,
        "facts": [{"text": text_value, "refs": list(refs)}],
        "unresolved": [],
        "next_steps": [],
    }


def source(env, identity, key, payload, parents=()):
    return ContextObservationModel(
        id=identity,
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
        source_key=key,
        version=1,
        privacy_generation=0,
        payload_json=encode(payload),
        parent_sources_json=encode(parents),
        summary_view_key=VIEW if parents or key.startswith("snapshot:") else None,
        created_at=datetime.now(UTC),
    )


def selected(env, identity, payload, event_ids=()):
    item = {
        "kind": "model_input",
        "event_ids": list(event_ids),
        "message": {"role": "user", "content": payload},
        "observation_id": identity,
        "observation_version": 1,
    }
    encoded = encode(item)
    key = hashlib.sha256(
        (json.dumps(list(event_ids)) if event_ids else encoded).encode()
    ).hexdigest()
    return ContextSelectionModel(
        view_key=VIEW,
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
        source_key=key,
        event_ids_json=json.dumps(list(event_ids)),
        observation_sources_json=encode(((identity, 1),)),
        payload_json=encoded,
        created_at=datetime.now(UTC),
    )


async def test_hot_reader_uses_real_note_and_actual_summary_roots(database, tmp_path):
    env = await social_env(database, tmp_path)
    first, second, summary = str(uuid4()), str(uuid4()), str(uuid4())
    async with database.immediate_session() as writer:
        writer.add_all(
            [
                source(env, first, "work-note:first:1", note("first", ("goal",))),
                source(env, second, "work-note:second:1", note("second", ("goal",))),
                source(
                    env,
                    summary,
                    "scope-summary:real",
                    note("first condensed", ("observation:" + first,)),
                    ((first, 1), (second, 1)),
                ),
                *(
                    source(
                        env,
                        str(uuid4()),
                        f"snapshot:retired-{index}",
                        {"kind": "dynamic_snapshot", "old_style": "retired"},
                    )
                    for index in range(800)
                ),
            ]
        )
        await writer.flush()
        writer.add(
            selected(
                env,
                summary,
                "[Observation; derived notes]\n"
                + encode(note("first condensed", ("observation:" + first,))),
            )
        )
    repository = ContextObservationRepository(database)
    scope = dict(
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
        view_key=VIEW,
    )
    with capture_sql(database) as hot_sql:
        hot = await repository.read(**scope, frozen_sources=())
    assert {row.id for row in hot} == {first, second}
    assert not any("model_context_selections" in sql for sql, _ in hot_sql)
    adopted = await repository.read(**scope, frozen_sources=((summary, 1),))
    assert {row.id for row in adopted} == {summary, second}
    cold = await repository.read(**scope)
    assert {row.id for row in cold} == {summary, second}
    assert all("dynamic_snapshot" not in row.payload_json for row in (*hot, *adopted, *cold))


async def seed_0107(url, tmp_path, protection, malformed=False, outdated_config=True):
    database = Database(url)
    try:
        env = await social_env(database, tmp_path)
        identities = [str(uuid4()) for _ in range(3)]
        summary = str(uuid4())
        useful_note = str(uuid4())
        useful_payload = encode(note("必须保留的工作事实", ("goal",)))
        raw_body = HEADER + '[{"id":"quoted-user-text"}]\n\nkeep this quotation'
        originals = []
        async with database.immediate_session() as writer:
            now = datetime.now(UTC)
            for key in (
                "plugins.max_prompt_fragment_characters",
                "plugins.max_prompt_characters_per_plugin",
                "plugins.max_total_prompt_characters",
                "plugins.hook_timeout_seconds",
            ):
                if not outdated_config and key != "plugins.hook_timeout_seconds":
                    continue
                writer.add(
                    RuntimeConfigOverrideModel(
                        config_key=key,
                        scope_type="global",
                        value_json="3",
                        value_type="number" if key.endswith("timeout_seconds") else "integer",
                        apply_mode="hot",
                        created_at=now,
                        updated_at=now,
                        updated_by="migration-fixture",
                    )
                )
            event_id = await writer.scalar(text("SELECT id FROM chat_events LIMIT 1"))
            await writer.execute(
                text("UPDATE chat_events SET content=:body WHERE id=:id"),
                {"body": raw_body, "id": event_id},
            )
            pure_item = {
                "kind": "model_input",
                "event_ids": [event_id],
                "message": {"role": "user", "content": raw_body},
            }
            writer.add(
                ContextSelectionModel(
                    view_key="f" * 64,
                    conversation_id=env.context.conversation_id,
                    generation=1,
                    actor_id=env.person,
                    read_scope="main",
                    source_key=hashlib.sha256(json.dumps([event_id]).encode()).hexdigest(),
                    event_ids_json=json.dumps([event_id]),
                    observation_sources_json="[]",
                    payload_json=encode(pure_item),
                    created_at=datetime.now(UTC),
                )
            )
            for index, identity in enumerate(identities):
                payload = {
                    "kind": "dynamic_snapshot",
                    "contributions": [
                        {
                            "id": "context.relationship",
                            "payload": {"style": "old"},
                        }
                    ],
                }
                writer.add(source(env, identity, "snapshot:" + identity, payload))
                dynamic = HEADER + encode([{"id": "runtime.time", "data": index}])
                if malformed and index == 0:
                    dynamic = HEADER + "{}"
                row = selected(env, identity, dynamic + "\n\n" + raw_body, (event_id,))
                if index:
                    row = selected(env, identity, dynamic + "\n\nsame real SDK input")
                writer.add(row)
                await writer.flush()
                originals.append((row.id, row.payload_json))
            writer.add(
                source(
                    env,
                    summary,
                    "scope-summary:old-auto",
                    note("old style", ("observation:" + identities[0],)),
                    ((identities[0], 1),),
                )
            )
            await writer.flush()
            writer.add(
                selected(
                    env,
                    identities[0],
                    "[Observation; derived notes]\n"
                    + encode(
                        {
                            "kind": "dynamic_snapshot",
                            "contributions": [
                                {
                                    "id": "context.relationship",
                                    "payload": {"style": "old"},
                                }
                            ],
                        }
                    ),
                )
            )
            writer.add(
                selected(
                    env,
                    summary,
                    "[Observation; derived notes]\n"
                    + encode(note("old style", ("observation:" + identities[0],))),
                )
            )
            if protection == "mixed":
                real_note, mixed = str(uuid4()), str(uuid4())
                writer.add(
                    source(env, real_note, "work-note:real:1", note("real task fact", ("goal",)))
                )
                writer.add(
                    source(
                        env,
                        mixed,
                        "scope-summary:mixed",
                        note("real mixed fact", ("observation:" + real_note,)),
                        ((identities[0], 1), (real_note, 1)),
                    )
                )
                await writer.flush()
                writer.add(
                    selected(
                        env,
                        mixed,
                        "[Observation; derived notes]\n"
                        + encode(note("real mixed fact", ("observation:" + real_note,))),
                    )
                )
            if protection == "artifact":
                artifact = "d" * 64
                now = datetime.now(UTC)
                writer.add(
                    ToolArtifactModel(
                        handle_id=artifact,
                        provider_id="test",
                        tool_name="research",
                        relative_path="retained-result.txt",
                        media_type="text/plain",
                        byte_size=9,
                        created_at=now,
                        expires_at=now + timedelta(days=1),
                    )
                )
                await writer.flush()
                await writer.execute(
                    text(
                        "INSERT INTO tool_artifact_refs(owner_kind,owner_id,handle_id) "
                        "VALUES ('observation',:id,:handle)"
                    ),
                    {"id": identities[0], "handle": artifact},
                )
            work_id = None
            if protection in {"active", "journal", "protocol"}:
                work_id = str(uuid4())
                await writer.execute(
                    text(
                        "INSERT INTO runtime_work("
                        "id,conversation_id,generation,source_key,source_json,"
                        "goal,state,model_requests,tool_calls,checkpoint_json,created,updated) "
                        "VALUES (:id,:conversation,1,:id,:source,'original goal',"
                        ":state,5,8,:checkpoint,"
                        ":now,:now)"
                    ),
                    {
                        "id": work_id,
                        "conversation": env.context.conversation_id,
                        "source": '{"original":"source"}',
                        "checkpoint": '{"paid":"original"}',
                        "state": "waiting_external" if protection == "active" else "completed",
                        "now": time.time(),
                    },
                )
                if protection == "journal":
                    await writer.execute(
                        text(
                            "INSERT INTO runtime_work_journal("
                            "work_id,chain_id,contract,source_revision,"
                            "phase,payload_json,updated) "
                            "VALUES (:id,:chain,'original:1',1,'response',"
                            '\'{"opaque":"original paid reply"}\',:now)'
                        ),
                        {"id": work_id, "chain": str(uuid4()), "now": time.time()},
                    )
                if protection == "protocol":
                    await writer.execute(
                        text(
                            "INSERT INTO runtime_protocol_objects("
                            "sha256,byte_size,prepared_at,deleting) "
                            "VALUES (:sha,17,:now,0)"
                        ),
                        {"sha": "e" * 64, "now": time.time()},
                    )
                    await writer.execute(
                        text("INSERT INTO runtime_protocol_refs(work_id,sha256) VALUES (:id,:sha)"),
                        {"id": work_id, "sha": "e" * 64},
                    )
            writer.add(source(env, useful_note, "work-note:kept:1", json.loads(useful_payload)))
            await writer.flush()
            writer.add(
                selected(env, useful_note, "[Observation; derived notes]\n" + useful_payload)
            )
            await writer.flush()
            owner = await writer.get(CanonicalConversationModel, env.context.conversation_id)
            original_revision = owner.prompt_source_revision
            projection_payload = encode([json.loads(payload) for _, payload in originals])
            await writer.execute(
                text(
                    "INSERT INTO prompt_projections("
                    "view_key,conversation_id,generation,source_revision,"
                    "starts_after_event_id,epoch_id,context_key,contract_revision,revision,rebuild_reason,"
                    "payload_json,byte_size,updated_at) "
                    "VALUES (:view,:conversation,1,:revision,0,:epoch,"
                    ":key,:key,1,'bootstrap',:payload,:size,:now)"
                ),
                {
                    "view": VIEW,
                    "conversation": owner.id,
                    "revision": original_revision,
                    "epoch": str(uuid4()),
                    "key": "b" * 64,
                    "payload": projection_payload,
                    "size": len(projection_payload.encode()),
                    "now": datetime.now(UTC),
                },
            )
        return dict(
            conversation=env.context.conversation_id,
            actor=env.person,
            original_revision=original_revision,
            identities=identities,
            summary=summary,
            raw_body=raw_body,
            originals=originals,
            work_id=work_id,
            useful_note=useful_note,
            useful_payload=useful_payload,
        )
    finally:
        await database.close()


@pytest.mark.parametrize("start_revision", ["0107", "0108"])
@pytest.mark.parametrize(
    "protection", ["none", "active", "journal", "protocol", "artifact", "mixed"]
)
def test_upgrade_retires_public_packages_without_deleting_real_occurrences_or_private_sources(
    tmp_path, monkeypatch, protection, start_revision
):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'upgrade.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, start_revision)
    baseline = asyncio.run(
        seed_0107(url, tmp_path, protection, outdated_config=start_revision == "0107")
    )
    command.upgrade(config, "0109")

    async def verify():
        database = Database(url)
        try:
            repository = ContextObservationRepository(database)
            selected_rows = await repository.selected(
                view_key=VIEW,
                conversation_id=baseline["conversation"],
                generation=1,
                actor_id=baseline["actor"],
                read_scope="main",
            )
            assert [item["message"]["content"] for item in selected_rows] == [
                baseline["raw_body"],
                "[Observation; derived notes]\n" + baseline["useful_payload"],
            ]
            assert all(
                item["event_ids"] or item.get("observation_id") == baseline["useful_note"]
                for item in selected_rows
            )
            assert all("observation_id" not in item for item in selected_rows if item["event_ids"])
            pure_chat = await repository.selected(
                view_key="f" * 64,
                conversation_id=baseline["conversation"],
                generation=1,
                actor_id=baseline["actor"],
                read_scope="main",
            )
            assert [item["message"]["content"] for item in pure_chat] == [baseline["raw_body"]]
            async with database.sessions() as reader:
                assert (
                    await reader.scalar(text("SELECT version_num FROM alembic_version")) == "0109"
                )
                cache = await reader.get(PromptProjectionModel, VIEW)
                assert (cache.payload_json, cache.byte_size) == ("[]", 2)
                columns = {
                    row[1]
                    for row in await reader.execute(text("PRAGMA table_info(prompt_projections)"))
                }
                assert "visible_material_json" not in columns
                rows = (await reader.scalars(select(ContextObservationModel))).all()
                assert list(
                    await reader.scalars(select(RuntimeConfigOverrideModel.config_key))
                ) == ["plugins.hook_timeout_seconds"]
                retained = {row.id for row in rows}
                assert baseline["useful_note"] in retained
                if protection == "none":
                    assert retained == {baseline["useful_note"]}
                if protection in {"active", "journal", "protocol"}:
                    assert retained == {
                        *baseline["identities"],
                        baseline["summary"],
                        baseline["useful_note"],
                    }
                    assert (
                        await reader.scalar(
                            text(
                                "SELECT prompt_source_revision FROM canonical_conversations "
                                "WHERE id=:id"
                            ),
                            {"id": baseline["conversation"]},
                        )
                        == baseline["original_revision"]
                    )
                    original_work = (
                        await reader.execute(
                            text(
                                "SELECT source_json,checkpoint_json,model_requests,tool_calls "
                                "FROM runtime_work WHERE id=:id"
                            ),
                            {"id": baseline["work_id"]},
                        )
                    ).one()
                    assert tuple(original_work) == (
                        '{"original":"source"}',
                        '{"paid":"original"}',
                        5,
                        8,
                    )
                if protection in {"artifact", "mixed"}:
                    assert baseline["identities"][0] in retained
                    assert baseline["identities"][1] not in retained
                if protection == "artifact":
                    assert await reader.scalar(text("SELECT COUNT(*) FROM tool_artifact_refs")) == 1
                if protection == "journal":
                    assert (
                        await reader.scalar(text("SELECT payload_json FROM runtime_work_journal"))
                        == '{"opaque":"original paid reply"}'
                    )
                if protection == "protocol":
                    assert (
                        await reader.scalar(text("SELECT sha256 FROM runtime_protocol_refs"))
                        == "e" * 64
                    )
                if protection == "mixed":
                    mixed = next(row for row in rows if row.source_key == "scope-summary:mixed")
                    assert await validate_observations(
                        reader,
                        baseline["conversation"],
                        1,
                        baseline["actor"],
                        "main",
                        ((mixed.id, 1),),
                    )
                    assert "real mixed fact" in mixed.payload_json
                assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
            displayed = await repository.read(
                conversation_id=baseline["conversation"],
                generation=1,
                actor_id=baseline["actor"],
                read_scope="main",
                view_key=VIEW,
            )
            assert all("old style" not in row.payload_json for row in displayed)
            assert all("dynamic_snapshot" not in row.payload_json for row in displayed)
        finally:
            await database.close()

    asyncio.run(verify())


def test_installed_cli_upgrades_copy_without_repository_pythonpath(tmp_path, monkeypatch):
    original = tmp_path / "original-0108.db"
    url = f"sqlite+aiosqlite:///{original.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0108")
    head = ScriptDirectory.from_config(config).get_current_head()
    asyncio.run(seed_0107(url, tmp_path, "protocol", outdated_config=False))
    private = (
        "runtime_work",
        "runtime_work_journal",
        "runtime_work_effects",
        "runtime_protocol_objects",
        "runtime_protocol_refs",
        "runtime_protocol_usage",
        "memory_facts",
        "memory_evidence",
        "memory_mutation_receipts",
        "memory_tool_receipts",
    )
    runtime = tmp_path / "installed-runtime"
    runtime.mkdir()
    shutil.copy("alembic.ini", runtime)
    shutil.copytree("migrations", runtime / "migrations")
    (runtime / "config").mkdir()
    shutil.copy("config/persona.md", runtime / "config/persona.md")
    copied = runtime / "copied.db"
    with sqlite3.connect(original) as before, sqlite3.connect(copied) as after:
        before.backup(after)
        original_rows = {
            table: before.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in private
        }
    cli = Path(sys.executable).parent / (
        "qq-ai-bot-cli.exe" if os.name == "nt" else "qq-ai-bot-cli"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{copied.as_posix()}"
    result = subprocess.run(
        [str(cli), "init-db"], cwd=runtime, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(copied) as after:
        assert after.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        assert after.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not after.execute("PRAGMA foreign_key_check").fetchall()
        for table, rows in original_rows.items():
            assert after.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() == rows


def test_failed_upgrade_rolls_back_ddl_public_retirement_and_original_triggers(
    tmp_path, monkeypatch
):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'rollback.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0107")
    baseline = asyncio.run(seed_0107(url, tmp_path, "active", malformed=True))
    with pytest.raises(ValueError, match="original compiled material envelope"):
        command.upgrade(config, "0109")

    async def verify():
        database = Database(url)
        try:
            async with database.sessions() as reader:
                assert (
                    await reader.scalar(text("SELECT version_num FROM alembic_version")) == "0107"
                )
                columns = {
                    row[1]
                    for row in await reader.execute(text("PRAGMA table_info(prompt_projections)"))
                }
                assert "visible_material_json" not in columns
                for identity, original in baseline["originals"]:
                    assert (
                        await reader.scalar(
                            text("SELECT payload_json FROM model_context_selections WHERE id=:id"),
                            {"id": identity},
                        )
                        == original
                    )
                installed = dict(
                    (
                        await reader.execute(
                            text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
                        )
                    ).all()
                )
                for name, expected in PROJECTION_TRIGGERS_0089.items():
                    assert " ".join(installed[name].split()) == " ".join(expected.split())
        finally:
            await database.close()

    asyncio.run(verify())


async def test_metadata_trigger_invalidates_selected_chat_and_preserves_ordinary_rollup_updates(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        owner = await writer.get(CanonicalConversationModel, env.context.conversation_id)
        writer.add(
            PromptProjectionModel(
                view_key=VIEW,
                conversation_id=owner.id,
                generation=1,
                source_revision=owner.prompt_source_revision,
                starts_after_event_id=0,
                epoch_id=str(uuid4()),
                context_key="b" * 64,
                contract_revision="c" * 64,
                revision=1,
                rebuild_reason="bootstrap",
                payload_json="[]",
                byte_size=2,
                updated_at=datetime.now(UTC),
            )
        )
        writer.add(
            CanonicalConversationRollupModel(
                conversation_id=owner.id,
                generation=1,
                covered_through_event_id=0,
                summary_text="original semantic",
                summary_kind="model",
                source_fingerprint="f" * 64,
                revision=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        original_revision = owner.prompt_source_revision
    async with database.immediate_session() as writer:
        await writer.execute(
            text(
                "UPDATE canonical_conversation_rollups SET summary_text='new semantic',"
                "revision=revision+1"
            )
        )
    async with database.sessions() as reader:
        current = await reader.get(PromptProjectionModel, VIEW)
        owner = await reader.get(CanonicalConversationModel, env.context.conversation_id)
        assert current.invalidated_reason is None
        assert owner.prompt_source_revision == original_revision
    async with database.immediate_session() as writer:
        await writer.execute(text("UPDATE chat_events SET audio_transcript='实际语音'"))
    async with database.sessions() as reader:
        row = await reader.get(PromptProjectionModel, VIEW)
        assert (row.payload_json, row.byte_size) == ("[]", 2)
        installed = dict(
            (
                await reader.execute(
                    text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
                )
            ).all()
        )
        for name, expected in PROJECTION_TRIGGERS_0109.items():
            assert " ".join(installed[name].replace("IF NOT EXISTS ", "").split()) == " ".join(
                expected.split()
            )
    assert not any(
        name.startswith("prompt_projection_canonical_conversation_rollup") for name in installed
    )


async def _trigger_sources(database, tmp_path, privacy_update):
    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig

    env = await social_env(database, tmp_path)
    ledger = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    async with database.immediate_session() as writer:
        await ensure_space(writer, "20002")
        other_space = await ensure_space(writer, "20003")
        if privacy_update:
            await writer.execute(
                text("INSERT INTO execution_trace_state(id,privacy_generation) VALUES(1,0)")
            )
    await ledger.append(
        scope=ConversationScope.group(env.bot.self_id, "20002"),
        platform_message_id="unrelated-trigger-owner",
        sender_user_id="10001",
        direction="inbound",
        content="unrelated real event",
    )
    async with database.sessions() as reader:
        events = (await reader.scalars(select(ChatEventModel).order_by(ChatEventModel.id))).all()
    main_event, other_event = events
    identities = []
    async with database.immediate_session() as writer:
        for view, event in ((VIEW, main_event), ("d" * 64, other_event)):
            identity = str(uuid4())
            identities.append(identity)
            observation = source(
                env,
                identity,
                "snapshot:" + identity,
                {
                    "kind": "dynamic_snapshot",
                    "contributions": {
                        "runtime.short_state": {"payload": [{"slot": 1, "text": "material"}]}
                    },
                },
            )
            observation.conversation_id = event.canonical_conversation_id
            observation.source_event_id = event.id
            observation.summary_view_key = view
            writer.add(observation)
            await writer.flush()
            representation = selected(env, identity, "original frozen expression", (event.id,))
            representation.view_key = view
            representation.conversation_id = event.canonical_conversation_id
            writer.add(representation)
            raw = ContextSelectionModel(
                view_key=view,
                conversation_id=event.canonical_conversation_id,
                generation=1,
                actor_id=env.person,
                read_scope="main",
                source_key=hashlib.sha256((view + "raw").encode()).hexdigest(),
                event_ids_json=json.dumps([event.id]),
                observation_sources_json="[]",
                payload_json=encode(
                    {
                        "kind": "model_input",
                        "event_ids": [event.id],
                        "message": {"role": "user", "content": "pure real event"},
                    }
                ),
                created_at=datetime.now(UTC),
            )
            writer.add(raw)
            owner = await writer.get(CanonicalConversationModel, event.canonical_conversation_id)
            body = encode([json.loads(representation.payload_json)])
            writer.add(
                PromptProjectionModel(
                    view_key=view,
                    conversation_id=owner.id,
                    generation=1,
                    source_revision=owner.prompt_source_revision,
                    starts_after_event_id=0,
                    epoch_id=str(uuid4()),
                    context_key="b" * 64,
                    contract_revision="c" * 64,
                    revision=1,
                    rebuild_reason="bootstrap",
                    payload_json=body,
                    byte_size=len(body.encode()),
                    updated_at=datetime.now(UTC),
                )
            )
        writer.add(
            CanonicalConversationRollupModel(
                conversation_id=env.context.conversation_id,
                generation=1,
                covered_through_event_id=0,
                summary_text="semantic",
                summary_kind="model",
                source_fingerprint="f" * 64,
                revision=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        writer.add(
            CanonicalConversationRollupEmergencyOverlayModel(
                conversation_id=env.context.conversation_id,
                generation=1,
                covered_through_event_id=0,
                summary_text="overlay",
                source_fingerprint="e" * 64,
                revision=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return env, main_event, other_event, identities, other_space


_TRIGGER_OPERATIONS = (
    ("prompt_projection_reset", "generation", "reset", 2, 0),
    ("prompt_projection_event_delete", "event_delete", "deleted_event", 2, 0),
    ("prompt_projection_event_update", "event_content", "source_changed", 1, 0),
    ("prompt_projection_audio_update", "audio", "source_changed", 1, 0),
    ("prompt_projection_owner_update", "owner", "read_scope_changed", 1, 0),
    ("prompt_projection_event_metadata_update", "nickname", "source_changed", 1, 0),
    ("context_selection_source_changed", "source_revision", None, 1, 0),
    ("context_observation_reset", "cut", "reset", 2, 0),
    ("context_observation_privacy", "privacy_update", "source_changed", 1, 1),
    ("context_observation_privacy_initial", "privacy_insert", "source_changed", 1, 1),
    ("context_observation_delete", "observation_delete", "source_changed", 1, 0),
    ("context_observation_update", "observation_payload", "source_changed", 1, 0),
    ("context_source_canonical_conversation_rollups_owner", "rollup_owner", "source_changed", 1, 0),
    (
        "context_source_canonical_conversation_rollup_emergency_overlays_owner",
        "overlay_owner",
        "source_changed",
        1,
        0,
    ),
    ("prompt_projection_event_update", "event_move", "source_changed", 1, 1),
    ("context_observation_update", "observation_move", "source_changed", 1, 1),
    ("context_source_canonical_conversation_rollups_owner", "rollup_move", "source_changed", 1, 1),
    (
        "context_source_canonical_conversation_rollup_emergency_overlays_owner",
        "overlay_move",
        "source_changed",
        1,
        1,
    ),
)


@pytest.mark.parametrize(
    "trigger,operation,reason,main_increment,other_increment",
    _TRIGGER_OPERATIONS,
    ids=[row[1] for row in _TRIGGER_OPERATIONS],
)
async def test_current_trigger_sql_keeps_material_and_source_boundaries(
    database, tmp_path, trigger, operation, reason, main_increment, other_increment
):
    assert trigger in PROJECTION_TRIGGERS_0109
    env, event, other, identities, moved_space = await _trigger_sources(
        database, tmp_path, operation == "privacy_update"
    )
    permissions = (
        "persons",
        "spaces",
        "presences",
        "identity_bindings",
        "space_bindings",
        "person_active_routes",
        "space_active_routes",
        "space_binding_ingest_routes",
    )
    async with database.sessions() as reader:
        owners = {
            row["id"]: dict(row)
            for row in (
                await reader.execute(text("SELECT * FROM canonical_conversations"))
            ).mappings()
        }
        caches = {
            row["view_key"]: dict(row)
            for row in (await reader.execute(text("SELECT * FROM prompt_projections"))).mappings()
        }
        grants = {
            table: (await reader.execute(text(f"SELECT * FROM {table} ORDER BY rowid"))).all()
            for table in permissions
        }
    statements = {
        "generation": "UPDATE canonical_conversations SET generation=generation+1 WHERE id=:owner",
        "cut": (
            "UPDATE canonical_conversations SET starts_after_event_id=last_event_id, "
            "covered_through_event_id=last_event_id WHERE id=:owner"
        ),
        "event_delete": "DELETE FROM chat_events WHERE id=:event",
        "event_content": "UPDATE chat_events SET content='changed real source' WHERE id=:event",
        "audio": "UPDATE chat_events SET audio_transcript='real ASR source' WHERE id=:event",
        "owner": "UPDATE canonical_conversations SET space_id=:space WHERE id=:owner",
        "nickname": "UPDATE chat_events SET sender_nickname='changed nickname' WHERE id=:event",
        "source_revision": (
            "UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1 "
            "WHERE id=:owner"
        ),
        "privacy_update": "UPDATE execution_trace_state SET privacy_generation=1 WHERE id=1",
        "privacy_insert": "INSERT INTO execution_trace_state(id,privacy_generation) VALUES(1,1)",
        "observation_delete": "DELETE FROM model_context_observations WHERE id=:source",
        "observation_payload": (
            "UPDATE model_context_observations SET payload_json=:payload WHERE id=:source"
        ),
        "rollup_owner": (
            "UPDATE canonical_conversation_rollups SET generation=2 WHERE conversation_id=:owner"
        ),
        "overlay_owner": (
            "UPDATE canonical_conversation_rollup_emergency_overlays SET generation=2 "
            "WHERE conversation_id=:owner"
        ),
        "event_move": "UPDATE chat_events SET canonical_conversation_id=:other WHERE id=:event",
        "observation_move": (
            "UPDATE model_context_observations SET conversation_id=:other WHERE id=:source"
        ),
        "rollup_move": (
            "UPDATE canonical_conversation_rollups SET conversation_id=:other "
            "WHERE conversation_id=:owner"
        ),
        "overlay_move": (
            "UPDATE canonical_conversation_rollup_emergency_overlays SET conversation_id=:other "
            "WHERE conversation_id=:owner"
        ),
    }
    async with database.immediate_session() as writer:
        await writer.execute(
            text(statements[operation]),
            {
                "owner": env.context.conversation_id,
                "event": event.id,
                "space": moved_space,
                "source": identities[0],
                "other": other.canonical_conversation_id,
                "payload": encode({"kind": "dynamic_snapshot", "contributions": {}}),
            },
        )
    async with database.sessions() as reader:
        after_owners = {
            row["id"]: dict(row)
            for row in (
                await reader.execute(text("SELECT * FROM canonical_conversations"))
            ).mappings()
        }
        for identity, increment in (
            (event.canonical_conversation_id, main_increment),
            (other.canonical_conversation_id, other_increment),
        ):
            expected = dict(owners[identity])
            expected["prompt_source_revision"] += increment
            if identity == event.canonical_conversation_id:
                if operation == "generation":
                    expected["generation"] += 1
                elif operation == "cut":
                    expected["starts_after_event_id"] = expected["last_event_id"]
                    expected["covered_through_event_id"] = expected["last_event_id"]
                elif operation == "owner":
                    expected["space_id"] = moved_space
            assert after_owners[identity] == expected
        after_caches = {
            row["view_key"]: dict(row)
            for row in (await reader.execute(text("SELECT * FROM prompt_projections"))).mappings()
        }
        for view, increment in ((VIEW, main_increment), ("d" * 64, other_increment)):
            current = after_caches[view]
            original = caches[view]
            if increment and reason is not None:
                assert (
                    current["payload_json"],
                    current["byte_size"],
                    current["invalidated_reason"],
                ) == ("[]", 2, reason)
                assert current["revision"] > original["revision"]
                assert all(
                    current[key] == value
                    for key, value in original.items()
                    if key
                    not in {
                        "payload_json",
                        "byte_size",
                        "invalidated_reason",
                        "revision",
                    }
                )
            else:
                assert current == original
        for table, original in grants.items():
            assert (
                await reader.execute(text(f"SELECT * FROM {table} ORDER BY rowid"))
            ).all() == original
        if operation == "source_revision":
            retained = list(
                await reader.scalars(
                    select(ContextSelectionModel).where(
                        ContextSelectionModel.conversation_id == env.context.conversation_id
                    )
                )
            )
            assert len(retained) == 1
            assert json.loads(retained[0].observation_sources_json) == [[identities[0], 1]]
        elif operation in {
            "generation",
            "cut",
            "event_delete",
            "observation_delete",
            "privacy_insert",
            "privacy_update",
        }:
            assert await reader.get(ContextObservationModel, identities[0]) is None
            assert not list(
                await reader.scalars(
                    select(ContextSelectionModel.id).where(
                        ContextSelectionModel.conversation_id == env.context.conversation_id
                    )
                )
            )
        assert not list(await reader.execute(text("PRAGMA foreign_key_check")))
