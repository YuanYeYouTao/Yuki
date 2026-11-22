"""E2E: memory layering P3, extraction tightening (#682).

Authoritative spec: docs/architecture/Yuki-记忆分层P3提取收紧任务书-2026-11-22.md §8
(cases 1-16 = X1-X16); every §9 question follows its 【推荐】 option.

Real SQLite, the real batch extraction worker (``MemoryWorker.process_once``) and the
real write path, the real Rebuild state machine, the real Dream service, repository and
worker loop body, real Main turns, the real ``yuki`` CLI (child process) for
``show/apply/revert`` and the real scan entry. The model is the only stand-in: the
extraction, Dream and scan doubles return fixed structured output. A double cannot
prove that a real model follows the v8 instruction (that is the offline replay of
§11.2); these cases prove the switch routing, the instruction and schema sent, what
gets written, and the deterministic clean-up and Dream behaviour.

Each case writes normalized JSON (instruction / schema digests and versions, written
facts with ID, kind, category and status, plan files, Dream clusters and operations;
person/space/conversation IDs replaced by fixture labels, times stripped) to
``test-results/memory-extraction-tightening/<X-case>.json``; identical input gives
byte-identical artifacts.

Failure cases, written before the implementation:
  X1  with the switch on, extraction still sends the old instruction (or the double's
      single long-term claim is not the only fact written);
  X2  with the switch off, the instruction, schema or versions differ from the deployed
      ones byte for byte, or the model's category is not written as given;
  X3  one batch writes two facts for one meaning (the v8 "one claim per meaning" line
      is missing);
  X4  with the switch on, a free category is still written, or the output schema still
      has ``category`` (an extra ``category`` from the model is not ignored);
  X5  Rebuild and live extraction send different v8 paragraphs, or Rebuild does not
      record the v8 versions;
  X6  flipping the switch while a batch request is in flight mixes variants inside the
      batch, or the next batch does not follow the new value;
  X7  the switch changes the recovery-contract hash or the Main system message;
  X8  two automatic synonyms of an explicit fact still coexist after a Dream merge to it
      (or the explicit anchor changes);
  X9  the per-run cluster cap is still 12 (20 pending clusters are not all planned, the
      run pays more than 24 calls, or the P1 selection step is counted in them);
  X10 the clean-up plan contains an explicit fact, a SELF fact or an episode;
  X11 ``apply`` invalidates anything when a planned fact changed or was retracted;
  X12 the clean-up cannot be reverted, or ``revert`` restores a fact that was later
      invalidated for another reason;
  X13 the Dream backlog is invisible (``DreamHealth.unreviewed_facts`` missing or not
      falling by the reviewed count);
  X14 the scan waits for the model inside a write transaction, or blocks other writers;
  X15 the plan file, the CLI output, the stats output or the logs carry fact text;
  X16 a cleaned fact still renders in the resident segment, or an unplanned fact
      (the evaluation-set basis analogue) changes state.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from tests.support.memory_resident_scene import (
    QQ,
    ResidentScene,
    _refs,
    build_dream_worker,
    build_selection,
    normalize,
    run_cli,
    system_text,
    tool_call,
)

from yuki.memory.enums import (
    MemoryAuthority,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryKind,
    MemorySourceType,
)
from yuki.model_runtime.models import ModelTask, StructuredOutputMode
from yuki.persistence.models import (
    MemoryFactModel,
    MemoryFactRelationModel,
    MemoryFactStateEventModel,
)

RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "memory-extraction-tightening"
EXPLICIT = {"source_type": MemorySourceType.EXPLICIT, "authority": MemoryAuthority.EXPLICIT}

# §4.1, verbatim. The v8 instruction keeps every old sentence and adds these.
V8_SENTENCES = (
    "只为当前这件事给出的要求、决定或安排——指向某个 PR、issue、分支、文件、任务书、"
    "当次交付物或当次会话——是当次任务指令，不提取；它们留在会话历史与任务记录里。",
    "对方表明以后都适用、或描述的是一般做事方式的，才作为长期偏好提取。",
    "同一批里同一个意思只输出一条 claim。",
)

# Digests of what main f0d7fed9 sends (instruction as sent, schema with sorted keys).
# X2 proves the switch-off side stays byte-identical to the deployed contract.
DEPLOYED = {
    "batch_instruction": "46b1108b003a89e54aa35662db1a8de719c73d7d8bd7d0b031ef5133b35c2ba8",
    "batch_schema": "b630c6c234f1080e0b121b621da4d24e01d9c53cc7bab9874f26c548bce65730",
    "single_instruction": "21a902d7aac2309dedbb0a97a20e48362e9b982a88ae696fae1a91914ba941f4",
    "single_schema": "c842c449eb8abd0e4005758ac4d200b0086b4244de0056fa72bfe2314a681c7a",
}
LEGACY_VERSIONS = {"batch_prompt": "memory-v2-batch-extraction-v6", "batch_schema": "5"}
TIGHTENED_VERSIONS = {"batch_prompt": "memory-v2-batch-extraction-v7", "batch_schema": "6"}


@pytest.fixture
async def scene(database, tmp_path):
    scene = await ResidentScene(database, tmp_path).build(
        memory_batch_trigger_count=1,
        memory_batch_max_wait_seconds=0,
        memory_rebuild_enabled=True,
    )
    # Extraction requests carry their output schema as the function-tool parameters
    # (a production structured mode), so the schema actually sent can be compared.
    models = scene.harness.processor._memory_worker.extractor._models
    plain = models.structured_output_mode
    models.structured_output_mode = lambda task: (
        StructuredOutputMode.FUNCTION_TOOL if task is ModelTask.MEMORY_EXTRACTION else plain(task)
    )
    return scene


# ---------------------------------------------------------------------- helpers
def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _schema(request) -> dict[str, Any]:
    (tool,) = request.tools
    assert tool.name == "emit_result"
    return dict(tool.parameters)


def _emit(payload: dict[str, Any]):
    """The structured result as the one ``emit_result`` call of a function-tool reply."""
    return tool_call("emit_result", payload, f"emit-{_sha(json.dumps(payload))[:12]}")


def _schema_sha(request) -> str:
    return _sha(json.dumps(_schema(request), ensure_ascii=False, sort_keys=True))


def _instruction(request) -> str:
    return str(request.messages[0].content or "")


def _claim_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """The claim object of the output schema, wherever pydantic placed it."""
    for definition in schema.get("$defs", {}).values():
        if "evidence_quote" in definition.get("properties", {}):
            return definition
    raise AssertionError("no claim definition in the output schema")


def _artifact(scene: ResidentScene, case: str, payload: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{case}.json"
    path.write_text(normalize(payload, scene.labels()) + "\n", encoding="utf-8")
    return path


async def _set_tightened(scene: ResidentScene, enabled: bool) -> None:
    """Toggle ``memory.extraction_tightened_enabled`` through the real config owner."""
    change = await scene.chat._runtime_config.set_override(
        "memory.extraction_tightened_enabled",
        enabled,
        scope_type="global",
        scope_id="",
        actor_user_id=QQ["A"],
        trigger_message_id=f"tightening-e2e-{enabled}-{scene._next()}",
    )
    assert change.success, change
    assert (await scene.runtime()).memory.extraction_tightened_enabled is enabled


def _batch_reply(request, claims: list[dict[str, Any]]):
    """Raw structured output: claims anchored to the event whose text holds the quote."""
    payload = json.loads(request.messages[-1].content)
    return _emit(
        {
            "claims": [
                {
                    "source_event_id": next(
                        item["source_event_id"]
                        for item in payload["events"]
                        if claim["evidence_quote"] in item["content"]
                    ),
                    "claim": claim,
                }
                for claim in claims
            ]
        }
    )


async def _extract(
    scene: ResidentScene, event_keys: list[str], claims: list[dict[str, Any]]
) -> tuple[list[Any], list[int]]:
    """One real live batch over ``event_keys``; returns its requests and new fact IDs."""
    requests: list[Any] = []

    def respond(request):
        requests.append(request)
        return _batch_reply(request, claims)

    worker = scene.harness.processor._memory_worker
    before = max(await scene.fact_ids_after(0), default=0)
    scene.extraction_responder = respond
    try:
        for key in event_keys:
            event = scene.records[key]
            await worker.enqueue(event.id, event.scope.key)
        assert await worker.process_once() == len(event_keys)
    finally:
        scene.extraction_responder = None
    return requests, await scene.fact_ids_after(before)


async def _facts(scene: ResidentScene, fact_ids: list[int]) -> list[dict[str, Any]]:
    async with scene.database.read_session() as reader:
        rows = (
            await reader.scalars(
                select(MemoryFactModel)
                .where(MemoryFactModel.id.in_(fact_ids))
                .order_by(MemoryFactModel.id)
            )
        ).all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "category": row.category,
            "status": row.status,
            "invalidated_reason": row.invalidated_reason,
            "source_type": row.source_type,
        }
        for row in rows
    ]


def _claim(content: str, quote: str, *, kind: str = "preference", **extra: Any) -> dict[str, Any]:
    return {
        "subject_ref": "speaker",
        "scope_type": "person",
        "kind": kind,
        "memory_key": f"habit:{_sha(content)[:8]}",
        "content": content,
        "evidence_quote": quote,
        "importance": 4,
        "confidence": 0.9,
        "source_type": "automatic",
        **extra,
    }


def _version_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("memory_extraction_batch_started")
    ]


def _versions_of(line: str) -> dict[str, str]:
    fields = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
    return {"batch_prompt": fields["prompt_version"], "batch_schema": fields["schema_version"]}


MIXED = "这次 PR #12 先别合，等我看完任务书再说；另外以后汇报都先写结论再写过程"
LONG_TERM_QUOTE = "以后汇报都先写结论再写过程"


# --------------------------------------------------------------------------- X1
async def test_x01_switch_on_sends_v8_and_writes_only_the_long_term_claim(scene, caplog):
    """Case 1: v8 instruction and versions; the double's one long-term claim is all."""
    from yuki.memory.event_extractor import TIGHTENED_BATCH_EXTRACTION_INSTRUCTION

    caplog.set_level(logging.INFO, logger="yuki.memory.worker")
    await _set_tightened(scene, True)
    await scene.event("private:A", "A", MIXED, key="mixed")
    requests, written = await _extract(
        scene, ["mixed"], [_claim("汇报先写结论再写过程", LONG_TERM_QUOTE)]
    )

    (request,) = requests
    assert _instruction(request) == TIGHTENED_BATCH_EXTRACTION_INSTRUCTION.strip()
    assert all(sentence in _instruction(request) for sentence in V8_SENTENCES)
    assert _sha(_instruction(request)) != DEPLOYED["batch_instruction"]
    (line,) = _version_lines(caplog)
    assert _versions_of(line) == TIGHTENED_VERSIONS
    facts = await _facts(scene, written)
    assert [(row["kind"], row["category"]) for row in facts] == [("preference", "preference")]
    _artifact(
        scene,
        "X1",
        {
            "instruction_sha256": _sha(_instruction(request)),
            "schema_sha256": _schema_sha(request),
            "versions": _versions_of(line),
            "facts": facts,
        },
    )


