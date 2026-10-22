"""Offline Linux measurements of the real history index, writer and read sessions.

Run from the repository root with its dependencies installed. All bodies are
synthetic, and TemporaryDirectory owns every database. This is not a CI test or
a production latency claim. The corpus loader uses real schema and token code;
separate samples exercise the actual inbound/outbound/ASR writers.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import resource
import sqlite3
import statistics
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.canonical_ingress import fixture_ingress

from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.identity.write_settings import IdentityWriteSettings, configure_identity_write_settings
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository, _history_read
from yuki.persistence.history_search import (
    compile_match,
    match_fields,
    namespace_terms,
    prepare_query,
    prepare_terms,
)
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


def milliseconds(start):
    return (time.perf_counter() - start) * 1000


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min_ms": ordered[0],
        "median_ms": statistics.median(ordered),
        "p95_observed_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max_ms": ordered[-1],
    }


def rss():
    current = next(
        int(line.split()[1])
        for line in Path("/proc/self/status").read_text().splitlines()
        if line.startswith("VmRSS:")
    )
    return {"current_kib": current, "peak_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def environment_limits():
    return {
        "logical_cpus": os.cpu_count(),
        "cgroup_cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
        "cgroup_memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
        "load_average": os.getloadavg(),
    }


def body(length, salt):
    generator = random.Random(salt)
    return "常见消息 " + "".join(
        chr(generator.randrange(0x4E00, 0x9FA6)) for _ in range(max(0, length - 5))
    )


def storage(path):
    with closing(sqlite3.connect(path)) as connection:
        return {
            "database_bytes": path.stat().st_size,
            "wal_bytes": Path(str(path) + "-wal").stat().st_size
            if Path(str(path) + "-wal").exists()
            else 0,
            "page_size": connection.execute("PRAGMA page_size").fetchone()[0],
            "page_count": connection.execute("PRAGMA page_count").fetchone()[0],
            "freelist_count": connection.execute("PRAGMA freelist_count").fetchone()[0],
            "fts_pages": dict(
                connection.execute(
                    "SELECT name,sum(pgsize) FROM dbstat WHERE name LIKE 'chat_history_fts%' "
                    "GROUP BY name"
                )
            ),
        }


async def foundation(database, scopes):
    conversations, people = [], []
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        presence = await create_presence(session, external_account_id="8000", now=now)
        for index in range(8):
            people.append(
                await create_person_binding(
                    session, external_account_id=str(1001 + index), display_name="", now=now
                )
            )
        for index in range(scopes):
            binding = await create_space_binding(
                session,
                external_space_id=str(2001 + index),
                name="",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                now=now,
            )
            scope = ConversationScope.group("8000", str(2001 + index))
            conversation = await ensure_canonical_conversation(
                session,
                kind="space",
                primary_scope_key=scope.key,
                space_id=binding.space_id,
                space_binding_id=binding.id,
            )
            conversations.append(conversation.conversation_id)
    writer = ScopedEventLedgerUnitOfWork(database)
    original = await writer.append(
        scope=ConversationScope.group(
            "8000", "2001", presence_id=presence.id, conversation_id=conversations[0]
        ),
        platform_message_id="template",
        sender_user_id="8000",
        direction="outbound",
        content="synthetic template",
    )
    return conversations, people, presence.id, original.event.id


def load_corpus(path, conversations, people, template_id, count):
    start = time.perf_counter()
    counters = defaultdict(int)
    batch_times = []
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(chat_events)")]
        original = dict(
            zip(
                columns,
                connection.execute(
                    "SELECT * FROM chat_events WHERE id=?", (template_id,)
                ).fetchone(),
                strict=True,
            )
        )
        columns.remove("id")
        insert = (
            "INSERT INTO chat_events("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ") RETURNING id"
        )
        for base in range(0, count, 64):
            prepared = []
            for index in range(base, min(count, base + 64)):
                conversation = conversations[index % len(conversations)]
                position = index // len(conversations)
                person = people[0 if position % 4 != 3 else 1 + position % 7]
                content = body(96, index)
                if index == 0:
                    content += " 稀有锚点001"
                audio = (
                    serialize_transcripts((AudioTranscript("current", 0, "自己的语音"),))
                    if index % 10 == 0
                    else ""
                )
                before = time.perf_counter()
                terms = prepare_terms(content, audio)
                counters["prepare_ms"] += milliseconds(before)
                tokens = namespace_terms(
                    conversation, terms, author_person_id=person.person_id, author_kind="person"
                )
                counters["gram_count"] += len(terms)
                counters["tag_count"] += len(tokens.split()) - len(terms)
                counters["encoded_token_bytes"] += len(tokens.encode())
                row = {
                    **original,
                    "platform_message_id": f"corpus:{index}",
                    "canonical_event_id": str(uuid4()),
                    "canonical_conversation_id": conversation,
                    "sender_user_id": person.external_account_id,
                    "direction": "inbound",
                    "author_kind": "person",
                    "author_person_id": person.person_id,
                    "author_presence_id": None,
                    "content": content,
                    "audio_transcript": audio,
                }
                prepared.append((tuple(row[name] for name in columns), tokens))
            before = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            for values, tokens in prepared:
                event_id = connection.execute(insert, values).fetchone()[0]
                connection.execute(
                    "INSERT INTO chat_history_fts(rowid,tokens) VALUES(?,?)", (event_id, tokens)
                )
            connection.commit()
            batch_times.append(milliseconds(before))
        before_checkpoint = storage(path)
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        after_checkpoint = storage(path)
    return {
        "events": count,
        "scopes": len(conversations),
        "authors": len(people),
        "elapsed_ms": milliseconds(start),
        "counters": counters,
        "batch_including_lock_acquisition_and_commit": distribution(batch_times),
        "before_checkpoint": before_checkpoint,
        "checkpoint_result": checkpoint,
        "after_checkpoint": after_checkpoint,
        "rss": rss(),
    }


async def query_samples(database, conversation, person):
    import yuki.persistence.event_repository as module

    original = module.match_fields
    results = {}
    ledger = EventLedgerRepository(database)
    for label, keyword, extra in (
        ("rare", "稀有锚点001", {}),
        ("absent", "不存在终极短语", {}),
        ("common", "常见", {}),
        ("rare_wide_date", "稀有锚点001", {"start_at": datetime(2000, 1, 1, tzinfo=UTC)}),
    ):
        observed, durations, virtual_machine = [], [], []
        for _ in range(6):
            converted = 0
            instructions = 0

            def counted(*args):
                nonlocal converted
                converted += 1
                return original(*args)

            def progress():
                nonlocal instructions
                instructions += 100
                return 0

            module.match_fields = counted
            try:
                async with database.engine.connect() as connection:
                    raw = await connection.get_raw_connection()
                    driver = raw.driver_connection
                    async with AsyncSession(bind=connection) as session:
                        await session.execute(text("BEGIN"))
                        await driver.set_progress_handler(progress, 100)
                        before = time.perf_counter()
                        page = await ledger.query_history(
                            conversation_id=conversation,
                            person_id=person,
                            keyword=keyword,
                            limit=20,
                            order="recent",
                            session=session,
                            **extra,
                        )
                        durations.append(milliseconds(before))
                        await driver.set_progress_handler(None, 0)
                        observed.append({"returned": len(page.events), "verified_rows": converted})
                        virtual_machine.append(instructions)
            finally:
                module.match_fields = original
        results[label] = {
            "elapsed": distribution(durations),
            "observations": observed,
            "approx_sqlite_vm_quantum_100": virtual_machine,
        }
    return results


def sql_alternatives(path, conversation, person):
    """Compare two explicit SQL shapes; neither is a product strategy selector."""
    results = {}
    metadata = (
        "SELECT id,content,audio_transcript FROM chat_events WHERE canonical_conversation_id=? "
        "AND author_person_id=? AND occurred_at>=? AND event_kind='message' "
        "AND (suppression_status IS NULL OR suppression_status='keeper') ORDER BY id DESC"
    )
    intersect = metadata.replace(
        " ORDER BY id DESC",
        " AND id IN (SELECT rowid FROM chat_history_fts WHERE chat_history_fts MATCH ?) "
        "ORDER BY id DESC",
    )
    with closing(sqlite3.connect(path)) as connection:
        for label, keyword, start in (
            ("wide_date_rare", "稀有锚点001", "2000-01-01"),
            ("wide_date_absent", "不存在终极短语", "2000-01-01"),
            ("empty_date_common", "常见", "2100-01-01"),
        ):
            query = prepare_query(keyword)
            for shape, sql, parameters in (
                ("metadata", metadata, (conversation, person, start)),
                (
                    "single_match_id_membership",
                    intersect,
                    (
                        conversation,
                        person,
                        start,
                        compile_match(conversation, query, person_ids=(person,)),
                    ),
                ),
            ):
                elapsed, observations = [], []
                for _ in range(6):
                    instructions, verified, matched = 0, 0, []

                    def progress():
                        nonlocal instructions
                        instructions += 100
                        return 0

                    connection.set_progress_handler(progress, 100)
                    before = time.perf_counter()
                    for event_id, content, audio in connection.execute(sql, parameters):
                        verified += 1
                        if match_fields(query, content, audio) is not None:
                            matched.append(event_id)
                            if len(matched) == 21:
                                break
                    elapsed.append(milliseconds(before))
                    connection.set_progress_handler(None, 0)
                    observations.append(
                        {
                            "matched": len(matched),
                            "verified_rows": verified,
                            "approx_vm_quantum_100": instructions,
                        }
                    )
                results[f"{label}:{shape}"] = {
                    "elapsed": distribution(elapsed),
                    "observations": observations,
                    "query_plan": list(connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)),
                }
    return results


async def background_prepare_sample(length, repetitions):
    gaps, elapsed, threads = [], [], set()
    running = True
    contents = await asyncio.to_thread(
        lambda: tuple(body(length, index) for index in range(repetitions))
    )

    async def ticker():
        while running:
            before = time.perf_counter()
            await asyncio.sleep(0.001)
            gaps.append(milliseconds(before) - 1)

    def prepare(content):
        threads.add(threading.get_ident())
        before = time.perf_counter()
        prepare_terms(content)
        elapsed.append(milliseconds(before))

    pulse = asyncio.create_task(ticker())
    for content in contents:
        await asyncio.to_thread(prepare, content)
    running = False
    await pulse
    return {
        "prepare": distribution(elapsed),
        "worker_threads": len(threads),
        "event_loop_extra_sleep_1ms": distribution(gaps),
        "rss": rss(),
    }


async def background_prepare_samples():
    return {
        str(length): await background_prepare_sample(length, repetitions)
        for length, repetitions in ((48, 16), (32768, 4))
    }


async def writer_samples(database, presence, conversations, people):
    import yuki.identity.canonical_uow as ingress_module
    import yuki.persistence.scoped_event_uow as scoped_module

    writer = ScopedEventLedgerUnitOfWork(database)
    ingress = fixture_ingress(database)
    immediate = database.immediate_session
    current_label = ""
    transactions, preparations, total, namespaces = defaultdict(list), defaultdict(list), {}, []
    originals = (ingress_module.prepare_terms, scoped_module.prepare_terms)
    active = False
    violated = False
    active_key = None
    event_loop_gaps = defaultdict(list)

    async def ticker():
        while True:
            key = active_key
            before = time.perf_counter()
            await asyncio.sleep(0.001)
            if key is not None and key == active_key:
                event_loop_gaps[key[0]].append(milliseconds(before) - 1)

    @asynccontextmanager
    async def measured_transaction():
        nonlocal active
        before, acquired = time.perf_counter(), None
        try:
            async with immediate() as session:
                acquired = time.perf_counter()
                active = True
                yield session
        finally:
            active = False
            if acquired is not None:
                transactions[current_label].append(
                    {
                        "acquire_ms": (acquired - before) * 1000,
                        "held_including_commit_ms": milliseconds(acquired),
                    }
                )

    def measured_prepare(*args):
        nonlocal violated
        violated = violated or active
        before = time.perf_counter()
        result = prepare_terms(*args)
        preparations[current_label].append(milliseconds(before))
        return result

    database.immediate_session = measured_transaction
    ingress_module.prepare_terms = scoped_module.prepare_terms = measured_prepare
    pulse = asyncio.create_task(ticker())
    try:
        for length, repetitions in ((48, 8), (512, 8), (4096, 8), (32768, 2)):
            for action in ("outbound", "inbound", "asr"):
                current_label = f"{action}:{length}"
                elapsed = []
                for index in range(repetitions):
                    content = body(length, length + index)
                    if action == "outbound":
                        active_key = (current_label, index)
                        before = time.perf_counter()
                        await writer.append(
                            scope=ConversationScope.group(
                                "8000",
                                "2001",
                                presence_id=presence,
                                conversation_id=conversations[0],
                            ),
                            platform_message_id=f"writer:{current_label}:{index}",
                            sender_user_id="8000",
                            direction="outbound",
                            content=content,
                        )
                    else:
                        message = InboundMessage(
                            message_id=f"writer:{current_label}:{index}",
                            event_type="message",
                            scope_type=ScopeType.PRIVATE,
                            sender=SenderIdentity("1001"),
                            bot_user_id="8000",
                            text=content if action == "inbound" else "",
                            segments=({"type": "record", "data": {"file": "synthetic"}},)
                            if action == "asr"
                            else (),
                        )
                        admitted = await ingress.pre_admit(None, message)
                        if action == "asr":
                            current_label = f"asr_seed:{length}"
                            appended = await ingress.uow.append_inbound(admitted.message, admitted)
                            current_label = f"asr:{length}"
                            transcript = serialize_transcripts(
                                (AudioTranscript("current", 0, content),)
                            )
                            active_key = (current_label, index)
                            before = time.perf_counter()
                            assert await writer.set_audio_transcript(
                                appended.event.id, transcript, generation=appended.scope.generation
                            )
                        else:
                            active_key = (current_label, index)
                            before = time.perf_counter()
                            await ingress.uow.append_inbound(admitted.message, admitted)
                    elapsed.append(milliseconds(before))
                    active_key = None
                    terms = prepare_terms(content)
                    before = time.perf_counter()
                    encoded = namespace_terms(
                        conversations[0],
                        terms,
                        author_person_id=people[0].person_id,
                        author_kind="person",
                    )
                    namespaces.append(
                        {
                            "characters": len(content),
                            "grams": len(terms),
                            "namespaced_bytes": len(encoded.encode()),
                            "namespace_ms": milliseconds(before),
                        }
                    )
                total[current_label] = distribution(elapsed)
        current_label = "outbound_first_conversation:512"
        before = time.perf_counter()
        await writer.append(
            scope=ConversationScope.private("8000", "1002", presence_id=presence),
            platform_message_id="first-conversation",
            sender_user_id="8000",
            direction="outbound",
            content=body(512, 42),
            identity_binding_id=people[1].id,
        )
        total[current_label] = distribution([milliseconds(before)])
    finally:
        pulse.cancel()
        try:
            await pulse
        except asyncio.CancelledError:
            pass
        database.immediate_session = immediate
        ingress_module.prepare_terms, scoped_module.prepare_terms = originals
    return {
        "whole_append_or_asr": total,
        "prepare": {label: distribution(values) for label, values in preparations.items()},
        "explicit_writer_transactions": transactions,
        "prepare_observed_inside_explicit_writer": violated,
        "namespace_observations": namespaces,
        "event_loop_extra_sleep_1ms": {
            label: distribution(values) for label, values in event_loop_gaps.items()
        },
        "rss": rss(),
        "inbound_note": (
            "apply_snapshot_write uses deferred BEGIN; explicit writer samples exclude it"
        ),
    }


async def cancellation_sample(database):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    instructions = 0
    worker_threads = set()

    async def slow_read():
        nonlocal instructions
        async with database.sessions() as session:
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            driver = raw.driver_connection

            def progress():
                nonlocal instructions
                instructions += 1000
                worker_threads.add(threading.get_ident())
                if instructions == 1000:
                    loop.call_soon_threadsafe(started.set)
                return 0

            await driver.set_progress_handler(progress, 1000)
            try:
                await _history_read(
                    driver,
                    session.scalar(
                        text(
                            "WITH RECURSIVE r(x) AS "
                            "(VALUES(1) UNION ALL SELECT x+1 FROM r WHERE x<2000000) "
                            "SELECT sum(x) FROM r"
                        )
                    ),
                )
            finally:
                await driver.set_progress_handler(None, 0)

    task = asyncio.create_task(slow_read())
    await started.wait()
    at_cancel = instructions
    before = time.perf_counter()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    cancelled_after_ms = milliseconds(before)
    before = time.perf_counter()
    async with database.sessions() as session:
        assert await session.scalar(text("SELECT 1")) == 1
    reusable_after_ms = milliseconds(before)
    return {
        "synthetic_recursive_sql_rows": 2000000,
        "cancel_to_cleanup_ms": cancelled_after_ms,
        "subsequent_select1_ms": reusable_after_ms,
        "approx_vm_at_cancel_quantum_1000": at_cancel,
        "approx_vm_after_cleanup_quantum_1000": instructions,
        "sqlite_callback_threads": len(worker_threads),
        "note": (
            "bounded CPU query exercises real history read interrupt and cleanup; "
            "this is not search latency"
        ),
    }


async def run(count, scopes):
    sources = (
        Path("src/yuki/persistence/history_search.py"),
        Path("src/yuki/persistence/history_search_index.py"),
        Path("src/yuki/persistence/event_repository.py"),
        Path("src/yuki/persistence/scoped_event_uow.py"),
        Path("src/yuki/identity/canonical_uow.py"),
    )
    before_sources = {
        str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources
    }
    with tempfile.TemporaryDirectory(prefix="yuki448-benchmark-") as directory:
        path = Path(directory) / "synthetic.sqlite3"
        database = Database(f"sqlite+aiosqlite:///{path}")
        try:
            os.environ["DATABASE_URL"] = database.url
            await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
            configure_identity_write_settings(IdentityWriteSettings())
            conversations, people, presence, template_id = await foundation(database, scopes)
            await database.close()
            loaded = await asyncio.to_thread(
                load_corpus, path, conversations, people, template_id, count
            )
            queries = await query_samples(database, conversations[0], people[0].person_id)
            alternatives = await asyncio.to_thread(
                sql_alternatives, path, conversations[0], people[0].person_id
            )
            writers = await writer_samples(database, presence, conversations, people)
            background = await background_prepare_samples()
            cancellation = await cancellation_sample(database)
            final_storage = storage(path)
            after_sources = {
                str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in sources
            }
            limits = await asyncio.to_thread(environment_limits)
            return {
                "environment": {
                    "generated_at_utc": datetime.now(UTC).isoformat(),
                    "python": platform.python_version(),
                    "sqlite": sqlite3.sqlite_version,
                    "system": platform.platform(),
                    **limits,
                    "opencc": importlib.metadata.version("OpenCC"),
                    "journal_mode": "wal",
                    "synchronous": "normal",
                    "wal_autocheckpoint_pages": 1000,
                    "vm_counters_exclude_fts_internal_work": True,
                    "source_sha256_before": before_sources,
                    "source_sha256_after": after_sources,
                    "source_unchanged_during_probe": before_sources == after_sources,
                },
                "corpus": loaded,
                "queries": queries,
                "sql_alternatives": alternatives,
                "writers": writers,
                "background_prepare": background,
                "cancellation": cancellation,
                "final_storage": final_storage,
            }
        finally:
            await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--scopes", type=int, default=4)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.events, args.scopes))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.out), "corpus_events": args.events, "rss": rss()}))
