"""Retry only a rolled-back journal publication, never its paid model response."""

import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy import event, text, update
from sqlalchemy.exc import OperationalError
from tests.support.work_runner_helpers import case, run, tool
from tests.unit.test_work_journal_source_retry import _change, _saved, _session

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse
from qq_ai_bot.llm.base import LLMUnavailableError
from qq_ai_bot.runtime.activation_outcome import WorkRecoveryDeferred
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import JournalUnavailable, WorkJournal
from qq_ai_bot.runtime.work_repository import WorkConflict


def fail_publications(
    monkeypatch, *, phase, failures, code=sqlite3.SQLITE_BUSY, cleanup=None, after_failure=None
):
    original_save = WorkJournal.save
    writers = []

    async def save(self, *args, **kwargs):
        if kwargs["phase"] == phase:
            original_publication = kwargs.get("publication")

            async def publication(writer):
                writers.append(writer)
                assert writer.in_transaction()
                if original_publication:
                    await original_publication(writer)
                if len(writers) <= failures:
                    original = sqlite3.OperationalError("database is locked: private fixture")
                    if code is not None:
                        original.sqlite_errorcode = code
                    error = OperationalError("private SQL", {}, original)
                    if cleanup:
                        error.add_note(cleanup)
                    raise error

            kwargs["publication"] = publication
        try:
            return await original_save(self, *args, **kwargs)
        except OperationalError:
            if kwargs["phase"] == phase and after_failure is not None:
                await after_failure(len(writers))
            raise

    monkeypatch.setattr(WorkJournal, "save", save)
    return writers


async def paid_case(database, tmp_path):
    call = tool("read_fixture", {"key": "same"}, "original-paid-call")
    result = await case(
        database,
        tmp_path,
        [ChatResponse("original-paid-response", 0, tool_calls=(call,))],
        reporting="quiet",
    )
    result.runtime = replace(result.runtime, max_model_requests=1)
    return result


async def snapshot(test_case):
    return await test_case.control.session.journal.load(
        test_case.control.lease, test_case.control.current["id"], test_case.control.session.contract
    )


@pytest.mark.parametrize("phase", ["response", "paired"])
@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_BUSY_SNAPSHOT])
async def test_busy_republishes_original_response_in_new_transactions_without_a_count_limit(
    database, tmp_path, monkeypatch, phase, code
):
    failures = 5
    writers = fail_publications(monkeypatch, phase=phase, failures=failures, code=code)
    test_case = await paid_case(database, tmp_path)
    result = await run(test_case)
    assert len(writers) >= failures + 1
    assert len({id(writer) for writer in writers}) == len(writers)
    assert all(not writer.in_transaction() for writer in writers)
    assert len(test_case.provider.requests) == 1
    assert test_case.observed == ["read_fixture"]
    assert result.work_state == "queued"  # normal segment budget boundary
    persisted = await test_case.repository.get(test_case.control.current["id"])
    assert persisted["model_requests"] == persisted["tool_calls"] == 1
    journal = await snapshot(test_case)
    assert journal.record["phase"] == "paired"
    assert "original-paid-call" in journal.record["payload_json"]
    assert "original-paid-response" in journal.record["payload_json"]


