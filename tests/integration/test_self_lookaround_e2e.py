"""E2E: the SELF "look around" turn over the things Yuki cares about (#541 P3).

Owner decisions of 2026-11-23 (research doc §8): off by default, an explicit daily hour
after the nightly Dream, read-only, telling only the owner through the existing proactive
private turn (``proactive_turns`` with a new ``source_kind``), one run's cost capped.
Real owner private chat, the real Main, Social, the shared ``ProactivePrivateTurns``
entry and the owner's proactive route (the watchdog scene); only model replies and time
are scripted. Facts (turn rows, model request counts, tool refusals, the concerns each
turn was given, sends; IDs replaced by labels) go to ``test-results/concern-research-e2e.json``.

Failure cases, written before the implementation:

L1  switched off (the default), a tick registers no turn and pays no model request;
L2  switched on, the slot registers one ``lookaround`` turn per superuser only; its
    material lists global concerns and the owner's private ones, never another person's
    private or a group concern; the owner gets one private message;
L3  the same slot registers no second turn; the next day's slot registers one more;
L4  the turn writes (save_memory, set_short_note, manage_schedule) or sends elsewhere;
L5  with no visible concern, the turn pays a model request;
L6  a turn pays more model requests than the cap, or is retried after reaching it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_watchdog_capacity_incident_e2e import (
    Scene,
    _call,
    _runtime_context,
    _tool_results,
)
from tests.support.canonical_identity import ensure_person, ensure_space

from yuki.memory.enums import (
    MemoryAuthority,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from yuki.memory.models import MemoryFactCreate
from yuki.persistence.models import ChatEventModel, MemoryFactModel
from yuki.runtime.proactive_schema import proactive_turns

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "concern-research-e2e.json"
RESULTS: dict[str, Any] = {}
DAY = 86_400.0


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    )


class LookScene(Scene):
    """The watchdog scene plus the look-around entry over the same turn entry."""

    def __init__(self, database, tmp_path, **settings: Any) -> None:
        super().__init__(database, tmp_path)
        self.look_settings = settings
        self.look_requests: list[Any] = []
        self.script: Any = None

    def respond(self, request):
        context = _runtime_context(request)
        if context is not None and "lookaround" in context:
            self.look_requests.append(request)
            return self.script(request)
        return super().respond(request)

    async def build(self) -> LookScene:  # type: ignore[override]
        await super().build()
        from yuki.memory.claim_processor import MemoryClaimProcessor
        from yuki.memory.mutation.service import MemoryMutationService

        # As the container wires Main's memory writes (save_memory / edit_memory).
        memories = self.chat._memories
        self.chat._tools._memory_mutations = MemoryMutationService(
            settings=self.chat._settings,
            facts=memories,
            processor=MemoryClaimProcessor(settings=self.chat._settings, facts=memories),
            ledger=self.harness.ledger,
        )
        from yuki.services.main_agent_contract import MainAgentContract

        # The frozen tool list is built with the memory writes declared.
        self.chat.runtime.runner.main_contract = MainAgentContract(
            self.chat, self.chat._tools.short_state
        )
        async with self.database.immediate_session() as writer:
            self.other = await ensure_person(writer, "10002", display_name="B")
            self.group = await ensure_space(writer, "20009", name="G2")
        self.look_restart()
        return self

    def look_restart(self) -> None:
        from yuki.application.lookaround import SelfLookaround

        settings = make_settings(
            self.database.url,
            memory_dream_timezone="UTC",
            **self.look_settings,
        )
        self.look = SelfLookaround(
            self.database, self.turns, self.config, settings=settings, clock=self.clock
        )

    async def look_tick(self, advance: float = 0.0) -> list[str]:
        self.clock.offset += advance
        await self.look.tick()
        return await self.look.drain()

    async def concern(self, content: str, visibility: str) -> int:
        target: dict[str, Any] = {"visibility_type": SelfMemoryVisibility.GLOBAL}
        if visibility == "owner":
            target = {
                "visibility_type": SelfMemoryVisibility.PRIVATE,
                "visibility_user_id": "10001",
                "canonical_visibility_person_id": self.env.person,
            }
        elif visibility == "other":
            target = {
                "visibility_type": SelfMemoryVisibility.PRIVATE,
                "visibility_user_id": "10002",
                "canonical_visibility_person_id": self.other,
            }
        elif visibility == "group":
            target = {
                "visibility_type": SelfMemoryVisibility.GROUP,
                "visibility_group_id": "20009",
                "canonical_visibility_space_id": self.group,
            }
        created = await self.chat._memories.remember(
            MemoryFactCreate(
                memory_key="concern",
                category="concern",
                content=content,
                source_type=MemorySourceType.AUTOMATIC,
                authority=MemoryAuthority.AGENT_REFLECTION,
                kind=MemoryKind.FACT,
                importance=3,
                scope_type=MemoryScopeType.SELF,
                **target,
            )
        )
        return int(created.id)

    async def turns_rows(self) -> list[dict[str, Any]]:
        async with self.database.read_session() as reader:
            rows = (
                await reader.execute(
                    select(
                        proactive_turns.c.source_kind,
                        proactive_turns.c.person_id,
                        proactive_turns.c.state,
                        proactive_turns.c.reason,
                        proactive_turns.c.attempts,
                    )
                    .where(proactive_turns.c.source_kind == "lookaround")
                    .order_by(proactive_turns.c.created)
                )
            ).mappings()
            return [
                {**row, "person_id": "owner" if row["person_id"] == self.env.person else "other"}
                for row in rows
            ]

    async def last_proactive_event(self) -> str | None:
        async with self.database.read_session() as reader:
            return await reader.scalar(
                select(ChatEventModel.origin)
                .where(ChatEventModel.direction == "outbound")
                .order_by(ChatEventModel.id.desc())
                .limit(1)
            )


def send_once(request):
    if any(message.role == "tool" for message in request.messages):
        return "看完了"
    concerns = _runtime_context(request)["lookaround"]["concerns"]
    ids = ",".join(str(item["fact_id"]) for item in concerns)
    return _call("send", {"text": f"今天看了看你在意的事：{ids}"}, "look-send")


def given(scene: LookScene) -> list[list[int]]:
    return [
        [item["fact_id"] for item in _runtime_context(request)["lookaround"]["concerns"]]
        for request in scene.look_requests
        if not any(message.role == "tool" for message in request.messages)
    ]


async def build(database, tmp_path, **settings: Any) -> LookScene:
    return await LookScene(database, tmp_path, **settings).build()


async def test_l1_off_by_default_registers_nothing(database, tmp_path):
    scene = await build(database, tmp_path)
    await scene.concern("全局在意的事", "global")
    outcomes = await scene.look_tick()
    RESULTS["L1"] = {"outcomes": outcomes, "turns": await scene.turns_rows()}
    assert outcomes == [] and await scene.turns_rows() == []
    assert scene.look_requests == []


async def test_l2_l3_one_turn_per_slot_for_superusers_with_visible_concerns(database, tmp_path):
    scene = await build(database, tmp_path, self_lookaround_enabled=True, self_lookaround_hour=0)
    scene.script = send_once
    visible = [await scene.concern("全局：一个技术方向", "global")]
    visible.append(await scene.concern("主人：下个月的考试", "owner"))
    hidden = [await scene.concern("B 的私事", "other"), await scene.concern("群里的事", "group")]
    first = await scene.look_tick()
    sends_after_first = len(scene.sends())
    origin = await scene.last_proactive_event()
    again = await scene.look_tick(advance=60)
    next_day = await scene.look_tick(advance=DAY)
    RESULTS["L2_L3"] = {
        "outcomes": [first, again, next_day],
        "turns": await scene.turns_rows(),
        "given_fact_ids": given(scene),
        "visible": visible,
        "hidden": hidden,
        "owner_private_sends": len(scene.sends()),
        "outbound_origin": origin,
    }
    assert first == ["finished"] and again == [] and next_day == ["finished"]
    assert [row["person_id"] for row in await scene.turns_rows()] == ["owner", "owner"]
    assert given(scene) == [visible, visible]
    assert sends_after_first == 1 and len(scene.sends()) == 2
    assert origin == "proactive_private"


async def test_l4_the_turn_only_reads_and_tells_the_owner(database, tmp_path):
    scene = await build(database, tmp_path, self_lookaround_enabled=True, self_lookaround_hour=0)
    fact_id = await scene.concern("全局：一个技术方向", "global")

    def script(request):
        if any(message.role == "tool" for message in request.messages):
            return "好"
        calls = {
            "w1": ("save_memory", {"target": {"self": True}, "content": "笔记"}),
            "w2": ("set_short_note", {"slot": 1, "text": "x"}),
            "w3": ("manage_schedule", {"action": "pause", "id": 1}),
            "w4": ("send", {"text": "发到群", "target": {"space_id": scene.env.space}}),
            "r1": ("search_memory", {"fact_id": fact_id}),
            "s1": ("send", {"text": "有个进展告诉你"}),
        }
        from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction

        return ChatResponse(
            "",
            0,
            tool_calls=tuple(
                ToolCall(identity, ToolFunction(name, json.dumps(arguments)))
                for identity, (name, arguments) in calls.items()
            ),
        )

    scene.script = script
    outcomes = await scene.look_tick()
    results = _tool_results(scene.look_requests[-1])
    errors = {key: (value or {}).get("error") for key, value in results.items()}
    async with scene.database.read_session() as reader:
        notes = list(
            await reader.scalars(
                select(MemoryFactModel.id).where(MemoryFactModel.content == "笔记")
            )
        )
    RESULTS["L4"] = {"outcomes": outcomes, "errors": errors, "notes": notes}
    assert outcomes == ["finished"]
    assert errors["w1"] == errors["w2"] == errors["w3"] == "report_only_source"
    assert errors["w4"] == "permission_denied"
    assert errors["r1"] is None and errors["s1"] is None
    assert notes == [] and len(scene.sends()) == 1


async def test_l5_no_concern_no_model_request(database, tmp_path):
    scene = await build(database, tmp_path, self_lookaround_enabled=True, self_lookaround_hour=0)
    await scene.concern("B 的私事", "other")
    outcomes = await scene.look_tick()
    RESULTS["L5"] = {"outcomes": outcomes, "turns": await scene.turns_rows()}
    assert outcomes == ["cleared_before_start"]
    assert scene.look_requests == []


async def test_l6_model_request_cap_ends_the_turn_without_retry(database, tmp_path):
    scene = await build(
        database,
        tmp_path,
        self_lookaround_enabled=True,
        self_lookaround_hour=0,
        self_lookaround_max_model_requests=3,
    )
    fact_id = await scene.concern("全局：一个技术方向", "global")
    counter = {"n": 0}

    def keep_reading(request):
        counter["n"] += 1
        return _call("search_memory", {"fact_id": fact_id}, f"read-{counter['n']}")

    scene.script = keep_reading
    first = await scene.look_tick()
    retry = await scene.look_tick(advance=3600)
    RESULTS["L6"] = {
        "outcomes": [first, retry],
        "model_requests": len(scene.look_requests),
        "turns": await scene.turns_rows(),
    }
    assert first == ["finished:model_request_cap"] and retry == []
    assert len(scene.look_requests) == 3
    assert [row["state"] for row in await scene.turns_rows()] == ["finished"]
