"""Real isolated Work/journal drivers for the manual serializer experiment.

These are fixture-owned requests, not a second chatbot or model-driven scheduler.
No application lifecycle or business transport is started.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse, ToolCall
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.model_runtime.structured import tool_free_structured_output_mode
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.turn_transcript import TurnTranscript


class WorkDriver:
    def __init__(self, app: Any, root: Path, namespace: str, scenario: str, prefix: tuple) -> None:
        self.app, self.root = app, root
        self.namespace, self.scenario, self.prefix = namespace, scenario, prefix
        self.repository = WorkRepository(app.database)
        self.controls: list[WorkControl] = []
        self.control: WorkControl | None = None
        self.source_event_ids: list[int] = []
        self.lease: Any = None
        self.evidence: dict[str, Any] = {"source": "real WorkRepository + WorkSession fixture"}
        self._token: Any = None
        self._observed_notes: set[str] = set()
        self._read_call: ToolCall | None = None
        self._read_result: str | None = None
        self._read_key: str | None = None
        self._read_invocations = 0

    async def initialize(self) -> None:
        async with self.app.database.immediate_session() as writer:
            person = await ensure_person(writer, "910000001", display_name="synthetic member")
            await ensure_presence(writer, "910000002")
            await ensure_space(writer, "910000003", name="synthetic cache room")
        for index in range(2 if self.scenario == "C03" else 1):
            await self.app.scoped_events.append(
                scope=ConversationScope.group("910000002", "910000003"),
                platform_message_id=f"{self.namespace}-{self.scenario}-{index}",
                sender_user_id="910000001",
                direction="inbound",
                content=f"合成请求：比较文档方案 W{index + 1}；只在隔离环境研究。",
            )
            async with self.app.database.sessions() as reader:
                row = await reader.scalar(select(ChatEventModel).order_by(ChatEventModel.id.desc()))
            assert row is not None
            self.source_event_ids.append(row.id)
            if self.lease is None:
                self.lease = await self.repository.acquire(
                    row.canonical_conversation_id, 1, seconds=300
                )
                assert self.lease is not None
            source = {
                "actor_person_id": person,
                "principal_kind": "person",
                "origin": "user_message",
                "trigger_event_id": row.id,
                "read_scope": json.dumps(
                    {"memory": [], "plugin_id": None, "delegation_id": None}, sort_keys=True
                ),
            }
            key = f"event:{row.id}"
            control = self._control(key, source)
            control.current = await self.repository.accept(
                self.lease,
                source_key=key,
                source=source,
                goal=f"比较合成文档方案 W{index + 1}，保留依据与未知事项。",
                output_kind="answer",
                reporting="quiet",
            )
            self.controls.append(control)
        self.evidence["original_work_ids"] = [control.current["id"] for control in self.controls]

    def _control(self, key: str, source: dict[str, Any]) -> WorkControl:
        async def validate() -> None:
            if not await self.repository.valid(self.lease):
                raise ValueError("synthetic_work_lease_obsolete")

        from qq_ai_bot.tool_results.access import access_from_source

        control = WorkControl(self.repository, self.lease, key, source, validate)
        control.bind_context_access(access_from_source(self.lease.conversation_id, 1, source))
        return control

    async def prepare(self, index: int) -> TurnTranscript:
        if self.scenario == "C03" and index > 0:
            text = f"合成群友新消息 {index}：请保留已核依据，未核内容仍标明未知。"
            await self.app.scoped_events.append(
                scope=ConversationScope.group("910000002", "910000003"),
                platform_message_id=f"{self.namespace}-new-chat-{index}",
                sender_user_id="910000001",
                direction="inbound",
                content=text,
            )
            async with self.app.database.sessions() as reader:
                row = await reader.scalar(select(ChatEventModel).order_by(ChatEventModel.id.desc()))
            self.source_event_ids.append(row.id)
            self.prefix = (*self.prefix, ChatMessage("user", f"event:{row.id} {row.content}"))
            from qq_ai_bot.conversation.observations import ContextObservationRepository
            from qq_ai_bot.tool_results.access import access_from_source

            # Fixture authority is fixed by the real source actor, never model fields.
            grant = access_from_source(self.lease.conversation_id, 1, self.controls[0].source)
            notes = await ContextObservationRepository(self.app.database).read(
                conversation_id=self.lease.conversation_id,
                generation=1,
                actor_id=grant.actor_person_id,
                read_scope=grant.read_scope,
            )
            for note in notes:
                if note.id not in self._observed_notes:
                    self.prefix = (*self.prefix, note.message())
                    self._observed_notes.add(note.id)
            self.evidence["appended_chat_event_ids"] = self.source_event_ids[2:]
            self.evidence["selected_observation_ids"] = sorted(self._observed_notes)
        selected = index % 3 == 1 if self.scenario == "C03" else False
        self.control = self.controls[int(selected)]
        if index == 0 or self.scenario == "C03":
            session = WorkSession(self.control, self.app.main_agent_contract.revision)
            self.control.session = session
            await session.restore(
                TurnTranscript(self.prefix), visible_event_ids=frozenset(self.source_event_ids)
            )
        assert self.control.session is not None
        self._token = current_work_control.set(self.control)
        await self.control.reserve_request()
        return self.control.session.transcript

    async def save(self, transcript: TurnTranscript) -> None:
        assert self.control is not None and self.control.session is not None
        self.control.session.transcript = transcript
        await self.control.session.save("paired")
        self.evidence.setdefault("request_work_ids", []).append(self.control.current["id"])
        current_work_control.reset(self._token)
        self._token = None

    async def record_read(self, call: ToolCall, result: dict[str, Any]) -> str:
        assert self.control is not None and self.control.session is not None

        async def invoke() -> str:
            self._read_invocations += 1
            return json.dumps(result, ensure_ascii=False)

        recorded = await self.control.session.execute(call, invoke, side_effecting=False)
        self._read_call, self._read_result = call, recorded
        self._read_key = self.control.session.call_key(call.id)
        return recorded

    async def reopen(self) -> None:
        """Close the real SQLite pool and recreate the repository/session from disk."""
        assert self.control is not None
        original = await self.repository.get(self.control.current["id"])
        await self.repository.release(self.lease)
        await self.app.database.engine.dispose()
        from qq_ai_bot.persistence.database import Database

        database = Database(self.app.database.url)
        self.app.database = database
        self.repository = WorkRepository(database)
        self.lease = await self.repository.acquire(
            original["conversation_id"], original["generation"], seconds=300
        )
        assert self.lease is not None
        control = self._control(original["source_key"], json.loads(original["source_json"]))
        control.current = await self.repository.get(original["id"])
        control.session = WorkSession(control, self.app.main_agent_contract.revision)
        restored = await control.session.restore(
            TurnTranscript(self.prefix), visible_event_ids=frozenset(self.source_event_ids)
        )
        self.controls = [control]
        self.control = control
        if self._read_call is not None:
            # Business resume deliberately uses a new model chain. Reconcile by
            # the original effect identity; never execute that old call on it.
            replayed = await control.session.journal.effect_result(self._read_key)
            self.evidence["accepted_read"] = {
                "original_call_id": self._read_call.id,
                "same_receipt": replayed == self._read_result,
                "business_invocations": self._read_invocations,
            }
        self.evidence["restart"] = {
            "work_id_unchanged": control.current["id"] == original["id"],
            "model_requests_before": original["model_requests"],
            "model_requests_after": control.current["model_requests"],
            "tool_calls_before": original["tool_calls"],
            "tool_calls_after": control.current["tool_calls"],
            "private_replay": control.session.uses_recovery_transcript,
            "restored_chain_id": restored.chain_id,
        }

    async def compact(self, executor: Any, template: ChatRequest) -> None:
        assert self.control is not None and self.control.session is not None
        session = self.control.session
        session.transcript.append(
            ChatMessage(
                "user", "[合成临时公开资料，不是新要求]\n" + "不同方案的细节正文。\n" * 6000
            )
        )
        await session.save("paired")
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="910000001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="synthetic-cache-probe",
            current_group_id="910000003",
            bot_user_id="910000002",
            gateway=None,
            runtime_config=await self.app.runtime_config.snapshot(),
            current_time=self.app.time_context.current_default(),
            allowed_capabilities=frozenset(),
            max_tool_calls=0,
            max_model_requests=24,
            work_control=self.control,
        )
        runner = self.app.runtime.runner
        runner._models = executor
        budget = executor.capacity(runner._task).input_budget(524288)
        previous = session.transcript.chain_id
        token = current_work_control.set(self.control)
        sequence = session.transcript.request()
        try:
            compacted = await runner._compact_work(
                runtime,
                ModelExecutionPriority.FOREGROUND,
                budget,
                replace(
                    template,
                    messages=sequence.messages,
                    continuation=sequence.continuation,
                    continuation_items=sequence.items,
                    request_chain_id=session.transcript.chain_id,
                ),
            )
        finally:
            current_work_control.reset(token)
        self.evidence["compaction"] = {
            "old_chain_id": previous,
            "new_chain_id": compacted.chain_id,
            "explicit_boundary": previous != compacted.chain_id,
            "source": "real Runner._compact_work, actual paid summary calls",
        }

    async def close(self) -> None:
        if self._token is not None:
            current_work_control.reset(self._token)
            self._token = None
        if self.lease is not None:
            await self.repository.release(self.lease)
        self.evidence["final_work"] = [
            {key: row[key] for key in ("id", "state", "model_requests", "tool_calls")}
            for control in self.controls
            if (row := await self.repository.get(control.current["id"])) is not None
        ]


class OrdinaryDriver:
    """The current ordinary working-summary path, without accepting a Work."""

    control = None

    def __init__(self, app: Any, prefix: tuple[ChatMessage, ...]) -> None:
        self.app, self.prefix = app, prefix
        self.transcript = TurnTranscript(prefix)
        self.observations: list[dict[str, Any]] = []
        self.evidence: dict[str, Any] = {"source": "real compact_ordinary + TaskModelExecutor"}
        self.before = 0

    async def initialize(self) -> None:
        async with self.app.database.sessions() as reader:
            self.before = len((await reader.execute(select(work.c.id))).all())

    async def prepare(self, index: int) -> TurnTranscript:
        return self.transcript

    def observe_response(self, response: ChatResponse) -> None:
        self.observations.append(
            {
                "content": response.content,
                "tool_calls": [asdict(call) for call in response.tool_calls],
                "citations": [asdict(item) for item in response.citations],
                "native_tool_events": [asdict(item) for item in response.native_tool_events],
                "status": response.status.value,
                "results": [],
            }
        )

    def observe_result(self, call: ToolCall, result: str) -> None:
        self.observations[-1]["results"].append(
            {
                "call_id": call.id,
                "tool": call.function.name,
                "result": result,
            }
        )

    async def save(self, transcript: TurnTranscript) -> None:
        self.transcript = transcript

    async def compact(self, executor: Any, template: ChatRequest) -> None:
        from qq_ai_bot.model_runtime.models import ModelTask
        from qq_ai_bot.services.ordinary_compaction import compact_ordinary

        self.transcript.append(ChatMessage("user", "[合成临时资料]\n" + "方案细节正文。\n" * 6000))
        sequence = self.transcript.request()
        request = replace(
            template,
            messages=sequence.messages,
            continuation=sequence.continuation,
            continuation_items=sequence.items,
            request_chain_id=self.transcript.chain_id,
        )
        capacity = executor.capacity(ModelTask.CHAT_AGENT)
        previous = self.transcript.chain_id
        self.transcript = await compact_ordinary(
            self.prefix,
            self.transcript,
            main_request=request,
            structured_mode=tool_free_structured_output_mode(executor, ModelTask.CHAT_AGENT),
            summary_budget=capacity.input_budget(524288),
            input_budget=capacity.input_budget(524288),
            output_tokens=capacity.output_tokens,
            prepare=lambda value: executor.capacity_request(ModelTask.CHAT_AGENT, value),
            execute=lambda value: executor.execute(ModelTask.CHAT_AGENT, value),
            evidence=[],
            model_observations=self.observations,
        )
        self.observations = []
        self.evidence["compaction"] = {
            "old_chain_id": previous,
            "new_chain_id": self.transcript.chain_id,
            "explicit_boundary": previous != self.transcript.chain_id,
            "source": "real compact_ordinary, actual paid summary calls",
        }

    async def close(self) -> None:
        async with self.app.database.sessions() as reader:
            after = len((await reader.execute(select(work.c.id))).all())
        self.evidence["created_works"] = after - self.before
