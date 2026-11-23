"""E2E: calling the current role name in a group is an explicit call, like a native @ (#653).

Owner decision 2026-11-23: no semantic participation for this; the role's name counts
as an explicit call. Production runs with ``conversation.auto_reply_enabled=false``
and ``conversation.semantic_participation_enabled=false`` on a ``require_mention``
group, so the call must work in exactly that configuration.

The scene is the Telegram runtime scene (isolated SQLite, fake Provider, the real
processor, Main, Social and delivery). The role name comes from the real character
profile (``CharacterRepository``); renames go through its real update path. The QQ
case runs the real OneBot normalizer and canonical ingress, whose native mention text
carries the target's group card.

Matching rule: every name is the current role ``display_name`` plus explicit
``BOT_ALIASES`` (the existing ``character_identity.aliases``), compared after
``casefold``. A name edge that is an ASCII letter or digit must not touch another
ASCII letter or digit; CJK or punctuation neighbours never block (Chinese has no word
separators). Text inside a native mention span is the target's display name and is
not a call. Only the current message text counts, not a quoted reply.

Failure cases, written before the implementation:

N1  auto_reply off + semantic off, require_mention group: ``Yuki 今天在吗`` (no @) is
    not answered at all today. It must be answered and delivered once.
N2  case and mixed scripts: ``问问yuki吧`` and ``YUKI，看这个`` must be calls.
N3  the name inside another Latin word is not a call: ``Yukiko 你好``,
    ``ayuki 和 yukihira`` are recorded only; no model request, no send.
N4  a native mention of another member whose mention text is the name (``@Yuki``
    pointing at member 777) is not a call.
N5  a bot author saying the name is not a call (the existing bot rule still wins).
N6  a name only in the quoted reply, not in the current text, is not a call.
N7  same chain as @: the call is admitted with the inbound event as source, without a
    participation binding when semantic is off, and redelivery of the same platform
    message does not answer twice. The native @ control is answered the same way.
N8  current role name, not a hard-coded "Yuki": after renaming the role to ``Aria``
    through the character profile, ``Aria 在吗`` is a call and ``Yuki 在吗`` is not.
N9  an explicit ``BOT_ALIASES`` name (CJK) is a call: ``小雪在吗``.
N10 semantic on keeps the same rule (control): the name call is answered with auto
    reply off.
N11 QQ: real OneBot ingress renders a native @ of a member carded ``Yuki迷`` as
    ``@Yuki迷``; that is not a call, while a plain ``Yuki 在吗`` is.

Facts go to ``test-results/name-call-e2e.json``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from tests.conftest import make_settings
from tests.integration.test_mention_snapshots import _qq, current_message
from tests.integration.test_participation_off_e2e import (
    _event_ids,
    _group,
    _message,
    _switch,
    _wire,
)
from tests.integration.test_telegram_runtime import drain
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.support.account_observations import apply_observations, profile_message
from tests.support.client_identity import client_identity_service
from tests.unit.test_normalizer import group_event

from yuki.character.repository import CharacterRepository
from yuki.services.policies import EffectiveGroupPolicy, evaluate_message

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "name-call-e2e.json"
RESULTS: dict[str, Any] = {}
CODE_UNDER_TEST = (
    "src/yuki/services/policies.py",
    "src/yuki/services/processor.py",
    "src/yuki/config.py",
)


@pytest.fixture(scope="module", autouse=True)
def _write_artifact():
    yield
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    table = json.dumps(RESULTS, ensure_ascii=False, indent=2, sort_keys=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "suite": "tests/integration/test_name_call_e2e.py",
                "generated_at": datetime.now(UTC).isoformat(),
                "suite_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "code_sha256": {
                    path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                    for path in CODE_UNDER_TEST
                },
                "scenarios_sha256": hashlib.sha256(table.encode()).hexdigest(),
                "scenarios": RESULTS,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _sends(env) -> int:
    return sum(1 for method, _ in env.wire if method == "sendMessage")


async def _deliver(env, processor, database, update: dict[str, Any]) -> dict[str, Any]:
    requests, sends = len(env.model.requests), _sends(env)
    before = set(await _event_ids(database))
    await env.gateway.receive(update)
    await drain(env)
    created = sorted(set(await _event_ids(database)) - before)
    admission = await processor.ordinary_admissions.get(created[0]) if created else None
    return {
        "recorded": len(created),
        "answered": _sends(env) > sends,
        "sends": _sends(env) - sends,
        "model_requests": len(env.model.requests) - requests,
        "admission_reason": admission.reason if admission is not None else None,
        "admission_has_binding": admission is not None and admission.binding is not None,
    }


def _text(identifier: int, chat: int, text: str, **extra: Any) -> dict[str, Any]:
    incoming = _message(identifier, chat, text)
    incoming["message"].update(extra)
    return incoming


@pytest.mark.parametrize("semantic", [False, True], ids=["semantic_off", "semantic_on"])
async def test_role_name_is_an_explicit_group_call(telegram_env, database, tmp_path, semantic):
    env = telegram_env
    _, processor, _, config = await _wire(
        env, database, tmp_path, conversation_semantic_participation_enabled=semantic
    )
    await _switch(config, "auto_reply_enabled", False)
    await _switch(config, "semantic_participation_enabled", semantic)
    character = CharacterRepository(client_identity_service(database), processor._settings)
    current = await character.initialize("Yuki")
    chat = -10061
    await _group(database, str(chat), require_mention=True)

    cases: dict[str, dict[str, Any]] = {}
    sequence = 0

    async def deliver(label: str, update: dict[str, Any]) -> None:
        cases[label] = await _deliver(env, processor, database, update)

    def text(body: str, **extra: Any) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        return _text(sequence, chat, body, **extra)

    await deliver("N1_plain_name", text("Yuki 今天在吗"))
    redelivered = _text(sequence, chat, "Yuki 今天在吗")
    await deliver("N7_redelivery", redelivered)
    await deliver("N2_lower_in_chinese", text("问问yuki吧"))
    await deliver("N2_upper_with_cjk_punctuation", text("YUKI，看这个"))
    await deliver("N3_prefix_of_word", text("Yukiko 你好"))
    await deliver("N3_inside_words", text("ayuki 和 yukihira"))
    await deliver(
        "N4_native_mention_of_member",
        text(
            "@Yuki 在吗",
            entities=[
                {
                    "type": "text_mention",
                    "offset": 0,
                    "length": 5,
                    "user": {"id": 777, "first_name": "Yuki", "is_bot": False},
                }
            ],
        ),
    )
    await deliver(
        "N5_bot_author",
        text("Yuki 在吗", **{"from": {"id": 4242, "first_name": "B", "is_bot": True}}),
    )
    await deliver(
        "N6_name_only_in_quote",
        text(
            "同意",
            reply_to_message={
                "message_id": 9001,
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 43, "first_name": "Q", "is_bot": False},
                "chat": {"id": chat, "type": "supergroup", "title": "G"},
                "text": "Yuki 说得对",
            },
        ),
    )
    await deliver(
        "N7_native_at_control",
        text(
            "@yuki_test 在吗",
            entities=[{"type": "mention", "offset": 0, "length": 10}],
        ),
    )
    renamed = await character.update(
        str(uuid4()),
        current.revision,
        {"display_name": "Aria"},
        operator_id=str(uuid4()),
    )
    await deliver("N8_new_name", text("Aria 在吗"))
    await deliver("N8_old_name", text("Yuki 在吗"))
    processor._settings.bot_aliases_csv = "小雪"
    await deliver("N9_cjk_alias", text("小雪在吗"))

    facts = {
        "role_display_name_after_rename": renamed["display_name"],
        "cases": cases,
    }
    RESULTS["semantic_on" if semantic else "semantic_off"] = facts

    answered = {label: case["answered"] for label, case in cases.items()}
    assert answered == {
        "N1_plain_name": True,
        "N7_redelivery": False,
        "N2_lower_in_chinese": True,
        "N2_upper_with_cjk_punctuation": True,
        "N3_prefix_of_word": False,
        "N3_inside_words": False,
        "N4_native_mention_of_member": False,
        "N5_bot_author": False,
        "N6_name_only_in_quote": False,
        "N7_native_at_control": True,
        "N8_new_name": True,
        "N8_old_name": False,
        "N9_cjk_alias": True,
    }
    for label, case in cases.items():
        if case["answered"]:
            assert case["sends"] == 1, label
            expected = "group_triggered" if label == "N7_native_at_control" else "group_name_call"
            assert case["admission_reason"] == expected, label
            if not semantic:
                assert not case["admission_has_binding"], label
        elif label != "N7_redelivery":
            assert case["model_requests"] == 0, label
            assert case["admission_reason"] is None, label


async def test_qq_native_mention_text_is_not_a_name_call(database):
    bot, resolver, _ = await _qq(database)
    await apply_observations(
        database,
        profile_message(user_id="1002", nickname="n-1002", group_id="2001", group_card="Yuki迷"),
    )
    settings = make_settings(database.url)
    policy = EffectiveGroupPolicy(True, require_mention=True)

    async def decide(message: Message, message_id: int) -> tuple[str, str]:
        native = current_message(group_event(message, message_id=message_id))
        admitted = await resolver.pre_admit(bot, native)
        assert not admitted.dropped
        decision = evaluate_message(replace(admitted.message), settings, group_policy=policy)
        return admitted.message.text, decision.reason

    member_text, member_reason = await decide(
        Message([MessageSegment.at(1002), MessageSegment.text(" 今天在吗")]), 65301
    )
    plain_text, plain_reason = await decide(Message("Yuki 在吗"), 65302)
    RESULTS["qq_ingress"] = {
        "N11_member_mention_rendered_with_name": "Yuki" in member_text,
        "N11_member_mention_reason": member_reason,
        "N11_plain_name_reason": plain_reason,
    }
    assert "Yuki迷" in member_text
    assert member_reason == "group_not_triggered"
    assert plain_text == "Yuki 在吗"
    assert plain_reason == "group_name_call"
