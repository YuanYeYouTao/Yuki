"""complete-v2 Person forget must delete Person-owned C24 conversation children."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from tests.unit.test_complete_v2_memory_evidence import _add_conversation, _seed_person
from tests.unit.test_user_profiles import _flip_complete_v2

from qq_ai_bot.conversation.cadence import conversation_key_hash, source_event_hash
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.db_models import ReplyEffectEventModel
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig,
    ensure_v2_space,
    set_identity_failpoint,
)
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    PersonModel,
    RuntimeTurnObservationModel,
    ToolInvocationModel,
    WebSearchRunModel,
    WebSearchSourceModel,
)
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.runtime.observability import hash_conversation_key
from qq_ai_bot.speech.db_models import (
    SpeechGenerationModel,
    SpeechVoiceProfileModel,
    SpeechVoiceReferenceModel,
)

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_PRIMARY = "bot:8000:private:1001"
_SECONDARY = "bot:9000:private:1001"
_RAW = "private:1001"
_Q_PRIMARY = "bot:8000:private:1003"
_SPACE_KEY = "bot:8000:group:2001"


async def _assert_fk_clean(session) -> None:
    rows = (await session.execute(text("PRAGMA foreign_key_check"))).all()
    assert rows == []


async def _seed_voice(session) -> tuple[str, int]:
    existing = await session.get(SpeechVoiceProfileModel, "c24-forget-voice")
    if existing is not None:
        reference = await session.scalar(
            select(SpeechVoiceReferenceModel).where(
                SpeechVoiceReferenceModel.profile_id == "c24-forget-voice"
            )
        )
        assert reference is not None
        return "c24-forget-voice", int(reference.id)
    session.add(
        SpeechVoiceProfileModel(
            profile_id="c24-forget-voice",
            display_name="C24 forget",
            provider="genie",
            engine_model_version="v2",
            language="zh",
            supported_languages_json='["zh"]',
            model_relative_path="voices/c24/model",
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
        profile_id="c24-forget-voice",
        reference_key="neutral",
        style="neutral",
        aliases_json="[]",
        audio_relative_path="voices/c24/neutral.wav",
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
    return "c24-forget-voice", int(reference.id)


def _observation(
    *,
    turn_id: str,
    scope_type: str,
    conversation_key: str,
    conversation_id: str | None,
    person_id: str | None,
    space_id: str | None = None,
) -> RuntimeTurnObservationModel:
    return RuntimeTurnObservationModel(
        runtime_turn_id=turn_id,
        origin="user_message",
        scope_type=scope_type,
        conversation_key_hash=hash_conversation_key(conversation_key),
        admission_outcome="admitted",
        handled=True,
        sent_messages=1,
        total_latency_ms=10,
        created_at=_NOW,
        expires_at=_NOW,
        canonical_conversation_id=conversation_id,
        canonical_person_id=person_id,
        canonical_space_id=space_id,
    )


def _cadence(
    *,
    conversation_key: str,
    raw: str,
    conversation_id: str | None,
) -> ReplyEffectEventModel:
    return ReplyEffectEventModel(
        conversation_key_hash=conversation_key_hash(conversation_key),
        source_event_hash=source_event_hash(source="runtime", raw=raw),
        text_sent=True,
        voice_sent=False,
        emoji_sent=False,
        voice_cadence_eligible=True,
        voice_request_basis="none",
        source="runtime",
        occurred_at=_NOW,
        recorded_at=_NOW,
        canonical_conversation_id=conversation_id,
    )


def _tool(
    *,
    conversation_key: str,
    conversation_id: str | None,
) -> ToolInvocationModel:
    return ToolInvocationModel(
        conversation_key_hash=hash_conversation_key(conversation_key),
        provider_id="test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        created_at=_NOW,
        canonical_conversation_id=conversation_id,
    )


def _web(
    *,
    conversation_key: str,
    trigger: str,
    conversation_id: str | None,
    query: str,
) -> WebSearchRunModel:
    return WebSearchRunModel(
        conversation_key=conversation_key,
        trigger_message_id=trigger,
        query=query,
        provider="tavily",
        created_at=_NOW,
        canonical_conversation_id=conversation_id,
    )


def _model(*, conversation_id: str | None) -> ModelInvocationModel:
    return ModelInvocationModel(
        task="chat_agent",
        profile_id="main",
        provider="fake",
        model="fake",
        success=True,
        latency_seconds=0.1,
        created_at=_NOW,
        canonical_conversation_id=conversation_id,
    )


def _speech(
    *,
    request_id: str,
    conversation_key: str,
    conversation_id: str | None,
    profile_id: str,
    reference_id: int,
) -> SpeechGenerationModel:
    return SpeechGenerationModel(
        request_id=request_id,
        conversation_key_hash=hash_conversation_key(conversation_key),
        profile_id=profile_id,
        reference_id=reference_id,
        engine_version="v2",
        target_language="zh",
        text_hash="b" * 64,
        normalized_text_hash="c" * 64,
        character_count=2,
        cache_key=request_id,
        status="succeeded",
        created_at=_NOW,
        canonical_conversation_id=conversation_id,
    )


async def _seed_c24_world(database: Database) -> dict[str, str]:
    await _flip_complete_v2(database)
    private_p = str(uuid4())
    private_q = str(uuid4())
    space_conversation = str(uuid4())
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        person_p = await _seed_person(session, external_id="1001", display_name="P")
        person_q = await _seed_person(session, external_id="1003", display_name="Q")
        space_id = await ensure_v2_space(session, "2001")
        await _add_conversation(
            session,
            conversation_id=private_p,
            kind="private",
            person_id=person_p.person_id,
            scope_key=_PRIMARY,
        )
        await _add_conversation(
            session,
            conversation_id=private_q,
            kind="private",
            person_id=person_q.person_id,
            scope_key=_Q_PRIMARY,
        )
        await _add_conversation(
            session,
            conversation_id=space_conversation,
            kind="space",
            space_id=space_id,
            scope_key=_SPACE_KEY,
        )
        session.add(
            ConversationLegacyAliasModel(
                id=str(uuid4()),
                conversation_id=private_p,
                scope_key=_SECONDARY,
                is_primary=0,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        profile_id, reference_id = await _seed_voice(session)
        session.add(
            _observation(
                turn_id="p-private",
                scope_type="private",
                conversation_key=_PRIMARY,
                conversation_id=private_p,
                person_id=person_p.person_id,
            )
        )
        session.add(
            _observation(
                turn_id="p-legacy-raw",
                scope_type="private",
                conversation_key=_RAW,
                conversation_id=None,
                person_id=person_p.person_id,
            )
        )
        session.add(
            _observation(
                turn_id="p-legacy-secondary",
                scope_type="private",
                conversation_key=_SECONDARY,
                conversation_id=None,
                person_id=None,
            )
        )
        session.add(
            _observation(
                turn_id="q-private",
                scope_type="private",
                conversation_key=_Q_PRIMARY,
                conversation_id=private_q,
                person_id=person_q.person_id,
            )
        )
        session.add(
            _observation(
                turn_id="space-only",
                scope_type="group",
                conversation_key=_SPACE_KEY,
                conversation_id=space_conversation,
                person_id=None,
                space_id=space_id,
            )
        )
        session.add(_cadence(conversation_key=_PRIMARY, raw="p-cadence", conversation_id=private_p))
        session.add(_cadence(conversation_key=_RAW, raw="p-raw-cadence", conversation_id=None))
        session.add(
            _cadence(conversation_key=_Q_PRIMARY, raw="q-cadence", conversation_id=private_q)
        )
        session.add(
            _cadence(
                conversation_key=_SPACE_KEY,
                raw="space-cadence",
                conversation_id=space_conversation,
            )
        )
        session.add(
            _speech(
                request_id="p-speech",
                conversation_key=_PRIMARY,
                conversation_id=private_p,
                profile_id=profile_id,
                reference_id=reference_id,
            )
        )
        session.add(
            _speech(
                request_id="p-raw-speech",
                conversation_key=_RAW,
                conversation_id=None,
                profile_id=profile_id,
                reference_id=reference_id,
            )
        )
        session.add(
            _speech(
                request_id="q-speech",
                conversation_key=_Q_PRIMARY,
                conversation_id=private_q,
                profile_id=profile_id,
                reference_id=reference_id,
            )
        )
        session.add(
            _speech(
                request_id="space-speech",
                conversation_key=_SPACE_KEY,
                conversation_id=space_conversation,
                profile_id=profile_id,
                reference_id=reference_id,
            )
        )
        session.add(_tool(conversation_key=_PRIMARY, conversation_id=private_p))
        session.add(_tool(conversation_key=_SECONDARY, conversation_id=None))
        session.add(_tool(conversation_key=_Q_PRIMARY, conversation_id=private_q))
        session.add(_tool(conversation_key=_SPACE_KEY, conversation_id=space_conversation))
        p_web = _web(
            conversation_key=_PRIMARY,
            trigger="p-web",
            conversation_id=private_p,
            query="p-secret",
        )
        p_raw_web = _web(
            conversation_key=_RAW,
            trigger="p-raw-web",
            conversation_id=None,
            query="p-raw-secret",
        )
        q_web = _web(
            conversation_key=_Q_PRIMARY,
            trigger="q-web",
            conversation_id=private_q,
            query="q-keep",
        )
        space_web = _web(
            conversation_key=_SPACE_KEY,
            trigger="space-web",
            conversation_id=space_conversation,
            query="space-keep",
        )
        session.add(p_web)
        session.add(p_raw_web)
        session.add(q_web)
        session.add(space_web)
        await session.flush()
        session.add(
            WebSearchSourceModel(
                run_id=int(p_web.id),
                ordinal=1,
                title="p-source",
                url="https://example.com/p",
                domain="example.com",
                snippet="p-pii",
                created_at=_NOW,
            )
        )
        session.add(
            WebSearchSourceModel(
                run_id=int(q_web.id),
                ordinal=1,
                title="q-source",
                url="https://example.com/q",
                domain="example.com",
                snippet="q-keep",
                created_at=_NOW,
            )
        )
        session.add(_model(conversation_id=private_p))
        session.add(_model(conversation_id=private_q))
        session.add(_model(conversation_id=space_conversation))
        return {
            "p_id": person_p.person_id,
            "q_id": person_q.person_id,
            "space_id": space_id,
            "private_p": private_p,
            "private_q": private_q,
            "space_conversation": space_conversation,
        }


@pytest.mark.asyncio
async def test_forget_deletes_person_c24_families_keeps_unrelated(database: Database) -> None:
    ids = await _seed_c24_world(database)
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        assert await session.get(CanonicalPersonModel, ids["p_id"]) is None
        assert await session.get(CanonicalPersonModel, ids["q_id"]) is not None
        leftover_private = await session.scalar(
            select(func.count())
            .select_from(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == ids["private_p"])
        )
        assert leftover_private == 0
        leftover_aliases = await session.scalar(
            select(func.count())
            .select_from(ConversationLegacyAliasModel)
            .where(ConversationLegacyAliasModel.conversation_id == ids["private_p"])
        )
        assert leftover_aliases == 0
        q_conversation = await session.get(CanonicalConversationModel, ids["private_q"])
        space_conversation = await session.get(
            CanonicalConversationModel, ids["space_conversation"]
        )
        assert q_conversation is not None
        assert space_conversation is not None
        assert space_conversation.space_id == ids["space_id"]

        observations = list(await session.scalars(select(RuntimeTurnObservationModel)))
        assert {row.runtime_turn_id for row in observations} == {"q-private", "space-only"}
        assert all(row.canonical_person_id != ids["p_id"] for row in observations)
        assert all(row.canonical_conversation_id != ids["private_p"] for row in observations)
        p_hashes = {
            hash_conversation_key(_PRIMARY),
            hash_conversation_key(_SECONDARY),
            hash_conversation_key(_RAW),
        }
        assert all(row.conversation_key_hash not in p_hashes for row in observations)

        cadence = list(await session.scalars(select(ReplyEffectEventModel)))
        assert {row.canonical_conversation_id for row in cadence} == {
            ids["private_q"],
            ids["space_conversation"],
        }
        assert all(
            row.conversation_key_hash
            not in {
                conversation_key_hash(_PRIMARY),
                conversation_key_hash(_RAW),
                conversation_key_hash(_SECONDARY),
            }
            for row in cadence
        )

        speech = list(await session.scalars(select(SpeechGenerationModel)))
        assert {row.request_id for row in speech} == {"q-speech", "space-speech"}

        tools = list(await session.scalars(select(ToolInvocationModel)))
        assert {row.canonical_conversation_id for row in tools} == {
            ids["private_q"],
            ids["space_conversation"],
        }
        assert all(row.conversation_key_hash not in p_hashes for row in tools)

        webs = list(await session.scalars(select(WebSearchRunModel)))
        assert {row.conversation_key for row in webs} == {_Q_PRIMARY, _SPACE_KEY}
        sources = list(await session.scalars(select(WebSearchSourceModel)))
        assert [row.title for row in sources] == ["q-source"]

        models = list(await session.scalars(select(ModelInvocationModel)))
        assert {row.canonical_conversation_id for row in models} == {
            ids["private_q"],
            ids["space_conversation"],
        }
        await _assert_fk_clean(session)


@pytest.mark.asyncio
async def test_forget_c24_failpoint_rolls_back_children_and_person(database: Database) -> None:
    ids = await _seed_c24_world(database)

    def boom(name: str) -> None:
        if name == "after_c24_forget_conversation":
            raise RuntimeError("after_c24_forget_conversation")

    set_identity_failpoint(boom)
    try:
        with pytest.raises(RuntimeError, match="after_c24_forget_conversation"):
            await PeopleRepository(database).delete_person("1001")
    finally:
        set_identity_failpoint(None)

    async with database.sessions() as session:
        assert await session.get(CanonicalPersonModel, ids["p_id"]) is not None
        assert await session.get(CanonicalConversationModel, ids["private_p"]) is not None
        assert (
            await session.scalar(select(func.count()).select_from(RuntimeTurnObservationModel)) == 5
        )
        assert await session.scalar(select(func.count()).select_from(ReplyEffectEventModel)) == 4
        assert await session.scalar(select(func.count()).select_from(SpeechGenerationModel)) == 4
        assert await session.scalar(select(func.count()).select_from(ToolInvocationModel)) == 4
        assert await session.scalar(select(func.count()).select_from(WebSearchRunModel)) == 4
        assert await session.scalar(select(func.count()).select_from(ModelInvocationModel)) == 3
        leftover_p_obs = await session.scalar(
            select(func.count())
            .select_from(RuntimeTurnObservationModel)
            .where(RuntimeTurnObservationModel.canonical_person_id == ids["p_id"])
        )
        assert leftover_p_obs == 2
        await _assert_fk_clean(session)


@pytest.mark.asyncio
async def test_v1_forgetme_does_not_use_c24_conversation_deletes(database: Database) -> None:
    people = PeopleRepository(database)
    await people.observe(user_id="1001", nickname="P")
    await people.observe(user_id="1003", nickname="Q")
    async with database.sessions() as session, session.begin():
        session.add(
            _observation(
                turn_id="q-v1",
                scope_type="private",
                conversation_key="private:1003",
                conversation_id=None,
                person_id=None,
            )
        )
        session.add(_cadence(conversation_key="private:1003", raw="q-v1", conversation_id=None))
        session.add(_tool(conversation_key="private:1003", conversation_id=None))

    def boom(name: str) -> None:
        if name == "after_c24_forget_conversation":
            raise RuntimeError("c24-should-not-run")

    set_identity_failpoint(boom)
    try:
        assert await people.delete_person("1001") is True
    finally:
        set_identity_failpoint(None)
    async with database.sessions() as session:
        assert await session.get(PersonModel, "1001") is None
        assert await session.get(PersonModel, "1003") is not None
        leftover_obs = await session.scalar(
            select(func.count()).select_from(RuntimeTurnObservationModel)
        )
        leftover_cadence = await session.scalar(
            select(func.count()).select_from(ReplyEffectEventModel)
        )
        leftover_tools = await session.scalar(select(func.count()).select_from(ToolInvocationModel))
        assert leftover_obs == 1
        assert leftover_cadence == 1
        assert leftover_tools == 1
