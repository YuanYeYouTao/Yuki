"""search_memory without query lists readable memories through the real Main backend.

Failure cases listed before the implementation
(docs/operations/search-memory-target-listing-20261108.md, section 1):
* target-only Person/Space/self lists that subject's readable facts instead of
  ``invalid_arguments``; an authorized Person with no facts is an empty list, an
  unrelated Person is still ``permission_denied``;
* no target keeps today's default scope: every authorized owner for a real person,
  only the current group and visible SELF for a SELF execution;
* the requester's private SELF never appears in a group scene; superseded and
  invalidated facts never appear;
* ``since``/``until`` alone filter strictly; ``limit`` caps the page and reports
  ``truncated``/``exhaustive`` with the same item shape as a query search;
* ``fact_id`` with another key and undeclared keys are still refused.

A scheduled automation in the real group conversation drives every call; the fixed
model replies are the only stand-in. Normalized evidence is written under
``test-results/search-memory-listing/``.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import update
from tests.support.account_observations import apply_observations, profile_message
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.canonical_identity import ensure_person

from yuki.automation.models import RunStatus
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.memory.enums import (
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
    SelfMemoryVisibility,
)
from yuki.memory.models import MemoryFactCreate
from yuki.persistence.models import MemoryFactModel

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _write(name, value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    seen: dict[str, str] = {}
    text = _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid:{len(seen) + 1}>"), text)
    # Only storage stamps vary between runs; fixed occurred_at values stay readable.
    text = re.sub(r'"last_confirmed_at": "[^"]+"', '"last_confirmed_at": "<now>"', text)
    directory = Path(__file__).resolve().parents[2] / "test-results" / "search-memory-listing"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(text + "\n", encoding="utf-8")


async def _scene(database, tmp_path, principal):
    case = await setup_run(
        database, tmp_path, mode="silent", principal=principal, self_memory_enabled=True
    )
    env = case.env
    async with database.immediate_session() as writer:
        other = await ensure_person(writer, "10002", now=datetime.now(UTC))
        stranger = await ensure_person(writer, "10003", now=datetime.now(UTC))
    # The other member shares group 20001; the stranger shares nothing.
    await apply_observations(
        database, profile_message(user_id="10002", nickname="other", group_id="20001")
    )
    memories = case.chat._memories

    async def remember(name, **fields):
        fact = await memories.remember(
            MemoryFactCreate(
                memory_key=f"listing:{name}",
                category="test",
                source_type=MemorySourceType.EXPLICIT,
                **fields,
            )
        )
        return name, fact.id

    person = {
        "subject_user_id": "10001",
        "canonical_subject_person_id": env.person,
        "scope_type": MemoryScopeType.PERSON,
    }
    facts = dict(
        [
            await remember(
                "astronomy",
                **person,
                content="喜欢天文",
                importance=5,
                valid_from=datetime(2026, 10, 1, tzinfo=UTC),
            ),
            await remember(
                "photographer",
                scope_type=MemoryScopeType.PERSON_GROUP,
                subject_user_id="10001",
                group_id="20001",
                canonical_subject_person_id=env.person,
                canonical_subject_space_id=env.space,
                content="在本群负责摄影",
                importance=4,
                confidence=0.9,
            ),
            await remember(
                "cat",
                **person,
                content="养了一只猫",
                importance=3,
                confidence=0.9,
                valid_from=datetime(2026, 11, 1, tzinfo=UTC),
            ),
            await remember("japanese", **person, content="在学日语", importance=2, confidence=0.9),
            await remember("old_tea", **person, content="旧：喜欢茶", importance=5),
            await remember("withdrawn", **person, content="撤回：住在北京", importance=5),
            await remember(
                "movie_night",
                scope_type=MemoryScopeType.GROUP,
                group_id="20001",
                canonical_subject_space_id=env.space,
                content="每周五群内观影",
                importance=3,
                confidence=0.8,
            ),
            await remember(
                "self_global",
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.GLOBAL,
                content="我喜欢下雨天",
                importance=2,
                confidence=0.8,
            ),
            await remember(
                "self_group",
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.GROUP,
                visibility_group_id="20001",
                canonical_visibility_space_id=env.space,
                content="我答应过本群周末分享歌单",
                importance=4,
                confidence=0.8,
            ),
            await remember(
                "self_private",
                scope_type=MemoryScopeType.SELF,
                visibility_type=SelfMemoryVisibility.PRIVATE,
                visibility_user_id="10001",
                canonical_visibility_person_id=env.person,
                content="私聊里答应替他保密",
                importance=5,
            ),
        ]
    )
    async with database.immediate_session() as writer:
        for name, values in (
            ("old_tea", {"status": MemoryStatus.SUPERSEDED.value}),
            (
                "withdrawn",
                {"status": MemoryStatus.INVALIDATED.value, "invalidated_reason": "用户撤回"},
            ),
        ):
            await writer.execute(
                update(MemoryFactModel).where(MemoryFactModel.id == facts[name]).values(**values)
            )
    return case, facts, other, stranger


async def _drive(case, calls):
    """One real scheduled turn; each fixed model reply issues the next call."""
    replies = {}

    def respond(request):
        index = len(case.provider.requests) - 1
        if index:
            replies[calls[index - 1][0]] = json.loads(request.messages[-1].content)
        if index >= len(calls):
            return "NO_REPLY"
        name, arguments = calls[index]
        return ChatResponse(
            "",
            0,
            tool_calls=(ToolCall(name, ToolFunction("search_memory", json.dumps(arguments))),),
        )

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, replies
    assert list(replies) == [name for name, _ in calls]
    return replies


def _ids(reply, facts):
    by_id = {fact_id: name for name, fact_id in facts.items()}
    return [by_id[row["fact_id"]] for row in reply["memories"]]


async def test_person_listing_by_target_default_scope_time_and_limit(database, tmp_path):
    # Ties in importance are broken by confidence, the next key of the existing order.
    case, facts, other, stranger = await _scene(database, tmp_path, "person")
    env = case.env
    calls = [
        ("person", {"target": {"person_id": env.person}}),
        ("space", {"target": {"space_id": env.space}}),
        ("self", {"target": {"self": True}}),
        ("empty_person", {"target": {"person_id": other}}),
        ("stranger", {"target": {"person_id": stranger}}),
        ("all_default_limit", {}),
        ("all", {"limit": 100}),
        ("person_page", {"target": {"person_id": env.person}, "limit": 2}),
        ("since_only", {"since": "2026-10-15T00:00:00+08:00", "limit": 100}),
        ("person_until", {"target": {"person_id": env.person}, "until": "2026-10-15T00:00:00Z"}),
        ("blank_query", {"query": "   ", "target": {"space_id": env.space}}),
        ("query", {"query": "天文"}),
        ("fact_id_conflict", {"fact_id": facts["astronomy"], "target": {"self": True}}),
        ("unknown_key", {"target": {"person_id": env.person}, "subject": "x"}),
    ]
    replies = await _drive(case, calls)

    assert _ids(replies["person"], facts) == ["astronomy", "photographer", "cat", "japanese"]
    assert replies["person"]["exhaustive"] is True and "truncated" not in replies["person"]
    assert _ids(replies["space"], facts) == ["movie_night"]
    # Group scene: global and this group's SELF only, never the requester's private SELF.
    assert _ids(replies["self"], facts) == ["self_group", "self_global"]
    assert replies["empty_person"] == {"memories": [], "exhaustive": True}
    assert replies["stranger"]["error"] == "permission_denied"
    readable = [
        "astronomy",
        "photographer",
        "self_group",
        "cat",
        "movie_night",
        "japanese",
        "self_global",
    ]
    assert _ids(replies["all"], facts) == readable
    assert replies["all"]["exhaustive"] is True
    # Default page is the configured per-read limit (4), reported as not exhaustive.
    assert _ids(replies["all_default_limit"], facts) == readable[:4]
    assert replies["all_default_limit"]["truncated"] is True
    assert replies["all_default_limit"]["exhaustive"] is False
    assert _ids(replies["person_page"], facts) == ["astronomy", "photographer"]
    assert replies["person_page"]["truncated"] is True
    assert _ids(replies["since_only"], facts) == ["cat"]
    assert _ids(replies["person_until"], facts) == ["astronomy"]
    assert _ids(replies["blank_query"], facts) == ["movie_night"]
    assert _ids(replies["query"], facts) == ["astronomy"]
    # Listing and query return the same item shape.
    assert replies["query"]["memories"][0] == replies["person"]["memories"][0]
    assert replies["fact_id_conflict"]["error"] == "invalid_arguments"
    assert replies["unknown_key"]["error"] == "tool_input_validation_failed"
    assert replies["unknown_key"]["executed"] is False
    for name in ("old_tea", "withdrawn", "self_private"):
        assert all(
            facts[name] not in {row["fact_id"] for row in reply.get("memories", ())}
            for reply in replies.values()
        )
    _write("person-group-scene", {"facts": facts, "calls": calls, "replies": replies})


async def test_self_execution_listing_keeps_current_group_scope(database, tmp_path):
    case, facts, _other, _stranger = await _scene(database, tmp_path, "self")
    env = case.env
    calls = [
        ("space", {"target": {"space_id": env.space}}),
        ("person_in_space", {"target": {"person_id": env.person}}),
        ("self", {"target": {"self": True}}),
        ("all", {"limit": 100}),
        ("page", {"limit": 1}),
    ]
    replies = await _drive(case, calls)

    assert _ids(replies["space"], facts) == ["movie_night"]
    # A SELF execution reads a member only inside the current space.
    assert _ids(replies["person_in_space"], facts) == ["photographer"]
    assert _ids(replies["self"], facts) == ["self_group", "self_global"]
    assert _ids(replies["all"], facts) == ["self_group", "movie_night", "self_global"]
    assert replies["all"]["exhaustive"] is True
    assert _ids(replies["page"], facts) == ["self_group"]
    assert replies["page"]["truncated"] is True and replies["page"]["exhaustive"] is False
    _write("self-execution", {"facts": facts, "calls": calls, "replies": replies})