# --------------------------------------------------------------------------- X2
async def test_x02_switch_off_is_byte_identical_to_the_deployed_contract(scene, caplog):
    """Case 2: switch off -> deployed instruction, schema and versions; model category kept."""
    caplog.set_level(logging.INFO, logger="yuki.memory.worker")
    await scene.event("private:A", "A", MIXED, key="mixed")
    requests, written = await _extract(
        scene,
        ["mixed"],
        [_claim("汇报先写结论再写过程", LONG_TERM_QUOTE, category="工作习惯")],
    )

    (request,) = requests
    assert _sha(_instruction(request)) == DEPLOYED["batch_instruction"]
    assert _schema_sha(request) == DEPLOYED["batch_schema"]
    assert not any(sentence in _instruction(request) for sentence in V8_SENTENCES)
    (line,) = _version_lines(caplog)
    assert _versions_of(line) == LEGACY_VERSIONS
    facts = await _facts(scene, written)
    assert [row["category"] for row in facts] == ["工作习惯"]
    _artifact(
        scene,
        "X2",
        {
            "instruction_sha256": _sha(_instruction(request)),
            "schema_sha256": _schema_sha(request),
            "versions": _versions_of(line),
            "facts": facts,
        },
    )


# --------------------------------------------------------------------------- X3
async def test_x03_one_meaning_in_one_batch_is_one_fact(scene):
    """Case 3: two synonymous events in one batch; the v8 double answers once."""
    await _set_tightened(scene, True)
    await scene.event("private:A", "A", "以后回我消息先说结论", key="s1")
    await scene.event("private:A", "A", "记得先给结论再展开讲", key="s2")
    requests, written = await _extract(
        scene, ["s1", "s2"], [_claim("回复先给结论再展开", "以后回我消息先说结论")]
    )

    (request,) = requests
    events = json.loads(request.messages[-1].content)["events"]
    assert len(events) == 2
    assert "同一批里同一个意思只输出一条 claim。" in _instruction(request)
    facts = await _facts(scene, written)
    assert len(facts) == 1
    _artifact(
        scene,
        "X3",
        {
            "batch_events": len(events),
            "instruction_sha256": _sha(_instruction(request)),
            "facts": facts,
        },
    )


