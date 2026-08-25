"""Permission, trigger, and conversation-key tests."""

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.services.policies import (
    CommandName,
    EffectiveGroupPolicy,
    EffectivePrivatePolicy,
    evaluate_message,
    replies_to_bot,
)


def message(
    *,
    user_id: str = "1001",
    scope: ScopeType = ScopeType.PRIVATE,
    text: str = "hello",
    group_id: str | None = None,
    mentions_bot: bool = False,
    is_bot: bool = False,
    bot_user_id: str = "8000",
    reply_sender_user_id: str | None = None,
    canonical_reply_to_yuki: bool | None = None,
    canonical_reply_author_kind: str | None = None,
    is_self_message: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id="1",
        event_type="message:test",
        scope_type=scope,
        sender=SenderIdentity(user_id, is_bot=is_bot),
        text=text,
        bot_user_id=bot_user_id,
        group_id=group_id,
        mentions_bot=mentions_bot,
        reply_sender_user_id=reply_sender_user_id,
        canonical_reply_to_yuki=canonical_reply_to_yuki,
        canonical_reply_author_kind=canonical_reply_author_kind,
        is_self_message=is_self_message,
    )


def settings() -> Settings:
    return Settings.model_validate(
        {
            "superusers_csv": "9000",
            "enabled_groups_csv": "2001",
            "ai_prefix": "!ai",
        }
    )


def test_all_private_users_are_allowed_by_default() -> None:
    assert evaluate_message(message(user_id="1001"), settings()).should_respond
    assert evaluate_message(message(user_id="9000"), settings()).should_respond
    new_user = evaluate_message(message(user_id="5555"), settings())
    assert new_user.should_respond
    assert new_user.reason == "private_allowed"


def test_private_database_policy_overrides_environment_but_not_superusers() -> None:
    enabled = evaluate_message(
        message(user_id="5555"),
        settings(),
        private_policy=EffectivePrivatePolicy(enabled=True),
    )
    disabled = evaluate_message(
        message(user_id="1001"),
        settings(),
        private_policy=EffectivePrivatePolicy(enabled=False),
    )
    superuser = evaluate_message(
        message(user_id="9000"),
        settings(),
        private_policy=EffectivePrivatePolicy(enabled=False),
    )
    assert enabled.should_respond
    assert not disabled.should_respond
    assert superuser.should_respond


def test_group_mention_triggers_but_plain_message_does_not() -> None:
    policy = EffectiveGroupPolicy(enabled=True)
    mentioned = evaluate_message(
        message(
            scope=ScopeType.GROUP,
            group_id="2001",
            mentions_bot=True,
            text="question",
        ),
        settings(),
        group_policy=policy,
    )
    plain = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="ordinary chat"),
        settings(),
        group_policy=policy,
    )
    assert mentioned.should_respond
    assert not plain.should_respond


def test_direct_plugin_trigger_respects_group_and_private_admission() -> None:
    enabled = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="*签到"),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=True),
        direct_triggered=True,
    )
    disabled_group = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2999", text="*签到"),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=False),
        direct_triggered=True,
    )
    disabled_private = evaluate_message(
        message(scope=ScopeType.PRIVATE, text="*签到"),
        settings(),
        private_policy=EffectivePrivatePolicy(enabled=False),
        direct_triggered=True,
    )

    assert enabled.should_respond and enabled.reason == "group_triggered"
    assert not disabled_group.should_respond and disabled_group.reason == "group_disabled"
    assert not disabled_private.should_respond
    assert disabled_private.reason == "private_not_allowed"


def test_prefix_and_commands_trigger_group() -> None:
    policy = EffectiveGroupPolicy(enabled=True)
    command = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="/ai status"),
        settings(),
        group_policy=policy,
    )
    prefixed = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="!ai hello"),
        settings(),
        group_policy=policy,
    )
    assert command.command is CommandName.STATUS
    assert prefixed.content == "hello"


