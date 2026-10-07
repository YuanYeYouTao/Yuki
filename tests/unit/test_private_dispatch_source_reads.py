"""Two real private turns through SQLite/Runner/serializer; transport stays in memory."""

from collections import Counter
from contextvars import ContextVar
from dataclasses import replace

from sqlalchemy import event
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.runtime_wire import install_wire
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.conversation.projections import PromptProjectionRepository
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.execution_trace.recorder import TraceRecorder
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.runtime.work_session import WorkSession as RuntimeWorkSession
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard


async def test_private_fresh_and_warm_dispatch_share_snapshot_proofs_only(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    receipt = await env.service.writer.append(
        scope=ConversationScope.private(env.bot.self_id, "10001"),
        platform_message_id="private-prior",
        sender_user_id="10001",
        direction="inbound",
        content="synthetic private prior",
    )
    fake = FakeLLMProvider(lambda _request: ChatResponse("NO_REPLY", 0))
    harness = build_harness(database, make_settings(database.url, runtime_work_enabled=True), fake)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    client, wire = install_wire(chat, fake, "chat_completions")
    writer = DiagnosticWriter()
    await writer.start()
    chat._models.traces = TraceRecorder(database, writer=writer)
    active = ContextVar("private_read_measurement", default=None)
    records = []
    original_sessions = database.sessions

    def sessions(*args, **kwargs):
        row = active.get()
        if row is not None:
            row["sessions"] += 1
        return original_sessions(*args, **kwargs)

    def sql(_connection, _cursor, statement, *_args):
        row = active.get()
        if row is not None:
            row["sql"].append(" ".join(statement.split()))

    def wrapped(original, name):
        async def call(*args, **kwargs):
            row = {"name": name, "sessions": 0, "sql": [], "turn": len(wire)}
            token = active.set(row)
            try:
                if name == "restore":
                    assert args[0].control.current is None
                    assert args[0].control.lease.work_id is None
                return await original(*args, **kwargs)
            finally:
                records.append(row)
                active.reset(token)

        return call

    monkeypatch.setattr(database, "sessions", sessions)
    monkeypatch.setattr(RuntimeWorkSession, "restore", wrapped(WorkSession.restore, "restore"))
    monkeypatch.setattr(WorkSourceGuard, "check", wrapped(WorkSourceGuard.check, "guard"))
    monkeypatch.setattr(
        PromptProjectionRepository,
        "prepare_commit",
        wrapped(PromptProjectionRepository.prepare_commit, "projection"),
    )
    event.listen(database.engine.sync_engine, "before_cursor_execute", sql)
    sender = MemorySender()
    try:
        for index in (1, 2):
            message = replace(
                inbound(
                    f"synthetic private question {index}",
                    message_id=f"private-new-{index}",
                    user_id="10001",
                    group_id=None,
                ),
                bot_user_id=env.bot.self_id,
                scope_type=ScopeType.PRIVATE,
                conversation_id=receipt.event.canonical_conversation_id,
                person_id=env.person,
                presence_id=env.presence,
            )
            result = await harness.processor.handle(message, sender)
            await writer.drain()
            assert result.handled
        assert len(wire) == len(fake.requests) == 2
        assert not sender.calls
        for sent, request in zip(wire, fake.requests, strict=True):
            assert [(m["role"], m.get("content")) for m in sent["messages"]] == [
                (m.role, m.content) for m in request.messages
            ]
            assert sent["tools"] == [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        projections = [row for row in records if row["name"] == "projection"]
        assert len(projections) == 2
        for row in projections:
            assert row["sessions"] == 3
            assert sum("FROM execution_trace_state" in sql for sql in row["sql"]) == 3
            assert [sql for sql in row["sql"] if "FROM chat_events" in sql] == [
                next(sql for sql in row["sql"] if "FROM chat_events" in sql)
            ] * 2
            assert all(
                sql.startswith("SELECT chat_events.canonical_conversation_id FROM")
                for sql in row["sql"]
                if "FROM chat_events" in sql
            )
        guards = [row for row in records if row["name"] == "guard"]
        assert guards
        for row in guards:
            assert row["sessions"] == 2
            assert sum("FROM execution_trace_state" in sql for sql in row["sql"]) == 2
            assert all(sql.startswith(("BEGIN", "SELECT")) for sql in row["sql"])
        restores = [row for row in records if row["name"] == "restore"]
        assert len(restores) == 2
        assert all(row["sessions"] == 1 and len(row["sql"]) == 1 for row in restores)
        assert all(
            row["sql"][0].startswith(
                "SELECT canonical_conversations.generation, "
                "canonical_conversations.prompt_source_revision FROM"
            )
            for row in restores
        )
        print(
            "private_source_read_counts",
            [
                {
                    "name": row["name"],
                    "turn": row["turn"],
                    "sessions": row["sessions"],
                    "statements": dict(Counter(sql.split()[0] for sql in row["sql"])),
                }
                for row in records
            ],
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", sql)
        await writer.close()
        await client.aclose()
