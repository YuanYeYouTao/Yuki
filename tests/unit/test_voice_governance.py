from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventRecord, PeopleRepository
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.speech.models import (
    VoiceIntent,
    VoicePreferenceChange,
    VoicePreferenceDuration,
    VoicePreferenceMode,
    VoiceReplyPlan,
)
from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository
from qq_ai_bot.speech.preference_service import VoicePreferenceService
from qq_ai_bot.speech.reply_effect import VoiceReplyEffectService


def test_voice_name_mapping_does_not_change_the_text_reply() -> None:
    effects = VoiceReplyEffectService(
        MagicMock(),
        bot_display_name="Mika",
        bot_voice_name="みか",
    )
    response_text = "我是Mika，今天也在。"

    assert effects.spoken_text(response_text) == "我是みか，今天也在。"
    assert response_text == "我是Mika，今天也在。"


@pytest.mark.asyncio
async def test_persistent_voice_preference_is_person_scoped_and_cascades(
    database: Database,
) -> None:
    people = PeopleRepository(database)
    repository = VoicePreferenceRepository(database)
    service = VoicePreferenceService(repository)
    await people.observe(user_id="1001", nickname="测试用户")

    turn_only = VoiceReplyPlan(
        intent=VoiceIntent.EXPLICIT_OPT_OUT,
        preference_change=VoicePreferenceChange(
            mode=VoicePreferenceMode.TEXT_ONLY,
            duration=VoicePreferenceDuration.TURN,
        ),
    )
    assert (
        await service.apply(
            turn_only,
            user_id="1001",
            source_message_id="turn-only",
            origin=TurnOrigin.USER_MESSAGE,
        )
        is None
    )
    assert await repository.get("1001") is None

    persistent = turn_only.model_copy(
        update={
            "preference_change": VoicePreferenceChange(
                mode=VoicePreferenceMode.TEXT_ONLY,
                duration=VoicePreferenceDuration.PERSISTENT,
            )
        }
    )
    saved = await service.apply(
        persistent,
        user_id="1001",
        source_message_id="persistent",
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert saved is not None
    assert saved.mode is VoicePreferenceMode.TEXT_ONLY
    assert saved.source_message_id == "persistent"

    await people.delete_person("1001")
    assert await repository.get("1001") is None


@pytest.mark.asyncio
async def test_autonomous_turn_can_write_voice_preference(database: Database) -> None:
    people = PeopleRepository(database)
    repository = VoicePreferenceRepository(database)
    service = VoicePreferenceService(repository)
    await people.observe(user_id="1001", nickname="测试用户")

    saved = await service.set_persistent(
        user_id="1001",
        mode=VoicePreferenceMode.PREFER_VOICE,
        source_message_id="auto-pref",
        origin=TurnOrigin.AUTONOMOUS_GROUP,
    )
    assert saved is not None
    assert saved.mode is VoicePreferenceMode.PREFER_VOICE

    denied = await service.set_persistent(
        user_id="1001",
        mode=VoicePreferenceMode.TEXT_ONLY,
        source_message_id="plugin-pref",
        origin=TurnOrigin.PLUGIN_BACKGROUND,
    )
    assert denied is None
    assert (await repository.get("1001")).mode is VoicePreferenceMode.PREFER_VOICE


_V2_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_V2_CUTOVER = "550e8400-e29b-41d4-a716-4466554400ab"


async def _flip_complete_v2(database: Database) -> None:
    from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel

    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _V2_CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _V2_NOW


async def _two_bindings_one_person(database: Database, first: str = "1001", second: str = "1002"):
    from uuid import uuid4

    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.dual_write import _create_person_binding
    from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM

    async with database.sessions() as session, session.begin():
        created = await _create_person_binding(
            session, external_id=first, display_name="", now=_V2_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id=second,
                display_name="",
                status="active",
                revision=1,
                created_at=_V2_NOW,
                updated_at=_V2_NOW,
            )
        )
        return created.person_id


