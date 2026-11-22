"""E2E: ``search_memory(fact_id)`` returns the evidence visible in this conversation (P4 §10).

Authoritative spec: docs/architecture/Yuki-记忆分层P4人物画像任务书-2026-11-22.md §10.2
(P4-Q5), §10.3 (P4-Q7) and case H25 of §13. The read authorization of
``search_memory`` is unchanged (no exception for profile facts); only the result of a
``fact_id`` read gains ``evidence`` (``{event_id, excerpt}`` ascending by event ID) and
``evidence_hidden`` (rows that exist but are not shown here). Visibility follows the
current conversation, with the same per-row predicates as the profile eligibility
(``memory/resident.py``): a group conversation sees direct chat events of any group; the
private chat with P also sees direct events of P's own private chat (by person root);
tool-receipt evidence is only counted, everywhere.

Fixture: the resident scene (A owner, B, C, B′; groups G and H). A and B are in G;
B and C are in H (B joins H here so C may read B's person facts). Facts and evidence go
through the real write path; tool receipts and the deleted event are direct table writes
like the resident suites. Turns run the real ``MessageProcessor`` -> ``ChatService`` ->
tool execution; only the model reply is scripted.

Failure cases, written before the implementation. A ``fact_id`` read that:
  E1 shows B's private-chat evidence (event ID or excerpt) in group G;
  E2 hides B's own private-chat evidence in B's private chat;
  E3 shows B's private-chat evidence in someone else's private chat (A's, C's);
  E4 hides a direct chat event of another group (H evidence read in G or a private chat);
  E5 shows a tool receipt's evidence (even one triggered in the reader's own chat), or
     does not count it;
  E6 invents evidence or a hidden count for a fact without evidence rows;
  E7 still shows (or counts) evidence whose chat event was deleted;
  E8 treats the private chat of a merged account (B′ into B) as someone else's;
  E9 changes the read authorization (a fact unreadable before stays unreadable);
  E10 lacks the grounding sentence that fact numbers can be looked up, or orders the
      shown evidence by anything but event ID.

Red baseline: before the implementation no ``fact_id`` read carries ``evidence``, so every
case fails on the missing key; the leakage cases E1/E3 also turn red when the private-chat
condition of the visibility predicate is removed.

Each case writes normalized JSON (the memory rows returned, fixture labels for IDs) to
``test-results/memory-profiles/<case>.json``; identical input gives identical bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from tests.support.account_observations import apply_observations, profile_message
from tests.support.memory_resident_scene import (
    GROUPS,
    QQ,
    ResidentScene,
    normalize,
    system_text,
)

from yuki.memory.enums import MemoryAuthority, MemoryEvidenceRelation, MemorySourceType
from yuki.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryToolReceiptModel

RESULTS = Path(__file__).resolve().parents[2] / "test-results" / "memory-profiles"
EXPLICIT = {"source_type": MemorySourceType.EXPLICIT, "authority": MemoryAuthority.EXPLICIT}
GROUNDING = (
    "常驻条目与画像每条后的 fact 编号是依据，想看细节用 search_memory(fact_id=编号) 读取原文。"
)


@pytest.fixture
async def scene(database, tmp_path):
    built = await ResidentScene(database, tmp_path).build()
    await apply_observations(
        database, profile_message(user_id=QQ["B"], nickname="person-B", group_id=GROUPS["H"])
    )
    return built


def _labelled(value: Any, events: dict[int, str]) -> Any:
    """Replace evidence event IDs by their fixture keys (IDs depend on seeding order)."""
    if isinstance(value, dict):
        return {
            key: f"<event:{events[item]}>"
            if key == "event_id" and item in events
            else _labelled(item, events)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_labelled(item, events) for item in value]
    return value


def _write(scene: ResidentScene, case: str, payload: dict[str, Any]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    events = {value: key for key, value in scene.events.items()}
    text = normalize(_labelled(payload, events), scene.labels())
    (RESULTS / f"{case}.json").write_text(text + "\n", encoding="utf-8")


async def _lookup(
    scene: ResidentScene,
    who: str,
    fact_id: int,
    *,
    group: str | None = None,
    person: str | None = None,
) -> dict[str, Any]:
    """One real turn whose scripted model calls ``search_memory(fact_id)``; the reply."""
    replies, turn = await scene.tool_turn(
        who,
        f"按编号回查 {fact_id} {group or who}",
        [("search_memory", {"fact_id": fact_id})],
        group=group,
        person=person,
    )
    scene.records.setdefault("_systems", []).append(system_text(turn.requests[0]))
    return replies[0]


def _row(reply: dict[str, Any], fact_id: int) -> dict[str, Any]:
    (row,) = reply["memories"]
    assert row["fact_id"] == fact_id
    return row


def _shown(row: dict[str, Any]) -> list[int]:
    return [item["event_id"] for item in row["evidence"]]


async def _receipt_evidence(scene: ResidentScene, fact_id: int, trigger_event_id: int) -> None:
    """A tool-receipt evidence row whose trigger event is ``trigger_event_id`` (direct write)."""
    now = datetime.now(UTC)
    async with scene.database.immediate_session() as writer:
        receipt = MemoryToolReceiptModel(
            conversation_key_hash="f" * 64,
            trigger_event_id=trigger_event_id,
            bot_user_id="80001",
            canonical_space_id=scene.spaces["G"],
            provider_id="evidence-lookup-e2e",
            tool_name="read_chat",
            success=True,
            result_excerpt="回执里看到的一段虚构内容",
            result_characters=12,
            created_at=now,
            expires_at=now + timedelta(days=30),
        )
        writer.add(receipt)
        await writer.flush()
        writer.add(
            MemoryEvidenceModel(
                fact_id=fact_id,
                tool_receipt_id=receipt.id,
                source_speaker_user_id="80001",
                relation=MemoryEvidenceRelation.AGENT_REFLECTION.value,
                authority=MemoryAuthority.AGENT_REFLECTION.value,
                confidence=0.9,
                excerpt="回执里看到的一段虚构内容",
                created_at=now,
            )
        )


# --------------------------------------------------------------------- E1 E2 E3 E10
async def test_h25_private_evidence_only_in_own_private_chat(scene):
    """H25: x has a G event and a B-private event; G, A and C see one, B sees both."""
    b_private = await scene.event("private:B", "B", "我周末去爬山了", key="b_private")
    g_event = await scene.event("space:G", "B", "周末去爬山了", key="g_event")
    # Evidence rows are inserted newest event first; the result must ascend by event ID.
    x = await scene.fact("x", "B", "周末爱去爬山", evidence=(g_event, b_private))
    excerpt = scene.contents[x][:40]

    in_g = _row(await _lookup(scene, "A", x, group="G"), x)
    in_b = _row(await _lookup(scene, "B", x), x)
    in_a = _row(await _lookup(scene, "A", x), x)
    in_c = _row(await _lookup(scene, "C", x), x)

    assert in_g["evidence"] == [{"event_id": g_event, "excerpt": excerpt}]
    assert in_g["evidence_hidden"] == 1
    assert b_private < g_event
    assert in_b["evidence"] == [
        {"event_id": b_private, "excerpt": excerpt},
        {"event_id": g_event, "excerpt": excerpt},
    ]
    assert "evidence_hidden" not in in_b
    for other in (in_a, in_c):
        assert _shown(other) == [g_event]
        assert other["evidence_hidden"] == 1
    # The fact fields themselves are unchanged by the new keys.
    assert {key: value for key, value in in_g.items() if not key.startswith("evidence")} == {
        key: value for key, value in in_b.items() if not key.startswith("evidence")
    }
    # E10: the grounding rule tells the model fact numbers can be looked up.
    systems = scene.records["_systems"]
    assert all(system.count(GROUNDING) == 1 for system in systems)
    _write(
        scene,
        "H25",
        {
            "facts": scene.facts,
            "space:G": in_g,
            "private:B": in_b,
            "private:A": in_a,
            "private:C": in_c,
            "grounding_sentence_in_system": GROUNDING in systems[0],
            "recovery_contract": await scene.contract(),
        },
    )


# ---------------------------------------------------------------------------- E4
async def test_h25_direct_events_of_any_group_are_visible(scene):
    """All groups are one public space: B's H evidence is shown in G and in A's private chat."""
    h_event = await scene.event("space:H", "B", "我在学日语", key="h_event")
    y = await scene.fact("y", "B", "在学日语", evidence=(h_event,))

    in_g = _row(await _lookup(scene, "A", y, group="G"), y)
    in_a = _row(await _lookup(scene, "A", y), y)

    for row in (in_g, in_a):
        assert row["evidence"] == [{"event_id": h_event, "excerpt": scene.contents[y][:40]}]
        assert "evidence_hidden" not in row
    _write(scene, "H25-cross-group", {"facts": scene.facts, "space:G": in_g, "private:A": in_a})


# ---------------------------------------------------------------------------- E5
async def test_h25_tool_receipt_evidence_is_only_counted(scene):
    """Receipts triggered in G and in B's private chat are counted, never shown, anywhere."""
    g_event = await scene.event("space:G", "B", "我养了两只猫", key="g_event")
    b_trigger = await scene.event("private:B", "B", "帮我看看那段记录", key="b_trigger")
    g_trigger = await scene.event("space:G", "B", "帮我查一下", key="g_trigger")
    r = await scene.fact("r", "B", "养了两只猫", evidence=(g_event,))
    await _receipt_evidence(scene, r, b_trigger)
    await _receipt_evidence(scene, r, g_trigger)

    in_g = _row(await _lookup(scene, "A", r, group="G"), r)
    in_b = _row(await _lookup(scene, "B", r), r)

    for row in (in_g, in_b):
        assert _shown(row) == [g_event]
        assert row["evidence_hidden"] == 2
        assert "回执里看到的一段虚构内容" not in str(row)
    _write(scene, "H25-receipt", {"facts": scene.facts, "space:G": in_g, "private:B": in_b})