# --------------------------------------------------------------------------- X4
async def test_x04_category_comes_from_kind_when_the_switch_is_on(scene):
    """Case 4: no category in the output schema; an extra one from the model is ignored."""
    await _set_tightened(scene, True)
    await scene.event(
        "private:A", "A", "我在杭州上班；我喜欢喝乌龙茶；昨天我们一起看了烟火", key="three"
    )
    requests, written = await _extract(
        scene,
        ["three"],
        [
            _claim("在杭州上班", "我在杭州上班", kind="fact", category="自由类别一"),
            _claim("喜欢喝乌龙茶", "我喜欢喝乌龙茶", kind="preference", category="自由类别二"),
            _claim(
                "和 Yuki 一起看了烟火",
                "昨天我们一起看了烟火",
                kind="episode",
                category="自由类别三",
            ),
        ],
    )

    (request,) = requests
    claim_schema = _claim_properties(_schema(request))
    assert "category" not in claim_schema["properties"]
    assert "category" not in claim_schema.get("required", [])
    facts = await _facts(scene, written)
    assert [(row["kind"], row["category"]) for row in facts] == [
        ("fact", "fact"),
        ("preference", "preference"),
        ("episode", "episode"),
    ]
    _artifact(
        scene,
        "X4",
        {
            "claim_schema_properties": sorted(claim_schema["properties"]),
            "schema_sha256": _schema_sha(request),
            "facts": facts,
        },
    )


# --------------------------------------------------------------------------- X5
def _v8_paragraph(instruction: str) -> str:
    start = instruction.index(V8_SENTENCES[0][:12])
    end = instruction.index(V8_SENTENCES[-1]) + len(V8_SENTENCES[-1])
    return instruction[start:end]


