"""Native Bot addresses can precede commands without changing stored message text."""

from dataclasses import replace

import pytest
from tests.conftest import make_settings

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, MessageMention, SenderIdentity
from yuki.services.group_recovery import NativeGroupRecovery
from yuki.services.policies import (
    CommandName,
    EffectiveGroupPolicy,
    evaluate_message,
    parse_message_command,
)


def message(text, *, kind="yuki", target="cli_app:ou_yuki", native=True):
    start = text.index("@Yuki") if "@Yuki" in text else 0
    return InboundMessage(
        "om_command",
        "message:feishu",
        ScopeType.GROUP,
        SenderIdentity("cli_app:ou_person"),
        text,
        platform="feishu",
        bot_user_id="cli_app:ou_yuki",
        group_id="oc_group",
        mentions_bot=native and kind == "yuki",
        mentions=(MessageMention(kind, 0, start, start + len("@Yuki"), target),) if native else (),
    )


@pytest.mark.parametrize("kind", ["yuki", "member"])
def test_native_current_bot_address_precedes_group_control_without_rewriting(kind):
    original = message(" \t@Yuki  /ai admin group current enable", kind=kind)
    before = (original.text, original.mentions)
    assert parse_message_command(original, "!ai") == (
        CommandName.ADMIN,
        "group current enable",
        True,
    )
    decision = evaluate_message(
        original,
        make_settings("sqlite+aiosqlite:///:memory:"),
        group_policy=EffectiveGroupPolicy(True),
    )
    assert decision.command is CommandName.ADMIN
    assert decision.content == "group current enable"
    assert (original.text, original.mentions) == before


def test_native_bot_address_works_for_disabled_group_recovery(database):
    recovery = NativeGroupRecovery(make_settings(database.url), database, None, None)
    assert recovery.is_enable_request(message("@Yuki /ai admin group current enable"))
    assert not recovery.is_enable_request(
        message("@Yuki /ai admin group current enable", native=False)
    )


@pytest.mark.parametrize(
    "original",
    [
        message("@Yuki /ai admin group current enable", native=False),
        message("@Yuki /ai admin group current enable", kind="member", target="ou_other"),
        message("hello @Yuki /ai admin group current enable"),
        message("@Yuki hello, how are you?"),
    ],
)
def test_literal_member_and_ordinary_chat_keep_the_original_body(original):
    assert parse_message_command(original, "!ai") == (None, original.text.strip(), False)


def test_multiple_native_bot_mentions_and_configured_prefix():
    original = message("@Yuki @Yuki !ai help me")
    original = replace(
        original,
        mentions=(
            *original.mentions,
            MessageMention("yuki", 1, 6, 11, "cli_app:ou_yuki"),
        ),
    )
    assert parse_message_command(original, "!ai") == (None, "help me", True)


def test_real_member_after_bot_address_is_not_removed():
    original = message("@Yuki @Other /ai admin group current enable")
    original = replace(
        original,
        mentions=(
            *original.mentions,
            MessageMention("member", 1, 6, 12, "ou_other"),
        ),
    )
    assert parse_message_command(original, "!ai") == (None, original.text, False)