# ---------------------------------------------------------------------------- E6
async def test_h25_fact_without_evidence_rows(scene):
    """An explicit fact without evidence rows reads with an empty list and no hidden count."""
    n = await scene.fact("n", "B", "生日在三月", **EXPLICIT)

    in_g = _row(await _lookup(scene, "A", n, group="G"), n)
    in_b = _row(await _lookup(scene, "B", n), n)

    for row in (in_g, in_b):
        assert row["evidence"] == []
        assert "evidence_hidden" not in row
    _write(scene, "H25-no-evidence", {"facts": scene.facts, "space:G": in_g, "private:B": in_b})


# ---------------------------------------------------------------------------- E7
async def test_h25_deleted_evidence_event_is_gone(scene):
    """Deleting one evidence event removes its row: neither shown nor counted."""
    kept = await scene.event("space:G", "B", "我喜欢下围棋", key="kept")
    gone = await scene.event("space:G", "B", "围棋下得不错", key="gone")
    d = await scene.fact("d", "B", "喜欢下围棋", evidence=(kept, gone))
    before = _row(await _lookup(scene, "A", d, group="G"), d)
    async with scene.database.immediate_session() as writer:
        await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == gone))

    after = _row(await _lookup(scene, "A", d, group="G"), d)

    assert _shown(before) == [kept, gone]
    assert _shown(after) == [kept]
    assert "evidence_hidden" not in after
    _write(scene, "H25-deleted-event", {"facts": scene.facts, "before": before, "after": after})