@pytest.mark.parametrize("phase", ["response", "paired"])
@pytest.mark.parametrize("stop", ["cancel", "lease"])
async def test_paid_checkpoint_busy_exits_on_original_cancellation_or_lost_lease(
    database, tmp_path, monkeypatch, phase, stop
):
    async def after_failure(count):
        if count == 3:
            if stop == "cancel":
                raise asyncio.CancelledError
            await test_case.repository.release(test_case.control.lease)

    writers = fail_publications(monkeypatch, phase=phase, failures=10, after_failure=after_failure)
    test_case = await paid_case(database, tmp_path)
    with pytest.raises(
        asyncio.CancelledError if stop == "cancel" else WorkRecoveryDeferred
    ) as raised:
        await run(test_case)
    if stop == "lease":
        assert isinstance(raised.value.__cause__, WorkConflict)
    assert len(writers) == 3 and all(not writer.in_transaction() for writer in writers)
    assert len(test_case.provider.requests) == 1
    assert test_case.observed == ([] if phase == "response" else ["read_fixture"])
    persisted = await test_case.repository.get(test_case.control.current["id"])
    assert persisted["model_requests"] == 1
    assert persisted["tool_calls"] == (0 if phase == "response" else 1)
    from qq_ai_bot.runtime.work_schema_v1 import journal

    async with test_case.repository.database.sessions() as reader:
        record = (
            (await reader.execute(journal.select().where(journal.c.work_id == persisted["id"])))
            .mappings()
            .one()
        )
    payload = await test_case.control.session.journal.objects.hydrate(
        json.loads(record["payload_json"])
    )
    if phase == "response":
        assert record["phase"] == "dispatched"
        assert "original-paid-response" not in json.dumps(payload)
        assert payload["pending"] == []
        if stop == "cancel":
            with pytest.raises(JournalUnavailable, match="work_response_not_persisted"):
                await snapshot(test_case)
    else:
        assert record["phase"] == "response"
        assert payload["pending"][0]["id"] == "original-paid-call"
        assert "original-paid-response" in json.dumps(payload)


async def test_known_provider_failure_retries_original_work_with_new_input_and_budget(
    database, tmp_path
):
    test_case = await paid_case(database, tmp_path)

    def respond(_request):
        if len(test_case.provider.requests) == 1:
            raise LLMUnavailableError("explicit provider failure")
        return ChatResponse("completed after known failure", 0)

    test_case.provider._responder = respond
    first = await run(test_case)
    assert first.work_state == "queued" and first.outcome.failure.code == "LLMUnavailableError"
    assert (await snapshot(test_case)).record["phase"] == "paired"
    old = test_case.control
    identity = old.current["id"]
    input_id = await test_case.repository.enqueue(
        old.lease.conversation_id,
        old.lease.generation,
        "latest-business-input",
        kind="message",
        work_id=identity,
        ready=False,
    )
    assert await test_case.repository.prepare_input(input_id, {"text": "latest instruction"})
    control = WorkControl(old.repository, old.lease, old.source_key, old.source, old.validate)
    control.current = await old.repository.get(identity)
    test_case.control = control
    test_case.runtime = replace(test_case.runtime, work_control=control)
    second = await run(test_case)
    await control.settle(pending_inputs=False)
    assert second.text == "completed after known failure"
    assert control.current["state"] == "completed"
    assert len(test_case.provider.requests) == 2 and test_case.observed == []
    assert any(
        "latest instruction" in (message.content or "")
        for message in test_case.provider.requests[-1].messages
    )
    assert (await old.repository.get(identity))["model_requests"] == 2


@pytest.mark.parametrize(
    "code,expected", [(sqlite3.SQLITE_LOCKED, "sqlite_locked"), (None, "database_failure")]
)
async def test_locked_or_text_without_driver_code_never_retries_paid_publication(
    database, tmp_path, monkeypatch, code, expected
):
    writers = fail_publications(monkeypatch, phase="response", failures=10, code=code)
    test_case = await paid_case(database, tmp_path)
    result = await run(test_case)
    assert len(writers) == 1 and not writers[0].in_transaction()
    assert result.work_state == "failed" and result.outcome.failure.code == expected
    assert not result.outcome.failure.retryable
    assert len(test_case.provider.requests) == 1 and test_case.observed == []


@pytest.mark.parametrize(
    "cleanup", ["rollback_failed:RuntimeError", "invalidation_failed:RuntimeError"]
)
async def test_cleanup_failure_cannot_authorize_checkpoint_retry(
    database, tmp_path, monkeypatch, cleanup
):
    writers = fail_publications(monkeypatch, phase="response", failures=1, cleanup=cleanup)
    test_case = await paid_case(database, tmp_path)
    result = await run(test_case)
    assert len(writers) == 1
    assert result.work_state == "failed"
    assert result.outcome.failure.code == "work_journal_unavailable"
    assert not result.outcome.failure.retryable
    assert len(test_case.provider.requests) == 1 and test_case.observed == []