def test_superuser_access_command_bypasses_disabled_group() -> None:
    decision = evaluate_message(
        message(
            user_id="9000",
            scope=ScopeType.GROUP,
            group_id="2999",
            text="/ai group 12345678 on",
        ),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=False),
    )
    assert decision.should_respond
    assert decision.command is CommandName.GROUP


def test_self_and_known_bot_messages_are_rejected() -> None:
    bot_message = message(is_bot=True)
    self_message = InboundMessage(
        message_id="2",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001"),
        text="hello",
        is_self_message=True,
    )
    assert not evaluate_message(bot_message, settings()).should_respond
    assert not evaluate_message(self_message, settings()).should_respond


def test_require_mention_false_opens_plain_group_chat() -> None:
    opened = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="ordinary chat"),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=True, require_mention=False),
    )
    mentioned = evaluate_message(
        message(
            scope=ScopeType.GROUP,
            group_id="2001",
            mentions_bot=True,
            text="question",
        ),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=True, require_mention=False),
    )
    disabled = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="ordinary chat"),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=False, require_mention=False),
    )
    self_msg = evaluate_message(
        message(
            scope=ScopeType.GROUP,
            group_id="2001",
            text="ordinary chat",
            is_self_message=True,
        ),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=True, require_mention=False),
    )
    bot_msg = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="ordinary chat", is_bot=True),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=True, require_mention=False),
    )
    required = evaluate_message(
        message(scope=ScopeType.GROUP, group_id="2001", text="ordinary chat"),
        settings(),
        group_policy=EffectiveGroupPolicy(enabled=True, require_mention=True),
    )
    assert opened.should_respond and opened.reason == "group_open"
    assert mentioned.should_respond and mentioned.reason == "group_triggered"
    assert not disabled.should_respond and disabled.reason == "group_disabled"
    assert not self_msg.should_respond and self_msg.reason == "bot_message"
    assert not bot_msg.should_respond and bot_msg.reason == "bot_message"
    assert not required.should_respond and required.reason == "group_not_triggered"


def test_canonical_reply_verdict_beats_spoofed_reply_sender() -> None:
    yuki = message(
        scope=ScopeType.GROUP,
        group_id="2001",
        text="接话",
        canonical_reply_to_yuki=True,
        canonical_reply_author_kind="yuki",
    )
    spoofed = message(
        scope=ScopeType.GROUP,
        group_id="2001",
        text="接话",
        reply_sender_user_id="8000",
        canonical_reply_to_yuki=False,
        canonical_reply_author_kind="external_bot",
    )
    missing = message(
        scope=ScopeType.GROUP,
        group_id="2001",
        text="接话",
        reply_sender_user_id="8001",
    )
    policy = EffectiveGroupPolicy(enabled=True)
    yuki_decision = evaluate_message(yuki, settings(), group_policy=policy)
    spoofed_decision = evaluate_message(
        spoofed,
        settings(),
        group_policy=policy,
        yuki_account_ids=frozenset({"8000", "8001"}),
    )
    fallback = evaluate_message(
        missing,
        settings(),
        group_policy=policy,
        yuki_account_ids=frozenset({"8000", "8001"}),
    )
    assert yuki_decision.should_respond and yuki_decision.reason == "group_reply_to_bot"
    assert not spoofed_decision.should_respond
    assert fallback.should_respond and fallback.reason == "group_reply_to_bot"
    assert replies_to_bot(yuki) is True
    assert replies_to_bot(spoofed, yuki_account_ids=frozenset({"8000"})) is False


def test_conversation_keys_are_isolated() -> None:
    assert ConversationScope.private("bot-a", "1").key == "bot:bot-a:private:1"
    assert ConversationScope.private("bot-a", "1") != ConversationScope.private("bot-a", "2")
    assert ConversationScope.group("bot-a", "9").key == "bot:bot-a:group:9"
    assert ConversationScope.group("bot-a", "9") == ConversationScope.group("bot-a", "9")
    assert ConversationScope.group("bot-a", "9") != ConversationScope.group("bot-b", "9")