async def test_x05_rebuild_and_live_send_the_same_v8_paragraph(scene):
    """Case 5: the same event through Rebuild and live; Rebuild records the v8 versions."""
    from yuki.memory.rebuild.models import MemoryRebuildSelection
    from yuki.memory.rebuild.repository import MemoryRebuildRepository
    from yuki.memory.rebuild.service import MemoryRebuildService, extraction_fingerprint

    await _set_tightened(scene, True)
    event_id = await scene.event("private:A", "A", MIXED, key="mixed")
    worker = scene.harness.processor._memory_worker
    rebuild = MemoryRebuildService(
        settings=scene.settings,
        repository=MemoryRebuildRepository(scene.database),
        ledger=scene.harness.ledger,
        extractor=worker.extractor,
        processor=worker.processor,
        runtime_config=scene.chat._runtime_config,
    )
    rebuild_requests: list[Any] = []

    def respond(request):
        rebuild_requests.append(request)
        if MIXED not in request.messages[-1].content:
            return _emit({"claims": []})
        return _emit(
            {"claims": [_claim("汇报先写结论再写过程", LONG_TERM_QUOTE, category="多给的类别")]}
        )

    previous, scene.responder = scene.responder, respond
    try:
        run = await rebuild.plan(
            MemoryRebuildSelection(event_ids=(event_id,)), actor_user_id=scene.persons["A"]
        )
        await rebuild.start(run.public_id, actor_user_id=scene.persons["A"])
        while await rebuild.process_extraction_once(
            await rebuild.repository.get_run(run.public_id)
        ):
            pass
    finally:
        scene.responder = previous
    # Rebuild skips events live extraction already processed, so it goes first.
    live_requests, _ = await _extract(
        scene, ["mixed"], [_claim("汇报先写结论再写过程", LONG_TERM_QUOTE)]
    )

    (live,) = live_requests
    (rebuilt,) = rebuild_requests
    assert _v8_paragraph(_instruction(live)) == _v8_paragraph(_instruction(rebuilt))
    assert "category" not in _claim_properties(_schema(rebuilt))["properties"]
    model_name = worker.extractor.model_name
    stored = (await rebuild.repository.get_run(run.public_id)).extraction_fingerprint
    assert stored == extraction_fingerprint(scene.settings, model_name=model_name, tightened=True)
    assert stored != extraction_fingerprint(scene.settings, model_name=model_name, tightened=False)
    async with scene.database.read_session() as reader:
        from yuki.persistence.models import MemoryRebuildProposalModel

        proposals = [
            json.loads(value)
            for value in await reader.scalars(select(MemoryRebuildProposalModel.claim_json))
        ]
    assert [item["category"] for item in proposals] == ["preference"]
    _artifact(
        scene,
        "X5",
        {
            "v8_paragraph_sha256": _sha(_v8_paragraph(_instruction(live))),
            "rebuild_instruction_sha256": _sha(_instruction(rebuilt)),
            "rebuild_schema_sha256": _schema_sha(rebuilt),
            "rebuild_records_v8": True,
            "staged_categories": [item["category"] for item in proposals],
        },
    )


# --------------------------------------------------------------------------- X6
async def test_x06_a_batch_keeps_its_starting_snapshot(scene):
    """Case 6: flip the switch while the batch request is in flight."""
    await _set_tightened(scene, True)
    await scene.event("private:A", "A", "我平时用 Vim 写代码", key="first")
    worker = scene.harness.processor._memory_worker
    requests: list[Any] = []
    claims = {
        "first": _claim("用 Vim 写代码", "我平时用 Vim 写代码", kind="fact", category="编辑器"),
        "second": _claim("周末去爬山", "我周末常去爬山", kind="fact", category="运动"),
    }

    def respond(request):
        requests.append(request)
        events = json.loads(request.messages[-1].content)["events"]
        key = "first" if any("Vim" in item["content"] for item in events) else "second"
        return _batch_reply(request, [claims[key]])

    provider = scene.provider
    number = len(provider.ownership) + 1
    provider.arrived[number] = asyncio.Event()
    provider.release[number] = asyncio.Event()
    before = max(await scene.fact_ids_after(0), default=0)
    scene.extraction_responder = respond
    try:
        event = scene.records["first"]
        await worker.enqueue(event.id, event.scope.key)
        batch = asyncio.create_task(worker.process_once())
        await provider.wait(number)
        await _set_tightened(scene, False)
        provider.release[number].set()
        assert await batch == 1
        first_facts = await _facts(scene, await scene.fact_ids_after(before))

        await scene.event("private:A", "A", "我周末常去爬山", key="second")
        middle = max(await scene.fact_ids_after(0))
        event = scene.records["second"]
        await worker.enqueue(event.id, event.scope.key)
        assert await worker.process_once() == 1
        second_facts = await _facts(scene, await scene.fact_ids_after(middle))
    finally:
        scene.extraction_responder = None

    first_request, second_request = requests
    assert all(sentence in _instruction(first_request) for sentence in V8_SENTENCES)
    assert [row["category"] for row in first_facts] == ["fact"]
    assert _sha(_instruction(second_request)) == DEPLOYED["batch_instruction"]
    assert [row["category"] for row in second_facts] == ["运动"]
    _artifact(
        scene,
        "X6",
        {
            "first_batch": {
                "instruction_sha256": _sha(_instruction(first_request)),
                "facts": first_facts,
            },
            "second_batch": {
                "instruction_sha256": _sha(_instruction(second_request)),
                "facts": second_facts,
            },
        },
    )


# --------------------------------------------------------------------------- X7
async def test_x07_switch_never_touches_main(scene):
    """Case 7: recovery contract hash and the Main system message are unchanged."""
    off_contract = await scene.contract()
    off_turn = await scene.turn("A", "开关关着的一轮")
    await _set_tightened(scene, True)
    on_contract = await scene.contract()
    on_turn = await scene.turn("A", "开关开着的一轮")

    assert off_contract == on_contract
    assert system_text(off_turn.requests[0]) == system_text(on_turn.requests[0])
    _artifact(
        scene,
        "X7",
        {
            "recovery_contract": [off_contract, on_contract],
            "system_sha256": [
                _sha(system_text(off_turn.requests[0])),
                _sha(system_text(on_turn.requests[0])),
            ],
        },
    )


