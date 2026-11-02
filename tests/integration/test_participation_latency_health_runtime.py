"""Real ASGI health and authorized diagnostic paths with controlled history barriers."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_event
from tests.support.model_profiles import write_fake_profiles
from tests.unit.test_admin_operator_access import operator_file

import yuki.main as main
from yuki.admin.models import AdminActor
from yuki.admin_api.http import attach_admin_api
from yuki.container import ApplicationContainer
from yuki.domain.conversations import ScopeType
from yuki.identity.db_models import CanonicalPersonModel
from yuki.memory.self_reflection.control import ReflectionDailyLimit
from yuki.persistence.models import (
    MemoryFactModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
)

HEALTH_TOKEN = "synthetic-health-operator-" + "x" * 48


def actor(person_id):
    return AdminActor(
        user_id="synthetic",
        is_superuser=True,
        trigger_message_id="synthetic",
        conversation_key="synthetic",
        person_id=person_id,
    )


async def build_real_container(database, tmp_path, monkeypatch, enabled):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YUKI_TEST_OPERATOR_TOKEN", HEALTH_TOKEN)
    operator_policy, _ = operator_file(tmp_path, capabilities=("control.health.read",))
    next_hour = (datetime.now(ZoneInfo("Asia/Shanghai")).hour + 1) % 24
    settings = make_settings(
        database.url,
        control_operators_file=operator_policy,
        model_profiles_file=write_fake_profiles(tmp_path / "models.toml"),
        plugin_system_enabled=False,
        plugin_directory=tmp_path / "plugins",
        workspace_directory=tmp_path / "workspace",
        conversation_media_cache_directory=tmp_path / "media",
        social_transfer_directory=tmp_path / "transfer",
        semantic_participation_state_path=tmp_path / "participation.sqlite3",
        memory_embedding_enabled=False,
        memory_embedding_worker_enabled=False,
        memory_self_reflection_enabled=enabled,
        memory_self_reflection_schedule_hours=str(next_hour),
        memory_self_reflection_event_threshold=1,
        memory_self_reflection_max_daily_calls=2,
        memory_dream_enabled=False,
        memory_maintenance_enabled=False,
        automation_enabled=False,
        conversation_proactive_wakeup_enabled=False,
        conversation_autonomous_enabled=False,
    )
    app = ApplicationContainer(settings, database=database)
    await app.character.initialize("Synthetic health")
    await app.start()
    app.memory_metrics.record_maintenance_success(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    root_id = ordinary_id = None
    try:
        async with database.immediate_session() as writer:
            root_id = await ensure_person(writer, "synthetic-health-root")
            ordinary_id = await ensure_person(writer, "synthetic-health-ordinary")
            (await writer.get(CanonicalPersonModel, root_id)).is_superuser = True
            writer.add(
                MemoryFactModel(
                    scope_type="self",
                    visibility_type="global",
                    kind="fact",
                    memory_key="synthetic-health-contested",
                    category="diagnostic",
                    content="synthetic local fact",
                    normalized_content="synthetic local fact",
                    source_type="automatic",
                    status="active",
                    conflict_state="contested",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        repository = app.memory_self_reflection_worker._repository
        # The disabled worker has not initialized the real ingestion cursor.
        await repository.scan_new_events()
        source, _ = await seed_event(
            app.ledger,
            bot_user_id="8000",
            origin_key="synthetic-health-source",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="synthetic local source",
            group_id="2001",
            occurred_at=datetime.now(UTC),
        )
        await repository.scan_new_events()
        async with database.immediate_session() as writer:
            state = await writer.scalar(
                select(MemorySelfReflectionStateModel).where(
                    MemorySelfReflectionStateModel.pending_events > 0
                )
            )
            assert state is not None
            now = datetime.now(UTC)
            run = MemorySelfReflectionRunModel(
                conversation_key_hash=state.conversation_key_hash,
                bot_user_id=state.bot_user_id,
                canonical_person_id=state.canonical_person_id,
                canonical_space_id=state.canonical_space_id,
                scheduled_slot="synthetic-health-slot",
                trigger_reason="scheduled",
                first_event_id=source.id,
                last_event_id=source.id,
                status="completed",
                started_at=now,
                completed_at=now,
                processed_events=0,
            )
            writer.add(run)
            await writer.flush()
            run_id = run.id
        control = app.memory_self_reflection_worker.control
        request_id = await control.reserve_request(run_id)
        await control.finish_request(request_id, "succeeded", 17)
        return app, actor(root_id), actor(ordinary_id)
    except BaseException:
        await app.close()
        raise


async def test_reflection_detail_does_not_run_discarded_daily_history_query(
    database, tmp_path, monkeypatch, latency_evidence
):
    app, root, _ = await build_real_container(database, tmp_path, monkeypatch, False)
    captured = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        text = " ".join(statement.lower().split())
        if "count(" in text and "memory_self_reflection_runs" in text and "scheduled_slot" in text:
            captured.append(text)

    try:
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            detail = await app.memory_admin.self_reflection_health(root)
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
        assert detail["calls_today"] == 1
        assert detail["last_run_status"] == "completed"
        assert any(
            row["status"] == "succeeded" and row["count"] == 1 and row["output_tokens"] == 17
            for row in detail["backlog"]["provider_requests"]
        )
        async with database.read_session() as session:
            run_id = await session.scalar(select(MemorySelfReflectionRunModel.id))
        control = app.memory_self_reflection_worker.control
        second_request = await control.reserve_request(run_id)
        await control.finish_request(second_request, "succeeded", 3)
        with pytest.raises(ReflectionDailyLimit):
            await control.reserve_request(run_id)
        actual = await control.snapshot()
        assert actual["calls_today"] == 2
        assert sum(row["count"] for row in actual["provider_requests"]) == 2
        latency_evidence.update(
            boundary="actual authorized worker/control/repository and SQLite; synthetic requests",
            discarded_scheduled_run_count_queries=len(captured),
            actual_calls_before=detail["calls_today"],
            actual_calls_after=actual["calls_today"],
            provider_daily_limit_rejected_third_reservation=True,
            actual_receipt_tokens_preserved=True,
        )
        assert not captured, "worker health ran a discarded scheduled-run daily count"
    finally:
        await app.close()


@pytest.mark.parametrize(("target", "enabled"), [("reflection", True), ("audit", False)])
async def test_public_health_does_not_wait_for_authorized_history_detail(
    database, tmp_path, monkeypatch, latency_evidence, target, enabled
):
    app, root, ordinary = await build_real_container(database, tmp_path, monkeypatch, enabled)
    gate = asyncio.Event()
    public_entered = asyncio.Event()
    admin_entered = asyncio.Event()
    public_task = admin_task = waiter = None
    http_app = FastAPI()
    monkeypatch.setattr(main, "get_container", lambda: app)
    main.register_health_routes(http_app)
    try:
        if target == "audit":
            # A synthetic inconsistent historical fact exercises the retained audit detail.
            async with database.immediate_session() as writer:
                writer.add(
                    MemoryFactModel(
                        scope_type="self",
                        visibility_type="global",
                        kind="fact",
                        memory_key="synthetic-health-unchained",
                        category="diagnostic",
                        content="synthetic local superseded fact",
                        normalized_content="synthetic local superseded fact",
                        source_type="automatic",
                        status="superseded",
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app), base_url="http://local-health-test"
        ) as client:
            with pytest.raises(PermissionError):
                await app.memory_admin.self_reflection_health(ordinary)
            with pytest.raises(PermissionError):
                await app.memory_admin.consistency_health(ordinary)
            detailed_reflection = await app.memory_admin.self_reflection_health(root)
            detailed_audit = await app.memory_admin.consistency_health(root)
            assert detailed_reflection is not None and detailed_reflection["calls_today"] == 1
            assert detailed_reflection["enabled"] is enabled
            assert any(
                item["attempt_kind"] == "initial"
                and item["status"] == "succeeded"
                and item["count"] == 1
                and item["output_tokens"] == 17
                for item in detailed_reflection["backlog"]["provider_requests"]
            )
            assert detailed_audit.active_contested_count == 1
            if target == "audit":
                assert detailed_audit.superseded_without_chain_count == 1
            baseline = await client.get("/healthz")
            assert baseline.status_code == 200
            baseline_payload = baseline.json()
            assert baseline_payload["database_read"] == "ok"
            assert baseline_payload["database_writer"] == "accepting"
            assert (
                app.memory_self_reflection_worker._settings.memory_self_reflection_enabled
                is enabled
            )
            assert baseline_payload["memory_self_reflection"]["enabled"] is enabled
            running = app.memory_self_reflection_worker._task is not None and not (
                app.memory_self_reflection_worker._task.done()
            )
            assert baseline_payload["memory_self_reflection"]["running"] is running
            assert baseline_payload["memory_maintenance_last_success_at"] == (
                app.memory_metrics.maintenance_last_success_at.isoformat()
            )

            owner = (
                app.memory_self_reflection_worker.control
                if target == "reflection"
                else app.memory_audit
            )
            method = "snapshot" if target == "reflection" else "health"
            original = getattr(owner, method)

            async def blocked(*args, **kwargs):
                name = asyncio.current_task().get_name()
                if name == "d10-public":
                    public_entered.set()
                if name == "d10-admin":
                    admin_entered.set()
                await gate.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(owner, method, blocked)
            public_task = asyncio.create_task(client.get("/healthz"), name="d10-public")
            waiter = asyncio.create_task(public_entered.wait())
            done, _ = await asyncio.wait(
                (public_task, waiter), timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "local public request neither completed nor reached the barrier"
            public_finished_before_release = public_task in done and not public_entered.is_set()
            live = await asyncio.wait_for(client.get("/livez"), timeout=5)
            assert live.status_code == 200 and live.json() == {"status": "alive"}
            detail = (
                app.memory_admin.self_reflection_health(root)
                if target == "reflection"
                else app.memory_admin.consistency_health(root)
            )
            admin_task = asyncio.create_task(detail, name="d10-admin")
            await asyncio.wait_for(admin_entered.wait(), timeout=10)
            assert not admin_task.done()
            latency_evidence.update(
                boundary=(
                    "actual ASGI /healthz and /livez, real ApplicationContainer, SQLite, "
                    "reflection worker and authorized MemoryAdmin; fake model profile"
                ),
                target=target,
                reflection_enabled=enabled,
                public_completed_before_release=public_finished_before_release,
                public_entered_detail=public_entered.is_set(),
                admin_entered_and_waited=True,
                unauthorized_detail_denied=True,
                authorized_calls_today=detailed_reflection["calls_today"],
                authorized_active_contested_count=detailed_audit.active_contested_count,
                authorized_superseded_without_chain_count=(
                    detailed_audit.superseded_without_chain_count
                ),
            )
            gate.set()
            public_response, actual_detail = await asyncio.gather(public_task, admin_task)
            assert public_response.status_code == 200
            payload = public_response.json()
            assert payload["database_read"] == "ok"
            assert payload["database_writer"] == "accepting"
            assert payload["status"] == baseline_payload["status"]
            assert payload["memory_self_reflection"]["enabled"] is enabled
            assert payload["memory_self_reflection"]["running"] is running
            assert payload["memory_maintenance_last_success_at"] == (
                app.memory_metrics.maintenance_last_success_at.isoformat()
            )
            assert payload["memory_embedding_enabled"] is app.memory_embeddings.enabled
            assert payload["memory_embedding_configured"] is (
                app.memory_embeddings.provider is not None
            )
            assert payload["web_configured"] is app.settings.web_configured
            assert payload["vision_configured"] is app.settings.vision_configured
            assert payload["automation_enabled"] is app.settings.automation_enabled
            assert payload["plugin_system_enabled"] is app.settings.plugin_system_enabled
            if target == "reflection":
                assert actual_detail["calls_today"] == 1
                assert (
                    actual_detail["backlog"]["provider_requests"]
                    == detailed_reflection["backlog"]["provider_requests"]
                )
            else:
                assert actual_detail.active_contested_count == detailed_audit.active_contested_count
                assert actual_detail.superseded_without_chain_count == 1

            def fail_ping(_connection, _cursor, statement, _parameters, _context, _many):
                if statement.strip().upper() == "SELECT 1":
                    raise OperationalError(statement, (), RuntimeError("synthetic reader failure"))

            event.listen(database.engine.sync_engine, "before_cursor_execute", fail_ping)
            try:
                failed = (await client.get("/healthz")).json()
            finally:
                event.remove(database.engine.sync_engine, "before_cursor_execute", fail_ping)
            assert failed["status"] == "degraded"
            assert failed["database_read"] == "unavailable"
            assert failed["database_writer"] == "accepting"
            latency_evidence["reader_failure_preserved"] = True
            if target == "audit":
                await app.plugin_background_turns.close()
                stopped = (await client.get("/healthz")).json()
                assert stopped["plugin_background_turns"]["running"] is False
                assert stopped["status"] == "degraded"
                assert stopped["database_read"] == "ok"
                assert stopped["database_writer"] == "accepting"
                await app.plugin_background_turns.start()
                restarted = (await client.get("/healthz")).json()
                assert restarted["plugin_background_turns"]["running"] is True
                assert restarted["status"] == baseline_payload["status"]
                latency_evidence["actual_plugin_stop_and_restart_preserved"] = True
            assert public_finished_before_release, "public health waited for detailed history"
    finally:
        gate.set()
        for task in (waiter, public_task, admin_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (waiter, public_task, admin_task) if task is not None),
            return_exceptions=True,
        )
        await app.close()


@pytest.mark.parametrize("target", ["rebuild", "dream"])
async def test_public_health_does_not_wait_for_other_historical_aggregates(
    database, tmp_path, monkeypatch, latency_evidence, target
):
    app, root, ordinary = await build_real_container(database, tmp_path, monkeypatch, False)
    gate = asyncio.Event()
    entered = asyncio.Event()
    task = waiter = None
    http_app = FastAPI()
    monkeypatch.setattr(main, "get_container", lambda: app)
    main.register_health_routes(http_app)
    try:
        with pytest.raises(PermissionError):
            await app.memory_rebuild_service.list(actor_user_id=ordinary.person_id)
        with pytest.raises(PermissionError):
            await app.memory_admin.dream_list(ordinary)
        rebuild_list = await app.memory_rebuild_service.list(actor_user_id=root.person_id)
        dream_list = await app.memory_admin.dream_list(root)
        assert isinstance(rebuild_list, tuple) and isinstance(dream_list, tuple)
        lifecycle = await app.lifecycle.health()
        assert "memory_dream_worker" in lifecycle
        owner = app.persistence.memory_rebuilds if target == "rebuild" else app.memory_dream_worker
        original = owner.health

        async def blocked(*args, **kwargs):
            if asyncio.current_task().get_name() == "d10-public":
                entered.set()
            await gate.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, "health", blocked)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app), base_url="http://local-health-test"
        ) as client:
            task = asyncio.create_task(client.get("/healthz"), name="d10-public")
            waiter = asyncio.create_task(entered.wait())
            done, _ = await asyncio.wait(
                (task, waiter), timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "local public request neither completed nor reached the barrier"
            finished = task in done and not entered.is_set()
            live = await asyncio.wait_for(client.get("/livez"), timeout=5)
            assert live.status_code == 200 and live.json() == {"status": "alive"}
            latency_evidence.update(
                boundary=(
                    "actual ASGI health, real ApplicationContainer and SQLite; "
                    "authorized rebuild/dream list and real dream lifecycle health"
                ),
                target=target,
                public_completed_before_release=finished,
                public_entered_aggregate=entered.is_set(),
                unauthorized_lists_denied=True,
                authorized_rebuild_count=len(rebuild_list),
                authorized_dream_count=len(dream_list),
                dream_lifecycle_present=True,
            )
            gate.set()
            response = await task
            assert response.status_code == 200
            assert response.json()["database_read"] == "ok"
            assert response.json()["database_writer"] == "accepting"
            if target == "rebuild":
                detail = await original(
                    enabled=app.settings.memory_rebuild_enabled,
                    active_in_flight_calls=app.memory_rebuild_service.active_in_flight_calls,
                )
            else:
                detail = await original()
            assert detail is not None
            assert finished, "public health waited for a historical aggregate"
    finally:
        gate.set()
        for pending in (waiter, task):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(pending for pending in (waiter, task) if pending is not None),
            return_exceptions=True,
        )
        await app.close()


@pytest.mark.parametrize("target", ["work", "automation"])
async def test_public_health_does_not_wait_for_work_or_automation_history(
    database, tmp_path, monkeypatch, latency_evidence, target
):
    app, _, _ = await build_real_container(database, tmp_path, monkeypatch, False)
    gate = asyncio.Event()
    entered = asyncio.Event()
    task = waiter = None
    http_app = FastAPI()
    monkeypatch.setattr(main, "get_container", lambda: app)
    main.register_health_routes(http_app)
    try:
        work_detail = await app.work_scheduler.health()
        for key in (
            "pending_oldest_seconds",
            "active_work_count",
            "active_wait_count",
            "active_wait_oldest_seconds",
        ):
            assert key in work_detail
        runtime_detail = await app.runtime.health()
        assert "runtime_work" in runtime_detail["workers"]
        assert await app.automation_repository.active_count() == 0
        owner = app.work_scheduler if target == "work" else app.automation_repository
        method = "health" if target == "work" else "active_count"
        original = getattr(owner, method)

        async def blocked(*args, **kwargs):
            if asyncio.current_task().get_name() == "d10-public":
                entered.set()
            await gate.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, method, blocked)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app), base_url="http://local-health-test"
        ) as client:
            task = asyncio.create_task(client.get("/healthz"), name="d10-public")
            waiter = asyncio.create_task(entered.wait())
            done, _ = await asyncio.wait(
                (task, waiter), timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "local public request neither completed nor reached the barrier"
            finished = task in done and not entered.is_set()
            live = await asyncio.wait_for(client.get("/livez"), timeout=5)
            assert live.status_code == 200
            latency_evidence.update(
                boundary="actual ASGI health, real Work/Automation owners and SQLite; no provider",
                target=target,
                public_completed_before_release=finished,
                public_entered_aggregate=entered.is_set(),
                actual_runtime_health_has_work=True,
                actual_automation_active_count=0,
            )
            gate.set()
            response = await task
            assert response.status_code == 200
            payload = response.json()
            assert payload["database_read"] == "ok" and payload["database_writer"] == "accepting"
            assert payload["automation_enabled"] is app.settings.automation_enabled
            assert payload["automation_worker_running"] is app.automation_worker.running
            work = payload["runtime_work"]
            scheduler = app.work_scheduler
            assert work["enabled"] is True
            assert work["chat_admission_enabled"] is app.settings.runtime_work_enabled
            assert work["running"] is scheduler.running
            assert work["active_scopes"] == len(scheduler._in_flight)
            assert work["wait_running"] is (
                scheduler._wait_worker is not None and not scheduler._wait_worker.done()
            )
            assert work["last_error_category"] == scheduler._last_error
            assert work["wait_error_category"] == scheduler._last_wait_error
            assert work["maintenance_error_category"] == scheduler._last_maintenance_error
            assert work["phase_timings"] == {
                name: timing.snapshot() for name, timing in scheduler._phase_timings.items()
            }
            assert await original() is not None
            assert finished, "public health waited for Work or Automation history"
    finally:
        gate.set()
        for pending in (waiter, task):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(pending for pending in (waiter, task) if pending is not None),
            return_exceptions=True,
        )
        await app.close()


async def test_authorized_admin_semantic_health_does_not_wait_for_unused_diagnostics(
    database, tmp_path, monkeypatch, latency_evidence
):
    app, _, _ = await build_real_container(database, tmp_path, monkeypatch, False)
    gate = asyncio.Event()
    entered = asyncio.Event()
    task = waiter = None
    http_app = FastAPI()
    attach_admin_api(http_app, lambda: app.admin_api)
    headers = {"Authorization": f"Bearer {HEALTH_TOKEN}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app), base_url="http://local-admin-test"
        ) as client:
            assert (await client.get("/admin/v1/health")).status_code == 401
            baseline = await client.get("/admin/v1/health", headers=headers)
            assert baseline.status_code == 200
            baseline_row = next(
                row for row in baseline.json()["data"] if row["name"] == "semantic_participation"
            )
            owner = app.semantic_participation
            assert baseline_row["enabled"] is owner._observation_ready()
            assert baseline_row["running"] is (owner._task is not None and not owner._task.done())
            assert baseline_row["error_category"] == (
                "model_config_invalid" if owner._model_config_error else None
            )
            lifecycle = await app.lifecycle.health()
            lifecycle_semantic = lifecycle["semantic_participation"]
            assert lifecycle_semantic["configured"] is baseline_row["enabled"]
            assert lifecycle_semantic["running"] is baseline_row["running"]
            assert lifecycle_semantic["model_config_error"] == owner._model_config_error

            module_name = "yuki.services.participation_diagnostics"
            module_exists = importlib.util.find_spec(module_name) is not None
            if module_exists:
                diagnostic_module = importlib.import_module(module_name)
                original = diagnostic_module.participation_diagnostics

                async def blocked(*args, **kwargs):
                    entered.set()
                    await gate.wait()
                    return await original(*args, **kwargs)

                monkeypatch.setattr(diagnostic_module, "participation_diagnostics", blocked)

            task = asyncio.create_task(
                client.get("/admin/v1/health", headers=headers), name="d10-admin"
            )
            waiter = asyncio.create_task(entered.wait())
            done, _ = await asyncio.wait(
                (task, waiter), timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "admin request neither completed nor reached diagnostics"
            finished = task in done and not entered.is_set()
            latency_evidence.update(
                boundary=(
                    "authorized real ASGI admin route, real semantic owner/lifecycle and SQLite"
                ),
                unauthorized_status=401,
                admin_completed_before_release=finished,
                admin_entered_diagnostics=entered.is_set(),
                semantic_lifecycle_retained=True,
            )
            gate.set()
            result = await task
            assert result.status_code == 200
            result_row = next(
                row for row in result.json()["data"] if row["name"] == "semantic_participation"
            )
            for key in ("enabled", "running", "healthy", "error_category"):
                assert result_row[key] == baseline_row[key]

            monkeypatch.setattr(owner, "_model_config_error", "synthetic-invalid-configuration")
            invalid = await client.get("/admin/v1/health", headers=headers)
            invalid_row = next(
                row for row in invalid.json()["data"] if row["name"] == "semantic_participation"
            )
            assert invalid_row["error_category"] == "model_config_invalid"

            def fail_ready():
                raise RuntimeError("synthetic owner health failure")

            monkeypatch.setattr(owner, "_observation_ready", fail_ready)
            failed = await client.get("/admin/v1/health", headers=headers)
            failed_row = next(
                row for row in failed.json()["data"] if row["name"] == "semantic_participation"
            )
            assert failed_row["error_category"] == "health_unavailable"
            assert failed_row["enabled"] is None and failed_row["running"] is None
            latency_evidence["configuration_and_failure_semantics_preserved"] = True
            assert finished, "authorized admin health waited for unused diagnostics"
    finally:
        gate.set()
        for pending in (waiter, task):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(
            *(pending for pending in (waiter, task) if pending is not None),
            return_exceptions=True,
        )
        await app.close()
