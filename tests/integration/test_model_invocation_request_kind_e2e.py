"""Every model_invocations row names its Main/compact request and request chain (#649).

Failure cases are written before the change (taskbook
docs/architecture/Yuki-模型调用请求类别与请求链观测任务书-2026-11-20.md §3):

* K1 a Main row lacks ``main`` or the chain the request was sent with;
* K2/K3 an execution compact (Responses, Work) is recorded as Main, or its row
  does not carry the pre-compact chain while the following Main carries the
  new chain recorded by the Work's ``chain_links`` compact link;
* K4/K5 automatic and manual public compact are recorded as anything else;
* K7 a failed paid dispatch loses its kind or chain;
* K8/K9 the observation reaches the provider wire (normalized body digests
  differ from the baseline run), the session header changes, or
  ``ChatRequest`` gains a field (compact receipts store asdict);
* K10 structured tasks get invented values; K14 model_phases lacks the kind.

Main, Work, compaction, the production protocol adapters, the executor, the
diagnostic writer and SQLite are real; the model replies are scripted at the
HTTP boundary. Evidence is written under
test-results/model-invocation-request-kind/ even when an assertion fails.
"""

import hashlib
import json
import re
import subprocess
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, replace
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound
from tests.support.correctness_wire import KINDS, body
from tests.support.event_seed import seed_scoped_event
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env
from tests.support.work_compaction_capacity_helpers import _runtime, _session

from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatMessage, ChatRequest, ChatResponse, ToolCall, ToolFunction
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.payload import decode_payload
from yuki.execution_trace.recorder import TraceRecorder
from yuki.identity.db_models import CanonicalPersonModel
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import estimate_request_tokens
from yuki.model_runtime.db_models import ModelInvocationModel
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import (
    ModelCapability,
    ModelExecutionPriority,
    ModelProfile,
    ModelRoute,
    ModelTask,
)
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.model_runtime.routes import ModelRouter
from yuki.persistence.diagnostic_writer import DiagnosticWriter
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import journal
from yuki.services.agent_runner import AgentRunner
from yuki.services.context_compaction import (
    EXECUTION_COMPACT_INSTRUCTION,
    PUBLIC_COMPACT_INSTRUCTION,
)
from yuki.services.turn_execution import TurnExecution
from yuki.web.models import WebMode

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "model-invocation-request-kind"
# The provider-independent request shape at the 04407fb7 baseline. Compact
# receipts persist asdict(ChatRequest) and digest it, so observation must not
# add a field here.
CHAT_REQUEST_FIELDS = [
    "messages",
    "model",
    "temperature",
    "max_output_tokens",
    "thinking_enabled",
    "reasoning_effort",
    "tools",
    "tool_choice",
    "response_format",
    "structured_output",
    "native_tools",
    "continuation",
    "continuation_items",
    "request_chain_id",
    "provider_session_id",
]
MISSING = "<column missing>"
_VOLATILE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\b[0-9a-f]{64}\b|\b[0-9a-f]{32}\b"
    r"|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:?\d{2}|Z)?"
    r"|\b\d{1,2}:\d{2}(:\d{2})?\b"
)


