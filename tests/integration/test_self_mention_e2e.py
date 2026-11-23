"""Yuki never natively @-mentions its own sending account by name (#626).

Real ingress, the real SocialService ``send`` and a fake OneBot gateway. Two QQ instances
can both carry one name; the sending account has often spoken last in the group, so the
recency choice used to pick it. Facts go to ``test-results/self-mention-e2e.json``.

Failure cases, written before the implementation:

S1  group, the sender and another account share the name, the sender spoke last:
    ``@Ice`` targets the other account, never the sender;
S2  group, only the sender carries the name: ``@Ice`` stays plain text with no native
    mention (the original text keeps ``@Ice``);
S3  group, an explicit ``@{account:UUID}`` of the other same-name account still targets it;
S4  private, the peer and Yuki share the name: ``@ Ice`` targets the peer whichever of the
    two spoke last.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.integration.test_indexed_mention_delivery import (
    account,
    append_message,
    native_targets,
    send,
)
from tests.support.social_identity_cases import social_env

from yuki.domain.messages import MessageMention
from yuki.identity.db_models import AccountModel, IdentityBindingModel
from yuki.persistence.models import ChatEventModel

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "self-mention-e2e.json"
RESULTS: dict[str, object] = {}


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


@pytest.fixture
async def env(database, tmp_path):
    result = await social_env(database, tmp_path)
    result.started = datetime.now(UTC)
    result.source = await append_message(
        result, "10001", "Owner", at=result.started, message_id="real-source"
    )
    return result


async def name_yuki(env, *, at):
    """A member's real native mention of Yuki records the sender account as ``Ice``."""
    await append_message(
        env,
        "10001",
        "Owner",
        at=at,
        message_id="name-yuki",
        mentions=(MessageMention("yuki", 0, 6, 10, env.bot.self_id, "Ice"),),
    )


def targets_since(env, start):
    calls = env.bot.calls
    env.bot.calls = calls[start:]
    try:
        return native_targets(env)
    finally:
        env.bot.calls = calls


async def sent_text(env, event_id):
    async with env.db.read_session() as session:
        row = await session.get(ChatEventModel, event_id)
    return row.content


async def test_s1_s3_same_name_never_selects_the_sending_account(env):
    past = env.started - timedelta(minutes=5)
    await name_yuki(env, at=past)
    await append_message(env, "10077", "Ice", at=past + timedelta(seconds=1), message_id="other")
    other = await account(env, "10077")
    # The sending account speaks last, so recency alone would choose it.
    await send(env, "先说一句", call_id="warm")
    start = len(env.bot.calls)
    natural = await send(env, "@Ice 你好", call_id="natural")
    natural_targets = targets_since(env, start)
    start = len(env.bot.calls)
    explicit = await send(env, f"@{{account:{other.id}}} 指定", call_id="explicit")
    explicit_targets = targets_since(env, start)
    RESULTS["S1"] = {
        "status": natural["status"],
        "native_targets": natural_targets,
        "mentioned_is_other": [item["account_id"] for item in natural["mentioned_accounts"]]
        == [other.id],
    }
    RESULTS["S3"] = {"status": explicit["status"], "native_targets": explicit_targets}
    assert natural["status"] == "succeeded"
    assert natural_targets == ["10077"]
    assert [item["account_id"] for item in natural["mentioned_accounts"]] == [other.id]
    assert explicit_targets == ["10077"]


async def test_s2_only_the_sender_has_the_name_keeps_plain_text(env):
    await name_yuki(env, at=env.started - timedelta(minutes=5))
    await send(env, "先说一句", call_id="warm")
    start = len(env.bot.calls)
    result = await send(env, "@Ice 自言自语", call_id="self")
    targets = targets_since(env, start)
    text = await sent_text(env, result["event_id"])
    RESULTS["S2"] = {
        "status": result["status"],
        "native_targets": targets,
        "mentioned_accounts": len(result.get("mentioned_accounts") or ()),
        "text_keeps_at": text.startswith("@Ice"),
    }
    assert result["status"] == "succeeded"
    assert targets == []
    assert not result.get("mentioned_accounts")
    assert text.startswith("@Ice")


@pytest.mark.parametrize("last", ["peer", "yuki"])
async def test_s4_private_same_name_targets_the_peer(env, last):
    private = await append_message(
        env,
        "10002",
        "Ice",
        at=datetime.now(UTC),
        message_id="private-member",
        group_id=None,
        mentions=(MessageMention("yuki", 0, 6, 10, env.bot.self_id, "Ice"),),
    )
    async with env.db.read_session() as session:
        member = await session.scalar(
            select(IdentityBindingModel)
            .join(AccountModel)
            .where(AccountModel.platform == "qq", AccountModel.external_account_id == "10002")
        )
    assert await env.router.cas_takeover_person(member.person_id) == "taken"
    target = {"person_id": member.person_id}
    await send(env, "先前的私聊回复", target=target, call_id="private-history")
    if last == "peer":
        await append_message(
            env,
            "10002",
            "Ice",
            at=datetime.now(UTC) + timedelta(seconds=1),
            message_id="private-latest-member",
            group_id=None,
        )
    peer = await account(env, "10002")
    result = await send(env, "@ Ice 私聊里的称呼", target=target, call_id="private-mention")
    sent = [params for method, params in env.bot.calls if method == "send_private_msg"]
    RESULTS[f"S4-{last}"] = {
        "status": result["status"],
        "native_target": sent[-1]["message"][0].get("data", {}).get("qq"),
        "mentioned_is_peer": [item["account_id"] for item in result["mentioned_accounts"]]
        == [peer.id],
    }
    assert result["status"] == "succeeded"
    assert [item["account_id"] for item in result["mentioned_accounts"]] == [peer.id]
    assert sent[-1]["user_id"] == 10002
    assert sent[-1]["message"][0] == {"type": "at", "data": {"qq": "10002"}}
    async with env.db.read_session() as session:
        row = await session.get(ChatEventModel, result["event_id"])
    assert row.canonical_conversation_id == private.conversation_id
