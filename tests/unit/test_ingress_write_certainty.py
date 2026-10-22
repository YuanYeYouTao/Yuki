"""A cold identity commit cannot be rewritten into an ordinary input drop."""

from contextlib import asynccontextmanager

import pytest
from tests.support.canonical_identity import ensure_presence
from tests.unit.test_canonical_ingress import _Bot, _message, _stack

from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.database import write_failure_committed


@pytest.mark.parametrize("committed", [None, True, False])
async def test_cold_owner_disabled_error_keeps_the_actual_writer_phase(
    database, monkeypatch, committed
):
    registry, resolver, _uow = await _stack(database)
    bot = _Bot("8000")
    async with database.immediate_session() as writer:
        presence = await ensure_presence(writer, bot.self_id)
    registry.connect(bot, provider_id="snowluma")
    registry.bind_presence(platform="qq", external_account_id=bot.self_id, presence_id=presence)
    error = CanonicalIdentityError("canonical_owner_disabled")
    if committed is not False:
        error.add_note(
            "database_write_phase:cleanup" if committed else "database_write_phase:commit"
        )
        if committed:
            error.add_note("database_commit:confirmed")
    original_writer = database.immediate_session

    @asynccontextmanager
    async def lost_ack():
        async with original_writer() as writer:
            yield writer
            if committed is False:
                raise error
        raise error

    with monkeypatch.context() as scoped:
        scoped.setattr(database, "immediate_session", lost_ack)
        if committed is False:
            result = await resolver.pre_admit(
                bot, _message(message_id="cold-original", user_id="90111")
            )
            assert result.dropped and result.reason == "private_not_allowed"
            assert write_failure_committed(error) is False
        else:
            with pytest.raises(CanonicalIdentityError) as caught:
                await resolver.pre_admit(bot, _message(message_id="cold-original", user_id="90111"))
            assert caught.value is error and write_failure_committed(error) is committed
    async with database.read_session() as reader:
        binding = await find_identity_binding(reader, "90111")
        assert (binding is None) is (committed is False)