def normalized_digest(payload):
    """Digest of the wire body with run-specific identifiers and clock values masked."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(_VOLATILE.sub("<v>", encoded).encode()).hexdigest()


@contextmanager
def evidence_directory(request):
    root = request.config.rootpath
    parent = ARTIFACT / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
    patch = subprocess.check_output(["git", "diff", "HEAD", "--", "src", "tests"], cwd=root)
    evidence = {
        "nodeid": request.node.nodeid,
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "source_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "test_sha256": hashlib.sha256(Path(request.node.path).read_bytes()).hexdigest(),
    }
    if patch:
        (directory / "source.patch").write_bytes(patch)
    try:
        yield evidence
    finally:
        for index, payload in enumerate(evidence.pop("wire_bodies", [])):
            # The exact JSON sent, for byte-level comparison across runs.
            (directory / f"wire-{index:02d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
        (directory / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        (parent / "latest.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )


@pytest.fixture
def evidence(request):
    with evidence_directory(request) as captured:
        yield captured


def oracle(request):
    """Independent request kind: the actual instruction ending the sent input."""
    # Responses continuation chains carry the new input as continuation items.
    inputs = [
        item
        for item in (*request.messages, *request.continuation_items)
        if isinstance(item, ChatMessage)
    ]
    content = (inputs[-1].content or "") if inputs else ""
    if content.startswith(PUBLIC_COMPACT_INSTRUCTION):
        return "public_compact"
    if content.startswith(EXECUTION_COMPACT_INSTRUCTION):
        return "execution_compact"
    return "main"


def tool(name, arguments, call_id):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(call_id, ToolFunction(name, json.dumps(arguments))),)
    )


class Wire:
    """The production executor with its diagnostic writer, over a scripted HTTP model."""

    def __init__(self, database, provider, kind, *, fail=frozenset()):
        self.database = database
        self.provider = provider
        self.kind = kind
        self.fail = fail
        self.sent = []
        original = ContextVar("request_kind_original_request")

        def transport(req):
            request = original.get()
            index = len(self.sent)
            self.sent.append(
                {
                    "request": request,
                    "body": json.loads(req.content),
                    "headers": dict(req.headers),
                }
            )
            if index in self.fail:
                return httpx.Response(500, json={"error": {"message": "scripted outage"}})
            provider.requests.append(request)
            return httpx.Response(200, json=body(kind, provider._responder(request), index + 1))

        self.client = httpx.AsyncClient(
            base_url="https://wire.invalid", transport=httpx.MockTransport(transport)
        )
        cls, vendor, protocol = KINDS[kind]
        adapter = cls(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=2,
            max_retries=0,
            client=self.client,
            **({"provider_name": "openai"} if kind == "chat" else {}),
        )

        async def complete(request):
            token = original.set(request)
            try:
                return await adapter.complete(request)
            finally:
                original.reset(token)

        provider.complete = complete
        profile = ModelProfile(
            id="request-kind-" + kind,
            provider=vendor,
            protocol=protocol,
            base_url="https://wire.invalid",
            api_key_env="UNUSED",
            model="synthetic-model",
            timeout_seconds=2,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=1024,
            capabilities=frozenset({ModelCapability.TOOLS, ModelCapability.REASONING}),
        )
        self.writer = DiagnosticWriter()
        self.executor = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={profile.id: profile},
                    routes={
                        task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask
                    },
                )
            ),
            pool=ModelClientPool(injected_profiles={profile.id: provider}),
            invocations=ModelInvocationRepository(database, writer=self.writer),
            traces=TraceRecorder(database, writer=self.writer),
        )

    async def __aenter__(self):
        await self.writer.start()
        return self

    async def __aexit__(self, *_):
        await self.writer.close()
        await self.client.aclose()

    async def rows(self):
        async with self.database.read_session() as reader:
            result = await reader.execute(text("SELECT * FROM model_invocations ORDER BY id"))
            return [dict(row) for row in result.mappings()]

    async def phases(self):
        async with self.database.read_session() as reader:
            entries = (
                await reader.scalars(
                    select(ExecutionTraceEntryModel)
                    .where(ExecutionTraceEntryModel.kind == "model_phases")
                    .order_by(ExecutionTraceEntryModel.id)
                )
            ).all()
        return [
            decode_payload(row.payload_gzip, size=row.payload_bytes, digest=row.payload_sha256)[
                "data"
            ]
            for row in entries
        ]

    def wire_facts(self):
        return [
            {
                "oracle": oracle(item["request"]),
                "request_chain_id": item["request"].request_chain_id,
                "provider_session_id": item["request"].provider_session_id,
                "estimated_tokens": estimate_request_tokens(item["request"]),
                "continuation": item["request"].continuation is not None,
                "body_bytes": len(json.dumps(item["body"], ensure_ascii=False)),
                "body_digest": normalized_digest(item["body"]),
                "x_session_id": item["headers"].get("x-session-id"),
            }
            for item in self.sent
        ]


def observed(row, column):
    return row.get(column, MISSING)


def assert_rows_name_sent_requests(sent, rows):
    assert len(rows) == len(sent), (len(rows), len(sent))
    for index, (row, item) in enumerate(zip(rows, sent, strict=True)):
        request = item["request"]
        assert row["success"], (index, row)
        assert observed(row, "request_kind") == oracle(request), (index, row)
        assert observed(row, "request_chain_id") == request.request_chain_id, (index, row)


def assert_wire_carries_no_observation(wire):
    """K8/K9: the observation stays out of the request and its session header.

    A request chain ID may already be in baseline content (execution evidence
    effect keys), so the cross-run comparison of normalized wire digests is the
    byte-level proof; here only names and kind values that the baseline never
    sends are rejected.
    """
    for item in wire.sent:
        encoded = json.dumps(item["body"], ensure_ascii=False)
        headers = json.dumps(item["headers"], ensure_ascii=False)
        for value in ("request_kind", "request_chain_id", "execution_compact", "public_compact"):
            assert value not in encoded and value not in headers, value
        assert item["headers"].get("x-session-id") == item["request"].provider_session_id
    assert [field.name for field in fields(ChatRequest)] == CHAT_REQUEST_FIELDS


def chained(env, text_, identity):
    return replace(
        inbound(text_, message_id=identity, user_id="10001", group_id="20001", mentions_bot=True),
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )


async def test_execution_compact_in_a_responses_work_is_recorded_on_its_original_chain(
    database, tmp_path, evidence
):
    """K1, K2, K3, K8, K9, K14 in the production shape (Responses + Work)."""
    goal = "WORK_GOAL: write the long fanout explanation, then wait for review."
    answer = "LONG_DELIVERED_FANOUT " + "independent branch; " * 4000
    sent = False
    reads = 0

    def respond(request):
        nonlocal sent, reads
        kind = oracle(request)
        if kind == "execution_compact":
            return ChatResponse("EXECUTION_CHECKPOINT: fanout already delivered; wait.", 0)
        if kind == "public_compact":
            return ChatResponse("PUBLIC_CHECKPOINT: nothing earlier.", 0)
        control = current_work_control.get()
        if control is None or control.current is None:
            return tool("manage_task", {"action": "start", "goal": goal}, "accept-work")
        if not sent:
            sent = True
            return tool("send", {"text": answer}, "work-long-send")
        if reads < 2:
            # A later round leaves the large delivered round compactable.
            reads += 1
            return tool("read_chat", {"limit": 1}, f"work-followup-read-{reads}")
        return tool("wait", {"for": [{"seconds": 3600}]}, "work-wait")

    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        enabled_groups_csv="20001",
        context_compact_trigger_tokens=30000,
    )
    harness = build_harness(database, settings, provider)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    async with Wire(database, provider, "responses") as wire:
        chat.runtime.runner._models = wire.executor
        try:
            result = await harness.processor.handle(
                chained(env, goal, "work-request-kind"), MemorySender()
            )
            evidence["result_reason"] = result.reason
        finally:
            await wire.writer.close()
            rows = await wire.rows()
            phases = await wire.phases()
            async with database.read_session() as reader:
                journals = [dict(row) for row in (await reader.execute(select(journal))).mappings()]
            objects = WorkJournal(WorkRepository(database)).objects
            links = [
                link
                for row in journals
                for link in (await objects.hydrate(json.loads(row["payload_json"])))
                .get("metadata", {})
                .get("progress", {})
                .get("chain_links", [])
            ]
            evidence.update(
                wire=wire.wire_facts(),
                wire_bodies=[item["body"] for item in wire.sent],
                rows=rows,
                model_phases=[
                    {key: item.get(key) for key in ("task", "request_kind", "request_chain_id")}
                    for item in phases
                ],
                chain_links=links,
            )
    kinds = [oracle(item["request"]) for item in wire.sent]
    assert "execution_compact" in kinds, kinds
    compact = kinds.index("execution_compact")
    before, after = wire.sent[compact - 1]["request"], wire.sent[compact + 1]["request"]
    compact_request = wire.sent[compact]["request"]
    assert kinds[compact - 1] == kinds[compact + 1] == "main"
    assert compact_request.request_chain_id == before.request_chain_id
    assert after.request_chain_id != before.request_chain_id
    assert {
        "from": before.request_chain_id,
        "to": after.request_chain_id,
        "reason": "compact",
    } in links, links
    assert_wire_carries_no_observation(wire)
    assert_rows_name_sent_requests(wire.sent, rows)
    assert {row["task"] for row in rows} == {"chat_agent"}
    assert [item.get("request_kind", MISSING) for item in phases] == kinds
    assert [item.get("request_chain_id") for item in phases] == [
        item["request"].request_chain_id for item in wire.sent
    ]


async def test_public_compact_automatic_and_manual_rows_and_structured_rows(
    database, tmp_path, evidence
):
    """K1, K4, K5, K8, K10 on Chat Completions without a Work."""
    question = "CURRENT_ORDINARY_GOAL: explain fanout once and finish."
    old_public = ("PUBLIC_OLD_CAN_DROP " + "historical decision. " * 9500).rstrip()
    answer = "REAL_DELIVERED_FANOUT " + "independent branch; " * 2400
    issued = False

    def respond(request):
        nonlocal issued
        if request.structured_output:
            return ChatResponse('{"ok": true}', 0)
        kind = oracle(request)
        if kind == "public_compact":
            return ChatResponse("PUBLIC_CHECKPOINT: preserve the earlier decision.", 0)
        if kind == "execution_compact":
            return ChatResponse("PRIVATE_CHECKPOINT: answered.", 0)
        if not issued:
            issued = True
            return tool("send", {"text": answer}, "public-real-send")
        return ChatResponse("NO_REPLY", 0)

    env = await social_env(database, tmp_path)
    async with database.immediate_session() as writer:
        (await writer.get(CanonicalPersonModel, env.person)).is_superuser = True
    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=False,
        enabled_groups_csv="20001",
        context_compact_trigger_tokens=96000,
    )
    harness = build_harness(database, settings, provider)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    await seed_scoped_event(
        harness.scoped_events,
        scope=ConversationScope.group("80001", "20001"),
        origin_key="request-kind-old-public",
        sender_user_id="10001",
        direction="inbound",
        content=old_public,
    )
    manual_from = None
    structured_from = None
    async with Wire(database, provider, "chat") as wire:
        chat.runtime.runner._models = wire.executor
        try:
            result = await harness.processor.handle(
                chained(env, question, "public-request-kind"), MemorySender()
            )
            evidence["result_reason"] = result.reason
            manual_from = len(wire.sent)
            command = replace(
                inbound(
                    "/ai compact",
                    message_id="manual-request-kind",
                    user_id="10001",
                    group_id="20001",
                ),
                bot_user_id="80001",
                person_id=env.person,
                space_id=env.space,
                conversation_id=env.context.conversation_id,
                presence_id=env.presence,
            )
            await harness.processor.handle(command, MemorySender())
            structured_from = len(wire.sent)
            await wire.executor.execute(
                ModelTask.UTILITY_STRUCTURED,
                ChatRequest(
                    messages=(ChatMessage("user", "Return ok."),),
                    model="synthetic-model",
                    structured_output=True,
                ),
                canonical_conversation_id=env.context.conversation_id,
            )
        finally:
            await wire.writer.close()
            rows = await wire.rows()
            evidence.update(
                wire=wire.wire_facts(),
                wire_bodies=[item["body"] for item in wire.sent],
                rows=rows,
                manual_from=manual_from,
                structured_from=structured_from,
            )
    kinds = [oracle(item["request"]) for item in wire.sent[:structured_from]]
    assert "public_compact" in kinds[:manual_from], kinds
    assert kinds[manual_from:] == ["public_compact"], kinds
    automatic = kinds.index("public_compact")
    assert kinds[automatic + 1] == "main"
    assert (
        wire.sent[automatic + 1]["request"].request_chain_id
        != wire.sent[automatic]["request"].request_chain_id
    )
    assert_wire_carries_no_observation(wire)
    assert_rows_name_sent_requests(wire.sent[:structured_from], rows[:structured_from])
    structured = rows[structured_from:]
    assert [row["task"] for row in structured] == ["utility_structured"]
    assert observed(structured[0], "request_kind") is None
    assert observed(structured[0], "request_chain_id") is None
    plan = await insert_plan(wire)
    evidence["insert_query_plan"] = plan
    # Only constant rows and primary-key lookups; no history table is scanned.
    assert plan and not [
        detail for detail in plan if detail.startswith("SCAN") and detail != "SCAN CONSTANT ROW"
    ], plan
    assert all("USING" in detail for detail in plan if detail.startswith("SEARCH")), plan


async def insert_plan(wire):
    """SQLite plan of the exact conditional INSERT the diagnostic writer executes."""
    repository = wire.executor._invocations
    async with wire.database.read_session() as reader:
        row = await reader.scalar(select(ModelInvocationModel).order_by(ModelInvocationModel.id))
        values = {
            column.name: getattr(row, column.name)
            for column in ModelInvocationModel.__table__.columns
            if column.name != "id"
        }
        write = await repository._prepare_write(tuple(values.items()), 0)
        compiled = write.statement.compile(
            dialect=wire.database.engine.dialect, compile_kwargs={"render_postcompile": True}
        )
        connection = await reader.connection()
        result = await connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN " + str(compiled),
            tuple(compiled.params[name] for name in compiled.positiontup or ()),
        )
        return [item[-1] for item in result.all()]


async def test_failed_paid_main_dispatch_keeps_kind_and_chain(database, tmp_path, evidence):
    """K7: an upstream failure row names the request that was actually sent."""
    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider(lambda _: ChatResponse("NO_REPLY", 0))
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=False, enabled_groups_csv="20001"),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    env.service.runtime_config = chat._runtime_config
    async with Wire(database, provider, "chat", fail=frozenset({0})) as wire:
        chat.runtime.runner._models = wire.executor
        try:
            try:
                result = await harness.processor.handle(
                    chained(env, "Please answer once.", "failed-request-kind"), MemorySender()
                )
                evidence["result_reason"] = result.reason
            except Exception as exc:  # The original failure is the fact under test.
                evidence["error"] = type(exc).__name__
        finally:
            await wire.writer.close()
            rows = await wire.rows()
            evidence.update(
                wire=wire.wire_facts(), rows=rows, wire_bodies=[item["body"] for item in wire.sent]
            )
    assert wire.sent, "The scripted outage must follow a real dispatch"
    assert rows and not rows[0]["success"]
    assert rows[0]["error_category"]
    assert observed(rows[0], "request_kind") == "main"
    assert observed(rows[0], "request_chain_id") == wire.sent[0]["request"].request_chain_id
    assert_wire_carries_no_observation(wire)


async def test_plugin_session_compact_is_recorded_under_its_own_task(database, tmp_path, evidence):
    """K11: a plugin Agent session reuses the same compact dispatch under its task."""
    control, session, initial = await _session(database, tmp_path)
    session.transcript.append(ChatMessage("assistant", "confirmed research " * 1000))
    provider = FakeLLMProvider(lambda _: ChatResponse("PLUGIN_CHECKPOINT: continue.", 0))
    main_runner, runtime = await _runtime(database, control, initial, provider)
    try:
        async with Wire(database, provider, "chat") as wire:
            runner = AgentRunner(
                wire.executor, main_runner._concurrency, task=ModelTask.PLUGIN_AGENT_SESSION
            )
            execution = TurnExecution(
                runner, initial, replace(runtime, auxiliary_requests=[0]), None
            )
            execution.state.transcript = session.transcript
            prepared = await execution.prepare_request(0, (), WebMode.DISABLED)
            try:
                await execution.compact_current(
                    0, prepared.request, ModelExecutionPriority.BACKGROUND, 128000
                )
            finally:
                await wire.writer.close()
                rows = await wire.rows()
                evidence.update(wire=wire.wire_facts(), rows=rows)
    finally:
        await control.repository.release(control.lease)
    assert [oracle(item["request"]) for item in wire.sent] == ["execution_compact"]
    assert [row["task"] for row in rows] == ["plugin_agent_session"]
    assert_rows_name_sent_requests(wire.sent, rows)
    assert rows[0]["request_chain_id"] == prepared.request.request_chain_id
    assert_wire_carries_no_observation(wire)