@pytest.mark.asyncio
async def test_v2_voice_preference_is_shared_across_bindings_without_people(
    database: Database,
) -> None:
    from sqlalchemy import func, select

    from qq_ai_bot.identity.canonical_projections import canonical_person_storage_key
    from qq_ai_bot.persistence.models import PersonModel
    from qq_ai_bot.speech.db_models import PersonSpeechPreferenceModel

    await _flip_complete_v2(database)
    person_id = await _two_bindings_one_person(database)
    repository = VoicePreferenceRepository(database)
    service = VoicePreferenceService(repository)

    saved = await service.set_persistent(
        user_id="1001",
        mode=VoicePreferenceMode.TEXT_ONLY,
        source_message_id="from-a",
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert saved is not None
    assert saved.user_id == "1001"
    assert await service.current_mode("1002") is VoicePreferenceMode.TEXT_ONLY

    updated = await service.set_persistent(
        user_id="1002",
        mode=VoicePreferenceMode.PREFER_VOICE,
        source_message_id="from-b",
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert updated is not None
    assert await service.current_mode("1001") is VoicePreferenceMode.PREFER_VOICE
    assert await repository.delete("1001") is True
    assert await service.current_mode("1002") is None

    async with database.sessions() as session:
        rows = list(await session.scalars(select(PersonSpeechPreferenceModel)))
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
    assert people == 0
    assert rows == []
    saved_again = await service.set_persistent(
        user_id="1002",
        mode=VoicePreferenceMode.AUTO,
        source_message_id="again",
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert saved_again is not None
    async with database.sessions() as session:
        rows = list(await session.scalars(select(PersonSpeechPreferenceModel)))
    assert len(rows) == 1
    assert rows[0].user_id == canonical_person_storage_key(person_id)
    assert rows[0].canonical_person_id == person_id


@pytest.mark.asyncio
async def test_v2_conflicting_speech_preferences_fail_closed(database: Database) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError
    from qq_ai_bot.speech.db_models import PersonSpeechPreferenceModel

    await _flip_complete_v2(database)
    person_id = await _two_bindings_one_person(database)
    async with database.sessions() as session, session.begin():
        session.add(
            PersonSpeechPreferenceModel(
                user_id="1001",
                mode=VoicePreferenceMode.TEXT_ONLY.value,
                source_message_id="a",
                created_at=_V2_NOW,
                updated_at=_V2_NOW,
                canonical_person_id=person_id,
            )
        )
        session.add(
            PersonSpeechPreferenceModel(
                user_id="1002",
                mode=VoicePreferenceMode.PREFER_VOICE.value,
                source_message_id="b",
                created_at=_V2_NOW,
                updated_at=_V2_NOW,
                canonical_person_id=person_id,
            )
        )
    with pytest.raises(IdentityDualWriteError) as exc:
        await VoicePreferenceRepository(database).get("1002")
    assert exc.value.category == "canonical_owner_mismatch"
    assert "1002" not in str(exc.value)


@pytest.mark.asyncio
async def test_v2_missing_binding_fail_closed_for_speech(database: Database) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    await _flip_complete_v2(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await VoicePreferenceRepository(database).get("1001")
    assert exc.value.category == "unclassified"
    assert "1001" not in str(exc.value)


@pytest.mark.asyncio
async def test_v2_disabled_person_qq_speech_fails_closed_and_admin_can_reenable(
    database: Database,
) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError
    from qq_ai_bot.persistence.repositories import PeopleRepository

    await _flip_complete_v2(database)
    await _two_bindings_one_person(database)
    people = PeopleRepository(database)
    repository = VoicePreferenceRepository(database)
    service = VoicePreferenceService(repository)
    saved = await service.set_persistent(
        user_id="1001",
        mode=VoicePreferenceMode.TEXT_ONLY,
        source_message_id="before-disable",
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert saved is not None
    assert await service.current_mode("1001") is VoicePreferenceMode.TEXT_ONLY

    disabled = await people.set_enabled("1001", False)
    assert disabled.enabled is False
    with pytest.raises(IdentityDualWriteError) as read_exc:
        await service.current_mode("1001")
    assert read_exc.value.category == "canonical_owner_disabled"
    assert "1001" not in str(read_exc.value)
    with pytest.raises(IdentityDualWriteError) as write_exc:
        await repository.set(
            "1001",
            VoicePreferenceMode.PREFER_VOICE,
            source_message_id="while-disabled",
        )
    assert write_exc.value.category == "canonical_owner_disabled"
    assert "1001" not in str(write_exc.value)
    with pytest.raises(IdentityDualWriteError) as sibling_exc:
        await service.current_mode("1002")
    assert sibling_exc.value.category == "canonical_owner_disabled"
    assert "1002" not in str(sibling_exc.value)

    restored = await people.set_enabled("1001", True)
    assert restored.enabled is True
    assert await service.current_mode("1001") is VoicePreferenceMode.TEXT_ONLY
    updated = await repository.set(
        "1002",
        VoicePreferenceMode.PREFER_VOICE,
        source_message_id="after-enable",
    )
    assert updated.mode is VoicePreferenceMode.PREFER_VOICE
    assert await service.current_mode("1001") is VoicePreferenceMode.PREFER_VOICE


def test_voice_ledger_separates_spoken_text_from_internal_metadata() -> None:
    technical_summary = "Yuki 发送了一条语音，声线：roxy，风格：happy，语言：jp"
    media_only = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.AUDIO,
                summary=technical_summary,
                voice_profile_id="roxy",
                voice_reference_key="happy",
                voice_language="jp",
            ),
        )
    )
    spoken = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.AUDIO,
                summary="语音消息",
                spoken_text="ゆきだよ。",
                voice_profile_id="roxy",
                voice_reference_key="happy",
                voice_language="jp",
            ),
        )
    )
    emoji = OutboundMessage(
        media=(
            OutboundMedia(
                kind=AttachmentKind.IMAGE,
                summary="一个开心的表情",
                emoji_id="emoji-1",
            ),
        )
    )

    assert ChatService._ledger_content(media_only) == ""
    assert ChatService._ledger_content(spoken) == "ゆきだよ。"
    assert ChatService._ledger_content(emoji) == ""
    segment = ChatService._ledger_media_segment(spoken.media[0])
    assert segment["data"]["target_language"] == "jp"  # type: ignore[index]
    assert technical_summary not in ChatService._ledger_content(media_only)


