"""Durable execution facts outlive presentation windows and result TTLs."""

import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession
from tests.support.workspace_snapshots import snapshot_bytes

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.persistence.models import ToolArtifactModel
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, work
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.workspace.store import WorkspaceStore


async def owned_session(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease is not None

    async def valid():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "result-test", {}, valid)
    control.current = await repo.accept(
        lease, source_key="result-test", source={}, goal="execute once"
    )
    session = WorkSession(control, "result-test")
    task = ChatMessage("user", "execute once")
    await session.restore(
        TurnTranscript((ChatMessage("system", "fixed"), task)), compaction_brief=task
    )
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    return control, session, store


async def execute(session, store, identity, outcome, *, side_effecting=True):
    call = ToolCall(
        id=identity, type="function", function=ToolFunction(outcome.tool_name or "test", "{}")
    )

    async def invoke():
        return (
            await ToolResultBudgeter(max_characters=24000, artifacts=store).render(outcome)
        ).text

    return await session.execute(call, invoke, side_effecting=side_effecting)


@pytest.mark.asyncio
async def test_large_uncertain_survives_projection_and_seventy_reads(database, tmp_path):
    control, session, store = await owned_session(database, tmp_path)
    value = await execute(
        session,
        store,
        "uncertain",
        ToolExecutionResult(
            ok=False,
            data={"output": "x" * 100000},
            uncertain=True,
            error_code="external_effect_unknown",
            provider_id="plugin",
            tool_name="mutate",
        ),
    )
    projected = json.loads(value)
    assert projected["uncertain"] is True
    assert projected["error_code"] == "external_effect_unknown"
    for index in range(70):
        await execute(
            session,
            store,
            str(index),
            ToolExecutionResult(
                ok=True,
                data={"read": index},
                mutation_committed=False,
                provider_id="core",
                tool_name="read",
            ),
            side_effecting=False,
        )
    await control.refresh_effects()
    assert len(control.known_effects) <= 65
    recent = await control.repository.effect_evidence(
        control.lease, control.current["id"], limit=64
    )
    assert len(recent) == 64 and not any(item.get("uncertain") for item in recent)
    assert any(item.get("uncertain") for item in control.known_effects)
    assert await control.has_unresolved_effects()
    resumed = WorkControl(control.repository, control.lease, "result-test", {}, control.validate)
    resumed.current = await control.repository.get(control.current["id"])
    assert await resumed.has_unresolved_effects()
    assert not json.loads(
        await resumed.execute("task_control", {"action": "complete"}, "complete")
    )["ok"]
    blocked = await execute(
        session, store, "next-mutation", ToolExecutionResult(ok=True, tool_name="mutate")
    )
    assert json.loads(blocked)["error"] == "unresolved_prior_effect"


@pytest.mark.asyncio
async def test_utf8_receipt_budget_preserves_full_chinese_result_and_replay(database, tmp_path):
    _control, session, store = await owned_session(database, tmp_path)
    outcome = ToolExecutionResult(
        ok=True,
        data={"text": "好" * 23000},
        provider_id="core",
        tool_name="write",
        mutation_committed=True,
    )
    value = await execute(session, store, "write", outcome)
    handle = json.loads(value)["artifact_handle"]
    key = session.call_key("write")
    async with database.sessions() as reader:
        row = (
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
    assert row["state"] == "accepted"
    assert len(row["receipt_json"].encode()) <= 65536
    page = await store.read(handle, operation="get", path=("text",), limit=100)
    assert page is not None
    assert (store._root / f"{handle}.json").read_text(encoding="utf-8").count("好") == 23000
    assert await session.journal.effect_result(key) == value


@pytest.mark.asyncio
async def test_active_owned_result_is_not_ttl_cache_and_gc_resumes_deleting(database, tmp_path):
    control, session, store = await owned_session(database, tmp_path)
    value = await execute(
        session,
        store,
        "large",
        ToolExecutionResult(ok=True, data="x" * 50000, provider_id="core", tool_name="read"),
        side_effecting=False,
    )
    handle = json.loads(value)["artifact_handle"]
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.handle_id == handle)
            .values(expires_at=datetime.now(UTC) - timedelta(days=2))
        )
    assert await store.cleanup() == 0
    assert await store.read(handle) is not None
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(work)
            .where(work.c.id == control.current["id"])
            .values(state="completed", updated=time.time() - 8 * 86400)
        )
        await writer.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.handle_id == handle)
            .values(deleting=True)
        )
    assert await store.cleanup() == 1
    assert await store.read(handle) is None
    assert not (store._root / f"{handle}.json").exists()


@pytest.mark.asyncio
async def test_resolution_keeps_original_mutating_run_evidence(database, tmp_path):
    control, session, store = await owned_session(database, tmp_path)
    await execute(
        session,
        store,
        "launch",
        ToolExecutionResult(
            ok=True,
            data={"run_id": "original-run", "pending": True, "status": "running"},
            provider_id="core",
            tool_name="terminal_exec",
        ),
    )
    assert await control.has_unresolved_effects()
    await execute(
        session,
        store,
        "poll",
        ToolExecutionResult(
            ok=True,
            data={
                "run_id": "original-run",
                "pending": False,
                "status": "succeeded",
                "exit_code": 0,
            },
            provider_id="core",
            tool_name="terminal_read",
        ),
        side_effecting=False,
    )
    assert not await control.has_unresolved_effects()
    facts = await control.effect_evidence()
    launch = next(item for item in facts if item["tool"] == "terminal_exec")
    assert launch["side_effecting"] is True and launch["ok"] is True
    assert launch["run_id"] == "original-run"


