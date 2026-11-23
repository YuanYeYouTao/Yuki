"""Pure permission, trigger, and command parsing policies."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from yuki.config import Settings
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage


class CommandName(StrEnum):
    """Supported `/ai` commands."""

    HELP = "help"
    NEW = "new"
    COMPACT = "compact"
    STATUS = "status"
    STOP = "stop"
    PING = "ping"
    WHOAMI = "whoami"
    FORGETME = "forgetme"
    MEMORY = "memory"
    PREFERENCE = "preference"
    CAPABILITIES = "capabilities"
    AUTOMATION = "automation"
    ADMIN = "admin"
    PLUGIN = "plugin"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Result of deciding whether and how to handle a message."""

    should_respond: bool
    content: str = ""
    command: CommandName | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EffectiveGroupPolicy:
    """Effective group state after database overrides."""

    enabled: bool
    require_mention: bool = True
    autonomous_enabled: bool = True


@dataclass(frozen=True, slots=True)
class EffectivePrivatePolicy:
    """Effective private-chat state after a database override."""

    enabled: bool


def parse_command(text: str, ai_prefix: str) -> tuple[CommandName | None, str, bool]:
    stripped = text.strip()
    lower = stripped.casefold()
    triggered = False
    remainder = stripped
    if lower == "/ai" or lower.startswith("/ai "):
        triggered = True
        remainder = stripped[3:].strip()
        if not remainder:
            return CommandName.HELP, "", True
        command_parts = remainder.split(maxsplit=1)
        first = command_parts[0].casefold()
        argument = command_parts[1].strip() if len(command_parts) > 1 else ""
        try:
            return CommandName(first), argument, True
        except ValueError:
            return None, remainder, True
    if ai_prefix and (stripped == ai_prefix or stripped.startswith(f"{ai_prefix} ")):
        triggered = True
        remainder = stripped[len(ai_prefix) :].strip()
    return None, remainder, triggered


def message_command_text(message: InboundMessage, ai_prefix: str = "") -> str:
    """Project command text after native Bot mentions without rewriting the source."""

    cursor = 0
    yuki_ids = message.yuki_account_ids | ({message.bot_user_id} if message.bot_user_id else set())
    for mention in message.mentions:
        if message.text[cursor : mention.text_start].strip():
            break
        if mention.kind != "yuki" and mention.target_user_id not in yuki_ids:
            break
        cursor = mention.text_end
    if cursor:
        remainder = message.text[cursor:]
        if parse_command(remainder, ai_prefix)[2]:
            return remainder
    return message.text


def parse_message_command(
    message: InboundMessage, ai_prefix: str
) -> tuple[CommandName | None, str, bool]:
    """Read a command after leading native Bot mentions, retaining the original message."""

    return parse_command(message_command_text(message, ai_prefix), ai_prefix)


def replies_to_bot(
    message: InboundMessage,
    *,
    yuki_account_ids: frozenset[str] = frozenset(),
) -> bool:
    """Return whether this inbound message is a platform reply to Yuki."""

    return message.replies_to_yuki(yuki_account_ids=yuki_account_ids)


def _latin(character: str) -> bool:
    return character.isascii() and character.isalnum()


def calls_by_name(message: InboundMessage, names: tuple[str, ...]) -> bool:
    """Whether the current text calls one of the role's names outside native mentions.

    A Latin name edge may not touch another ASCII letter or digit; CJK names have no
    word separators. A native mention's text is its target's display name, not a call.
    """

    text = list(message.text)
    for mention in message.mentions:
        text[mention.text_start : mention.text_end] = " " * (mention.text_end - mention.text_start)
    folded = "".join(text).casefold()
    for name in names:
        name = name.strip().casefold()
        if name and re.search(
            ("(?<![0-9a-z])" if _latin(name[0]) else "")
            + re.escape(name)
            + ("(?![0-9a-z])" if _latin(name[-1]) else ""),
            folded,
        ):
            return True
    return False


def evaluate_message(
    message: InboundMessage,
    settings: Settings,
    *,
    group_policy: EffectiveGroupPolicy | None = None,
    private_policy: EffectivePrivatePolicy | None = None,
    direct_triggered: bool = False,
    commands_enabled: bool = True,
    yuki_account_ids: frozenset[str] = frozenset(),
    actor_is_superuser: bool = False,
) -> PolicyDecision:
    """Apply self/bot, allowlist, group, mention, reply-to-bot, name, prefix and command rules."""

    if message.is_self_message or message.sender.is_bot:
        return PolicyDecision(False, reason="bot_message")

    command, content, prefix_triggered = (
        parse_message_command(message, settings.ai_prefix)
        if commands_enabled
        else (None, message.text, False)
    )

    if message.scope_type is ScopeType.PRIVATE:
        private_policy_effective = private_policy or EffectivePrivatePolicy(True)
        if not actor_is_superuser and not private_policy_effective.enabled:
            return PolicyDecision(False, reason="private_not_allowed")
        return PolicyDecision(True, content=content, command=command, reason="private_allowed")

    if message.group_id is None:
        return PolicyDecision(False, reason="missing_group_id")
    group_policy_effective = group_policy or EffectiveGroupPolicy(
        message.group_id in settings.enabled_groups
    )

    if not group_policy_effective.enabled:
        return PolicyDecision(False, reason="group_disabled")
    if message.mentions_bot or prefix_triggered or direct_triggered:
        reason = "group_triggered"
    elif replies_to_bot(message, yuki_account_ids=yuki_account_ids):
        reason = "group_reply_to_bot"
    elif calls_by_name(message, settings.character_identity.aliases):
        reason = "group_name_call"
    else:
        reason = ""
    if reason:
        return PolicyDecision(
            True,
            content=content,
            command=command,
            reason=reason,
        )
    if not group_policy_effective.require_mention:
        return PolicyDecision(
            True,
            content=content,
            command=command,
            reason="group_open",
        )
    return PolicyDecision(False, reason="group_not_triggered")
