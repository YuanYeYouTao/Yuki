"""C24b live canonical Conversation correlation for telemetry writers."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from qq_ai_bot.conversation.cadence import ReplyEffectRepository
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.db_models import ReplyEffectEventModel
from qq_ai_bot.identity.c24_conversation import (
    CANONICAL_KIND_MISMATCH,
    load_unique_live_chat_event,
    stamp_conversation_correlation,
)
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_presence_preconfig,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, ToolInvocationModel, WebSearchRunModel
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository
from qq_ai_bot.speech.db_models import (
    SpeechGenerationModel,
    SpeechVoiceProfileModel,
    SpeechVoiceReferenceModel,
)
from qq_ai_bot.speech.repository import SpeechGenerationRepository
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-4466554400c4"


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _add_conversation(
    session,
    *,
    conversation_id: str,
    person_id: str,
    scope_key: str,
) -> None:
    alias_id = str(uuid4())
    session.add(
        CanonicalConversationModel(
            id=conversation_id,
            kind="private",
            person_id=person_id,
            space_id=None,
            primary_alias_id=alias_id,
            primary_marker=1,
            generation=1,
            starts_after_event_id=10_000,
            last_event_id=10_000,
            last_generation_change_event_id=10_000,
            covered_through_event_id=10_000,
            uncovered_event_count=0,
            uncovered_character_count=0,
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.add(
        ConversationLegacyAliasModel(
            id=alias_id,
            conversation_id=conversation_id,
            scope_key=scope_key,
            is_primary=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def _event(
    *,
    message_id: str,
    conversation_id: str,
    bot_user_id: str = "8000",
    ingress_presence_id: str | None = None,
    sender_user_id: str = "1001",
) -> ChatEventModel:
    return ChatEventModel(
        bot_user_id=bot_user_id,
        platform_message_id=message_id,
        scope_type="private",
        private_peer_user_id=sender_user_id,
        sender_user_id=sender_user_id,
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
        canonical_event_id=str(uuid4()),
        canonical_conversation_id=conversation_id,
        ingress_presence_id=ingress_presence_id,
        suppression_status="keeper",
    )


async def _seed_pair(database: Database) -> tuple[str, str, str, str]:
    conversation_a = str(uuid4())
    conversation_b = str(uuid4())
    async with database.sessions() as session, session.begin():
        person_a = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        person_b = await ensure_canonical_person_preconfig(session, "1002", now=_NOW)
        presence_a = await ensure_canonical_presence_preconfig(session, "8000", now=_NOW)
        presence_b = await ensure_canonical_presence_preconfig(session, "8001", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=conversation_a,
            person_id=person_a,
            scope_key="bot:8000:private:1001",
        )
        await _add_conversation(
            session,
            conversation_id=conversation_b,
            person_id=person_b,
            scope_key="bot:8001:private:1002",
        )
    return conversation_a, conversation_b, presence_a, presence_b


async def _seed_voice(database: Database) -> tuple[str, int]:
    async with database.sessions() as session, session.begin():
        existing = await session.get(SpeechVoiceProfileModel, "c24b-voice")
        if existing is not None:
            reference = await session.scalar(
                select(SpeechVoiceReferenceModel).where(
                    SpeechVoiceReferenceModel.profile_id == "c24b-voice"
                )
            )
            assert reference is not None
            return "c24b-voice", int(reference.id)
        session.add(
            SpeechVoiceProfileModel(
                profile_id="c24b-voice",
                display_name="C24b",
                provider="genie",
                engine_model_version="v2",
                language="zh",
                supported_languages_json='["zh"]',
                model_relative_path="voices/c24b/model",
                model_checksum="c" * 64,
                default_style="neutral",
                enabled=True,
                is_default=False,
                source="test",
                source_note="",
                license_note="",
                manifest_hash="d" * 64,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        reference = SpeechVoiceReferenceModel(
            profile_id="c24b-voice",
            reference_key="neutral",
            style="neutral",
            aliases_json="[]",
            audio_relative_path="voices/c24b/neutral.wav",
            audio_checksum="e" * 64,
            transcript="hi",
            language="zh",
            enabled=True,
            priority=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(reference)
        await session.flush()
        return "c24b-voice", int(reference.id)


def _web_response() -> WebSearchResponse:
    return WebSearchResponse(
        query="now",
        sources=(
            WebSearchSource(
                source_id="s1",
                title="Example",
                url="https://example.com/article",
                domain="example.com",
                snippet="ok",
                relevant_content="",
            ),
        ),
        provider_request_id=None,
        latency_seconds=0.1,
    )


async def _create_speech(
    database: Database,
    *,
    request_id: str,
    trigger_event_id: int | None = None,
    canonical_conversation_id: str | None = None,
    conversation_key_hash: str = "a" * 64,
) -> SpeechGenerationModel:
    profile_id, reference_id = await _seed_voice(database)
    await SpeechGenerationRepository(database).create(
        request_id=request_id,
        conversation_key_hash=conversation_key_hash,
        trigger_event_id=trigger_event_id,
        profile_id=profile_id,
        reference_id=reference_id,
        engine_version="v2",
        target_language="zh",
        text_hash="b" * 64,
        normalized_text_hash="c" * 64,
        character_count=2,
        cache_key=request_id,
        expires_at=None,
        canonical_conversation_id=canonical_conversation_id,
    )
    async with database.sessions() as session:
        row = await session.scalar(
            select(SpeechGenerationModel).where(SpeechGenerationModel.request_id == request_id)
        )
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_v1_writers_stay_null_without_trusted_or_unique_event(database: Database) -> None:
    await ModelInvocationRepository(database).record(
        task=ModelTask.CHAT_AGENT,
        profile_id="main",
        provider="fake",
        model="fake",
        success=True,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cached_prompt_tokens=None,
        latency_seconds=0.1,
        error_category=None,
    )
    await ReplyEffectRepository(database).record(
        conversation_key="private:1001",
        source_event_id="v1-src",
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_request_basis="none",
    )
    speech = await _create_speech(database, request_id="v1-speech")
    await MCPRepository(database).record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
    )
    await WebSearchSourceRepository(database).save_response(
        conversation_key="private:1001",
        trigger_message_id="v1-web",
        provider="tavily",
        response=_web_response(),
        max_runs=4,
    )
    async with database.sessions() as session:
        model = (await session.scalars(select(ModelInvocationModel))).one()
        cadence = (await session.scalars(select(ReplyEffectEventModel))).one()
        tool = (await session.scalars(select(ToolInvocationModel))).one()
        web = (await session.scalars(select(WebSearchRunModel))).one()
    assert model.canonical_conversation_id is None
    assert cadence.canonical_conversation_id is None
    assert speech.canonical_conversation_id is None
    assert tool.canonical_conversation_id is None
    assert web.canonical_conversation_id is None


@pytest.mark.asyncio
async def test_complete_v2_trusted_live_stamp_for_each_writer(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, other_id, presence_id, _ = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        session.add(_event(message_id="live-1", conversation_id=conversation_id))
        session.add(
            _event(
                message_id="live-2",
                conversation_id=conversation_id,
                bot_user_id="8000",
                ingress_presence_id=presence_id,
            )
        )
        await session.flush()
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "live-1")
        )
        assert event is not None
        event_id = event.id

    await ModelInvocationRepository(database).record(
        task=ModelTask.CHAT_AGENT,
        profile_id="main",
        provider="fake",
        model="fake",
        success=True,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cached_prompt_tokens=None,
        latency_seconds=0.1,
        error_category=None,
        canonical_conversation_id=conversation_id,
    )
    await ReplyEffectRepository(database).record(
        conversation_key="private:1001",
        source_event_id="live-2",
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_request_basis="none",
        canonical_conversation_id=conversation_id,
    )
    speech = await _create_speech(
        database,
        request_id="v2-speech",
        trigger_event_id=event_id,
    )
    await MCPRepository(database).record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
        trigger_message_id="live-2",
        bot_user_id="8000",
        ingress_presence_id=presence_id,
    )
    await WebSearchSourceRepository(database).save_response(
        conversation_key="private:1001",
        trigger_message_id="live-2",
        provider="tavily",
        response=_web_response(),
        max_runs=4,
        bot_user_id="8000",
        ingress_presence_id=presence_id,
    )
    async with database.sessions() as session:
        model = (await session.scalars(select(ModelInvocationModel))).one()
        cadence = (await session.scalars(select(ReplyEffectEventModel))).one()
        tool = (await session.scalars(select(ToolInvocationModel))).one()
        web = (await session.scalars(select(WebSearchRunModel))).one()
    assert model.canonical_conversation_id == conversation_id
    assert cadence.canonical_conversation_id == conversation_id
    assert speech.canonical_conversation_id == conversation_id
    assert tool.canonical_conversation_id == conversation_id
    assert web.canonical_conversation_id == conversation_id
    assert other_id != conversation_id


@pytest.mark.asyncio
async def test_unique_persisted_event_stamps_without_trusted_kwarg(
    database: Database,
) -> None:
    await _flip_v2(database)
    conversation_id, _, presence_id, _ = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        session.add(
            _event(
                message_id="prod-1",
                conversation_id=conversation_id,
                ingress_presence_id=presence_id,
            )
        )
        await session.flush()
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "prod-1")
        )
        assert event is not None
        event_id = event.id

    await ReplyEffectRepository(database).record(
        conversation_key="private:1001",
        source_event_id="prod-1",
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_request_basis="none",
    )
    speech = await _create_speech(database, request_id="prod-speech", trigger_event_id=event_id)
    await MCPRepository(database).record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
        trigger_message_id="prod-1",
        bot_user_id="8000",
    )
    await WebSearchSourceRepository(database).save_response(
        conversation_key="private:1001",
        trigger_message_id="prod-1",
        provider="tavily",
        response=_web_response(),
        max_runs=4,
    )
    async with database.sessions() as session:
        cadence = (await session.scalars(select(ReplyEffectEventModel))).one()
        tool = (await session.scalars(select(ToolInvocationModel))).one()
        web = (await session.scalars(select(WebSearchRunModel))).one()
    assert cadence.canonical_conversation_id == conversation_id
    assert speech.canonical_conversation_id == conversation_id
    assert tool.canonical_conversation_id == conversation_id
    assert web.canonical_conversation_id == conversation_id


@pytest.mark.asyncio
async def test_presence_collision_cannot_cross_link(database: Database) -> None:
    await _flip_v2(database)
    conversation_a, conversation_b, presence_a, presence_b = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        session.add(
            _event(
                message_id="shared-msg",
                conversation_id=conversation_a,
                bot_user_id="8000",
                ingress_presence_id=presence_a,
            )
        )
        session.add(
            _event(
                message_id="shared-msg",
                conversation_id=conversation_b,
                bot_user_id="8001",
                ingress_presence_id=presence_b,
            )
        )
        session.add(
            _event(
                message_id="same-bot",
                conversation_id=conversation_a,
                bot_user_id="8000",
                ingress_presence_id=presence_a,
            )
        )
        session.add(
            _event(
                message_id="same-bot",
                conversation_id=conversation_b,
                bot_user_id="8000",
                ingress_presence_id=presence_b,
            )
        )

    await ReplyEffectRepository(database).record(
        conversation_key="private:1001",
        source_event_id="shared-msg",
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_request_basis="none",
    )
    await MCPRepository(database).record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
        trigger_message_id="same-bot",
        bot_user_id="8000",
    )
    await WebSearchSourceRepository(database).save_response(
        conversation_key="private:1001",
        trigger_message_id="shared-msg",
        provider="tavily",
        response=_web_response(),
        max_runs=4,
    )
    async with database.sessions() as session:
        chosen = await load_unique_live_chat_event(session, platform_message_id="shared-msg")
        cadence = (await session.scalars(select(ReplyEffectEventModel))).one()
        tool = (await session.scalars(select(ToolInvocationModel))).one()
        web = (await session.scalars(select(WebSearchRunModel))).one()
    assert chosen is None
    assert cadence.canonical_conversation_id is None
    assert tool.canonical_conversation_id is None
    assert web.canonical_conversation_id is None


@pytest.mark.asyncio
async def test_conflicting_existing_shadow_fails_closed(database: Database) -> None:
    await _flip_v2(database)
    conversation_a, conversation_b, _, _ = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        session.add(_event(message_id="conflict-1", conversation_id=conversation_a))
        await session.flush()
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "conflict-1")
        )
        assert event is not None
        event_id = event.id

    with pytest.raises(IdentityDualWriteError, match="identity dual-write failed") as speech_exc:
        await _create_speech(
            database,
            request_id="conflict-speech",
            trigger_event_id=event_id,
            canonical_conversation_id=conversation_b,
        )
    assert speech_exc.value.category == "canonical_owner_mismatch"

    with pytest.raises(IdentityDualWriteError) as cadence_exc:
        await ReplyEffectRepository(database).record(
            conversation_key="private:1001",
            source_event_id="conflict-1",
            text_sent=True,
            voice_sent=False,
            emoji_sent=False,
            voice_request_basis="none",
            canonical_conversation_id=conversation_b,
        )
    assert cadence_exc.value.category == "canonical_owner_mismatch"

    with pytest.raises(IdentityDualWriteError) as web_exc:
        await WebSearchSourceRepository(database).save_response(
            conversation_key="private:1001",
            trigger_message_id="conflict-1",
            provider="tavily",
            response=_web_response(),
            max_runs=4,
            canonical_conversation_id=conversation_b,
        )
    assert web_exc.value.category == "canonical_owner_mismatch"

    with pytest.raises(IdentityDualWriteError) as tool_exc:
        await MCPRepository(database).record_invocation(
            conversation_key="private:1001",
            provider_id="test",
            tool_name="web_search",
            success=True,
            latency_seconds=0.01,
            result_size=1,
            artifact_created=False,
            error_category=None,
            trigger_message_id="conflict-1",
            bot_user_id="8000",
            canonical_conversation_id=conversation_b,
        )
    assert tool_exc.value.category == "canonical_owner_mismatch"

    async with database.sessions() as session, session.begin():
        row = ModelInvocationModel(
            task=ModelTask.CHAT_AGENT.value,
            profile_id="main",
            provider="fake",
            model="fake",
            success=True,
            latency_seconds=0.1,
            created_at=_NOW,
            canonical_conversation_id=conversation_a,
        )
        session.add(row)
        await session.flush()
        with pytest.raises(IdentityDualWriteError) as model_exc:
            await stamp_conversation_correlation(session, row, conversation_b)
        assert model_exc.value.category == "canonical_owner_mismatch"


@pytest.mark.asyncio
async def test_no_hash_or_raw_key_fabrication(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, _, _, _ = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        session.add(_event(message_id="hash-1", conversation_id=conversation_id))

    await ModelInvocationRepository(database).record(
        task=ModelTask.CHAT_AGENT,
        profile_id="main",
        provider="fake",
        model="fake",
        success=True,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cached_prompt_tokens=None,
        latency_seconds=0.1,
        error_category=None,
    )
    await ReplyEffectRepository(database).record(
        conversation_key="private:1001",
        source_event_id="not-a-persisted-event",
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_request_basis="none",
    )
    speech = await _create_speech(
        database,
        request_id="hash-speech",
        conversation_key_hash="f" * 64,
    )
    await MCPRepository(database).record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
        trigger_message_id="hash-1",
    )
    await WebSearchSourceRepository(database).save_response(
        conversation_key="private:1001",
        trigger_message_id="missing-web",
        provider="tavily",
        response=_web_response(),
        max_runs=4,
    )
    async with database.sessions() as session:
        model = (await session.scalars(select(ModelInvocationModel))).one()
        cadence = (await session.scalars(select(ReplyEffectEventModel))).one()
        tool = (await session.scalars(select(ToolInvocationModel))).one()
        web = (await session.scalars(select(WebSearchRunModel))).one()
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        assert conversation is not None
        person_id = conversation.person_id
    assert model.canonical_conversation_id is None
    assert cadence.canonical_conversation_id is None
    assert speech.canonical_conversation_id is None
    assert tool.canonical_conversation_id is None
    assert web.canonical_conversation_id is None

    async with database.sessions() as session, session.begin():
        row = ModelInvocationModel(
            task=ModelTask.CHAT_AGENT.value,
            profile_id="main",
            provider="fake",
            model="fake",
            success=True,
            latency_seconds=0.1,
            created_at=_NOW,
        )
        session.add(row)
        with pytest.raises(IdentityDualWriteError) as exc:
            await stamp_conversation_correlation(session, row, person_id)
        assert exc.value.category == CANONICAL_KIND_MISMATCH
