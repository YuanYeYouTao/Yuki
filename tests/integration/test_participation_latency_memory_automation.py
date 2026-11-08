"""Real automation Memory receipts: source ownership and actual SQLite plan."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text, update
from tests.integration import test_participation_latency_basis_race as basis_case
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.integration.test_self_automation_delivery import (
    test_self_schedule_memory_uses_original_execution_receipt_and_remains_readable as seed_direct,
)
from tests.integration.test_self_automation_delivery import (
    test_self_scheduled_work_memory_keeps_original_version_after_task_update as seed_child,
)
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.unit import test_memory_mutation as memory_case
from tests.unit import test_participation_legacy_runs as legacy_case
from tests.unit.test_self_initiative_memory import _service

from yuki.conversation.autonomy_binding import InitiativeSource, InitiativeSourceKind
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.initiative_sources import memory_revision
from yuki.conversation.ordinary_admission import (
    OrdinaryAdmissionConflict,
    OrdinaryAdmissionRepository,
)
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.memory.enums import MemoryKind, MemoryScopeType
from yuki.memory.mutation.models import (
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
    SelfMemoryVisibilityMode,
)
from yuki.memory.self_origin import read_self_initiative_facts, read_self_initiative_revisions
from yuki.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryMutationReceiptModel,
    MemoryToolReceiptModel,
)
from yuki.runtime.work_schema_v1 import work
from yuki.services import semantic_participation
from yuki.tool_results.recorder import ToolReceiptRepository


@pytest.mark.parametrize("shape", ["direct", "work", "plugin_wrapper"])
async def test_automation_memory_original_source_uses_bounded_plan(
    database, tmp_path, latency_evidence, shape
):
    if shape == "work":
        await seed_child(database, tmp_path)
    else:
        await seed_direct(database, tmp_path, False)
    async with database.read_session() as reader:
        receipt = await reader.scalar(
            select(MemoryToolReceiptModel).where(MemoryToolReceiptModel.tool_name == "get_members")
        )
        mutation = await reader.scalar(select(MemoryMutationReceiptModel))
        conversation = await reader.scalar(
            select(CanonicalConversationModel.id).where(
                CanonicalConversationModel.space_id == receipt.canonical_space_id,
                CanonicalConversationModel.kind == "space",
            )
        )
        receipt_id, source, fact_id = receipt.id, receipt.execution_id, mutation.new_fact_id
        work_row = (
            (await reader.execute(select(work).where(work.c.id == source))).mappings().one_or_none()
        )
    assert receipt_id and source and conversation and fact_id
    if shape == "plugin_wrapper":
        # This formatting matches the separately E2E-verified SDK source shape;
        # this particular wrapper is synthetic, not an actual plugin invocation.
        source = f"plugin:test.scheduled-self-read:execution:{source}:{'a' * 64}"
        async with database.immediate_session() as writer:
            await writer.execute(
                update(MemoryToolReceiptModel)
                .where(MemoryToolReceiptModel.id == receipt_id)
                .values(execution_id=source)
            )
    captured = []

    def capture(_connection, _cursor, sql, parameters, _context, _many):
        if "memory_evidence" in sql.lower():
            captured.append((sql, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        found = await read_self_initiative_facts(
            database, canonical_conversation_id=conversation, fact_ids=(fact_id,)
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert [fact.id for fact in found] == [fact_id]
    plans = []
    async with database.read_session() as reader:
        connection = await reader.connection()
        for sql, parameters in captured:
            plans.extend(
                row[3]
                for row in (
                    await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)
                ).all()
            )
    assert captured
    if shape == "work":
        assert work_row is not None
        original_json = work_row["source_json"]
        changed = json.loads(original_json)
        for owner in (changed, changed.get("execution_source", {})):
            parent = owner.get("parent_execution_id")
            if isinstance(parent, str) and parent.startswith("automation:"):
                owner["parent_execution_id"] = parent[:-1] + ("0" if parent[-1] != "0" else "1")
        async with database.immediate_session() as writer:
            await writer.execute(
                update(work).where(work.c.id == source).values(source_json=json.dumps(changed))
            )
    else:
        invalid = source[:-1] + ("0" if source[-1] != "0" else "1")
        if shape == "plugin_wrapper":
            # The final component is the SDK digest, so corrupt the original
            # automation script hash within the preserved embedded source.
            parts = source.rsplit(":", 2)
            parts[-2] = parts[-2][:-1] + ("0" if parts[-2][-1] != "0" else "1")
            invalid = ":".join(parts)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(MemoryToolReceiptModel)
                .where(MemoryToolReceiptModel.id == receipt_id)
                .values(execution_id=invalid)
            )
    assert (
        await read_self_initiative_facts(
            database, canonical_conversation_id=conversation, fact_ids=(fact_id,)
        )
        == ()
    )
    latency_evidence.update(
        boundary="real local automation/Memory/SQLite; plugin wrapper synthetic where named",
        shape=shape,
        source_initially_readable=True,
        changed_hash_rejected=True,
        query_plan=plans,
        automation_full_scan=any(entry == "SCAN ar" for entry in plans),
    )
    assert not any(entry == "SCAN ar" for entry in plans), (
        "legacy Memory proof scans unrelated automation history"
    )


@pytest.mark.parametrize("shape", ["direct", "work", "plugin_wrapper"])
async def test_automation_memory_basis_writer_uses_original_source_keys(
    database, tmp_path, monkeypatch, latency_evidence, shape
):
    if shape == "work":
        await seed_child(database, tmp_path)
    else:
        await seed_direct(database, tmp_path, False)
    _, facts, _, _ = _service(database, self_memory_enabled=True)
    async with database.read_session() as reader:
        mutation = await reader.scalar(select(MemoryMutationReceiptModel))
        receipt = await reader.scalar(
            select(MemoryToolReceiptModel).where(MemoryToolReceiptModel.tool_name == "get_members")
        )
    fact_id = mutation.new_fact_id
    if shape == "plugin_wrapper":
        source = f"plugin:test.scheduled-self-read:execution:{receipt.execution_id}:{'a' * 64}"
        async with database.immediate_session() as writer:
            await writer.execute(
                update(MemoryToolReceiptModel)
                .where(MemoryToolReceiptModel.id == receipt.id)
                .values(execution_id=source)
            )

    async def source_fixture(_database):
        return facts, SimpleNamespace(group_id="20001"), None, None, fact_id

    monkeypatch.setattr(basis_case, "reflection_fact", source_fixture)
    await basis_case.test_writer_rechecks_legacy_memory_basis_after_preparation(
        database, tmp_path, latency_evidence, "none"
    )
    plan = latency_evidence["memory_writer_query_plan"]
    latency_evidence["boundary"] = (
        "real scheduled automation Memory fact/evidence and Host writer; "
        "plugin wrapper synthetic where named; fake model"
    )
    latency_evidence["shape"] = shape
    latency_evidence["automation_full_scan"] = any(line == "SCAN ar" for line in plan)
    assert not latency_evidence["automation_full_scan"]


async def _two_automation_evidence_inputs(database, tmp_path):
    case = await setup_run(
        database,
        tmp_path,
        principal="self",
        mode="silent",
        self_memory_enabled=True,
        trigger={"type": "interval", "seconds": 60},
    )
    mutations, facts, _, _ = _service(database, self_memory_enabled=True)
    case.chat._tools._memory_mutations = mutations
    case.chat._tool_receipts = ToolReceiptRepository(database)
    requests = [
        ("get_members", {"limit": 10}),
        (
            "save_memory",
            {
                "target": {"self": True},
                "content": "我实际查询了当前群成员列表，确认了 test-10001 的成员资料。",
                "kind": "episode",
                "quote": "test-10001",
            },
        ),
        None,
        ("get_members", {"limit": 10}),
        None,
    ]

    def respond(_request):
        current = requests[len(case.provider.requests) - 1]
        if current is None:
            return "NO_REPLY"
        name, arguments = current
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    f"replacement-source-{len(case.provider.requests)}",
                    ToolFunction(name, json.dumps(arguments)),
                ),
            ),
        )

    case.provider._responder = respond
    case.clock.advance(61)
    first = await case.dispatch()
    case.clock.advance(61)
    second = await case.dispatch()
    assert first.id != second.id
    async with database.read_session() as reader:
        mutation = await reader.scalar(select(MemoryMutationReceiptModel))
        evidence = await reader.scalar(
            select(MemoryEvidenceModel).where(MemoryEvidenceModel.fact_id == mutation.new_fact_id)
        )
        receipts = list(
            await reader.scalars(
                select(MemoryToolReceiptModel)
                .where(MemoryToolReceiptModel.tool_name == "get_members")
                .order_by(MemoryToolReceiptModel.id)
            )
        )
        assert len(receipts) == 2 and receipts[0].id == evidence.tool_receipt_id
        assert receipts[0].execution_id != receipts[1].execution_id
        replacement = {
            "fact_id": evidence.fact_id,
            "event_id": None,
            "tool_receipt_id": receipts[1].id,
            "source_speaker_user_id": evidence.source_speaker_user_id,
            "relation": evidence.relation,
            "confidence": evidence.confidence,
            "authority": evidence.authority,
            "excerpt": evidence.excerpt,
            "created_at": evidence.created_at,
        }
        evidence_id = evidence.id
        fact_id = mutation.new_fact_id
        conversation = await reader.scalar(
            select(CanonicalConversationModel.id).where(
                CanonicalConversationModel.space_id == receipts[0].canonical_space_id,
                CanonicalConversationModel.kind == "space",
            )
        )
    return facts, replacement, evidence_id, fact_id, first, second, conversation


@pytest.mark.parametrize(
    "witness_order", ["added_during_wait", "already_readable", "first_unreadable"]
)
async def test_new_automation_evidence_reprepares_without_persistent_veto(
    database, tmp_path, monkeypatch, latency_evidence, witness_order
):
    (
        facts,
        replacement,
        evidence_id,
        fact_id,
        first,
        second,
        _,
    ) = await _two_automation_evidence_inputs(database, tmp_path)

    if witness_order != "added_during_wait":
        async with database.immediate_session() as writer:
            writer.add(MemoryEvidenceModel(**replacement))
            if witness_order == "first_unreadable":
                await writer.execute(
                    update(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.id == evidence_id)
                    .values(excerpt="not present in the receipt")
                )

    async def source_fixture(_database):
        return facts, SimpleNamespace(group_id="20001"), None, None, fact_id

    monkeypatch.setattr(basis_case, "reflection_fact", source_fixture)
    commit = OrdinaryAdmissionRepository.commit
    changes = []

    async def swap_before_writer(repository, prepared, *args, **kwargs):
        if (
            prepared.admission.reason == "continuation"
            and kwargs.get("session") is None
            and not changes
            and witness_order != "first_unreadable"
        ):
            changes.append(prepared.admission.event_id)
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.id == evidence_id)
                    .values(excerpt="not present in the receipt")
                )
                if witness_order == "added_during_wait":
                    writer.add(MemoryEvidenceModel(**replacement))
        return await commit(repository, prepared, *args, **kwargs)

    monkeypatch.setattr(OrdinaryAdmissionRepository, "commit", swap_before_writer)
    statements = []

    def capture(_connection, _cursor, sql, parameters, _context, _many):
        if sql.lstrip().lower().startswith("select") and "continuation_evidence" in sql.lower():
            statements.append((sql, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await basis_case.test_writer_rechecks_legacy_memory_basis_after_preparation(
            database, tmp_path, latency_evidence, "none"
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    plans = []
    async with database.read_session() as reader:
        connection = await reader.connection()
        for sql, parameters in statements:
            plans.extend(
                row[3]
                for row in (
                    await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)
                ).all()
            )
    assert len(changes) == (0 if witness_order == "first_unreadable" else 1)
    if witness_order == "added_during_wait":
        assert (
            latency_evidence["first_commit_conflict"]
            == "ordinary_participation_preparation_changed"
        )
    assert latency_evidence["selected"] is True
    assert any("SEARCH continuation_evidence" in line and "fact_id=?" in line for line in plans)
    writer_plan = latency_evidence["memory_writer_query_plan"]
    assert any(
        "SEARCH memory_evidence" in line and ("rowid=?" in line or "INTEGER PRIMARY KEY" in line)
        for line in writer_plan
    )
    assert not any(line == "SCAN ar" for line in plans + writer_plan)
    latency_evidence.update(
        boundary=(
            "two real scheduled automation runs/receipts and local Host writer; "
            "second evidence association constructed from original receipt; fake provider"
        ),
        witness_order=witness_order,
        reader_witness_query_plan=plans,
        original_fact_id=fact_id,
        first_run_id=first.id,
        second_run_id=second.id,
    )


@pytest.mark.parametrize("entry", ["sources_current", "dispatch"])
async def test_memory_revision_entries_keep_visibility_without_count_projection(
    database, tmp_path, monkeypatch, latency_evidence, entry
):
    (
        facts,
        replacement,
        original_id,
        fact_id,
        _,
        _,
        conversation,
    ) = await _two_automation_evidence_inputs(database, tmp_path)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryEvidenceModel)
            .where(MemoryEvidenceModel.id == original_id)
            .values(excerpt="not present in the original receipt")
        )
        writer.add(MemoryEvidenceModel(**replacement))

    (fact,) = await read_self_initiative_facts(
        database, canonical_conversation_id=conversation, fact_ids=(fact_id,)
    )
    expected = memory_revision(fact)
    assert await read_self_initiative_revisions(
        database, canonical_conversation_id=conversation, fact_ids=(fact_id,)
    ) == {fact_id: expected}

    async def source_fixture(_database):
        return facts, SimpleNamespace(group_id="20001"), None, None, fact_id

    monkeypatch.setattr(basis_case, "reflection_fact", source_fixture)
    monkeypatch.setattr(legacy_case, "reflection_fact", source_fixture)
    stage = ContextVar("revision_validation_stage", default=None)
    revision_reads = []
    service = semantic_participation.SemanticParticipationService
    source_check = service._sources_current
    dispatch = service._dispatch
    revision_query = semantic_participation.read_self_initiative_revisions

    async def trace_source_check(self, *args, **kwargs):
        token = stage.set("sources_current")
        try:
            return await source_check(self, *args, **kwargs)
        finally:
            stage.reset(token)

    async def trace_dispatch(self, *args, **kwargs):
        token = stage.set("dispatch")
        try:
            return await dispatch(self, *args, **kwargs)
        finally:
            stage.reset(token)

    async def trace_revision_query(*args, **kwargs):
        statements = []

        def capture(_connection, _cursor, sql, parameters, _context, _many):
            if sql.lstrip().lower().startswith("select") and "memory_evidence" in sql.lower():
                statements.append((sql, parameters))

        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            result = await revision_query(*args, **kwargs)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        if statements:
            revision_reads.append((stage.get(), result, statements))
        return result

    monkeypatch.setattr(service, "_sources_current", trace_source_check)
    monkeypatch.setattr(service, "_dispatch", trace_dispatch)
    monkeypatch.setattr(
        semantic_participation, "read_self_initiative_revisions", trace_revision_query
    )
    if entry == "sources_current":
        await basis_case.test_writer_rechecks_legacy_memory_basis_after_preparation(
            database, tmp_path, latency_evidence, "none"
        )
    else:
        await legacy_case.test_expired_legacy_pending_queries_original_admission_before_retirement(
            database, tmp_path, monkeypatch, True
        )
    matching = [item for item in revision_reads if item[0] == entry]
    assert matching and all(result.get(fact_id) == expected for _, result, _ in matching)
    plans = []
    async with database.read_session() as reader:
        connection = await reader.connection()
        for _, _, statements in matching:
            for sql, parameters in statements:
                assert "count(" not in sql.lower() and "readable_evidence_count" not in sql.lower()
                plans.extend(
                    row[3]
                    for row in (
                        await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)
                    ).all()
                )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryEvidenceModel)
            .where(MemoryEvidenceModel.fact_id == fact_id)
            .values(excerpt="not present in the original receipt")
        )
    assert (
        await read_self_initiative_facts(
            database, canonical_conversation_id=conversation, fact_ids=(fact_id,)
        )
        == ()
    )
    assert (
        await read_self_initiative_revisions(
            database, canonical_conversation_id=conversation, fact_ids=(fact_id,)
        )
        == {}
    )
    latency_evidence.update(
        boundary=(
            "real Host entry, SQLite and two scheduled automation receipts; "
            "second evidence association constructed from original receipt; fake provider"
        ),
        entry=entry,
        revision_equal_to_original_dto=True,
        last_evidence_invalidation_removes_visibility=True,
        no_readable_evidence_count_projection=True,
        revision_query_plan=plans,
    )


@pytest.mark.parametrize("timestamp_form", ["fractional", "whole_second"])
async def test_legacy_memory_timestamps_keep_unchanged_basis_current(
    database, tmp_path, monkeypatch, latency_evidence, timestamp_form
):
    fixture = await basis_case.reflection_fact(database)
    fact_id = fixture[-1]
    suffix = ".000000" if timestamp_form == "fractional" else ""
    start, end = f"2020-01-01 00:00:00{suffix}", f"2090-01-01 00:00:00{suffix}"
    async with database.immediate_session() as writer:
        await writer.execute(
            text("UPDATE memory_facts SET valid_from = :start, valid_until = :end WHERE id = :id"),
            {"start": start, "end": end, "id": fact_id},
        )
    async with database.read_session() as reader:
        raw = (
            await reader.execute(
                text("SELECT valid_from, valid_until FROM memory_facts WHERE id = :id"),
                {"id": fact_id},
            )
        ).one()
    assert tuple(raw) == (start, end)

    async def source_fixture(_database):
        return fixture

    monkeypatch.setattr(basis_case, "reflection_fact", source_fixture)
    commit = OrdinaryAdmissionRepository.commit

    async def capture_first_conflict(repository, prepared, *args, **kwargs):
        try:
            return await commit(repository, prepared, *args, **kwargs)
        except OrdinaryAdmissionConflict as error:
            latency_evidence["first_commit_conflict"] = str(error)
            raise AssertionError("unchanged legacy Memory timestamps caused a conflict") from error

    monkeypatch.setattr(OrdinaryAdmissionRepository, "commit", capture_first_conflict)
    latency_evidence.update(
        boundary="real legacy Memory fact and SQLite raw timestamp strings; no external provider",
        timestamp_form=timestamp_form,
        raw_valid_from=start,
        raw_valid_until=end,
    )
    await basis_case.test_writer_rechecks_legacy_memory_basis_after_preparation(
        database, tmp_path, latency_evidence, "none"
    )
    assert latency_evidence["selected"] is True


@pytest.mark.parametrize("restore_before_writer", [True, False])
async def test_memory_basis_temporarily_unreadable_at_preparation(
    database, tmp_path, monkeypatch, latency_evidence, restore_before_writer
):
    mutations, facts, ledger, _ = memory_case._service(database, self_memory_enabled=True)
    source_event = await memory_case._event(
        ledger,
        message_id="transient-memory-original-source",
        sender_user_id="1001",
        content="我觉得你回答复杂问题时更喜欢先想清楚再说",
        group_id="3001",
    )
    result = await mutations.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            visibility=SelfMemoryVisibilityMode.CURRENT_SCOPE,
            new_content="面对复杂问题时，我偏好先想清楚再回答",
            memory_key="preference:transient-source",
            category="self_preference",
            kind=MemoryKind.PREFERENCE,
            reason="根据当前用户反馈形成自我判断",
            confidence=0.82,
        ),
        memory_case._context(source_event),
    )
    assert result.ok and result.new_fact_id is not None
    fact_id = result.new_fact_id
    fixture = facts, source_event, None, None, fact_id
    async with database.read_session() as reader:
        evidence = await reader.scalar(
            select(MemoryEvidenceModel).where(MemoryEvidenceModel.fact_id == fact_id)
        )
        assert evidence.event_id == source_event.id
        row = await reader.get(ChatEventModel, source_event.id)
        original_status, original_fingerprint = row.suppression_status, row.utterance_fingerprint

    async def source_fixture(_database):
        return fixture

    monkeypatch.setattr(basis_case, "reflection_fact", source_fixture)
    service = semantic_participation.SemanticParticipationService
    prepare = service.prepare_continuation
    calls = []

    async def prepare_with_transient_evidence(self, binding):
        if calls:
            return await prepare(self, binding)
        item = self._sessions[(binding.conversation_id, binding.generation)]
        original_state = item.controller.state.model_dump_json()
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == source_event.id)
                .values(suppression_status="duplicate", utterance_fingerprint="f" * 64)
            )
        result = await prepare(self, binding)
        assert item.controller.state.model_dump_json() == original_state
        calls.append(True)
        if restore_before_writer:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source_event.id)
                    .values(
                        suppression_status=original_status,
                        utterance_fingerprint=original_fingerprint,
                    )
                )
        return result

    monkeypatch.setattr(service, "prepare_continuation", prepare_with_transient_evidence)
    if restore_before_writer:
        await basis_case.test_writer_rechecks_legacy_memory_basis_after_preparation(
            database, tmp_path, latency_evidence, "none"
        )
        assert latency_evidence["selected"] is True
    else:
        with pytest.raises(AssertionError):
            await basis_case.test_writer_rechecks_legacy_memory_basis_after_preparation(
                database, tmp_path, latency_evidence, "none"
            )
        assert latency_evidence["selected"] is False
        assert latency_evidence["new_activation_id"] is None
    assert calls == [True]
    latency_evidence.update(
        boundary=(
            "actual Memory mutation and immutable event evidence; "
            "canonical event suppression changed/restored through test SQL; no provider"
        ),
        restore_before_writer=restore_before_writer,
        controller_unchanged_during_preparation=True,
    )


@pytest.mark.parametrize("changed_field", ["valid_from", "valid_until"])
async def test_legacy_memory_actual_period_change_conflicts_before_admission(
    database, tmp_path, monkeypatch, latency_evidence, changed_field
):
    fixture = await basis_case.reflection_fact(database)
    fact_id = fixture[-1]
    async with database.immediate_session() as writer:
        await writer.execute(
            text("UPDATE memory_facts SET valid_from = :start, valid_until = :end WHERE id = :id"),
            {"start": "2020-01-01 00:00:00", "end": "2090-01-01 00:00:00", "id": fact_id},
        )

    async def source_fixture(_database):
        return fixture

    class ConflictObserved(Exception):
        pass

    monkeypatch.setattr(basis_case, "reflection_fact", source_fixture)
    commit = OrdinaryAdmissionRepository.commit
    attempted = []

    async def change_before_writer(repository, prepared, *args, **kwargs):
        if prepared.admission.reason == "continuation" and kwargs.get("session") is None:
            attempted.append((repository, prepared.admission.event_id))
            value = (
                "2019-01-01 00:00:00" if changed_field == "valid_from" else "2089-01-01 00:00:00"
            )
            async with database.immediate_session() as writer:
                await writer.execute(
                    text(f"UPDATE memory_facts SET {changed_field} = :value WHERE id = :id"),
                    {"value": value, "id": fact_id},
                )
        try:
            return await commit(repository, prepared, *args, **kwargs)
        except OrdinaryAdmissionConflict as error:
            assert str(error) == "ordinary_participation_preparation_changed"
            raise ConflictObserved from error

    monkeypatch.setattr(OrdinaryAdmissionRepository, "commit", change_before_writer)
    with pytest.raises(ConflictObserved):
        await basis_case.test_writer_rechecks_legacy_memory_basis_after_preparation(
            database, tmp_path, latency_evidence, "none"
        )
    assert len(attempted) == 1
    assert await attempted[0][0].get(attempted[0][1]) is None
    latency_evidence.update(changed_field=changed_field, original_event_not_admitted=True)


@pytest.mark.parametrize(
    "timestamp_form",
    ["fractional", "whole_second", "same_instant", "changed_from", "changed_until"],
)
async def test_autonomous_source_guard_accepts_unchanged_legacy_memory_timestamp(
    database, tmp_path, monkeypatch, latency_evidence, timestamp_form
):
    facts, source, _run, _batch, fact_id = await basis_case.reflection_fact(database)
    suffix = ".000000" if timestamp_form == "fractional" else ""
    async with database.immediate_session() as writer:
        await writer.execute(
            text("UPDATE memory_facts SET valid_from = :start, valid_until = :end WHERE id = :id"),
            {
                "start": f"2020-01-01 00:00:00{suffix}",
                "end": f"2090-01-01 00:00:00{suffix}",
                "id": fact_id,
            },
        )
    host, _ = await basis_case._host(database, tmp_path, observer=False)
    try:
        source_event = await basis_case._event_and_route(
            database, host.ledger, group=source.group_id
        )
        item = await basis_case._item(host, source_event)
        binding = await host._binding(item)
        (fact,) = await read_self_initiative_facts(
            database, canonical_conversation_id=item.scene.conversation_id, fact_ids=(fact_id,)
        )
        assert memory_revision(fact) == memory_revision(await facts.get_fact(fact_id))
        reference = InitiativeSource(
            InitiativeSourceKind.MEMORY, str(fact_id), memory_revision(fact)
        )
        original_session = database.immediate_session
        changed_digests = []

        @asynccontextmanager
        async def change_before_writer():
            if not changed_digests:
                async with original_session() as writer:
                    await writer.execute(
                        text(
                            "UPDATE memory_facts SET valid_from = :start, valid_until = :end "
                            "WHERE id = :id"
                        ),
                        {
                            "start": (
                                "2019-01-01 00:00:00"
                                if timestamp_form == "changed_from"
                                else "2020-01-01 00:00:00.000000"
                            ),
                            "end": (
                                "2089-01-01 00:00:00"
                                if timestamp_form == "changed_until"
                                else "2090-01-01 00:00:00.000000"
                            ),
                            "id": fact_id,
                        },
                    )
                changed_digests.append(memory_revision(await facts.get_fact(fact_id)))
            async with original_session() as writer:
                yield writer

        if timestamp_form in {"same_instant", "changed_from", "changed_until"}:
            monkeypatch.setattr(database, "immediate_session", change_before_writer)
        result = await host.repository.accept_host_proposal(
            proposal_id="legacy-memory-source-guard",
            binding=binding,
            owner=binding.effective_owner,
            space_id=item.scene.space_id,
            presence_id=item.scene.presence_id,
            sources=(reference,),
            source_guard=(reference,),
            thread_key=f"memory:{fact_id}",
        )
        latency_evidence.update(
            boundary="actual local Host, authorized legacy Memory source and SQLite; no provider",
            timestamp_form=timestamp_form,
            admission_outcome=result.outcome,
            digest_unchanged=(changed_digests[0] == reference.revision)
            if changed_digests
            else None,
        )
        if timestamp_form in {"changed_from", "changed_until"}:
            assert changed_digests and changed_digests[0] != reference.revision
            assert result.outcome == "source_changed" and result.run is None
        else:
            if timestamp_form == "same_instant":
                assert changed_digests == [reference.revision]
            assert result.run is not None, f"unchanged source guard rejected: {result.outcome}"
    finally:
        await host.close()