# --------------------------------------------------------------------------- X8
def _dream_aliases(request, contents: dict[int, str]) -> dict[int, str]:
    by_text = {" ".join(content.split()): fact_id for fact_id, content in contents.items()}
    aliases: dict[int, str] = {}
    for message in request.messages:
        try:
            payload = json.loads(message.content or "")
        except ValueError:
            continue
        for row in _refs(payload):
            fact_id = by_text.get(" ".join(str(row["content"]).split()))
            if fact_id is not None:
                aliases[fact_id] = str(row["ref"])
    return aliases


async def test_x08_dream_merges_automatic_synonyms_into_the_explicit_anchor(scene):
    """Case 8: existing Dream merge, now reachable for backlog synonyms."""
    a1 = await scene.event("private:A", "A", "记住：回复先写结论", key="a1")
    a2 = await scene.event("private:A", "A", "回我的时候先讲结论", key="a2")
    a3 = await scene.event("private:A", "A", "先说结论，再说过程", key="a3")
    anchor = await scene.fact(
        "anchor", "A", "回复先写结论", evidence=(a1,), kind=MemoryKind.PREFERENCE, **EXPLICIT
    )
    first = await scene.fact(
        "syn1", "A", "回复时先讲结论", evidence=(a2,), kind=MemoryKind.PREFERENCE
    )
    second = await scene.fact(
        "syn2", "A", "先说结论再说过程", evidence=(a3,), kind=MemoryKind.PREFERENCE
    )
    async with scene.database.read_session() as reader:
        anchor_before = (await reader.get(MemoryFactModel, anchor)).content

    def merge(request):
        aliases = _dream_aliases(request, scene.contents)
        return json.dumps(
            {
                "actions": [
                    {
                        "operation": "merge",
                        "source_refs": [aliases[anchor], aliases[first], aliases[second]],
                        "anchor_ref": aliases[anchor],
                    }
                ]
            }
        )

    scene.dream_responder = merge
    run = await scene.dream_run(clusters=(("anchor", "syn1", "syn2"),))
    async with scene.database.read_session() as reader:
        relations = sorted(
            (row.source_fact_id, row.target_fact_id)
            for row in (
                await reader.scalars(
                    select(MemoryFactRelationModel).where(
                        MemoryFactRelationModel.relation_type
                        == MemoryFactRelationType.EQUIVALENT.value
                    )
                )
            ).all()
        )
        anchor_after = await reader.get(MemoryFactModel, anchor)

    facts = await _facts(scene, [anchor, first, second])
    assert run.status.value == "completed"
    assert [row["status"] for row in facts] == ["active", "superseded", "superseded"]
    assert relations == [(first, anchor), (second, anchor)]
    assert anchor_after.content == anchor_before and anchor_after.status == "active"
    _artifact(scene, "X8", {"facts": facts, "equivalent_to_anchor": relations})


# ------------------------------------------------------------------ X9 / X13 Dream
class _OrthogonalEmbedding:
    """Fake embedding with one-hot vectors: every fact is its own singleton cluster."""

    def __new__(cls):
        from yuki.memory.embedding.fake import FakeEmbeddingProvider
        from yuki.memory.embedding.models import EmbeddingVector

        class Provider(FakeEmbeddingProvider):
            def __init__(self) -> None:
                super().__init__(dimensions=256)
                self._slots: dict[str, int] = {}

            def _vector(self, text: str) -> EmbeddingVector:
                slot = self._slots.setdefault(text, len(self._slots))
                values = tuple(1.0 if index == slot else 0.0 for index in range(256))
                return EmbeddingVector(values=values, dimensions=256)

        return Provider()


class _HttpBoundaryExecutor:
    """Injected executor that runs the per-attempt accounting hook like the HTTP client.

    ``json_http`` awaits ``before_provider_request`` right before every dispatch; Dream
    reserves its run budget there and the P1 selection step counts its own calls there.
    """

    def __new__(cls, provider):
        from tests.support.model_executor import InjectedModelExecutor

        from yuki.model_runtime.request_accounting import before_provider_request

        class Executor(InjectedModelExecutor):
            async def execute(self, task, request, **options):
                account = before_provider_request.get()
                if account is not None:
                    await account()
                return await super().execute(task, request, **options)

        return Executor(provider)


async def _scheduled_dream(scene: ResidentScene):
    """Real embeddings (fake provider) + the real incremental plan + the worker loop body."""
    from yuki.memory.dream.repository import DreamRepository
    from yuki.memory.dream.service import DreamService
    from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
    from yuki.memory.embedding.worker import MemoryEmbeddingWorker
    from yuki.memory.fts import SQLiteMemoryFTSIndex
    from yuki.memory.retrieval import MemoryRetriever
    from yuki.services.concurrency import ConcurrencyManager

    settings = scene.settings.model_copy(
        update={
            "memory_embedding_enabled": True,
            "memory_embedding_worker_enabled": False,
            "memory_embedding_dimensions": 256,
        }
    )
    memories = scene.chat._memories
    provider = _OrthogonalEmbedding()
    embeddings = MemoryEmbeddingRuntime(
        settings=settings,
        database=scene.database,
        facts=memories,
        retriever=MemoryRetriever(
            repository=memories.repository, lexical_index=SQLiteMemoryFTSIndex(scene.database)
        ),
        provider=provider,
    )
    await embeddings.start()
    assert embeddings.jobs is not None
    vectors = MemoryEmbeddingWorker(
        provider=provider,
        jobs=embeddings.jobs,
        interval_seconds=1,
        claim_limit=20,
        retry_initial_seconds=1,
    )
    while await vectors.process_once():
        pass
    models = _HttpBoundaryExecutor(scene.provider)
    repository = DreamRepository(scene.database)
    service = DreamService(
        settings=settings,
        repository=repository,
        facts=memories,
        mutations=scene.harness_mutations(),
        embeddings=embeddings,
        models=models,
        concurrency=ConcurrencyManager(),
    )
    worker = build_dream_worker(scene, repository, service, build_selection(scene, models))
    return repository, service, worker


