"""New history windows and media downloads retain original canonical boundaries."""

from tests.unit.test_canonical_ingress import _Bot, _stack

from qq_ai_bot.identity.canonical_repository import ensure_presence


async def ingress(database):
    registry, resolver, uow = await _stack(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    return resolver, uow, bot
