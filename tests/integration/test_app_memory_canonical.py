"""Personal HTTP ingress exercises Memory without a QQ account projection."""

import json
from uuid import uuid4

from sqlalchemy import func, select, update
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.support.service_dependencies import tool_dependencies
from tests.unit.test_app_messages import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_memory_mutation import _service
from tests.unit.test_memory_rebuild import _admin
from tests.unit.test_memory_rebuild import _service as rebuild_service

from yuki.identity.db_models import IdentityBindingModel
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.models import MemoryFactQuery
from yuki.memory.quality.audit import MemoryProductionQualityAudit
from yuki.memory.quality.hygiene import MemoryProvenanceHygiene
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.memory.rebuild.worker import MemoryRebuildWorker
from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.memory.self_reflection.service import SelfReflectionService
from yuki.persistence.models import (
    ChatEventModel,
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from yuki.persistence.people_repository import PeopleRepository
from yuki.services.agent_tools import AgentToolService


async def test_app_memory_write_search_detail_and_correction_keep_original_person(
    app_env, database, monkeypatch
):
    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    chat = env.harness.processor._chat
    runtimes = []
    original = chat._run_agent

    async def capture(key, messages, runtime, **kwargs):
        runtimes.append(runtime)
        return await original(key, messages, runtime, **kwargs)

    monkeypatch.setattr(chat, "_run_agent", capture)
    env.provider._responder = lambda _: "setup"
    response = await env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "我喜欢喝茶，现在改为喜欢咖啡"},
    )
    assert response.status_code == 200, response.text
    await drain(env)
    runtime = runtimes[0]
    assert runtime.require_actor().person_id == person
    profile = await PeopleRepository(database).get(person_id=person)
    assert profile is not None and profile.user_id == person
    async with database.read_session() as reader:
        assert not await reader.scalar(
            select(IdentityBindingModel.id).where(
                IdentityBindingModel.person_id == person, IdentityBindingModel.platform == "qq"
            )
        )
    mutations, facts, ledger, _ = _service(database)
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=ledger,
        memories=facts,
        memory_mutations=mutations,
        **tool_dependencies(database, make_settings(database.url)),
    )
    created = (
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "create",
                    "target": {"subject_ref": "current_speaker", "scope_type": "person"},
                    "new_content": "喜欢喝茶",
                    "memory_key": "drink",
                    "category": "preference",
                    "evidence_quote": "我喜欢喝茶",
                }
            ),
            runtime,
        )
    ).model_payload()
    assert created["ok"], created
    fact_id = created["data"]["new_fact_id"]
    fact = await facts.get_fact(fact_id)
    assert fact.canonical_subject_person_id == person
    assert [row.id for row in await facts.list_person(person_id=person)] == [fact_id]
    found = (await tools.execute("search_memory", '{"query":"喝茶"}', runtime)).model_payload()
    assert [row["fact_id"] for row in found["data"]["memories"]] == [fact_id]
    assert found["data"]["memories"][0]["subject"] == {"person_id": person, "space_id": None}
    detail = (
        await tools.execute(
            "read_memory", json.dumps({"view": "fact", "fact_id": fact_id}), runtime
        )
    ).model_payload()
    evidence = (
        await tools.execute(
            "read_memory", json.dumps({"view": "evidence", "fact_id": fact_id}), runtime
        )
    ).model_payload()
    assert detail["ok"] and evidence["ok"] and evidence["data"]["evidence"]
    corrected = (
        await tools.execute(
            "memory_change",
            json.dumps(
                {
                    "operation": "correct",
                    "fact_id": fact_id,
                    "new_content": "喜欢咖啡",
                    "evidence_quote": "喜欢咖啡",
                }
            ),
            runtime,
        )
    ).model_payload()
    assert corrected["ok"], corrected
    replacement = await facts.get_fact(corrected["data"]["new_fact_id"])
    assert (
        replacement.canonical_subject_person_id == person and replacement.supersedes_id == fact_id
    )