def _keep_all(request) -> str:
    refs = sorted(
        {
            str(row["ref"])
            for message in request.messages[1:]
            for row in _refs(json.loads(message.content or "null"))
        }
    )
    return json.dumps({"actions": [{"operation": "keep", "source_refs": refs}]})


async def _pending_facts(scene: ResidentScene, count: int) -> list[int]:
    ids = []
    for index in range(count):
        event = await scene.event("private:A", "A", f"长期事实样本 {index}")
        ids.append(
            await scene.fact(f"pending_{index}", "A", f"长期事实样本 {index}", evidence=(event,))
        )
    return ids


async def test_x09_one_run_plans_twenty_clusters(scene):
    """Case 9: 20 pending singleton clusters, default settings -> all 20 in one run."""
    await _pending_facts(scene, 20)
    repository, service, worker = await _scheduled_dream(scene)
    scene.dream_responder = _keep_all
    scene.selection_responder = lambda request: json.dumps({"selected": []})
    run = await service.plan_incremental(scheduled_slot="x9")
    # A scheduled incremental run is created running, as the worker loop plans it.
    await worker._drain_active()
    finished = await repository.get_run(run.public_id)

    assert scene.settings.memory_dream_max_clusters_per_run == 24
    assert scene.settings.memory_dream_max_model_calls_per_run == 24
    assert finished.statistics.candidate_clusters >= 20
    assert finished.completed_clusters == 20
    assert len(scene.dream_requests) == finished.model_calls == 20 <= 24
    # The P1 selection step pays its own call, outside the Dream run's budget.
    assert len(scene.selection_requests) == 1
    _artifact(
        scene,
        "X9",
        {
            "cluster_cap": scene.settings.memory_dream_max_clusters_per_run,
            "planned_clusters": finished.completed_clusters + finished.failed_clusters,
            "dream_model_calls": finished.model_calls,
            "selection_calls": len(scene.selection_requests),
            "status": finished.status.value,
        },
    )


async def test_x13_backlog_is_visible_and_falls_by_the_reviewed_count(scene):
    """Case 13: 30 unreviewed facts; one run reviews 24 of them."""
    await _pending_facts(scene, 30)
    repository, service, worker = await _scheduled_dream(scene)
    scene.dream_responder = _keep_all
    scene.selection_responder = lambda request: json.dumps({"selected": []})
    before = (await worker.health()).unreviewed_facts
    run = await service.plan_incremental(scheduled_slot="x13")
    # A scheduled incremental run is created running, as the worker loop plans it.
    await worker._drain_active()
    finished = await repository.get_run(run.public_id)
    after = (await worker.health()).unreviewed_facts

    reviewed = finished.completed_clusters
    assert before == 30
    assert reviewed == 24
    assert after == before - reviewed
    _artifact(
        scene,
        "X13",
        {"unreviewed_before": before, "reviewed": reviewed, "unreviewed_after": after},
    )


# ----------------------------------------------------------------- clean-up helpers
def _scan_reply(predicate):
    """Alias-only scan output: every offered alias whose text satisfies ``predicate``."""

    def respond(request):
        payload = json.loads(request.messages[-1].content)
        return json.dumps(
            {
                "task_scoped": [
                    row["ref"] for row in _refs(payload) if predicate(str(row["content"]))
                ]
            },
            ensure_ascii=False,
        )

    return respond


def _scan_provider(responder):
    from tests.integration.test_execution_continuity_e2e import ScriptedProvider

    return ScriptedProvider(responder)


async def _scan(scene: ResidentScene, responder, *, provider=None):
    from tests.support.model_executor import InjectedModelExecutor

    from yuki.memory.quality.task_scoped import TaskScopedHygiene
    from yuki.services.concurrency import ConcurrencyManager

    provider = provider or _scan_provider(responder)
    return await TaskScopedHygiene(scene.database).scan(
        models=InjectedModelExecutor(provider), concurrency=ConcurrencyManager()
    )


async def _write_plan(scene: ResidentScene, plan, name: str = "plan.json") -> Path:
    path = scene.tmp_path / name
    plan.write(path)
    return path


async def _cli(scene: ResidentScene, command: str, plan_path: Path, *extra: str):
    return await run_cli(
        scene.database.url,
        "memory",
        "hygiene",
        "task-scoped",
        command,
        "--plan",
        str(plan_path),
        *extra,
    )


async def _state_events(scene: ResidentScene) -> int:
    async with scene.database.read_session() as reader:
        return int(await reader.scalar(select(func.count()).select_from(MemoryFactStateEventModel)))


def _plan_ids(plan_path: Path) -> list[int]:
    return [item["fact_id"] for item in json.loads(plan_path.read_text("utf-8"))["facts"]]


