"""Saved reflection outputs retain their original result positions and effects."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_self_initiative_memory import claim, finish, record, seed

from yuki.llm.fake import FakeLLMProvider
from yuki.memory.self_reflection.control import ReflectionControlRepository
from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.memory.self_reflection.service import (
    _CHECKPOINT,
    ReflectionCheckpoint,
    SelfReflectionMutationError,
    SelfReflectionService,
)
from yuki.persistence.models import (
    MemoryClaimCandidateEvidenceModel,
    MemoryClaimCandidateModel,
    MemoryFactModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionRequestModel,
    MemorySelfReflectionResultModel,
    MemorySelfReflectionRunModel,
)


def proposal(key):
    return {
        "category": "self_preference",
        "kind": "preference",
        "memory_key": key,
        "content": f"我会记录已核验的产物：{key}。",
        "evidence_refs": ["tool_1"],
    }


def episode():
    return {
        "passages": [
            {"evidence_refs": ["tool_1"], "content": "我生成了曲线绘图，并校验了实际产物。"}
        ],
        "importance": 4,
    }


async def reflection_case(database, output):
    mutations, _, source, initiative = await seed(database)
    await record(database, source, initiative)
    await finish(database, initiative)
    (batch,) = await claim(database)
    settings = make_settings(
        database.url,
        self_memory_enabled=True,
        memory_self_reflection_max_daily_calls=1,
    )
    provider = FakeLLMProvider(lambda _: json.dumps(output, ensure_ascii=False))
    repository = SelfReflectionRepository(database)
    reflection = SelfReflectionService(
        settings=settings,
        repository=repository,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
    )
    return reflection, mutations, repository, provider, batch, source


async def spend_original_request(database, reflection, run_id):
    # The injected local provider makes no physical HTTP call; seed that original
    # paid-request fact explicitly, then exercise the real persistent limit.
    control = ReflectionControlRepository(database, reflection._settings)
    request_id = await control.reserve_request(run_id)
    await control.finish_request(request_id, "completed", 17)
    return control


async def results(database, run_id):
    async with database.sessions() as reader:
        return tuple(
            (row.result_kind, row.result_index, row.fact_id)
            for row in await reader.scalars(
                select(MemorySelfReflectionResultModel)
                .where(MemorySelfReflectionResultModel.run_id == run_id)
                .order_by(
                    MemorySelfReflectionResultModel.result_kind,
                    MemorySelfReflectionResultModel.result_index,
                )
            )
        )


async def test_new_checkpoint_resumes_original_items_without_model_or_budget_replay(
    database, monkeypatch
):
    reflection, mutations, repository, provider, batch, _ = await reflection_case(
        database,
        {"proposals": [proposal("first"), proposal("second")], "episodes": [episode()]},
    )
    mutate = mutations.mutate_resolved

    async def interrupt_second(request, context, **kwargs):
        if kwargs.get("self_reflection_result") == (batch.run_id, "proposal", 2):
            raise RuntimeError("synthetic interruption before the second item commits")
        return await mutate(request, context, **kwargs)

    monkeypatch.setattr(mutations, "mutate_resolved", interrupt_second)
    with pytest.raises(SelfReflectionMutationError, match="proposal_commit_2"):
        await reflection.reflect(batch)
    first = await results(database, batch.run_id)
    assert len(first) == 1 and first[0][:2] == ("proposal", 1)
    raw = json.loads(await repository.load_checkpoint(batch.run_id))
    assert set(raw) == {"output", "event_map", "tool_map", "completed_counts"}
    assert all("operation" not in item for item in raw["output"]["proposals"])
    assert all(item["reason"] == "" for item in raw["output"]["proposals"])
    control = await spend_original_request(database, reflection, batch.run_id)
    monkeypatch.setattr(mutations, "mutate_resolved", mutate)

    assert await reflection.reflect(batch) == (3, 3)
    final = await results(database, batch.run_id)
    assert first[0] in final
    assert [row[:2] for row in final] == [("episode", 1), ("proposal", 1), ("proposal", 2)]
    assert len(provider.requests) == 1
    assert (await control.snapshot())["calls_today"] == 1
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count(MemoryFactModel.id))) == 3
        assert await reader.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 3
    await repository.complete(batch, proposals=3, committed=3)
    assert await reflection.reflect(batch) == (3, 3)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("saved_output", [False, True])
async def test_empty_explanation_does_not_block_new_self_record(database, saved_output):
    item = dict(proposal("empty-explanation"), reason="")
    reflection, _, repository, provider, batch, _ = await reflection_case(
        database, {"proposals": [item]}
    )
    if saved_output:
        _, event_map, tool_map = await reflection._input(batch)
        await repository.save_checkpoint(
            batch.run_id,
            _CHECKPOINT.dump_json(
                ReflectionCheckpoint({"proposals": [item]}, event_map, tool_map)
            ).decode(),
        )
        control = await spend_original_request(database, reflection, batch.run_id)
    assert await reflection.reflect(batch) == (1, 1)
    assert [row[:2] for row in await results(database, batch.run_id)] == [("proposal", 1)]
    assert (
        json.loads(await repository.load_checkpoint(batch.run_id))["output"]["proposals"][0][
            "reason"
        ]
        == ""
    )
    assert len(provider.requests) == int(not saved_output)
    if saved_output:
        assert (await control.snapshot())["calls_today"] == 1


@pytest.mark.parametrize("committed_candidate", [False, True])
async def test_legacy_noop_keeps_indexes_and_committed_episode_is_not_repeated(
    database, committed_candidate
):
    reflection, _, repository, provider, batch, _ = await reflection_case(
        database, {"proposals": [proposal("already-committed")], "episodes": [episode()]}
    )
    assert await reflection.reflect(batch) == (2, 2)
    original_results = await results(database, batch.run_id)
    original = await repository.load_checkpoint(batch.run_id)
    raw = json.loads(original)
    original_proposal = dict(raw["output"]["proposals"][0], operation="create")
    if committed_candidate:
        original_proposal.update(candidate_ref="candidate_1", candidate_decision="accept")
    raw["output"]["proposals"] = [
        original_proposal,
        {"operation": "noop", "candidate_ref": "candidate_1", "candidate_decision": "defer"},
        dict(proposal("remaining-original-third"), operation="create"),
    ]
    raw["completed_counts"] = None
    raw["fact_map"] = {}
    raw["candidate_map"] = {}
    raw["payload"] = (await reflection._input(batch))[0].model_dump(mode="json")
    raw["payload"].update(self_facts=[], existing_episodes=[], self_candidates=[])
    await repository.save_checkpoint(batch.run_id, json.dumps(raw), expected=original)
    control = await spend_original_request(database, reflection, batch.run_id)

    assert await reflection.reflect(batch) == (4, 3)
    recovered = await results(database, batch.run_id)
    assert set(original_results) <= set(recovered)
    assert [row[:2] for row in recovered] == [("episode", 1), ("proposal", 1), ("proposal", 3)]
    assert len(provider.requests) == 1
    assert (await control.snapshot())["calls_today"] == 1
    assert (
        json.loads(await repository.load_checkpoint(batch.run_id))["output"]["proposals"][1][
            "operation"
        ]
        == "noop"
    )


async def test_isolated_legacy_candidate_accept_retains_original_output_and_receipts(database):
    reflection, _, repository, provider, batch, source = await reflection_case(
        database, {"episodes": [episode()]}
    )
    assert await reflection.reflect(batch) == (1, 1)
    existing_results = await results(database, batch.run_id)
    original = await repository.load_checkpoint(batch.run_id)
    raw = json.loads(original)
    now = datetime.now(UTC)
    async with database.sessions() as writer, writer.begin():
        candidate = MemoryClaimCandidateModel(
            fingerprint="a" * 64,
            candidate_type="self",
            target_scope="self",
            target_fingerprint="b" * 64,
            normalized_memory_key="historical:self",
            content="历史候选正文与当前工具回执是两份不同证据。",
            subject_basis="explicit_self_evaluation",
            retention="long_term",
            source_style="plain_statement",
            confidence=0.9,
            status="pending",
            evidence_count=1,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=1),
        )
        writer.add(candidate)
        await writer.flush()
        writer.add(
            MemoryClaimCandidateEvidenceModel(
                candidate_id=candidate.id, event_id=source.id, created_at=now
            )
        )
        candidate_id = candidate.id
    raw["output"]["proposals"] = [
        dict(
            proposal("uncommitted-historical-accept"),
            operation="create",
            candidate_ref="candidate_1",
            candidate_decision="accept",
        ),
        {"operation": "noop", "candidate_ref": "candidate_1", "candidate_decision": "defer"},
    ]
    raw["completed_counts"] = None
    raw["fact_map"] = {}
    raw["candidate_map"] = {
        "candidate_1": {
            "id": candidate_id,
            "candidate_type": "self",
            "target_scope": "self",
            "subject_user_id": None,
            "group_id": None,
            "memory_key": "historical:self",
            "content": "历史候选正文与当前工具回执是两份不同证据。",
            "confidence": 0.9,
            "status": "pending",
            "evidence_count": 1,
            "expires_at": (now + timedelta(days=1)).isoformat(),
        }
    }
    raw["payload"] = (await reflection._input(batch))[0].model_dump(mode="json")
    raw["payload"].update(self_facts=[], existing_episodes=[], self_candidates=[])
    saved = json.dumps(raw, ensure_ascii=False)
    digest = hashlib.sha256(saved.encode()).hexdigest()
    await repository.save_checkpoint(batch.run_id, saved, expected=original)
    async with database.sessions() as writer, writer.begin():
        row = await writer.get(MemorySelfReflectionRunModel, batch.run_id)
        row.status, row.retry_state, row.attempt_count = "failed", "isolated", 3
        row.proposal_count, row.committed_count = 3, 1
    control = await spend_original_request(database, reflection, batch.run_id)

    with pytest.raises(SelfReflectionMutationError, match="proposal_commit_1"):
        await reflection.reflect(batch)
    after = await repository.load_checkpoint(batch.run_id)
    assert after == saved and hashlib.sha256(after.encode()).hexdigest() == digest
    assert await results(database, batch.run_id) == existing_results
    assert len(provider.requests) == 1
    assert (await control.snapshot())["calls_today"] == 1
    async with database.sessions() as reader:
        row = await reader.get(MemorySelfReflectionRunModel, batch.run_id)
        assert (row.status, row.retry_state, row.attempt_count) == ("failed", "isolated", 3)
        assert (row.proposal_count, row.committed_count) == (3, 1)
        assert (await reader.get(MemoryClaimCandidateModel, candidate_id)).status == "pending"
        assert await reader.scalar(select(func.count(MemoryFactModel.id))) == 1
        assert await reader.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 1
        assert await reader.scalar(select(func.count(MemorySelfReflectionRequestModel.id))) == 1
