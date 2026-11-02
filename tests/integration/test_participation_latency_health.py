"""Public health and privileged detailed embedding status on real local SQLite."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.canonical_identity import ensure_person
from tests.unit.test_embedding_claim_boundaries import _jobs
from tests.unit.test_memory_vec1 import populate
from tests.unit.test_telegram_health import health_container

from yuki.admin.models import AdminActor
from yuki.health import build_health_payload
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.embedding.fake import FakeEmbeddingProvider
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.repository import MemoryFactRepository
from yuki.persistence.models import MemoryFactModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.admin.memory_admin import MemoryAdminService


async def test_public_health_does_not_scan_embedding_details_but_authorized_status_does(
    database, monkeypatch, latency_evidence
):
    jobs = await _jobs(database, 5)
    async with database.immediate_session() as writer:
        facts = list(await writer.scalars(select(MemoryFactModel).order_by(MemoryFactModel.id)))
        await populate(writer, jobs, facts[:3], [(1.0, 0.0)] * 3)
        root = await ensure_person(writer, "synthetic-root")
        ordinary = await ensure_person(writer, "synthetic-ordinary")
        (await writer.get(CanonicalPersonModel, root)).is_superuser = True
    settings = make_settings(database.url, memory_embedding_enabled=True)
    provider = FakeEmbeddingProvider(dimensions=2)
    runtime = MemoryEmbeddingRuntime(
        settings=settings, database=database, facts=None, retriever=None, provider=provider
    )
    runtime._profile_id = jobs.profile.id
    app = health_container(database, None)
    app.memory_embeddings = runtime
    calls = []
    original = runtime.repository.local_health

    async def count_actual_scan(*args, **kwargs):
        calls.append("local_health")
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.repository, "local_health", count_actual_scan)
    public = await build_health_payload(app)
    public_scans = len(calls)
    admin = MemoryAdminService(
        settings=settings,
        memories=SimpleNamespace(repository=MemoryFactRepository(database)),
        audit=None,
        mutations=None,
        ledger=EventLedgerRepository(database),
        memory_embeddings=runtime,
    )

    def actor(person_id):
        return AdminActor(
            user_id="synthetic",
            is_superuser=True,
            trigger_message_id="synthetic",
            conversation_key="synthetic",
            person_id=person_id,
        )

    with pytest.raises(PermissionError):
        await admin.embedding_status(actor(ordinary))
    detailed = await admin.embedding_status(actor(root))
    authorized_scans = len(calls) - public_scans
    assert detailed.active_fact_count == 5 and detailed.ready_embedding_count == 3
    assert detailed.coverage_ratio == 0.6
    assert detailed.pending_job_count == detailed.failed_job_count == 0
    assert provider.document_requests == provider.query_requests == 0

    def broken_ping(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.strip().upper() == "SELECT 1":
            raise OperationalError(statement, (), RuntimeError("injected reader failure"))

    # Fault is limited to the actual SQLite ping operation; other readers work.
    event.listen(database.engine.sync_engine, "before_cursor_execute", broken_ping)
    try:
        reader_failed = await build_health_payload(app)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", broken_ping)
    assert reader_failed["status"] == "degraded"
    assert reader_failed["database_read"] == "unavailable"
    assert reader_failed["database_writer"] == "accepting"
    await database.close()
    stopped = await build_health_payload(app)
    latency_evidence.update(
        boundary=(
            "real public health, MemoryEmbeddingRuntime, privileged admin and SQLite; "
            "fake local provider"
        ),
        public_embedding_detail_scans=public_scans,
        authorized_detail_scans=authorized_scans,
        detail_active=detailed.active_fact_count,
        detail_ready=detailed.ready_embedding_count,
        detail_coverage=detailed.coverage_ratio,
        unprivileged_denied=True,
        reader_after_close=stopped["database_read"],
        writer_after_close=stopped["database_writer"],
        injected_ping_failure_read=reader_failed["database_read"],
        injected_ping_failure_writer=reader_failed["database_writer"],
        overall_after_close=stopped["status"],
        provider_requests=provider.document_requests + provider.query_requests,
    )
    assert public["memory_embedding_enabled"] is True
    assert public["memory_embedding_configured"] is True
    assert stopped["database_read"] == "ok" and stopped["database_writer"] == "stopped"
    assert stopped["status"] == "degraded"
    assert public_scans == 0, "public health scanned privileged embedding details"