# ---------------------------------------------------------------------------- E8
async def test_h25_merged_account_private_chat_is_own_private(scene):
    """B′ merged into B: evidence from B′'s private chat is B's own private chat."""
    from tests.support.person_merge import merge_persons

    b2_event = await scene.event("private:B2", "B2", "我在杭州上班", key="b2_event")
    m = await scene.fact("m", "B2", "在杭州上班", evidence=(b2_event,))
    async with scene.database.immediate_session() as writer:
        await merge_persons(writer, scene.persons["B2"], scene.persons["B"])

    in_b = _row(await _lookup(scene, "B", m), m)
    in_b2 = _row(await _lookup(scene, "B2", m, person="B"), m)
    in_g = _row(await _lookup(scene, "A", m, group="G"), m)

    for row in (in_b, in_b2):
        assert _shown(row) == [b2_event]
        assert "evidence_hidden" not in row
    assert in_g["evidence"] == []
    assert in_g["evidence_hidden"] == 1
    _write(
        scene,
        "H25-merged",
        {"facts": scene.facts, "private:B": in_b, "private:B2": in_b2, "space:G": in_g},
    )


# ---------------------------------------------------------------------------- E9
async def test_h25_read_authorization_is_unchanged(scene):
    """C shares no group with A: A's fact stays unreadable in C's private chat."""
    a_event = await scene.event("space:G", "A", "我最近在读三体", key="a_event")
    a_fact = await scene.fact("a_fact", "A", "最近在读三体", evidence=(a_event,))

    in_c = await _lookup(scene, "C", a_fact)
    in_g = _row(await _lookup(scene, "B", a_fact, group="G"), a_fact)

    assert in_c["error"] == "memory_not_found", in_c
    assert _shown(in_g) == [a_event]
    _write(scene, "H25-authorization", {"facts": scene.facts, "private:C": in_c, "space:G": in_g})