TASK_TEXT = ("PR #31 先别合", "把任务书第三节改完再交", "这个分支今晚推上去")


async def _cleanup_fixture(scene: ResidentScene) -> dict[str, int]:
    """Automatic task-scoped facts plus every kind of fact the clean-up must never touch."""
    ids: dict[str, int] = {}
    a = await scene.event("private:A", "A", "PR #31 先别合，任务书第三节改完再交")
    b = await scene.event("private:B", "B", "这个分支今晚推上去")
    g = await scene.event("space:G", "B", "群里周五晚上一起打游戏")
    ids["task_a"] = await scene.fact(
        "task_a", "A", TASK_TEXT[0], evidence=(a,), kind=MemoryKind.PREFERENCE
    )
    ids["task_a2"] = await scene.fact("task_a2", "A", TASK_TEXT[1], evidence=(a,))
    ids["task_b"] = await scene.fact("task_b", "B", TASK_TEXT[2], evidence=(b,))
    ids["long_a"] = await scene.fact(
        "long_a", "A", "喜欢喝乌龙茶", evidence=(a,), kind=MemoryKind.PREFERENCE
    )
    ids["group"] = await scene.fact("group", "G", "群里周五晚上一起打游戏", evidence=(g,))
    ids["explicit"] = await scene.fact(
        "explicit", "A", "PR 合并前先看测试", evidence=(a,), kind=MemoryKind.PREFERENCE, **EXPLICIT
    )
    ids["episode"] = await scene.fact(
        "episode", "A", "一起把 PR #31 的任务书改完了", evidence=(a,), kind=MemoryKind.EPISODE
    )
    ids["self"] = await scene.fact("self", "self", "这次要把分支今晚推上去")
    return ids


def _everything(_text: str) -> bool:
    return True


def _task_scoped(text: str) -> bool:
    return text in TASK_TEXT


# -------------------------------------------------------------------------- X10
async def test_x10_plan_never_holds_explicit_self_or_episode(scene):
    """Case 10: the double marks everything; candidates exclude the protected kinds."""
    ids = await _cleanup_fixture(scene)
    plan = await _scan(scene, _scan_reply(_everything))
    path = await _write_plan(scene, plan)
    planned = _plan_ids(path)

    assert planned == sorted(
        [ids["task_a"], ids["task_a2"], ids["task_b"], ids["long_a"], ids["group"]]
    )
    assert not {ids["explicit"], ids["episode"], ids["self"]} & set(planned)
    _artifact(scene, "X10", {"planned": planned, "fixture": ids})


# -------------------------------------------------------------------------- X11
async def test_x11_apply_rejects_the_whole_plan_when_a_fact_changed(scene):
    """Case 11: one planned fact edited, another retracted -> nothing changes."""
    ids = await _cleanup_fixture(scene)
    path = await _write_plan(scene, await _scan(scene, _scan_reply(_task_scoped)))
    await scene.set_fact(ids["task_a2"], content="把任务书第三节和第四节改完再交")
    assert await scene.chat._memories.invalidate_fact(
        ids["task_b"], reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id=QQ["B"]
    )
    before = await _facts(scene, list(ids.values()))
    events_before = await _state_events(scene)
    result = await _cli(scene, "apply", path)

    assert result.returncode != 0, result
    assert await _facts(scene, list(ids.values())) == before
    assert await _state_events(scene) == events_before
    output = json.loads(result.stdout)
    assert sorted(item["fact_id"] for item in output["rejected"]) == sorted(
        [ids["task_a2"], ids["task_b"]]
    )
    _artifact(scene, "X11", {"cli": output, "facts": before})


# -------------------------------------------------------------------------- X12
async def test_x12_revert_restores_only_administrator_invalidations(scene):
    """Case 12: apply, a user re-retracts one planned fact, revert restores the rest."""
    ids = await _cleanup_fixture(scene)
    path = await _write_plan(scene, await _scan(scene, _scan_reply(_task_scoped)))
    applied = await _cli(scene, "apply", path)
    assert applied.returncode == 0, applied
    after_apply = await _facts(scene, [ids["task_a"], ids["task_a2"], ids["task_b"]])
    memories = scene.chat._memories
    assert await memories.restore_fact(ids["task_b"], actor_user_id=QQ["B"]) is not None
    assert await memories.invalidate_fact(
        ids["task_b"], reason=MemoryInvalidationReason.USER_RETRACTED, actor_user_id=QQ["B"]
    )
    reverted = await _cli(scene, "revert", path)
    after_revert = await _facts(scene, [ids["task_a"], ids["task_a2"], ids["task_b"]])

    assert [(row["status"], row["invalidated_reason"]) for row in after_apply] == [
        ("invalidated", "administrator_invalidated")
    ] * 3
    assert reverted.returncode == 0, reverted
    assert [(row["status"], row["invalidated_reason"]) for row in after_revert] == [
        ("active", None),
        ("active", None),
        ("invalidated", "user_retracted"),
    ]
    _artifact(
        scene,
        "X12",
        {
            "apply": json.loads(applied.stdout),
            "after_apply": after_apply,
            "revert": json.loads(reverted.stdout),
            "after_revert": after_revert,
        },
    )