async def test_app_only_self_reflection_claim_and_write_do_not_require_qq_binding(
    app_env, database
):
    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    env.provider._responder = lambda _: "收到"
    response = await env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "你很喜欢认真核对事实"},
    )
    assert response.status_code == 200, response.text
    await drain(env)
    await repository.scan_new_events()
    (batch,) = await repository.claim_due(
        scheduled_slot="app-reflection",
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=10,
        max_characters=16000,
        force=True,
    )
    assert batch.state.canonical_person_id == person
    assert batch.state.external_person_id is None
    mutations, facts, _, _ = _service(database, self_memory_enabled=True)
    provider = FakeLLMProvider(
        lambda _: json.dumps(
            {
                "proposals": [
                    {
                        "operation": "create",
                        "category": "self_preference",
                        "kind": "preference",
                        "memory_key": "self:verify",
                        "content": "我喜欢认真核对事实",
                        "importance": 3,
                        "confidence": 0.8,
                        "reason": "current feedback",
                        "evidence_refs": ["event_1"],
                        "visibility": "current_scope",
                    }
                ]
            }
        )
    )
    service = SelfReflectionService(
        settings=make_settings(database.url, self_memory_enabled=True),
        repository=repository,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
    )
    payload, *_ = await service._input(batch)
    assert payload.person_id == person and payload.private_peer_user_id is None
    assert await service.reflect(batch) == (1, 1)
    async with database.read_session() as reader:
        assert not await reader.scalar(
            select(IdentityBindingModel.id).where(
                IdentityBindingModel.person_id == person, IdentityBindingModel.platform == "qq"
            )
        )
    visible = await facts.repository.list_facts(
        MemoryFactQuery(
            scope_type="self",
            visibility_type="private",
            canonical_visibility_person_id=person,
        )
    )
    assert len(visible) == 1 and visible[0].canonical_visibility_person_id == person


async def test_app_rebuild_preserves_source_owner_after_review_commit_and_staging_purge(
    app_env, database
):
    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    env.provider._responder = lambda _: "收到"
    response = await env.client.post(
        "/client/v1/messages",
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "我住在杭州"},
    )
    assert response.status_code == 200, response.text
    await drain(env)
    async with database.read_session() as reader:
        event = (
            await reader.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.author_person_id == person,
                    ChatEventModel.direction == "inbound",
                )
            )
        ).one()
    settings, _, facts, provider, service = await rebuild_service(database)
    # This historical extraction failed before review; rebuild replaces only
    # the failed live receipt, never the normal pending live job.
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryJobModel)
            .where(
                MemoryJobModel.event_id == event.id,
            )
            .values(status="failed", error_category="historical-provider-failure")
        )
    actor = await _admin(database)
    run = await service.plan(
        MemoryRebuildSelection(
            minimum_event_id=event.id,
            maximum_event_id=event.id,
            include_failed_live_jobs=True,
        ),
        actor_user_id=actor,
    )
    await service.start(run.public_id, actor_user_id=actor)
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    assert await service.set_review(run.public_id, "all", approved=True, actor_user_id=actor) == 1
    await service.commit(run.public_id, actor_user_id=actor)
    assert await worker.process_once() == 1
    stored = await facts.list_person(person_id=person)
    assert len(stored) == 1 and stored[0].canonical_subject_person_id == person
    assert [row.event_id for row in await facts.list_evidence(stored[0].id)] == [event.id]
    assert provider.requests == 1
    async with database.read_session() as reader:
        original_run = await reader.scalar(
            select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == run.public_id)
        )
        original_run_id = original_run.id
        original_job = (
            await reader.execute(
                select(
                    MemoryJobModel.id,
                    MemoryJobModel.status,
                    MemoryJobModel.outcome,
                    MemoryJobModel.rebuild_run_id,
                ).where(MemoryJobModel.event_id == event.id)
            )
        ).one()
        assert original_job.status == "done" and original_job.rebuild_run_id == original_run_id
    hygiene = MemoryProvenanceHygiene(database)
    plan = await hygiene.scan()
    assert original_run_id in plan.purge_terminal_rebuild_run_ids
    assert stored[0].id not in plan.invalid_fact_ids
    await hygiene.apply(plan.fingerprint)
    async with database.read_session() as reader:
        assert await reader.get(MemoryRebuildRunModel, original_run_id) is not None
        assert not await reader.scalar(
            select(func.count(MemoryRebuildItemModel.id)).where(
                MemoryRebuildItemModel.run_id == original_run_id
            )
        )
        assert not await reader.scalar(
            select(func.count(MemoryRebuildProposalModel.id)).where(
                MemoryRebuildProposalModel.run_id == original_run_id
            )
        )
        assert (
            await reader.execute(
                select(
                    MemoryJobModel.id,
                    MemoryJobModel.status,
                    MemoryJobModel.outcome,
                    MemoryJobModel.rebuild_run_id,
                ).where(MemoryJobModel.event_id == event.id)
            )
        ).one() == original_job
    audit = await MemoryProductionQualityAudit(database).run()
    assert not any(
        issue.count
        and issue.issue_code
        in {
            "rebuild_receipt_without_proposal",
            "memory_job_source_invalid",
            "evidence_source_invalid",
        }
        for issue in audit.issues
    )
    assert [row.event_id for row in await facts.list_evidence(stored[0].id)] == [event.id]