def test_immutable_artifact_pagination_reconstructs_text(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace")
    original = "汉字abc123\n" * 15000
    artifact = snapshot_bytes(store, "large.txt", original.encode("utf-8"))
    offset, parts = 0, []
    while True:
        page = store.read(artifact["artifact_id"], offset=offset)
        parts.append(page["text"])
        offset = page["next_offset"]
        if not page["truncated"]:
            break
    assert "".join(parts) == original


@pytest.mark.asyncio
async def test_result_admission_failure_keeps_known_execution_and_forbids_replay(
    database, tmp_path
):
    control, session, store = await owned_session(database, tmp_path)
    store._max_total_bytes = 1
    with pytest.raises(ValueError, match="tool_artifact_capacity"):
        await execute(
            session,
            store,
            "accepted",
            ToolExecutionResult(
                ok=True,
                data="x" * 50000,
                provider_id="core",
                tool_name="write",
                mutation_committed=True,
            ),
        )
    replay = json.loads(await session.journal.effect_result(session.call_key("accepted")))
    assert replay["ok"] is True
    assert replay["result_unavailable"] is True
    assert replay["replay_forbidden"] is True
    assert not await control.has_unresolved_effects()
    assert (await control.effect_evidence())[0]["mutation_committed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "gemini", "responses", "unmatched_native"])
async def test_migrated_active_work_completes_from_original_file_caption_without_resending(
    database, tmp_path, protocol
):
    from sqlalchemy.dialects.sqlite import insert
    from tests.unit.test_work_result_migrations import load_migration

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.runtime.work_journal import encode_transcript
    from qq_ai_bot.runtime.work_schema_v1 import journal

    control, first, _store = await owned_session(database, tmp_path)
    original_id = control.current["id"]
    async with database.sessions() as reader:
        conversation = await reader.get(CanonicalConversationModel, control.lease.conversation_id)
    target = {
        "kind": "space" if conversation.space_id else "person",
        "id": conversation.space_id or conversation.person_id,
    }
    arguments = json.dumps(
        {"artifact_id": "original-product", "attachment_kind": "file", "text": "caption"}
    )
    call = ToolCall("original-send", ToolFunction("send_message", arguments))
    result = json.dumps(
        {
            "ok": True,
            "data": {
                "status": "succeeded",
                "target": target,
                "file": {"status": "succeeded"},
                "caption": {"status": "succeeded"},
            },
        }
    )
    if protocol == "chat":
        first.transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
        first.transcript.append_result(call.id, result)
    else:
        from qq_ai_bot.domain.messages import ProviderContinuation

        if protocol == "gemini":
            native = (
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "send_message", "args": json.loads(arguments)},
                            "thoughtSignature": "private",
                        }
                    ],
                    "_call_ids": [call.id],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "send_message",
                                "response": {"output": result},
                            }
                        }
                    ],
                },
            )
        else:
            native = (
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": "send_message",
                    "arguments": arguments,
                },
                {"type": "function_call_output", "call_id": call.id, "output": result},
            )
        if protocol == "unmatched_native":
            native = ()
        first.transcript.accept(
            ProviderContinuation(
                "gemini" if protocol == "gemini" else "openai",
                "gemini" if protocol == "gemini" else "responses",
                native,
            )
        )
    key = first.call_key(call.id)
    await control.repository.prepare_effect(control.lease, original_id, key, "tool")
    legacy = {"result": result}
    payload = {
        "transcript": encode_transcript(first.transcript),
        "pending": [],
        "metadata": {
            "sequence": first.sequence,
            "effects": [{"delivered_artifacts": ["FORGED"]}],
            "compaction_anchor": encode_transcript(first.compaction_anchor),
        },
    }
    async with database.immediate_session() as writer:
        await writer.execute(
            update(effects)
            .where(effects.c.effect_key == key)
            .values(state="accepted", receipt_json=json.dumps(legacy))
        )
        await writer.execute(
            update(work)
            .where(work.c.id == original_id)
            .values(output_kind="artifact", deliver_artifacts=True, model_requests=3, tool_calls=2)
        )
        await writer.execute(
            insert(journal).values(
                work_id=original_id,
                chain_id=first.transcript.chain_id,
                contract="result-test",
                source_revision=0,
                phase="paired",
                payload_json=json.dumps(payload),
                updated=time.time(),
            )
        )
    migration = load_migration("0086")
    async with database.engine.begin() as connection:

        def migrate(bind):
            migration._convert_outcome(bind, key, original_id, "accepted", legacy)
            assert legacy["outcome"]["delivered_artifacts"] == (
                [] if protocol == "unmatched_native" else ["original-product"]
            )
            bind.execute(
                update(effects)
                .where(effects.c.effect_key == key)
                .values(receipt_json=json.dumps(legacy))
            )

        await connection.run_sync(migrate)
    control.current = await control.repository.get(original_id)
    resumed = WorkSession(control, "result-test")
    await resumed.restore(TurnTranscript((ChatMessage("user", "fresh activation"),)))
    completed = json.loads(
        await control.execute(
            "task_control", {"action": "complete", "artifact_ids": ["original-product"]}, "complete"
        )
    )
    if protocol == "unmatched_native":
        assert completed == {"ok": False, "error": "work_has_unresolved_execution"}
        assert await control.has_unresolved_effects(pending=False)
        blocked = await resumed.execute(
            ToolCall("new-send", ToolFunction("send_message", arguments)),
            lambda: None,
            side_effecting=True,
        )
        assert json.loads(blocked)["error"] == "unresolved_prior_effect"
        assert await resumed.journal.effect_result(key) == result
        return
    assert completed["ok"] is True and completed["ending_proposed"] == "completed", completed
    row = await control.repository.get(original_id)
    assert row["model_requests"] == 3 and row["tool_calls"] == 2
    assert row["id"] == original_id
    assert await resumed.journal.effect_result(key) == result
