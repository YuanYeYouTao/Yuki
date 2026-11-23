"""A relayed message carries its requesting event into the target Conversation (#289 P1).

Real ingress, the real SocialService ``send`` with a fake OneBot gateway, the real
prompt snapshot hydration and the main-Agent history renderer. Facts (internal IDs,
the rendered relay fields and whether the source body leaked) go to
``test-results/relay-provenance-e2e.json``.

Failure cases, written before the implementation:

R1  private -> group: the group outbound event keeps ``caused_by_event_id`` = the private
    request event; the group history line carries ``转达:#<request>/受 <name> 私下委托``
    and never the private request body;
R2  a reply in the same Conversation stays without a cause and without a relay field;
R3  group -> another person's private chat: ``转达:#<request>/来自群 <group name>``;
R4  private -> another person's private chat: ``转达:#<request>/受 <name> 委托``;
R5  group -> another group: ``来自群 <group name>``;
R6  the same call replayed returns the original receipt with its delivered text and no
    second transport send (the stored cause still matches);
R7  a send without a trusted request event (no inbound, e.g. automation / SELF) keeps no
    cause and no relay field;
R8  forgetting the requester clears the cause; the relayed message stays and loses the
    relay field;
R9  a Yuki outbound with a cause that is not a kept inbound message (an external event)
    is still rejected by the ledger; plugin replies keep their same-Conversation rule
    (``test_ledger_append_chain_matrix``: ``causal_user_message_*``).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update
from tests.integration.test_indexed_mention_delivery import append_message, send
from tests.support.canonical_identity import ensure_space
from tests.support.social_identity_cases import social_env

from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.conversations import ConversationScope
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.identity.db_models import CanonicalSpaceModel
from yuki.persistence.models import ChatEventModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repository_helpers import _event_record

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "relay-provenance-e2e.json"
RESULTS: dict[str, object] = {}
PRIVATE_SECRET = "私聊里的原话不该进群"


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@pytest.fixture
async def env(database, tmp_path):
    result = await social_env(database, tmp_path)
    result.started = datetime.now(UTC)
    async with database.immediate_session() as session:
        await session.execute(
            update(CanonicalSpaceModel)
            .where(CanonicalSpaceModel.id == result.space)
            .values(name="摄影群")
        )
    result.group_request = await append_message(
        result, "10001", "Owner", at=result.started, message_id="group-request"
    )
    result.bot.calls.clear()
    return result


async def private_request(env, native, name, *, message_id, text=PRIVATE_SECRET):
    message = await append_message(
        env,
        native,
        name,
        at=datetime.now(UTC),
        message_id=message_id,
        group_id=None,
    )
    async with env.db.immediate_session() as session:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == message.source_event_id)
            .values(content=text)
        )
    return message


async def person_of(env, event_id):
    async with env.db.read_session() as session:
        return (await session.get(ChatEventModel, event_id)).author_person_id


async def relay_send(env, source, text, *, call_id, target=None):
    """Main ``send`` in the turn triggered by ``source`` (the helper reads ``env.source``)."""
    env.source = source
    return await send(env, text, call_id=call_id, **({"target": target} if target else {}))


async def event_row(env, event_id):
    async with env.db.read_session() as session:
        return await session.get(ChatEventModel, event_id)


async def history_lines(env, conversation_id):
    """The real main-Agent history of one Conversation, as the model reads it."""
    repository = ConversationScopeRepository(env.db)
    async with env.db.read_session() as session:
        row = await session.scalar(
            select(ChatEventModel)
            .where(ChatEventModel.canonical_conversation_id == conversation_id)
            .limit(1)
        )
    scope: ConversationScope = _event_record(row).scope
    snapshot = await repository.prepare_prompt_snapshot(scope, self_name="Yuki")
    renderer = ChatEventPromptRenderer(self_name="Yuki", references=snapshot.references)
    return "\n".join(
        message.content or "" for _, _, message in renderer.main_agent_history(snapshot.raw_events)
    )


def relay_field(rendered: str, event_id: int) -> str | None:
    for line in rendered.splitlines():
        if line.startswith(f"#{event_id}|") or f"\n#{event_id}|" in line:
            fields = line.partition(">")[0].split("|")
            return next((field for field in fields if field.startswith("转达:")), None)
    return None


async def takeover_person(env, native, name):
    message = await append_message(
        env, native, name, at=datetime.now(UTC), message_id=f"seed-{native}", group_id=None
    )
    person = await person_of(env, message.source_event_id)
    assert await env.router.cas_takeover_person(person) == "taken"
    return person, message.conversation_id


async def test_r1_r6_private_to_group_keeps_the_request_without_its_body(env):
    request = await private_request(env, "10001", "Owner", message_id="private-request")
    first = await relay_send(
        env, request, "主人让我说一声：周六拍照", call_id="relay", target={"space_id": env.space}
    )
    sent = await event_row(env, first["event_id"])
    rendered = await history_lines(env, sent.canonical_conversation_id)
    field = relay_field(rendered, sent.id)
    transport_calls = len(env.bot.calls)
    replay = await relay_send(
        env, request, "主人让我说一声：周六拍照", call_id="relay", target={"space_id": env.space}
    )
    RESULTS["R1"] = {
        "status": first["status"],
        "source_conversation": request.conversation_id,
        "target_conversation": sent.canonical_conversation_id,
        "request_event_id": request.source_event_id,
        "caused_by_event_id": sent.caused_by_event_id,
        "relay_field": field,
        "source_body_in_target_history": PRIVATE_SECRET in rendered,
    }
    RESULTS["R6"] = {
        "status": replay["status"],
        "same_event": replay["event_id"] == first["event_id"],
        "delivered_text_available": replay["delivered_text_available"],
        "new_transport_calls": len(env.bot.calls) - transport_calls,
    }
    assert first["status"] == "succeeded"
    assert sent.canonical_conversation_id != request.conversation_id
    assert sent.caused_by_event_id == request.source_event_id
    assert field == f"转达:#{request.source_event_id}/受 Owner 私下委托"
    assert PRIVATE_SECRET not in rendered
    assert replay["status"] == "succeeded" and replay["event_id"] == first["event_id"]
    assert replay["delivered_text_available"] is True
    assert len(env.bot.calls) == transport_calls


async def test_r2_same_conversation_reply_has_no_cause(env):
    result = await relay_send(env, env.group_request, "群里直接回", call_id="reply")
    sent = await event_row(env, result["event_id"])
    rendered = await history_lines(env, sent.canonical_conversation_id)
    RESULTS["R2"] = {
        "caused_by_event_id": sent.caused_by_event_id,
        "relay_field": relay_field(rendered, sent.id),
    }
    assert sent.canonical_conversation_id == env.group_request.conversation_id
    assert sent.caused_by_event_id is None
    assert relay_field(rendered, sent.id) is None


async def test_r3_group_to_private_names_the_group(env):
    person, conversation = await takeover_person(env, "10002", "Mika")
    result = await relay_send(
        env, env.group_request, "群里有人找你", call_id="to-private", target={"person_id": person}
    )
    sent = await event_row(env, result["event_id"])
    rendered = await history_lines(env, conversation)
    field = relay_field(rendered, sent.id)
    RESULTS["R3"] = {
        "caused_by_event_id": sent.caused_by_event_id,
        "request_event_id": env.group_request.source_event_id,
        "relay_field": field,
    }
    assert sent.canonical_conversation_id == conversation
    assert sent.caused_by_event_id == env.group_request.source_event_id
    assert field == f"转达:#{env.group_request.source_event_id}/来自群 摄影群"


async def test_r4_r8_private_to_another_private_and_forget(env):
    person, conversation = await takeover_person(env, "10002", "Mika")
    requester = await private_request(env, "10003", "Rin", message_id="rin-request")
    result = await relay_send(
        env, requester, "Rin 让我转告你", call_id="to-mika", target={"person_id": person}
    )
    sent = await event_row(env, result["event_id"])
    rendered = await history_lines(env, conversation)
    field = relay_field(rendered, sent.id)
    assert await PeopleRepository(env.db).delete_person_id(
        await person_of(env, requester.source_event_id)
    )
    kept = await event_row(env, sent.id)
    after = await history_lines(env, conversation)
    RESULTS["R4"] = {
        "caused_by_event_id": sent.caused_by_event_id,
        "request_event_id": requester.source_event_id,
        "relay_field": field,
        "source_body_in_target_history": PRIVATE_SECRET in rendered,
    }
    RESULTS["R8"] = {
        "relayed_event_kept": kept is not None,
        "caused_by_after_forget": kept.caused_by_event_id,
        "relay_field_after_forget": relay_field(after, sent.id),
    }
    assert sent.caused_by_event_id == requester.source_event_id
    assert field == f"转达:#{requester.source_event_id}/受 Rin 委托"
    assert PRIVATE_SECRET not in rendered
    assert kept.caused_by_event_id is None
    assert relay_field(after, sent.id) is None


async def test_r5_group_to_another_group(env):
    async with env.db.immediate_session() as session:
        other = await ensure_space(session, "20002", name="另一个群")
    assert await env.router.cas_takeover_space(other) == "taken"
    result = await relay_send(
        env, env.group_request, "那边的群在问", call_id="to-group", target={"space_id": other}
    )
    sent = await event_row(env, result["event_id"])
    rendered = await history_lines(env, sent.canonical_conversation_id)
    field = relay_field(rendered, sent.id)
    RESULTS["R5"] = {"caused_by_event_id": sent.caused_by_event_id, "relay_field": field}
    assert sent.caused_by_event_id == env.group_request.source_event_id
    assert field == f"转达:#{env.group_request.source_event_id}/来自群 摄影群"


async def test_r7_send_without_request_event_keeps_no_cause(env):
    person, conversation = await takeover_person(env, "10002", "Mika")
    result = await env.service.execute(
        "send", {"text": "没有请求事件", "target": {"person_id": person}}, env.context
    )
    sent = await event_row(env, result["event_id"])
    rendered = await history_lines(env, conversation)
    RESULTS["R7"] = {
        "caused_by_event_id": sent.caused_by_event_id,
        "relay_field": relay_field(rendered, sent.id),
    }
    assert sent.caused_by_event_id is None
    assert relay_field(rendered, sent.id) is None
