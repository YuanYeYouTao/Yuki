"""Test transport wiring for the real canonical resolver and receipt writer."""

from dataclasses import replace
from types import SimpleNamespace

from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
from qq_ai_bot.identity.routing import PresenceRouter
from tests.support.gateway import napcat_registry


async def _member(*args, **kwargs):
    return True


class FixtureIngress(CanonicalIngressResolver):
    def __init__(self, database):
        registry = napcat_registry()
        router = PresenceRouter(database, registry, membership_probe=_member)
        super().__init__(database, registry, router)
        self.uow = CanonicalIngressUnitOfWork(database, router)
        self.handles = {}

    async def pre_admit(self, bot, message):
        account = message.bot_user_id or str(getattr(bot, "self_id", "8000"))
        if account not in self.handles:
            handle = (
                bot
                if bot is not None and str(getattr(bot, "self_id", "")) == account
                else SimpleNamespace(self_id=account)
            )
            self._registry.connect(handle)
            self.handles[account] = handle
        return await super().pre_admit(self.handles[account], replace(message, bot_user_id=account))


def fixture_ingress(database):
    value = getattr(database, "_fixture_canonical_ingress", None)
    if value is None:
        value = FixtureIngress(database)
        database._fixture_canonical_ingress = value
    return value


async def append_inbound(writer, message, *, bot_user_id=None):
    resolver = fixture_ingress(writer._database)
    if bot_user_id is not None:
        message = replace(message, bot_user_id=bot_user_id)
    admitted = await resolver.pre_admit(None, message)
    result = await resolver.uow.append_inbound(admitted.message, admitted)
    return (result.event, result.created) if bot_user_id is not None else result


async def append_new_generation(writer, *, scope, inbound):
    resolver = fixture_ingress(writer._database)
    admitted = await resolver.pre_admit(None, inbound)
    return await resolver.uow.append_new_generation(admitted.message, admitted)