def test_legacy_voice_metadata_is_hidden_from_model_history() -> None:
    now = datetime.now(UTC)
    legacy = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="legacy-voice",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="[语音：Yuki 发送了一条语音，声线：roxy，风格：happy，语言：jp]",
        visual_summary="",
        segments=(
            {
                "type": "text",
                "data": {"text": "[语音：Yuki 发送了一条语音，声线：roxy，风格：happy，语言：jp]"},
            },
        ),
        occurred_at=now,
        private_peer_user_id="1001",
    )

    assert ChatEventPromptRenderer.event_content(legacy, "current", "当前消息") == ""


def test_model_history_omits_transport_annotations_and_media_only_events() -> None:
    now = datetime.now(UTC)
    image = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="image",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="[表情：一个开心的表情]",
        visual_summary="",
        segments=({"type": "image", "data": {"summary": "一个开心的表情"}},),
        occurred_at=now,
        private_peer_user_id="1001",
    )
    contaminated_text = replace(
        image,
        id=2,
        platform_message_id="text",
        content="[21:10] 我会正常说话。",
        segments=({"type": "text", "data": {"text": "[21:10] 我会正常说话。"}},),
    )
    copied_media_description = replace(
        image,
        id=3,
        platform_message_id="copied-placeholder",
        content="[21:10] [表情：不应作为台词]",
        segments=({"type": "text", "data": {"text": "[21:10] [表情：不应作为台词]"}},),
    )
    leaked_identity = replace(
        contaminated_text,
        id=4,
        platform_message_id="leaked-identity",
        content=("[发送者:Yuki|QQ:8000|消息:old-output|时间:2026-08-05T15:39:05.884399] 看到了。"),
    )

    renderer = ChatEventPromptRenderer((image, contaminated_text))
    assert renderer.event_content(image, "current", "当前消息") == ""
    rendered = renderer.render_event(contaminated_text)
    assert rendered == "[发送者:Yuki|QQ:8000|消息:text] 我会正常说话。"
    assert (
        renderer.event_content(
            leaked_identity,
            "current",
            "当前消息",
        )
        == "看到了。"
    )
    assert (
        renderer.event_content(
            copied_media_description,
            "current",
            "当前消息",
        )
        == ""
    )
