"""Plugin QQ publication prepares its history text before taking the writer."""

from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import select
from tests.integration.test_person_execution_receipts import invocation
from tests.support.social_identity_cases import social_env

from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence import scoped_event_uow
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki_plugin_sdk.permissions import PluginPermission


async def test_onebot_terms_precede_writer_and_original_receipt_skips_preparation(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        (await session.get(CanonicalPersonModel, event.author_person_id)).is_superuser = True

    class Gateway:
        async def call_api(self, action, params):
            return await env.bot.call_api(action, **params)

    plugin = "test.onebot-history-preparation"
    source = await invocation(
        database, plugin, event, platform="qq", space_id=env.space, gateway=Gateway()
    )
    host = HostPluginContext(
        plugin_id=plugin,
        approved_permissions=(PluginPermission.ONEBOT_SEND, PluginPermission.ONEBOT_MUTATE),
        services=PluginFacadeServices(ledger=EventLedgerRepository(database), social=env.service),
    )
    writer_active = ContextVar("test_writer_active", default=False)
    original_writer, original_prepare = database.immediate_session, scoped_event_uow.prepare_terms
    prepared = []

    @asynccontextmanager
    async def writer():
        async with original_writer() as session:
            token = writer_active.set(True)
            try:
                yield session
            finally:
                writer_active.reset(token)

    def prepare(content):
        assert not writer_active.get(), "history normalization ran while holding the writer"
        prepared.append(content)
        return original_prepare(content)

    monkeypatch.setattr(database, "immediate_session", writer)
    monkeypatch.setattr(scoped_event_uow, "prepare_terms", prepare)
    params = {"group_id": event.group_id, "message": "原发送搜索正文"}
    try:
        with host.bind(source):
            first = await host.onebot.call_mutating_action("send_group_msg", params)
        assert first.ok and prepared == ["原发送搜索正文"]
        async with database.immediate_session() as session:
            person = await session.get(CanonicalPersonModel, event.author_person_id)
            person.is_superuser = False
        with host.bind(source):
            replay = await host.onebot.call_mutating_action("send_group_msg", params)
        assert replay.data["operation_id"] == first.data["operation_id"]
        assert prepared == ["原发送搜索正文"]
        assert [action for action, _ in env.bot.calls].count("send_group_msg") == 1
    finally:
        await host.close_host_resources()
