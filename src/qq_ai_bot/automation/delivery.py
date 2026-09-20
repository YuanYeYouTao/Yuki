"""Scheduled transport delivery using the main Agent's media and layout services."""

from __future__ import annotations

import json
from typing import Any

from qq_ai_bot.automation.gateway import ProactiveGateway
from qq_ai_bot.automation.registry import CapabilityExecutionContext
from qq_ai_bot.conversation.delivery import ReplySequenceSpec
from qq_ai_bot.domain.messages import AttachmentKind, OutboundMessage
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.emoji.models import EmojiPlacement, EmojiReplyMode, PendingReplyEffect
from qq_ai_bot.speech.models import VoiceMode


async def deliver_reply(
    arguments: dict[str, Any],
    context: CapabilityExecutionContext,
    gateway: ProactiveGateway,
    *,
    chat: Any,
) -> int:
    text = str(arguments["text"])
    group_id = str(arguments["group_id"]) if arguments.get("group_id") else None
    user_id = str(arguments["user_id"]) if arguments.get("user_id") else None
    state = arguments.get("reply_state") or {}
    if not isinstance(state, dict):
        raise ValueError("unresolved_reply_state")
    actor = ToolActor(
        user_id=context.creator_user_id,
        bot_user_id=context.bot_user_id,
        group_id=group_id,
        origin=context.authority.origin,
        instruction="",
        person_id=context.canonical_creator_person_id,
        conversation_id=context.canonical_conversation_id,
        execution_id=f"automation:{context.automation_run_id}:{context.step_id}",
    )
    before: list[OutboundMessage] = []
    after: list[OutboundMessage] = []
    suppress = False
    config = (
        await chat._runtime_config.snapshot(user_id=actor.user_id, group_id=group_id)
        if chat
        else None
    )
    for raw in state.get("effects", []):
        if raw["kind"] == "voice":
            if chat is None or chat._speech_effects is None:
                raise RuntimeError("speech_unavailable")
            voice = await chat._speech_effects.prepare(
                actor=actor,
                response_text=text,
                runtime=config,
                token=None,
                conversation_key=context.conversation_key,
                mode=VoiceMode(raw["mode"]),
                style_hint=raw.get("style_hint", ""),
                language_hint=raw.get("language_hint", "auto"),
                profile_id=raw.get("profile_id", ""),
            )
            if voice is None:
                raise RuntimeError("speech_generation_failed")
            after.append(voice.message)
            suppress = suppress or voice.suppress_text
        else:
            if chat is None or chat._emoji_effects is None:
                raise RuntimeError("emoji_unavailable")
            effect = PendingReplyEffect.model_validate_json(json.dumps(raw))
            prepared = await chat._emoji_effects.prepare(
                effect, actor=actor, response_text=text, runtime=config
            )
            if prepared.message is None:
                raise RuntimeError(prepared.reason_code or "emoji_generation_failed")
            (before if effect.placement is EmojiPlacement.BEFORE_TEXT else after).append(
                prepared.message
            )
            suppress = (
                suppress
                or effect.mode is EmojiReplyMode.EMOJI_ONLY
                or effect.placement is EmojiPlacement.ONLY
            )

    chunks = (
        ()
        if suppress
        else (
            chat._reply_sequence.render(
                text,
                spec=ReplySequenceSpec(
                    **(state.get("layout") or {"max_messages": config.reply.hard_max_messages})
                ),
                runtime=config,
            )
            if config is not None and state
            else ((text,) if text.strip() else ())
        )
    )
    quote = None
    target = state.get("reply_target") or {}
    if target.get("event_id") is not None and chat is not None:
        resolution = await chat._reply_target_resolver.resolve(target["event_id"], actor=actor)
        quote = resolution.platform_message_id
    count = 0
    for message in (*before, *(OutboundMessage(text=chunk) for chunk in chunks), *after):
        if context.revalidate_authority is not None:
            await context.revalidate_authority(None)
        if message.text:
            if group_id:
                await gateway.send_group(
                    group_id, message.text, **({"reply_to_message_id": quote} if quote else {})
                )
            else:
                await gateway.send_private(
                    user_id or context.creator_user_id,
                    message.text,
                    **({"reply_to_message_id": quote} if quote else {}),
                )
            count += 1
            quote = None
        for media in message.media:
            if media.kind is AttachmentKind.AUDIO:
                await gateway.send_voice(
                    user_id=user_id,
                    group_id=group_id,
                    local_path=media.local_path or "",
                    spoken_text=media.spoken_text,
                    generation_id=media.generation_id or 0,
                    profile_id=media.voice_profile_id or "",
                    reference_key=media.voice_reference_key or "",
                    duration_milliseconds=media.duration_milliseconds or 0,
                )
                await chat._speech_effects.record_success(message)
            else:
                await gateway.send_emoji(
                    user_id=user_id,
                    group_id=group_id,
                    content=media.content,
                    mime_type=media.mime_type,
                    emoji_id=media.emoji_id or "",
                    summary=media.summary,
                )
                await chat._emoji_effects.record_send_accepted(message, source="reply_effect")
            count += 1
    return count
