"""E2E: switching daily participation off stops its prompt, binding and upkeep (#565).

The scene is the Telegram runtime scene (isolated SQLite, fake Provider, the real
processor, Main, Social and delivery) with the real participation host and its
snapshot store. The two existing HOT switches are written through the real config
writer as global overrides, as production did: ``conversation.semantic_participation_enabled``
and ``conversation.auto_reply_enabled``.

Who consumes daily participation intent: own ``join/stay/quiet`` reports, ordinary unit
bindings, participation feedback and the background Controller upkeep are only read by
the semantic proposer (semantic invitations/continuation, Jev snapshots, SELF
proposals). Implicit follow-up classification and legacy scoring only produce
conversation opportunities, which ``auto_reply_enabled`` owns.

Failure cases, written before the implementation:

F1  semantic off: a new Main request no longer carries the ``<yuki-state>``
    join/stay/quiet instruction; with semantic on it still does (control).
F2  semantic off: an explicit call is still answered and delivered; a control tail in
    the model output never reaches the platform or the final text.
F3  semantic off: the explicit call does no participation binding and its tail does
    not reach participation (no ``binding_for_event`` / ``observe_main_response``);
    the admission carries no binding.
F4  semantic off: the background tick does not read new history into a loaded
    Controller, does not advance its clock and does not save its snapshot; the
    binding moves to legacy with a new controller epoch.
F5  semantic off: neither startup nor discovery loads a scope Controller; with
    semantic on startup still loads it (control).
F6  semantic on again: the next tick reads the new history and advances again; the
    Controller adopts the new epoch with no pending proposal, so the off period is
    not sampled as elapsed time.
F7  semantic off: accepted-run reconciliation still runs every tick.
F8  auto_reply off (semantic on or off): a non-call group message is only recorded
    and offered to the original Work/wait; no implicit continuation classification,
    no group_open/legacy queueing, no model call. An explicit call is still answered.
F9  switching off erases nothing: the unit and own hint saved while on stay in the
    persisted snapshot; no quiet or completion is invented.

Facts go to ``test-results/participation-off-e2e.json``. Statement counts per tick are
timing-independent here but kept as diagnostics outside the hashed table; they compare
the same scope state, not a production distribution.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.support.semantic_participation_host_helpers import _host
from yuki_participation.participation import ParticipationCheckpoint
from yuki_participation.snapshot import thaw

from yuki.admin.config_service import RuntimeConfigService
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.canonical_repository import create_space_binding
from yuki.persistence.models import ChatEventModel

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "participation-off-e2e.json"
RESULTS: dict[str, Any] = {}
DIAGNOSTICS: dict[str, Any] = {}
CODE_UNDER_TEST = (
    "src/yuki/prompting/contracts.py",
    "src/yuki/services/prompt_composer.py",
    "src/yuki/services/main_agent_backend.py",
    "src/yuki/services/processor.py",
    "src/yuki/services/semantic_participation.py",
)
STATE_TAG = "<yuki-state>"


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    table = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_participation_off_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "code_sha256": {
                    path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                    for path in CODE_UNDER_TEST
                },
                "scenarios_sha256": hashlib.sha256(table.encode()).hexdigest(),
                "scenarios": RESULTS,
                "statement_diagnostics": DIAGNOSTICS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


class Calls:
    def __init__(self, target: object, name: str, *, replace: bool = False) -> None:
        self.count = 0
        original = getattr(target, name)

        async def call(*args: object, **kwargs: object) -> object:
            self.count += 1
            return await original(*args, **kwargs)

        def record(*args: object, **kwargs: object) -> None:
            self.count += 1

        setattr(target, name, record if replace else call)


class Statements:
    def __init__(self, database: Any) -> None:
        self.engine = database.engine.sync_engine
        self.count = 0
        self.statements: dict[str, Any] = {}

    def __enter__(self) -> Statements:
        sa_event.listen(self.engine, "before_cursor_execute", self.record)
        return self

    def __exit__(self, *_: object) -> None:
        sa_event.remove(self.engine, "before_cursor_execute", self.record)

    def record(self, _conn: object, _cursor: object, statement: str, parameters: Any, *_: object):
        self.count += 1
        if statement.lstrip().upper().startswith("SELECT"):
            self.statements.setdefault(statement, parameters)

    async def plans(self, database: Any) -> list[dict[str, Any]]:
        """EXPLAIN QUERY PLAN for each distinct read this window issued."""
        plans = []
        async with database.read_session() as reader:
            connection = await reader.connection()
            for statement, parameters in self.statements.items():
                rows = await connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters
                )
                plans.append(
                    {
                        "statement": " ".join(statement.split())[:240],
                        "plan": [str(row[-1]) for row in rows],
                    }
                )
        return plans


async def _switch(config: RuntimeConfigService, key: str, value: bool) -> None:
    change = await config.set_override(
        f"conversation.{key}",
        value,
        scope_type="global",
        scope_id="",
        actor_user_id="operator",
        trigger_message_id="participation-off-e2e",
    )
    assert change.success


async def _wire(env, database, tmp_path, **settings: object):
    host, _ = await _host(database, tmp_path)
    processor = env.harness.processor
    chat = processor._chat
    config = RuntimeConfigService(
        database=database, settings=make_settings(database.url, **settings)
    )
    processor._runtime_config = chat._runtime_config = host.runtime_config = config
    host.validate_turn_snapshot = chat.validate_turn_snapshot
    processor.set_participation(host)
    chat.observe_main_response = host.observe_main_response
    chat.participation_runtime_context = host.runtime_context
    return host, processor, chat, config


async def _group(database, chat_id: str, *, require_mention: bool) -> None:
    async with database.immediate_session() as session:
        await create_space_binding(
            session,
            external_space_id=chat_id,
            name="participation off",
            enabled=True,
            autonomous_enabled=True,
            require_mention=require_mention,
            now=datetime.now(UTC),
            platform="telegram",
        )


def _message(identifier: int, chat_id: int, text: str) -> dict[str, Any]:
    incoming = update(identifier, chat=chat_id, text=text)
    incoming["message"]["date"] = int(datetime.now(UTC).timestamp())
    return incoming


async def _event_ids(database) -> list[int]:
    async with database.read_session() as reader:
        return list(
            await reader.scalars(
                select(ChatEventModel.id).where(ChatEventModel.direction == "inbound")
            )
        )


def _system(request: Any) -> str:
    return next(m.content or "" for m in request.messages if m.role == "system")


async def _persisted_hint(host, item) -> dict[str, Any]:
    _, progress, body = await host._store.load(item.scene.scope)
    checkpoint = ParticipationCheckpoint.model_validate(
        thaw(item.scene.scope, progress, body).host_checkpoint["participation_v1"]
    )
    return {
        key: unit.hint.delta.engage if unit.hint is not None else None
        for key, unit in checkpoint.units.items()
    }


async def test_semantic_off_stops_state_prompt_binding_and_scope_upkeep(
    telegram_env, database, tmp_path
):
    env = telegram_env
    host, _, chat, config = await _wire(
        env, database, tmp_path, conversation_semantic_participation_enabled=True
    )
    bindings = Calls(host, "binding_for_event")
    hints = Calls(chat, "observe_main_response")
    reconciled = Calls(host.repository, "list_active")

    def respond(request):
        if any(message.role == "tool" for message in request.messages):
            return ChatResponse('<yuki-state>{"engage":"join"}</yuki-state>', 0)
        text = "".join(
            message.content or "" for message in request.messages if message.role == "user"
        )
        reply = "off reply" if "second" in text else "on reply"
        return ChatResponse(
            '<yuki-state>{"engage":"join"}</yuki-state>',
            0,
            provider_request_id=f"main-{reply}",
            tool_calls=(
                ToolCall(f"send-{reply}", ToolFunction("send", json.dumps({"text": reply}))),
            ),
        )

    env.model._responder = respond
    await _group(database, "-10051", require_mention=True)

    # Semantic on: the original contract.
    await env.gateway.receive(_message(1, -10051, "!ai first question"))
    await drain(env)
    on_request = env.model.requests[0]
    (item,) = host._sessions.values()
    conversation, generation = item.scene.conversation_id, item.scene.generation
    first_event = max(await _event_ids(database))
    first_admission = await host.ordinary.get(first_event)
    saved_on = await _persisted_hint(host, item)
    with Statements(database) as on_tick:
        await host.tick()
    on_now, on_revision = item.controller.state.now, item.revision
    binding_on = await host.repository.get_binding(conversation, generation)
    calls_on = (bindings.count, hints.count)

    # Semantic off through the real HOT writer.
    await _switch(config, "semantic_participation_enabled", False)
    requests_before = len(env.model.requests)
    await env.gateway.receive(_message(2, -10051, "!ai second question"))
    await drain(env)
    off_request = env.model.requests[requests_before]
    sends = [params["text"] for method, params in env.wire if method == "sendMessage"]
    second_event = max(await _event_ids(database))
    second_admission = await host.ordinary.get(second_event)
    reconciled_before = reconciled.count
    with Statements(database) as off_tick:
        await host.tick()
    await host.tick()
    off_now, off_revision = item.controller.state.now, item.revision
    binding_off = await host.repository.get_binding(conversation, generation)
    saved_off = await _persisted_hint(host, item)
    hydrated_while_off = f"event:{second_event}" in item.controller.state.events

    # Semantic on again.
    await _switch(config, "semantic_participation_enabled", True)
    await host.tick()
    binding_again = await host.repository.get_binding(conversation, generation)

    facts = {
        "F1_on_prompt_has_state_instruction": STATE_TAG in _system(on_request),
        "F1_off_prompt_has_state_instruction": STATE_TAG in _system(off_request),
        "F1_off_prompt_mentions_join_stay_quiet": "join 表示" in _system(off_request),
        "F2_sent_texts": sends,
        "F2_any_tag_delivered": any("yuki-state" in text for text in sends),
        "F3_bindings_on_off": [calls_on[0], bindings.count - calls_on[0]],
        "F3_hint_observations_on_off": [calls_on[1], hints.count - calls_on[1]],
        "F3_on_admission_has_binding": first_admission is not None
        and first_admission.binding is not None,
        "F3_off_admission_binding": None
        if second_admission is None
        else second_admission.binding is not None,
        "F4_off_tick_advanced_clock": off_now != on_now,
        "F4_off_tick_saved_snapshot": off_revision != on_revision,
        "F4_off_tick_read_new_history": hydrated_while_off,
        "F4_binding_owner_on_off": [
            binding_on.effective_owner.value,
            binding_off.effective_owner.value,
        ],
        "F4_binding_epoch_step_off": binding_off.controller_epoch - binding_on.controller_epoch,
        "F6_binding_owner_again": binding_again.effective_owner.value,
        "F6_controller_epoch_matches": item.controller.state.epoch
        == binding_again.controller_epoch,
        "F6_pending_after_reenable": item.controller.state.pending is not None,
        "F6_reenable_read_new_history": f"event:{second_event}" in item.controller.state.events,
        "F6_reenable_advanced_clock": item.controller.state.now > off_now,
        "F7_reconcile_calls_while_off": reconciled.count - reconciled_before >= 2,
        "F9_saved_hints_on": sorted(saved_on.values(), key=str),
        "F9_saved_hints_off_unchanged": saved_off == saved_on,
    }
    RESULTS["semantic_off"] = facts
    DIAGNOSTICS["statements_per_tick"] = {
        "semantic_on": on_tick.count,
        "semantic_off": off_tick.count,
    }
    DIAGNOSTICS["semantic_off_tick_query_plans"] = await off_tick.plans(database)

    assert facts["F1_on_prompt_has_state_instruction"]
    assert not facts["F1_off_prompt_has_state_instruction"]
    assert not facts["F1_off_prompt_mentions_join_stay_quiet"]
    assert sends == ["on reply", "off reply"]
    assert not facts["F2_any_tag_delivered"]
    assert facts["F3_bindings_on_off"] == [1, 0]
    assert facts["F3_hint_observations_on_off"][0] > 0
    assert facts["F3_hint_observations_on_off"][1] == 0
    assert facts["F3_on_admission_has_binding"]
    assert facts["F3_off_admission_binding"] in (None, False)
    assert not facts["F4_off_tick_advanced_clock"]
    assert not facts["F4_off_tick_saved_snapshot"]
    assert not facts["F4_off_tick_read_new_history"]
    assert facts["F4_binding_owner_on_off"] == ["semantic", "legacy"]
    assert facts["F4_binding_epoch_step_off"] == 1
    assert facts["F6_binding_owner_again"] == "semantic"
    assert facts["F6_controller_epoch_matches"]
    assert not facts["F6_pending_after_reenable"]
    assert facts["F6_reenable_read_new_history"]
    assert facts["F6_reenable_advanced_clock"]
    assert facts["F7_reconcile_calls_while_off"]
    assert facts["F9_saved_hints_on"] == ["join"]
    assert facts["F9_saved_hints_off_unchanged"]


@pytest.mark.parametrize("semantic", [True, False], ids=["semantic_on", "semantic_off"])
async def test_auto_reply_off_skips_implicit_classification_and_keeps_explicit_calls(
    telegram_env, database, tmp_path, semantic
):
    env = telegram_env
    host, processor, _, config = await _wire(
        env, database, tmp_path, conversation_semantic_participation_enabled=semantic
    )
    await _switch(config, "auto_reply_enabled", False)
    classified = Calls(host, "continuation_for_event")
    queued = Calls(processor._autonomous, "observe", replace=True)
    await _group(database, "-10052", require_mention=False)

    await env.gateway.receive(_message(1, -10052, "plain group chatter"))
    await drain(env)
    implicit_requests = len(env.model.requests)
    await env.gateway.receive(_message(2, -10052, "!ai explicit call"))
    await drain(env)
    sends = [params["text"] for method, params in env.wire if method == "sendMessage"]
    inbound = await _event_ids(database)

    facts = {
        "F8_recorded_inbound_events": len(inbound),
        "F8_model_requests_for_chatter": implicit_requests,
        "F8_implicit_classifications": classified.count,
        "F8_queued_implicit_or_legacy": queued.count,
        "F8_explicit_sends": sends,
    }
    RESULTS[f"auto_reply_off_{'semantic_on' if semantic else 'semantic_off'}"] = facts

    assert facts["F8_recorded_inbound_events"] == 2
    assert facts["F8_model_requests_for_chatter"] == 0
    assert facts["F8_implicit_classifications"] == 0
    assert facts["F8_queued_implicit_or_legacy"] == 0
    assert facts["F8_explicit_sends"] == ["TG reply"]


@pytest.mark.parametrize("semantic", [True, False], ids=["semantic_on", "semantic_off"])
async def test_startup_and_discovery_load_scopes_only_with_semantic_on(
    telegram_env, database, tmp_path, semantic
):
    env = telegram_env
    await _group(database, "-10053", require_mention=True)
    await env.gateway.receive(_message(1, -10053, "!ai create the conversation"))
    await drain(env)

    host, _ = await _host(database, tmp_path, observer=False)
    await host._store.close()
    host._store = None
    profiles = tmp_path / "model_profiles.toml"
    profiles.write_text("", encoding="utf-8")
    settings = make_settings(
        database.url,
        conversation_semantic_participation_enabled=semantic,
        semantic_participation_state_path=tmp_path / f"state-{semantic}.sqlite3",
        semantic_participation_model_config_file=tmp_path / "autonomous-model.json",
        model_profiles_file=profiles,
    )
    host.settings = settings
    host.runtime_config = RuntimeConfigService(database=database, settings=settings)
    await host.start()
    try:
        loaded_at_start = len(host._sessions)
        host._last_discovery_at = 0
        await host.tick()
        loaded_after_discovery = len(host._sessions)
    finally:
        await host.close()

    facts = {
        "F5_loaded_at_start": loaded_at_start,
        "F5_loaded_after_discovery": loaded_after_discovery,
    }
    RESULTS[f"startup_{'semantic_on' if semantic else 'semantic_off'}"] = facts
    expected = 1 if semantic else 0
    assert facts == {
        "F5_loaded_at_start": expected,
        "F5_loaded_after_discovery": expected,
    }
