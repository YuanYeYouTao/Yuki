from uuid import uuid4

import pytest
from tests.support.social_identity_cases import social_env

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.correlation import require_live_conversation, try_live_conversation
from yuki.identity.errors import CanonicalIdentityError


async def test_conversation_resolution_keeps_the_actual_source_row(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        source = await require_live_conversation(reader, env.context.conversation_id)
        assert source is await reader.get(CanonicalConversationModel, source.id)
        assert source.space_id == env.space
        assert await try_live_conversation(reader, None) is None
        assert await try_live_conversation(reader, " ") is None
        assert await try_live_conversation(reader, str(uuid4())) is None


async def test_wrong_identity_kinds_cannot_be_used_as_conversations(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        for identity in (env.person, env.presence, env.space):
            with pytest.raises(CanonicalIdentityError) as error:
                await require_live_conversation(reader, identity)
            assert error.value.category == "canonical_kind_mismatch"
        with pytest.raises(CanonicalIdentityError) as error:
            await require_live_conversation(reader, str(uuid4()))
        assert error.value.category == "missing_canonical_conversation"
