"""Paired local SQLite/real Main load; fake model and in-memory platform only.

Run the same file with each --checkout and the same arguments in alternating
order. Imports must come from that checkout; no network or live Bot is started.
The fixture helpers only create an identical logical deployment before timing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path


def distribution(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "mean_ms": statistics.mean(ordered) * 1000,
        "p50_ms": ordered[(len(ordered) - 1) // 2] * 1000,
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000,
        "max_ms": ordered[-1] * 1000,
    }


def reader(database):
    # The same script runs against the frozen old public session API.
    return database.read_session() if hasattr(database, "read_session") else database.sessions()


def checkout_metadata(checkout):
    return {
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "head": subprocess.check_output(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
        ).strip(),
        "dirty_source_paths": subprocess.check_output(
            ["git", "-C", str(checkout), "diff", "--name-only", "HEAD", "--", "src", "tests"],
            text=True,
        ).splitlines(),
    }


def verify_imports(checkout):
    paths = [
        Path(module.__file__).resolve()
        for name, module in tuple(sys.modules.items())
        if name.split(".", 1)[0] in {"yuki", "tests"} and getattr(module, "__file__", None)
    ]
    assert all(path.is_relative_to(checkout) for path in paths)
    return len(paths)


async def prepare_memory_load(database, args):
    from sqlalchemy import insert, select
    from tests.conftest import make_settings
    from tests.support.canonical_identity import ensure_person
    from tests.support.model_executor import InjectedModelExecutor
    from tests.unit.test_memory_mutation import _context, _event, _service

    from yuki.llm.fake import FakeLLMProvider
    from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate
    from yuki.memory.mutation.models import MemoryMutationRequest
    from yuki.memory.self_reflection.control import ReflectionControlRepository
    from yuki.memory.self_reflection.repository import SelfReflectionRepository
    from yuki.memory.self_reflection.service import (
        _CHECKPOINT,
        ReflectionCheckpoint,
        SelfReflectionService,
    )
    from yuki.persistence.models import (
        MemoryEvidenceModel,
        MemoryMutationReceiptModel,
        MemorySelfReflectionRunModel,
    )

    mutations, facts, ledger, _ = _service(database, self_memory_enabled=True)
    async with database.immediate_session() as writer:
        await ensure_person(writer, "70001")
        await ensure_person(writer, "70002")
    sources = [
        await _event(
            ledger,
            message_id=f"evidence-{index}",
            sender_user_id="70001",
            content=f"source {index} " + "material " * max(1, args.payload_chars // 9),
        )
        for index in range(args.scale)
    ]
    original = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="70001",
            memory_key="benchmark:source",
            category="benchmark",
            content="original fact",
            source_type="automatic",
        ),
        evidence=MemoryEvidenceCreate(
            event_id=sources[0].id,
            source_speaker_user_id="70001",
            relation="self_statement",
            authority="self_report",
            excerpt=sources[0].content,
        ),
    )
    if len(sources) > 1:
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(MemoryEvidenceModel),
                [
                    dict(
                        fact_id=original.id,
                        event_id=source.id,
                        tool_receipt_id=None,
                        source_speaker_user_id="70001",
                        relation="confirmation",
                        confidence=1,
                        authority="self_report",
                        excerpt=source.content,
                        created_at=datetime.now(UTC),
                    )
                    for source in sources[1:]
                ],
            )
    reflection = SelfReflectionRepository(database)
    await reflection.scan_new_events()
    paid_source = await _event(
        ledger,
        message_id="paid-source",
        sender_user_id="70002",
        content="我重视认真查证。",
    )
    await reflection.scan_new_events()
    (batch,) = await reflection.claim_due(
        scheduled_slot="benchmark-paid",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=10,
        max_characters=16000,
        force=True,
    )
    output = {
        "proposals": [
            dict(
                category="self_preference",
                kind="preference",
                memory_key="self:bench",
                content="我重视认真查证。",
                importance=3,
                confidence=0.8,
                evidence_refs=["event_1"],
                visibility="current_scope",
            )
        ],
        "episodes": [],
    }
    await reflection.save_checkpoint(
        batch.run_id,
        _CHECKPOINT.dump_json(ReflectionCheckpoint(output, {"event_1": paid_source}, {})).decode(),
    )
    settings = make_settings(database.url, self_memory_enabled=True)
    control = ReflectionControlRepository(database, settings)
    request_id = await control.reserve_request(batch.run_id)
    await control.finish_request(request_id, "completed", 8)
    provider = FakeLLMProvider(lambda _: (_ for _ in ()).throw(AssertionError("paid model replay")))
    models = InjectedModelExecutor(provider)
    service = SelfReflectionService(
        settings=settings, repository=reflection, mutations=mutations, models=models
    )
    request = MemoryMutationRequest(
        operation="correct",
        fact_id=original.id,
        new_content="corrected fact",
        evidence_quote=sources[0].content,
    )
    state = {}

    async def run():
        state["mutation"] = await mutations.mutate(request, _context(sources[0]))
        assert state["mutation"].ok
        proposals, committed = await service.reflect(batch)
        await reflection.complete(batch, proposals=proposals, committed=committed)

    async def verify():
        mutation = state["mutation"]
        assert mutation.old_fact_id == original.id and mutation.new_fact_id != original.id
        assert (await facts.get_fact(original.id)).status.value == "superseded"
        replacement = await facts.get_fact(mutation.new_fact_id)
        assert replacement.content == "corrected fact"
        assert {
            item.event_id
            for item in await facts.repository.list_evidence(replacement.id, limit=None)
        } == {source.id for source in sources}
        async with reader(database) as session:
            receipt = await session.scalar(
                select(MemoryMutationReceiptModel).where(
                    MemoryMutationReceiptModel.mutation_id == mutation.mutation_id
                )
            )
            saved = await session.get(MemorySelfReflectionRunModel, batch.run_id)
            assert receipt.new_fact_id == replacement.id
            assert saved.status == "completed" and saved.committed_count == 1
        assert not provider.requests
        assert (await control.snapshot())["calls_today"] == 1
        return {
            "copied_evidence": len(sources),
            "original_fact_id": original.id,
            "new_fact_id": replacement.id,
            "original_paid_run_id": batch.run_id,
            "paid_model_requests_before": 1,
            "paid_model_requests_after": 1,
            "new_paid_provider_calls": 0,
        }

    return run, verify, {"evidence_rows": args.scale, "evidence_payload_chars": args.payload_chars}


async def prepare_people_load(database, args):
    from sqlalchemy import insert, select, text, update
    from tests.support.canonical_identity import ensure_person, ensure_space
    from tests.unit.test_memory_mutation import _event, _service

    from yuki.identity import person_identity
    from yuki.identity.db_models import CanonicalPersonModel
    from yuki.memory.models import MemoryFactCreate
    from yuki.persistence.models import (
        AdminOperationEventModel,
        ChatEventModel,
        RuntimeConfigOverrideModel,
    )
    from yuki.persistence.people_repository import PeopleRepository

    _mutations, facts, ledger, _ = _service(database)
    async with database.immediate_session() as writer:
        family = [await ensure_person(writer, str(71000 + index)) for index in range(args.family)]
        target = await ensure_person(writer, "72000")
        await ensure_person(writer, "73000")
        await ensure_space(writer, "74000")
    sources, references, original_facts = [], [], []
    for index in range(args.scale):
        sources.append(
            await _event(
                ledger,
                message_id=f"owned-{index}",
                sender_user_id=str(71000 + index % args.family),
                content="original private material " + "x" * args.payload_chars,
            )
        )
        references.append(
            await _event(
                ledger,
                message_id=f"reference-{index}",
                sender_user_id="73000",
                group_id="74000",
                content="reference " + "x" * args.payload_chars,
                mentioned_user_ids=(str(71000 + index % args.family),),
            )
        )
        original_facts.append(
            await facts.remember(
                MemoryFactCreate(
                    scope_type="person",
                    subject_user_id=str(71000 + index % args.family),
                    memory_key=f"benchmark:{index}",
                    category="benchmark",
                    content=f"fact {index}",
                    source_type="explicit",
                )
            )
        )
    now = datetime.now(UTC)
    configs = []
    for index in range(args.scale):
        for owner in [family[0], target] if index % 2 == 0 else [family[0]]:
            configs.append(
                dict(
                    config_key=f"benchmark.key.{index}",
                    scope_type="user",
                    canonical_person_id=owner,
                    canonical_space_id=None,
                    value_json=json.dumps("x" * args.payload_chars),
                    value_type="string",
                    apply_mode="hot",
                    version=1,
                    created_at=now,
                    updated_at=now,
                    updated_by="benchmark",
                )
            )
    async with database.immediate_session() as writer:
        if len(family) > 1:
            await writer.execute(
                update(CanonicalPersonModel)
                .where(CanonicalPersonModel.id.in_(family[1:]))
                .values(merged_into_person_id=family[0])
            )
        await writer.execute(insert(RuntimeConfigOverrideModel), configs)
        await writer.execute(
            insert(AdminOperationEventModel),
            [
                dict(
                    actor_user_id="73000",
                    capability="benchmark.source",
                    operation="benchmark.source",
                    target_type="person",
                    target_id=family[0],
                    before_json=json.dumps(
                        {
                            "person_id": family[0],
                            "account": "71000",
                            "text": "x" * args.payload_chars,
                        }
                    ),
                    after_json="{}",
                    success=True,
                    duration_seconds=0,
                    created_at=now,
                )
                for _ in range(args.scale)
            ],
        )
    merge = person_identity.merge_persons
    prepared_api = "prepared" in inspect.signature(merge).parameters

    async def run():
        if args.scenario == "forget":
            assert await PeopleRepository(database).delete_person_id(family[0])
        elif prepared_api:
            async with reader(database) as session:
                await session.execute(text("BEGIN"))
                prepared = await person_identity.prepare_person_merge(session, family[0], target)
            async with database.immediate_session() as writer:
                assert await merge(writer, family[0], target, prepared=prepared) == target
        else:

            async def apply(session):
                return await merge(session, family[0], target)

            assert await database.apply_snapshot_write(apply) == target

    async def verify():
        async with reader(database) as session:
            originals = tuple(
                await session.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.id.in_(tuple(event.id for event in sources))
                    )
                )
            )
            preserved = tuple(
                await session.scalars(
                    select(ChatEventModel.id).where(
                        ChatEventModel.id.in_(tuple(event.id for event in references))
                    )
                )
            )
            people = tuple(
                await session.scalars(
                    select(CanonicalPersonModel.id).where(CanonicalPersonModel.id.in_(family))
                )
            )
            if args.scenario == "forget":
                assert not people and not originals
                assert all(
                    [
                        await facts.repository.get_fact(fact.id, session=session) is None
                        for fact in original_facts
                    ]
                )
            else:
                assert set(people) == set(family)
                assert {row.id: row.author_person_id for row in originals} == {
                    event.id: event.author_person_id for event in sources
                }
                assert await person_identity.current_person_id(session, family[0]) == target
                moved = tuple(
                    await session.scalars(
                        select(RuntimeConfigOverrideModel).where(
                            RuntimeConfigOverrideModel.canonical_person_id == target
                        )
                    )
                )
                assert len(moved) == args.scale
                audits = tuple(
                    await session.scalars(
                        select(AdminOperationEventModel.id).where(
                            AdminOperationEventModel.operation == "identity.person.merge"
                        )
                    )
                )
                assert len(audits) == 1
            assert set(preserved) == {event.id for event in references}
        return {
            "original_family": len(family),
            "original_source_events": len(sources),
            "preserved_reference_ids": len(preserved),
            "config_rows_before": len(configs),
            "verified_original_fact_ids": len(original_facts),
        }

    return (
        run,
        verify,
        {
            "family_members": args.family,
            "owned_events": args.scale,
            "reference_events": args.scale,
            "facts": args.scale,
            "config_rows": len(configs),
            "audit_payload_rows": args.scale,
            "payload_chars": args.payload_chars,
        },
    )


async def prepare_gc_load(database, root, env, args):
    import io

    from PIL import Image
    from sqlalchemy import delete, select, update
    from tests.support.canonical_identity import ensure_person
    from tests.unit.test_memory_mutation import _event

    from yuki.persistence.models import ChatEventModel
    from yuki.persistence.repositories import EventLedgerRepository
    from yuki.plugin_host.db_models import PluginMediaArtifactModel, PluginNotificationOutboxModel
    from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
    from yuki.plugin_host.repository import PluginInstallationRepository
    from yuki.runtime.protocol_schema import objects, refs
    from yuki.runtime.protocol_store import ProtocolStore
    from yuki.runtime.work_repository import WorkRepository
    from yuki.runtime.work_schema_v1 import work
    from yuki_plugin_sdk.api import PLUGIN_API_VERSION

    async with database.immediate_session() as writer:
        await ensure_person(writer, "75000")
    source = await _event(
        EventLedgerRepository(database),
        message_id="gc-work-source",
        sender_user_id="75000",
        content="retain original work tree",
    )
    works = WorkRepository(database)
    lease = await works.acquire(source.canonical_conversation_id, 1, seconds=300)
    assert lease is not None
    parent = await works.accept(
        lease,
        source_key="benchmark:root",
        source={"trigger_event_id": source.id},
        goal="original root",
    )
    identities = [parent["id"]]
    protocol = ProtocolStore(database)
    digests, retained = [], []
    for index in range(args.scale):
        child = await works.derive(
            lease, parent["id"], f"benchmark:child:{index}", f"child {index}"
        )
        identities.append(child["id"])
        digest = await protocol.put({"source": index, "payload": "x" * args.payload_chars})
        digests.append(digest)
        async with protocol.publication(child["id"]) as prepared:
            async with database.immediate_session() as writer:
                await protocol.publish_refs(writer, child["id"], prepared)
        if index % 2 == 0:
            retained.append(digest)
    plugin = "benchmark.gc"
    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id=plugin,
        name="Benchmark",
        version="1",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3",
        manifest_hash="b" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=(),
    )
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, "PNG")
    content = buffer.getvalue() + b"x" * args.payload_chars
    store = PluginMediaArtifactStore(database, root / "gc-plugin-media")
    handles = [
        await store.create(
            plugin_id=plugin,
            data=content,
            content_type="image/png",
            filename=f"original-{index}.png",
            ttl_seconds=60,
            storage_mb=max(1, math.ceil(args.scale * len(content) / 1048576) + 1),
        )
        for index in range(args.scale)
    ]
    paths = {
        handle.handle_id: (
            await store.resolve(plugin_id=plugin, handle_id=handle.handle_id)
        ).local_path
        for handle in handles
    }
    protected = {handle.handle_id for index, handle in enumerate(handles) if index % 2 == 0}
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        await writer.execute(delete(refs).where(refs.c.sha256.in_(set(digests) - set(retained))))
        await writer.execute(
            update(objects).where(objects.c.sha256.in_(digests)).values(prepared_at=0)
        )
        await writer.execute(
            update(PluginMediaArtifactModel)
            .where(PluginMediaArtifactModel.handle_id.in_(paths))
            .values(expires_at=now - timedelta(days=1))
        )
        event_id = await writer.scalar(
            select(ChatEventModel.id)
            .where(ChatEventModel.canonical_conversation_id == env.context.conversation_id)
            .limit(1)
        )
        writer.add_all(
            [
                PluginNotificationOutboxModel(
                    notification_id=f"benchmark-{index}",
                    part_key="original-media",
                    source_event_id=event_id,
                    plugin_id=plugin,
                    source_person_id=env.person,
                    target_type="group",
                    target_id=env.space,
                    bot_user_id=env.bot.self_id,
                    part_type="media",
                    media_handle_id=handle,
                    status="pending",
                    canonical_target_space_id=env.space,
                    canonical_presence_id=env.presence,
                    canonical_conversation_id=env.context.conversation_id,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
                for index, handle in enumerate(sorted(protected))
            ]
        )
    removed = {}

    async def run():
        removed["artifacts"] = await store.cleanup()
        total = 0
        # The real collector releases its writer between pages. Drain the known
        # finite unreferenced fixture, preserving all original live references.
        for _ in range(args.scale + 1):
            count = await protocol.cleanup(grace_seconds=0)
            total += count
            if total == len(digests) - len(retained):
                break
            await asyncio.sleep(0)
        removed["protocol"] = total
        assert await works.renew(lease, seconds=300)

    async def verify():
        assert removed == {
            "artifacts": len(handles) - len(protected),
            "protocol": len(digests) - len(retained),
        }
        async with reader(database) as session:
            remaining_media = set(
                await session.scalars(
                    select(PluginMediaArtifactModel.handle_id).where(
                        PluginMediaArtifactModel.plugin_id == plugin
                    )
                )
            )
            remaining_objects = set(
                await session.scalars(select(objects.c.sha256).where(objects.c.sha256.in_(digests)))
            )
            remaining_work = set(
                await session.scalars(select(work.c.id).where(work.c.id.in_(identities)))
            )
        assert remaining_media == protected and remaining_objects == set(retained)
        assert remaining_work == set(identities)
        for handle, path in paths.items():
            assert path.exists() is (handle in protected)
        for digest in digests:
            assert protocol._path(digest).exists() is (digest in retained)
        await works.release(lease)
        return {
            **removed,
            "preserved_artifact_references": len(protected),
            "preserved_protocol_references": len(retained),
            "original_work_ids": len(identities),
        }

    return (
        run,
        verify,
        {
            "protocol_objects": args.scale,
            "media_artifacts": args.scale,
            "work_tree_rows": len(identities),
            "payload_chars": args.payload_chars,
        },
    )


async def main(args: argparse.Namespace) -> dict[str, object]:
    checkout = args.checkout.resolve()
    sys.path[:0] = [str(checkout / "src"), str(checkout)]
    os.chdir(checkout)
    os.environ["SOUL_FILE"] = str(checkout / "config/SOUL.md")
    from sqlalchemy import event, select
    from sqlalchemy.orm import Session
    from tests.conftest import _seed_database
    from tests.integration.test_current_material_main_contract import _deployment
    from tests.unit.test_memory_mutation import _event
    from tests.unit.test_self_initiative_memory import seed

    import yuki.persistence.database as database_module
    from yuki.conversation.ordinary_admission_db_models import OrdinaryTurnAdmissionModel
    from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
    from yuki.llm.fake import FakeLLMProvider
    from yuki.memory.self_reflection.repository import SelfReflectionRepository
    from yuki.memory.self_reflection.service import _CHECKPOINT, ReflectionCheckpoint
    from yuki.persistence.database import Database
    from yuki.persistence.models import ChatEventModel, MemorySelfReflectionRunModel
    from yuki.runtime.work_schema_v1 import effects, inputs, work
    from yuki.social.db_models import SocialOperationModel

    source = await asyncio.to_thread(Path(database_module.__file__).resolve)
    assert source.is_relative_to(checkout), source
    args.scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sw-", dir=args.scratch) as temporary:
        root = Path(temporary)
        database = Database(f"sqlite+aiosqlite:///{root / 'load.db'}")
        await database.create_schema()
        await _seed_database(database)
        waits: list[float] = []
        scopes: list[float] = []
        acquired_to_end: list[float] = []
        sql_times: dict[str, list[float]] = {}
        sql_errors: dict[str, int] = {}
        measuring = False

        def began(session, transaction, connection):
            if measuring and transaction.parent is None:
                connection.info["benchmark_session"] = session
                session.info["benchmark_connection_info"] = connection.info

        def sql_started(connection, cursor, statement, parameters, context, many):
            if measuring:
                context.benchmark_started = time.perf_counter()

        def acquired(connection, cursor, statement, parameters, context, many):
            if not measuring:
                return
            command = statement.lstrip().upper()
            kind = command.split(None, 1)[0] if command else "EMPTY"
            sql_times.setdefault(kind, []).append(time.perf_counter() - context.benchmark_started)
            if command.startswith(
                ("BEGIN IMMEDIATE", "INSERT ", "UPDATE ", "DELETE ", "REPLACE ")
            ) or any(getattr(context, key, False) for key in ("isinsert", "isupdate", "isdelete")):
                session = connection.info.get("benchmark_session")
                if session is not None:
                    session.info.setdefault("benchmark_acquired", time.perf_counter())

        def sql_failed(context):
            if measuring:
                error = context.original_exception
                name = getattr(error, "sqlite_errorname", type(error).__name__)
                sql_errors[name] = sql_errors.get(name, 0) + 1

        def ended(session, transaction):
            if transaction.parent is None:
                acquired_at = session.info.pop("benchmark_acquired", None)
                info = session.info.pop("benchmark_connection_info", None)
                if info is not None:
                    info.pop("benchmark_session", None)
                if acquired_at is not None:
                    acquired_to_end.append(time.perf_counter() - acquired_at)

        event.listen(Session, "after_begin", began)
        event.listen(Session, "after_transaction_end", ended)
        event.listen(database.engine.sync_engine, "before_cursor_execute", sql_started)
        event.listen(database.engine.sync_engine, "after_cursor_execute", acquired)
        event.listen(database.engine.sync_engine, "handle_error", sql_failed)
        immediate = database.immediate_session

        @asynccontextmanager
        async def measured_writer():
            start = time.perf_counter()
            async with immediate() as writer:
                entered = time.perf_counter()
                if measuring:
                    waits.append(entered - start)
                yield writer
            if measuring:
                scopes.append(time.perf_counter() - entered)

        database.immediate_session = measured_writer
        result: dict[str, object] = {
            "checkout": str(checkout),
            "source": str(source),
            "scenario": args.scenario,
            **await asyncio.to_thread(checkout_metadata, checkout),
            "python": sys.version,
            "dataset": "synthetic logical fixture; setup and post-run verification are untimed",
            "model_transport": "fixed FakeLLMProvider and in-memory platform; no real requests",
        }
        app = None
        try:
            repository = SelfReflectionRepository(database)
            if args.scenario == "tail":
                mutations, _, _, _ = await seed(database)
                await repository.scan_new_events()
                await _event(
                    mutations._ledger,
                    message_id="first",
                    sender_user_id="1001",
                    content="completed range",
                    group_id="3001",
                )
                await repository.scan_new_events()
                (batch,) = await repository.claim_due(
                    scheduled_slot="tail",
                    event_threshold=1,
                    character_threshold=1,
                    max_wait_seconds=0,
                    max_events=1,
                    max_characters=16000,
                )
                for index in range(args.tail):
                    await _event(
                        mutations._ledger,
                        message_id=f"tail-{index}",
                        sender_user_id="1001",
                        content=f"source {index}: " + "conversation material " * 80,
                        group_id="3001",
                    )
                await repository.scan_new_events()
                async with immediate() as writer:
                    run = await writer.get(MemorySelfReflectionRunModel, batch.run_id)
                    run.checkpoint_json = '{"completed_counts":[0,0]}'
                waits.clear()
                scopes.clear()
                acquired_to_end.clear()
                started = time.perf_counter()
                cpu_started = time.process_time()
                measuring = True
                recovered = await repository.recover_stale_runs(started_before=datetime.now(UTC))
                elapsed = time.perf_counter() - started
                cpu_elapsed = time.process_time() - cpu_started
                measuring = False
                assert recovered == 1
                result.update(
                    tail_events=args.tail,
                    recovery_ms=elapsed * 1000,
                    process_cpu_seconds=cpu_elapsed,
                )
            else:

                def respond(request):
                    if request.messages[-1].role == "tool":
                        return "NO_REPLY"
                    return ChatResponse(
                        content="",
                        latency_seconds=0,
                        tool_calls=(
                            ToolCall(
                                "fixed-send",
                                ToolFunction("send", '{"text":"local benchmark reply"}'),
                            ),
                        ),
                    )

                provider = FakeLLMProvider(respond)
                app, environment, message, sender = await _deployment(
                    database,
                    root,
                    provider,
                    per_user_requests_per_minute=100000,
                    per_group_requests_per_minute=100000,
                    global_llm_concurrency=16,
                )
                await app._freeze_main_manifest()
                await repository.scan_new_events()
                # Warm the same real Main entry before measuring arrivals.
                await app.processor.handle(replace(message, message_id="warmup"), sender)
                await app.work_scheduler.drain_once()
                warmup_calls = len(provider.requests)
                domain_run = domain_verify = None
                if args.scenario == "memory":
                    domain_run, domain_verify, shape = await prepare_memory_load(database, args)
                    result["logical_load"] = shape
                elif args.scenario in {"forget", "merge"}:
                    domain_run, domain_verify, shape = await prepare_people_load(database, args)
                    result["logical_load"] = shape
                elif args.scenario == "gc":
                    domain_run, domain_verify, shape = await prepare_gc_load(
                        database, root, environment, args
                    )
                    result["logical_load"] = shape
                work_repository = work_lease = None
                if args.scenario == "mixed":
                    from tests.support.canonical_identity import ensure_person

                    from yuki.persistence.repositories import EventLedgerRepository
                    from yuki.runtime.work_repository import WorkRepository

                    async with database.immediate_session() as writer:
                        await ensure_person(writer, "76000")
                    work_source = await _event(
                        EventLedgerRepository(database),
                        message_id="renew-source",
                        sender_user_id="76000",
                        content="original heartbeat source",
                    )
                    work_repository = WorkRepository(database)
                    work_lease = await work_repository.acquire(
                        work_source.canonical_conversation_id, 1, seconds=300
                    )
                    assert work_lease is not None
                    work_row = await work_repository.accept(
                        work_lease,
                        source_key="benchmark:heartbeat",
                        source={"trigger_event_id": work_source.id},
                        goal="retain heartbeat",
                    )
                    result["logical_load"] = {
                        "reflection_arrivals": args.background,
                        "work_renew_arrivals": args.background,
                    }
                waits.clear()
                scopes.clear()
                acquired_to_end.clear()
                latencies: list[float] = []
                handler_results: list[dict[str, object]] = []
                completions: list[float] = []
                background_latencies: list[float] = []
                reflection_receipts: list[dict[str, object]] = []
                workload_errors: list[dict[str, object]] = []
                domain_latencies: list[float] = []
                domain_completion: list[float] = []
                renew_latencies: list[float] = []
                loop_lag: list[float] = []
                stop_probe = asyncio.Event()
                started = time.perf_counter()
                cpu_started = time.process_time()
                measuring = True

                async def loop_probe():
                    while not stop_probe.is_set():
                        due = time.perf_counter() + 0.01
                        await asyncio.sleep(0.01)
                        loop_lag.append(max(0, time.perf_counter() - due))

                async def chat(index):
                    due = started + index * args.interval
                    while (remaining := due - time.perf_counter()) > 0:  # noqa: ASYNC110
                        await asyncio.sleep(remaining)
                    begin = time.perf_counter()
                    try:
                        handled = await app.processor.handle(
                            replace(
                                message,
                                message_id=f"measured-{index}",
                                text=f"CURRENT_CHAT_INSTRUCTION arrival {index}",
                            ),
                            sender,
                        )
                        handler_results.append(
                            {
                                "index": index,
                                "reason": handled.reason,
                                "sent_messages": handled.sent_messages,
                            }
                        )
                    except Exception as exc:
                        workload_errors.append(
                            {"kind": "chat", "index": index, "category": type(exc).__name__}
                        )
                    end = time.perf_counter()
                    latencies.append(end - begin)
                    completions.append(end - due)

                async def reflection(index):
                    due = started + index * args.background_interval
                    while (remaining := due - time.perf_counter()) > 0:  # noqa: ASYNC110
                        await asyncio.sleep(remaining)
                    begin = time.perf_counter()
                    try:
                        await repository.scan_new_events()
                        batches = await repository.claim_due(
                            scheduled_slot=f"load-{index}",
                            event_threshold=1,
                            character_threshold=1,
                            max_wait_seconds=0,
                            max_events=100,
                            max_characters=16000,
                        )
                        for batch in batches:
                            raw = _CHECKPOINT.dump_json(
                                ReflectionCheckpoint(
                                    {"proposals": [], "episodes": []},
                                    {
                                        f"event_{n}": event
                                        for n, event in enumerate(batch.events, 1)
                                    },
                                    {},
                                )
                            ).decode()
                            await repository.save_checkpoint(batch.run_id, raw)
                            await repository.complete(batch, proposals=0, committed=0)
                            reflection_receipts.append(
                                {
                                    "run_id": batch.run_id,
                                    "source_event_ids": [event.id for event in batch.events],
                                }
                            )
                    except Exception as exc:
                        workload_errors.append(
                            {"kind": "reflection", "index": index, "category": type(exc).__name__}
                        )
                    background_latencies.append(time.perf_counter() - begin)

                async def domain():
                    due = started + args.domain_delay
                    await asyncio.sleep(max(0, due - time.perf_counter()))
                    begin = time.perf_counter()
                    try:
                        await domain_run()
                    except Exception as exc:
                        workload_errors.append(
                            {"kind": args.scenario, "category": type(exc).__name__}
                        )
                    domain_latencies.append(time.perf_counter() - begin)
                    domain_completion.append(time.perf_counter() - due)

                async def renew(index):
                    due = started + index * args.background_interval
                    await asyncio.sleep(max(0, due - time.perf_counter()))
                    begin = time.perf_counter()
                    try:
                        assert await work_repository.renew(work_lease, seconds=300)
                    except Exception as exc:
                        workload_errors.append(
                            {"kind": "renew", "index": index, "category": type(exc).__name__}
                        )
                    renew_latencies.append(time.perf_counter() - begin)

                jobs = [chat(index) for index in range(args.messages)]
                if args.scenario == "mixed":
                    jobs += [reflection(index) for index in range(args.background)]
                    jobs += [renew(index) for index in range(args.background)]
                if domain_run is not None:
                    jobs.append(domain())
                probe = asyncio.create_task(loop_probe())
                try:
                    await asyncio.gather(*jobs)
                    # The real Main may merge arrivals into an existing Work. Join
                    # those original producers and consume their persisted inputs.
                    for _ in range(args.messages + 1):
                        async with reader(database) as snapshot:
                            pending = tuple(
                                await snapshot.scalars(
                                    select(inputs.c.id)
                                    .join(ChatEventModel, inputs.c.event_id == ChatEventModel.id)
                                    .where(
                                        ChatEventModel.platform_message_id.like("measured-%"),
                                        inputs.c.state.in_(("pending", "staged")),
                                    )
                                )
                            )
                        if not pending:
                            break
                        await app.work_scheduler.drain_once()
                    assert not pending, pending
                    elapsed = time.perf_counter() - started
                    cpu_elapsed = time.process_time() - cpu_started
                finally:
                    stop_probe.set()
                    await probe
                measuring = False
                result["workload_errors"] = workload_errors
                if domain_verify is not None and not workload_errors:
                    result["domain_verified"] = await domain_verify()
                if work_repository is not None:
                    assert (await work_repository.get(work_row["id"]))["id"] == work_row["id"]
                    assert await work_repository.valid(work_lease)
                    await work_repository.release(work_lease)
                # Actual input coverage and original delivery receipts, rather
                # than an assumed one-send-per-arrival ratio.
                reader_factory = (
                    database.read_session
                    if hasattr(database, "read_session")
                    else database.sessions
                )
                async with reader_factory() as snapshot:
                    incoming = (
                        (
                            await snapshot.execute(
                                select(
                                    ChatEventModel.id,
                                    ChatEventModel.platform_message_id,
                                    ChatEventModel.content,
                                    ChatEventModel.canonical_conversation_id,
                                ).where(
                                    ChatEventModel.platform_message_id.like("measured-%"),
                                    ChatEventModel.direction == "inbound",
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    sent = tuple(
                        await snapshot.scalars(
                            select(ChatEventModel.id).where(
                                ChatEventModel.content == "local benchmark reply",
                                ChatEventModel.direction == "outbound",
                            )
                        )
                    )
                    admissions = (
                        (
                            await snapshot.execute(
                                select(
                                    OrdinaryTurnAdmissionModel.event_id,
                                    OrdinaryTurnAdmissionModel.route,
                                    OrdinaryTurnAdmissionModel.work_id,
                                    OrdinaryTurnAdmissionModel.input_id,
                                )
                                .join(
                                    ChatEventModel,
                                    OrdinaryTurnAdmissionModel.event_id == ChatEventModel.id,
                                )
                                .where(ChatEventModel.platform_message_id.like("measured-%"))
                            )
                        )
                        .mappings()
                        .all()
                    )
                    input_rows = (
                        (
                            await snapshot.execute(
                                select(inputs).where(
                                    inputs.c.id.in_(
                                        [row["input_id"] for row in admissions if row["input_id"]]
                                    )
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    work_rows = (
                        (
                            await snapshot.execute(
                                select(
                                    work.c.id,
                                    work.c.source_json,
                                    work.c.state,
                                    work.c.model_requests,
                                    work.c.sent_messages,
                                ).where(
                                    work.c.conversation_id.in_(
                                        {row["canonical_conversation_id"] for row in incoming}
                                    )
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    work_ids = {row["id"] for row in work_rows}
                    effect_rows = (
                        (
                            await snapshot.execute(
                                select(effects).where(effects.c.work_id.in_(work_ids))
                            )
                        )
                        .mappings()
                        .all()
                    )
                    receipts = (
                        (
                            await snapshot.execute(
                                select(
                                    SocialOperationModel.id,
                                    SocialOperationModel.status,
                                    SocialOperationModel.event_id,
                                    SocialOperationModel.platform_reference,
                                    SocialOperationModel.source_turn_id,
                                ).where(SocialOperationModel.event_id.in_(sent))
                            )
                        )
                        .mappings()
                        .all()
                    )
                if not workload_errors:
                    assert len(incoming) == args.messages, incoming
                    assert len(admissions) == len({row["event_id"] for row in admissions})
                    assert all(row["state"] == "consumed" for row in input_rows), input_rows
                    assert len(input_rows) == sum(row["route"] == "work" for row in admissions)
                    assert all(
                        row["status"] == "succeeded" and row["platform_reference"]
                        for row in receipts
                    ), receipts
                    assert {row["event_id"] for row in receipts} == set(sent)
                    for item in reflection_receipts:
                        async with reader_factory() as snapshot:
                            saved = await snapshot.get(MemorySelfReflectionRunModel, item["run_id"])
                            assert saved.status == "completed"
                coverage = []
                for request in provider.requests:
                    if request.messages[-1].role == "tool":
                        continue
                    seen = [
                        row
                        for row in incoming
                        if any(
                            row["content"] in (message.content or "")
                            and re.search(rf"#{row['id']}(?=\D|$)", message.content or "")
                            for message in request.messages
                        )
                    ]
                    if not seen:
                        continue
                    trigger = max(seen, key=lambda row: row["id"])
                    source_turn = f"{trigger['canonical_conversation_id']}:event:{trigger['id']}"
                    delivered = [
                        row["id"]
                        for row in receipts
                        if row["source_turn_id"] == source_turn and row["status"] == "succeeded"
                    ]
                    coverage.append(
                        {
                            "request_chain_id": request.request_chain_id,
                            "source_event_ids": [row["id"] for row in seen],
                            "trigger_event_id": trigger["id"],
                            "social_receipt_ids": delivered,
                        }
                    )
                covered = {
                    event_id
                    for item in coverage
                    if item["social_receipt_ids"]
                    for event_id in item["source_event_ids"]
                }
                producer_count = len(app.runtime.bindings.owners(work_ids))
                if not workload_errors:
                    assert covered == {row["id"] for row in incoming}, (covered, incoming)
                    assert producer_count == 0
                result.update(
                    messages=args.messages,
                    background_jobs=len(background_latencies),
                    interval_seconds=args.interval,
                    elapsed_seconds=elapsed,
                    completed_input_observations_per_second=len(covered) / elapsed,
                    completion_semantics=(
                        "Original event ID and unique body must occur in an actual "
                        "Fake model request linked to a successful Social receipt for its current "
                        "source; history observations may share a reply. Work inputs must be "
                        "consumed and original producers joined. This is not one reply per arrival."
                    ),
                    handler=distribution(latencies),
                    arrival_to_exit=distribution(completions),
                    background=distribution(background_latencies),
                    reflection_receipts=reflection_receipts,
                    verified_inbound=len(incoming),
                    verified_sends=len(sent),
                    fixed_fake_model_calls=len(provider.requests),
                    timed_chat_model_calls=len(provider.requests) - warmup_calls,
                    warmup_model_calls=warmup_calls,
                    warmup_excluded_from_timing=True,
                    verified_sends_includes_warmup=True,
                    chat_completion={
                        "handler_results": handler_results,
                        "completed_input_observations": len(covered),
                        "model_source_receipts": coverage,
                        "active_original_producers": producer_count,
                        "admissions": [dict(row) for row in admissions],
                        "inputs": [dict(row) for row in input_rows],
                        "work": [dict(row) for row in work_rows],
                        "effects": [dict(row) for row in effect_rows],
                        "social_receipts": [dict(row) for row in receipts],
                    },
                    domain_handler=distribution(domain_latencies),
                    domain_arrival_to_exit=distribution(domain_completion),
                    work_renew=distribution(renew_latencies),
                    event_loop_lag=distribution(loop_lag),
                    process_cpu_seconds=cpu_elapsed,
                    domain_delay_seconds=args.domain_delay if domain_run else None,
                )
            imported_sources = await asyncio.to_thread(verify_imports, checkout)
            result.update(
                imported_source_modules=imported_sources,
                imported_sources_checked=True,
                sql={kind: distribution(values) for kind, values in sorted(sql_times.items())},
                sqlite_errors=sql_errors,
                writer_entry_wait=distribution(waits),
                writer_scope_and_exit=distribution(scopes),
                acknowledged_write_to_root_end=distribution(acquired_to_end),
                acknowledged_write_semantics=(
                    "all Session root transactions; starts after BEGIN IMMEDIATE "
                    "or first successful DML; "
                    "a lower bound on actual SQLite writer hold time, "
                    "excluding the first SQL duration"
                ),
                phase_semantics=(
                    "entry_wait includes checkout/BEGIN; scope_and_exit starts after BEGIN "
                    "and includes physical commit/cleanup; explicit immediate scopes only, "
                    "baseline mixed sessions are excluded from these two distributions"
                ),
            )
            return result
        finally:
            if app is not None:
                errors = await app.lifecycle._close_entries(app.lifecycle._entries)
                assert not errors, errors
            await database.close()
            event.remove(Session, "after_begin", began)
            event.remove(Session, "after_transaction_end", ended)
            event.remove(database.engine.sync_engine, "before_cursor_execute", sql_started)
            event.remove(database.engine.sync_engine, "after_cursor_execute", acquired)
            event.remove(database.engine.sync_engine, "handle_error", sql_failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scratch", type=Path, default=Path("D:/Temp/yuki-sqlite-perf"))
    parser.add_argument(
        "--scenario",
        choices=("chat", "mixed", "tail", "memory", "forget", "merge", "gc"),
        required=True,
    )
    parser.add_argument("--messages", type=int, default=12)
    parser.add_argument("--interval", type=float, default=0.15)
    parser.add_argument("--background", type=int, default=8)
    parser.add_argument("--background-interval", type=float, default=0.2)
    parser.add_argument("--tail", type=int, default=512)
    parser.add_argument("--scale", type=int, default=128)
    parser.add_argument("--family", type=int, default=4)
    parser.add_argument("--payload-chars", type=int, default=4096)
    parser.add_argument("--domain-delay", type=float, default=0.05)
    arguments = parser.parse_args()
    report = asyncio.run(main(arguments))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