# -------------------------------------------------------------------------- X14
async def test_x14_scan_holds_no_write_transaction_while_waiting(scene):
    """Case 14: the scan's model call is held open; another writer goes through."""
    await _cleanup_fixture(scene)
    provider = _scan_provider(_scan_reply(_task_scoped))
    provider.arrived[1] = asyncio.Event()
    provider.release[1] = asyncio.Event()
    database = scene.database
    original = database.immediate_session
    scan_writers: list[str] = []

    @contextlib.asynccontextmanager
    async def counted():
        task = asyncio.current_task()
        if task is not None and task.get_name() == "x14-scan":
            scan_writers.append("writer")
        async with original() as session:
            yield session

    database.immediate_session = counted
    try:
        scan = asyncio.create_task(_scan(scene, None, provider=provider), name="x14-scan")
        await provider.wait(1)
        writer_free = not database._writer.locked()
        c = await scene.event("private:C", "C", "我最近在学吉他")
        written = await asyncio.wait_for(
            scene.fact("during_scan", "C", "最近在学吉他", evidence=(c,)), 5
        )
        provider.release[1].set()
        plan = await scan
    finally:
        database.immediate_session = original

    assert writer_free
    assert written > 0
    assert scan_writers == []
    assert len(plan.facts) == 3
    _artifact(
        scene,
        "X14",
        {
            "writer_free_during_model_call": writer_free,
            "scan_writer_admissions": 0,
            "concurrent_write_completed": True,
            "planned": len(plan.facts),
        },
    )


# -------------------------------------------------------------------------- X15
async def test_x15_no_fact_text_in_plan_cli_stats_or_logs(scene, caplog):
    """Case 15: plan, scan/apply/show output, stats and logs carry IDs and counts only."""
    from yuki.cli import _memory_command

    caplog.set_level(logging.DEBUG)
    ids = await _cleanup_fixture(scene)
    texts = sorted(set(scene.contents.values()), key=len)
    path = scene.tmp_path / "cli-plan.json"
    provider = _scan_provider(_scan_reply(_task_scoped))

    @contextlib.asynccontextmanager
    async def scripted_models(settings, database):
        from tests.support.model_executor import InjectedModelExecutor

        yield settings, InjectedModelExecutor(provider)

    import yuki.cli as cli

    original = cli._cli_model_executor
    cli._cli_model_executor = scripted_models
    try:
        code = await _memory_command(
            scene.settings,
            argparse.Namespace(
                memory_command="hygiene",
                hygiene_command="task-scoped",
                task_scoped_command="scan",
                output=path,
                limit=None,
                database_url=scene.database.url,
            ),
        )
    finally:
        cli._cli_model_executor = original
    assert code == 0
    plan_text = path.read_text("utf-8")
    plan = json.loads(plan_text)
    show = await _cli(scene, "show", path)
    applied = await _cli(scene, "apply", path)
    stats = await run_cli(scene.database.url, "memory", "stats", "--hours", "1")
    # Yuki's own log records (third-party driver debug output is not Yuki's logging).
    logs = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("yuki")
    )

    assert set(plan) == {"scanned_at", "facts"}
    assert all(set(item) == {"fact_id", "signature"} for item in plan["facts"])
    assert [item["fact_id"] for item in plan["facts"]] == sorted(
        [ids["task_a"], ids["task_a2"], ids["task_b"]]
    )
    assert show.returncode == 0 and applied.returncode == 0, (show, applied)
    assert stats.returncode == 0, stats
    stats_payload = json.loads(stats.stdout)
    weekly = stats_payload["automatic_facts_by_week"]
    assert sum(row["count"] for row in weekly) >= 5
    assert sum(row["task_scoped_pattern"] for row in weekly) >= 3
    for surface in (plan_text, show.stdout, applied.stdout, stats.stdout, logs):
        assert not [text for text in texts if text in surface]
    _artifact(
        scene,
        "X15",
        {
            "plan_keys": sorted(plan),
            "plan_entry_keys": sorted({key for item in plan["facts"] for key in item}),
            "planned": [item["fact_id"] for item in plan["facts"]],
            "show": json.loads(show.stdout),
            "apply": json.loads(applied.stdout),
            "stats_weekly": [
                {key: value for key, value in row.items() if key != "week"} for row in weekly
            ],
        },
    )


# -------------------------------------------------------------------------- X16
async def test_x16_cleaned_fact_leaves_the_resident_segment_and_basis_stays(scene):
    """Case 16: a core-set fact is cleaned; the unplanned basis fact keeps its state."""
    a1 = await scene.event("private:A", "A", "PR #40 今晚合并；我对花生过敏", key="a1")
    task = await scene.fact("task", "A", "PR #40 今晚合并", evidence=(a1,))
    basis = await scene.fact("basis", "A", "对花生过敏", evidence=(a1,))
    await scene.write_set("A", [task, basis], source="dream")
    before = await scene.turn("A", "清理之前")
    path = await _write_plan(scene, await _scan(scene, _scan_reply(lambda t: "PR #40" in t)))
    applied = await _cli(scene, "apply", path)
    after = await scene.turn("A", "清理之后")

    assert applied.returncode == 0, applied
    assert _plan_ids(path) == [task]
    assert task in before.fact_ids and basis in before.fact_ids
    assert task not in after.fact_ids and basis in after.fact_ids
    facts = await _facts(scene, [task, basis])
    assert [row["status"] for row in facts] == ["invalidated", "active"]
    _artifact(
        scene,
        "X16",
        {
            "rendered_before": before.fact_ids,
            "rendered_after": after.fact_ids,
            "facts": facts,
        },
    )