async def test_predispatch_busy_republishes_without_buying_an_extra_request(
    database, tmp_path, monkeypatch
):
    fail_publications(monkeypatch, phase="dispatched", failures=10)
    test_case = await paid_case(database, tmp_path)
    result = await run(test_case)
    assert result.work_state == "queued" and result.outcome is None
    assert len(test_case.provider.requests) == 1 and test_case.observed == ["read_fixture"]
    persisted = await test_case.repository.get(test_case.control.current["id"])
    assert persisted["model_requests"] == persisted["tool_calls"] == 1


async def test_real_competing_sqlite_writer_retries_only_the_received_publication(
    database, tmp_path, monkeypatch
):
    original_save = WorkJournal.save
    busy_codes = []
    response_attempts = 0
    competing = sqlite3.connect(database.engine.url.database, timeout=0)

    def no_wait(connection, cursor, statement, *_args):
        if statement == "BEGIN IMMEDIATE":
            cursor.execute("PRAGMA busy_timeout=0")

    event.listen(database.engine.sync_engine, "before_cursor_execute", no_wait)

    async def save(self, *args, **kwargs):
        nonlocal response_attempts
        if kwargs["phase"] != "response":
            return await original_save(self, *args, **kwargs)
        response_attempts += 1
        if response_attempts != 1:
            return await original_save(self, *args, **kwargs)
        competing.execute("BEGIN IMMEDIATE")
        try:
            return await original_save(self, *args, **kwargs)
        except OperationalError as exc:
            busy_codes.append(exc.orig.sqlite_errorcode)
            raise
        finally:
            competing.rollback()

    monkeypatch.setattr(WorkJournal, "save", save)
    try:
        test_case = await paid_case(database, tmp_path)
        result = await run(test_case)
        assert busy_codes == [sqlite3.SQLITE_BUSY] and response_attempts == 2
        assert len(test_case.provider.requests) == 1
        assert test_case.observed == ["read_fixture"]
        assert result.work_state == "queued"
        persisted = await test_case.repository.get(test_case.control.current["id"])
        assert persisted["model_requests"] == persisted["tool_calls"] == 1
        assert (await snapshot(test_case)).record["phase"] == "paired"
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", no_wait)
        competing.close()
        async with database.sessions() as session:
            await session.execute(text("PRAGMA busy_timeout=5000"))


@pytest.mark.parametrize("change", ["selected_source", "generation", "lease"])
async def test_busy_retry_rechecks_source_and_lease_without_overwriting_old_checkpoint(
    database, monkeypatch, change
):
    control, session, selected, _unselected = await _session(database)
    before = await _saved(database, control)
    await control.reserve_request()
    session.transcript.append(ChatMessage("assistant", "not published"))
    original_save = WorkJournal.save
    attempts = 0

    async def save(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts != 1:
            return await original_save(self, *args, **kwargs)

        async def publication(writer):
            assert writer.in_transaction()
            original = sqlite3.OperationalError("fixture busy")
            original.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise OperationalError("fixture publication", {}, original)

        kwargs["publication"] = publication
        try:
            return await original_save(self, *args, **kwargs)
        except OperationalError:
            # A competing change occurs only after the failed writer has closed.
            if change == "selected_source":
                await _change(database, selected)
            elif change == "generation":
                async with database.immediate_session() as writer:
                    await writer.execute(
                        update(CanonicalConversationModel)
                        .where(CanonicalConversationModel.id == control.lease.conversation_id)
                        .values(generation=CanonicalConversationModel.generation + 1)
                    )
            else:
                await control.repository.release(control.lease)
            raise

    monkeypatch.setattr(WorkJournal, "save", save)
    with pytest.raises(WorkConflict):
        await session.save("response")
    assert attempts == (1 if change == "lease" else 2)
    assert await _saved(database, control) == before
    persisted = await control.repository.get(control.current["id"])
    assert persisted["model_requests"] == 1 and persisted["tool_calls"] == 0
