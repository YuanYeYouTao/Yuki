"""C21 slice 2A: v1 expand dual-write of 0047 Memory owner columns."""

from __future__ import annotations

import ast
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_memory_v2 import _append_event, _fact

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.owner_dual_write import (
    AMBIGUOUS_OWNER,
    MISSING_BINDING,
    MIXED_DREAM_SOURCE,
    REFLECTION_OWNER_UNIQUE,
    reset_v1_owner_unresolved_log_for_tests,
)
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.dream.db_models import MemoryDreamClusterModel
from qq_ai_bot.memory.dream.models import DreamPlanStatistics, DreamRunMode
from qq_ai_bot.memory.dream.repository import DreamCandidate, DreamRepository
from qq_ai_bot.memory.embedding.models import EmbeddingVector
from qq_ai_bot.memory.enums import (
    MemoryProcessingSource,
)
from qq_ai_bot.memory.rebuild.models import MemoryRebuildPlanStatistics, MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.self_reflection.repository import (
    SelfReflectionRepository,
    conversation_key_hash,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryJobModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
    PersonModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "qq_ai_bot"


async def _seed_person_binding(
    database: Database,
    *,
    person_id: str,
    external_id: str,
    status: str = "active",
) -> None:
    async with database.sessions() as session, session.begin():
        if await session.get(CanonicalPersonModel, person_id) is None:
            session.add(
                CanonicalPersonModel(
                    id=person_id,
                    enabled=True,
                    revision=1,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=external_id,
                display_name="",
                status=status,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


async def _seed_space_binding(
    database: Database,
    *,
    space_id: str,
    external_id: str,
) -> None:
    async with database.sessions() as session, session.begin():
        if await session.get(CanonicalSpaceModel, space_id) is None:
            session.add(
                CanonicalSpaceModel(
                    id=space_id,
                    name="",
                    enabled=True,
                    autonomous_enabled=True,
                    require_mention=True,
                    revision=1,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
        session.add(
            SpaceBindingModel(
                id=str(uuid4()),
                space_id=space_id,
                platform=IDENTITY_PLATFORM,
                external_space_id=external_id,
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )


def _empty_dream_statistics() -> DreamPlanStatistics:
    return DreamPlanStatistics(
        eligible_facts=0,
        ready_facts=0,
        missing_embeddings=0,
        ambiguous_bot_facts=0,
        partitions=0,
        candidate_clusters=0,
        isolated_facts=0,
        estimated_model_calls=0,
    )


async def _create_person_fact(
    database: Database,
    *,
    user_id: str,
    content: str,
    memory_key: str,
) -> SimpleNamespace:
    async with database.sessions() as session, session.begin():
        row = await MemoryFactRepository(database).create_fact(
            _fact(content=content, memory_key=memory_key, user_id=user_id),
            normalized_content=content,
            supersedes_id=None,
            session=session,
        )
        return SimpleNamespace(
            id=row.id,
            canonical_subject_person_id=row.canonical_subject_person_id,
        )


def _empty_rebuild_statistics() -> MemoryRebuildPlanStatistics:
    return MemoryRebuildPlanStatistics(
        matched_events=0,
        eligible_events=0,
        already_processed=0,
        live_pending_processing=0,
        failed_live_jobs=0,
        private_events=0,
        group_events=0,
        input_characters=0,
        estimated_extraction_requests=0,
    )


@pytest.mark.asyncio
async def test_v1_resolvable_owner_fills_five_row_kinds_and_keeps_legacy_keys(
    database: Database,
) -> None:
    person_id = str(uuid4())
    space_id = str(uuid4())
    await _seed_person_binding(database, person_id=person_id, external_id="1001")
    await _seed_space_binding(database, space_id=space_id, external_id="2001")
    ledger = EventLedgerRepository(database)
    private = await _append_event(ledger, message_id="dw-private")
    group = await _append_event(ledger, message_id="dw-group", group_id="2001")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(private.id, "private:1001")
    assert await jobs.enqueue(group.id, "group:2001")

    mcp = MCPRepository(database)
    await mcp.record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=4,
        artifact_created=False,
        error_category=None,
        trigger_message_id="dw-private",
        bot_user_id="8000",
        result_excerpt="ok",
    )

    reflection = SelfReflectionRepository(database)
    await reflection.scan_new_events()
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="dw-reflect-in",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="继续聊",
        private_peer_user_id="1001",
    )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="dw-reflect-out",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="好",
        private_peer_user_id="1001",
        sender_is_bot=True,
    )
    assert await reflection.scan_new_events() == 2
    batches = await reflection.claim_due(
        scheduled_slot="2026-08-24:04",
        local_date="2026-08-24",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=1,
        max_sessions=3,
        max_daily_calls=9,
        max_events=20,
        max_characters=8000,
    )
    assert len(batches) == 1

    first = await _create_person_fact(
        database, user_id="1001", content="喜欢红茶", memory_key="likes:tea"
    )
    second = await _create_person_fact(
        database, user_id="1001", content="喜欢绿茶", memory_key="likes:green"
    )
    dreams = DreamRepository(database)
    run = await dreams.create_run(
        mode=DreamRunMode.INCREMENTAL,
        statistics=_empty_dream_statistics(),
        clusters=(
            (
                "cluster-a",
                "legacy-partition",
                "8000",
                "fact",
                (first.id, second.id),
                "fp-a",
            ),
        ),
        snapshot_max_fact_id=second.id,
        actor_user_id="1001",
        scheduled_slot=None,
    )

    async with database.sessions() as session:
        job_rows = list(await session.scalars(select(MemoryJobModel).order_by(MemoryJobModel.id)))
        assert {
            (row.conversation_key, row.canonical_person_id, row.canonical_space_id)
            for row in job_rows
        } == {
            ("private:1001", person_id, None),
            ("group:2001", None, space_id),
        }
        receipt = await session.scalar(select(MemoryToolReceiptModel))
        assert receipt is not None
        assert receipt.bot_user_id == "8000"
        assert receipt.conversation_key_hash == hashlib.sha256(b"private:1001").hexdigest()
        assert receipt.canonical_person_id == person_id
        assert receipt.canonical_space_id is None
        state = await session.scalar(select(MemorySelfReflectionStateModel))
        assert state is not None
        assert state.conversation_key_hash == conversation_key_hash(
            ScopeType.PRIVATE, group_id=None, private_peer_user_id="1001"
        )
        assert state.bot_user_id == "8000"
        assert state.canonical_person_id == person_id
        assert state.canonical_space_id is None
        reflection_run = await session.scalar(select(MemorySelfReflectionRunModel))
        assert reflection_run is not None
        assert reflection_run.conversation_key_hash == state.conversation_key_hash
        assert reflection_run.bot_user_id == "8000"
        assert reflection_run.canonical_person_id == person_id
        cluster = await session.scalar(select(MemoryDreamClusterModel))
        assert cluster is not None
        assert cluster.partition_key == "legacy-partition"
        assert cluster.bot_user_id == "8000"
        assert cluster.canonical_subject_person_id == first.canonical_subject_person_id == person_id
        assert cluster.canonical_subject_space_id is None
    assert run.public_id


@pytest.mark.asyncio
async def test_v1_missing_binding_leaves_owner_null_and_keeps_behavior(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    reset_v1_owner_unresolved_log_for_tests()
    caplog.set_level("INFO", logger="qq_ai_bot.identity.owner_dual_write")
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="dw-missing", user_id="5555")
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.identity.dual_write import _binding_for

        binding = await _binding_for(session, "5555")
        if binding is not None:
            await session.delete(binding)
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:5555")
    claimed = await jobs.claim(limit=5)
    assert [job.event_id for job in claimed] == [event.id]
    assert {job.conversation_key for job in claimed} == {"private:5555"}
    async with database.sessions() as session:
        row = await session.scalar(select(MemoryJobModel))
        assert row is not None
        assert row.conversation_key == "private:5555"
        assert row.canonical_person_id is None
        assert row.canonical_space_id is None
    assert any(MISSING_BINDING in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_v1_ambiguous_owner_leaves_null_and_does_not_pick_one(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    reset_v1_owner_unresolved_log_for_tests()
    caplog.set_level("INFO", logger="qq_ai_bot.identity.owner_dual_write")
    binding_person = str(uuid4())
    shadow_person = str(uuid4())
    await _seed_person_binding(database, person_id=binding_person, external_id="1001")
    await _seed_person_binding(database, person_id=shadow_person, external_id="1999")
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="dw-ambiguous")
    async with database.sessions() as session, session.begin():
        people = await session.get(PersonModel, "1001")
        assert people is not None
        people.canonical_person_id = shadow_person
    assert await MemoryJobRepository(database).enqueue(event.id, "private:1001")
    async with database.sessions() as session:
        row = await session.scalar(select(MemoryJobModel))
        assert row is not None
        assert row.canonical_person_id is None
        assert row.canonical_space_id is None
        assert row.conversation_key == "private:1001"
    assert any(AMBIGUOUS_OWNER in record.message for record in caplog.records)
    assert "1001" not in caplog.text
    assert "secret" not in caplog.text.casefold()


@pytest.mark.asyncio
async def test_v1_reflection_owner_collision_stays_legacy_and_logs_reason(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    reset_v1_owner_unresolved_log_for_tests()
    caplog.set_level("INFO", logger="qq_ai_bot.identity.owner_dual_write")
    person_id = str(uuid4())
    await _seed_person_binding(database, person_id=person_id, external_id="1001")
    await _seed_person_binding(database, person_id=person_id, external_id="1002")
    ledger = EventLedgerRepository(database)
    reflection = SelfReflectionRepository(database)
    await reflection.scan_new_events()
    for user_id, suffix in (("1001", "a"), ("1002", "b")):
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"dw-collide-{suffix}",
            scope_type=ScopeType.PRIVATE,
            sender_user_id=user_id,
            direction="inbound",
            content="还在",
            private_peer_user_id=user_id,
        )
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"dw-collide-out-{suffix}",
            scope_type=ScopeType.PRIVATE,
            sender_user_id="8000",
            direction="outbound",
            content="嗯",
            private_peer_user_id=user_id,
            sender_is_bot=True,
        )
    assert await reflection.scan_new_events() == 4
    batches = await reflection.claim_due(
        scheduled_slot="2026-08-24:12",
        local_date="2026-08-24",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=1,
        max_sessions=3,
        max_daily_calls=9,
        max_events=20,
        max_characters=8000,
    )
    assert len(batches) == 2
    hashes = {
        conversation_key_hash(ScopeType.PRIVATE, group_id=None, private_peer_user_id="1001"),
        conversation_key_hash(ScopeType.PRIVATE, group_id=None, private_peer_user_id="1002"),
    }
    assert {batch.state.conversation_key_hash for batch in batches} == hashes
    async with database.sessions() as session:
        states = list(await session.scalars(select(MemorySelfReflectionStateModel)))
        owned = [row for row in states if row.canonical_person_id == person_id]
        bare = [row for row in states if row.canonical_person_id is None]
        assert len(owned) == 1
        assert len(bare) == 1
        assert {row.bot_user_id for row in states} == {"8000"}
        assert {row.conversation_key_hash for row in states} == hashes
        runs = list(await session.scalars(select(MemorySelfReflectionRunModel)))
        assert len(runs) == 2
        assert {row.conversation_key_hash for row in runs} == hashes
        assert sum(1 for row in runs if row.canonical_person_id == person_id) == 1
        assert sum(1 for row in runs if row.canonical_person_id is None) == 1
    assert any(REFLECTION_OWNER_UNIQUE in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_v1_dream_mixed_source_leaves_owner_null(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    reset_v1_owner_unresolved_log_for_tests()
    caplog.set_level("INFO", logger="qq_ai_bot.identity.owner_dual_write")
    first_person = str(uuid4())
    second_person = str(uuid4())
    await _seed_person_binding(database, person_id=first_person, external_id="1001")
    await _seed_person_binding(database, person_id=second_person, external_id="1003")
    await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id="dw-dream-peer",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1003",
        direction="inbound",
        content="hi",
        private_peer_user_id="1003",
    )
    left = await _create_person_fact(
        database, user_id="1001", content="喜欢猫", memory_key="likes:cat"
    )
    right = await _create_person_fact(
        database, user_id="1003", content="喜欢狗", memory_key="likes:dog"
    )
    assert left.canonical_subject_person_id == first_person
    assert right.canonical_subject_person_id == second_person
    loaded = await MemoryFactRepository(database).get_fact(left.id)
    assert loaded is not None
    v1_identity = DreamCandidate(
        fact=loaded,
        bot_user_id="8000",
        vector=EmbeddingVector(values=(1.0, 0.0), dimensions=2),
        signature="sig",
    ).partition_identity
    assert v1_identity[0] == "8000"
    assert v1_identity[2] == "1001"
    dreams = DreamRepository(database)
    await dreams.create_run(
        mode=DreamRunMode.INCREMENTAL,
        statistics=_empty_dream_statistics(),
        clusters=(("mixed", "legacy-mixed", "8000", "fact", (left.id, right.id), "fp-m"),),
        snapshot_max_fact_id=right.id,
        actor_user_id="1001",
        scheduled_slot=None,
    )
    async with database.sessions() as session:
        cluster = await session.scalar(select(MemoryDreamClusterModel))
        assert cluster is not None
        assert cluster.partition_key == "legacy-mixed"
        assert cluster.bot_user_id == "8000"
        assert cluster.canonical_subject_person_id is None
        assert cluster.canonical_subject_space_id is None
        assert cluster.canonical_visibility_person_id is None
        assert cluster.canonical_visibility_space_id is None
    assert any(MIXED_DREAM_SOURCE in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_v1_rebuild_done_job_fills_owner_and_stays_off_live_queue(
    database: Database,
) -> None:
    person_id = str(uuid4())
    await _seed_person_binding(database, person_id=person_id, external_id="1001")
    event = await _append_event(EventLedgerRepository(database), message_id="dw-rebuild")
    rebuilds = MemoryRebuildRepository(database)
    selection = MemoryRebuildSelection(all_events=True)
    run = await rebuilds.create_run(
        selection=selection,
        selection_json=selection.model_dump_json(),
        selection_hash="s" * 64,
        snapshot_max_event_id=event.id,
        fingerprint="f" * 64,
        statistics=_empty_rebuild_statistics(),
        actor_user_id="9000",
    )
    await rebuilds.ensure_item(run.public_id, event_id=event.id, source_event_hash="e" * 64)
    assert await rebuilds.complete_item_receipts(run.public_id, include_failed_live_jobs=True) == 1
    async with database.sessions() as session:
        row = await session.scalar(select(MemoryJobModel))
        assert row is not None
        assert row.status == "done"
        assert row.processing_source == MemoryProcessingSource.REBUILD.value
        assert row.conversation_key == f"rebuild:{run.public_id}"
        assert row.canonical_person_id == person_id
        assert row.canonical_space_id is None
    claimed = await MemoryJobRepository(database).claim(limit=10)
    assert claimed == ()
    batched = await MemoryJobRepository(database).claim_ready_batch(
        limit=10,
        trigger_count=1,
        max_characters=1000,
        max_wait_seconds=0,
    )
    assert batched == ()


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _is_complete_v2_test(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Name) and expr.id == "complete_v2":
        return True
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return False
    if isinstance(expr, ast.Await):
        return _is_complete_v2_test(expr.value)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        return expr.func.id == "identity_runtime_is_complete_v2"
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        return expr.func.attr == "identity_runtime_is_complete_v2"
    return False


def _is_not_complete_v2_test(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.UnaryOp)
        and isinstance(expr.op, ast.Not)
        and _is_complete_v2_test(expr.operand)
    )


def _v1_blocks(node: ast.AST) -> list[ast.AST]:
    blocks: list[ast.AST] = []

    class Visitor(ast.NodeVisitor):
        def visit_If(self, item: ast.If) -> None:
            if _is_not_complete_v2_test(item.test):
                blocks.extend(item.body)
            elif _is_complete_v2_test(item.test):
                blocks.extend(item.orelse)
            self.generic_visit(item)

    Visitor().visit(node)
    return blocks


def _uses_legacy_identity(block: ast.AST) -> bool:
    names: set[str] = set()
    attrs: set[str] = set()
    for child in ast.walk(block):
        if isinstance(child, ast.Name):
            names.add(child.id)
        if isinstance(child, ast.Attribute):
            attrs.add(child.attr)
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            names.add(child.value)
    return bool(
        names & {"conversation_key", "conversation_key_hash", "key_hash", "stored_key"}
        or attrs
        & {
            "conversation_key",
            "conversation_key_hash",
            "bot_user_id",
            "group_id",
            "private_peer_user_id",
        }
    )


def _v1_filter_uses_owner_column(block: ast.AST) -> bool:
    for child in ast.walk(block):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.attr if isinstance(func, ast.Attribute) else ""
        if name not in {"where", "group_by", "on_conflict_do_nothing"}:
            continue
        for arg in (*child.args, *(item.value for item in child.keywords)):
            for node in ast.walk(arg):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "canonical_person_id",
                    "canonical_space_id",
                }:
                    return True
                if isinstance(node, ast.Constant) and node.value in {
                    "canonical_person_id",
                    "canonical_space_id",
                }:
                    return True
    return False


def test_v1_dual_write_ast_keeps_legacy_read_where_group_and_conflict() -> None:
    reflection = ast.parse(
        (_SRC / "memory" / "self_reflection" / "repository.py").read_text(encoding="utf-8")
    )
    jobs = ast.parse((_SRC / "memory" / "repository.py").read_text(encoding="utf-8"))
    failures: list[str] = []
    for tree, name in (
        (reflection, "scan_new_events"),
        (reflection, "claim_due"),
        (reflection, "_apply_event_scope"),
        (reflection, "_run_conflict_target"),
        (jobs, "enqueue_resolved"),
        (jobs, "claim_ready_batch"),
    ):
        func = _function(tree, name)
        blocks = _v1_blocks(func)
        assert blocks, f"{name} missing v1 branch"
        if not any(_uses_legacy_identity(block) for block in blocks):
            failures.append(f"{name} v1 branch dropped legacy identity keys")
        if any(_v1_filter_uses_owner_column(block) for block in blocks):
            failures.append(f"{name} v1 where/group/conflict uses owner columns")
    claim = _function(jobs, "claim")
    if any(_v1_filter_uses_owner_column(block) for block in _v1_blocks(claim)):
        failures.append("claim v1 where/group/conflict uses owner columns")
    target = _function(reflection, "_run_conflict_target")
    source = ast.unparse(target)
    assert "conversation_key_hash" in source
    assert "bot_user_id" in source
    assert "scheduled_slot" in source
    dual = (_SRC / "identity" / "owner_dual_write.py").read_text(encoding="utf-8")
    assert "_ensure_person" not in dual
    assert "_ensure_group" not in dual
    assert "MembershipModel" not in dual
    assert "from sqlalchemy.exc import IntegrityError" not in dual
    assert "except IntegrityError" not in dual
    assert not failures, "\n".join(failures)
