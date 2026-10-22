"""Native group recovery preserves platform identity and original actor authority."""

from datetime import UTC, datetime

from sqlalchemy import select
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.support.person_merge import merge_persons

from yuki.conversation.canonical_db_models import (
    ControlCommandReceiptModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from yuki.gateway.providers.telegram_ingress import TelegramSender, normalize_message
from yuki.identity.canonical_repository import create_person_binding
from yuki.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from yuki.persistence.models import ChatEventModel


async def test_merged_disabled_source_cannot_borrow_root_for_telegram_recovery(
    telegram_env, database
):
    env = telegram_env
    await env.gateway.receive(update(text="establish sender"))
    await drain(env)
    await env.gateway.receive(update(2, chat=-10042, text="!ai establish group"))
    await drain(env)
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "telegram",
                IdentityBindingModel.external_account_id == "42",
            )
        )
        source = await session.get(CanonicalPersonModel, binding.person_id)
        source.enabled = False
        root_binding = await create_person_binding(
            session,
            external_account_id="merge-root",
            display_name="Root",
            now=datetime.now(UTC),
            platform="telegram",
        )
        root = await session.get(CanonicalPersonModel, root_binding.person_id)
        root.is_superuser = True
        await merge_persons(session, source.id, root.id)
        group_binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == "telegram",
                SpaceBindingModel.external_space_id == "-10042",
            )
        )
        space = await session.get(CanonicalSpaceModel, group_binding.space_id)
        space.enabled = False
        space_id = space.id
        events_before = list(await session.scalars(select(ChatEventModel.id)))
    before_requests = len(env.model.requests)
    await env.gateway.receive(update(3, chat=-10042, text="/ai admin group current enable"))
    await drain(env)
    async with database.read_session() as session:
        assert not (await session.get(CanonicalSpaceModel, space_id)).enabled
        assert list(await session.scalars(select(ChatEventModel.id))) == events_before
        assert (
            await session.scalar(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.principal_id == source.id
                )
            )
        ) is None
        assert (await session.get(CanonicalPersonModel, root.id)).enabled
    assert len(env.model.requests) == before_requests
    sent_texts = [params["text"] for method, params in env.wire if method == "sendMessage"]
    assert sent_texts[-1].startswith("无法恢复")


async def test_processor_native_telegram_can_register_first_group(telegram_env, database):
    env = telegram_env
    await env.gateway.receive(update(text="establish sender"))
    await drain(env)
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "telegram",
                IdentityBindingModel.external_account_id == "42",
            )
        )
        person = await session.get(CanonicalPersonModel, binding.person_id)
        person.is_superuser = True
        assert (
            await session.scalar(
                select(SpaceBindingModel).where(
                    SpaceBindingModel.platform == "telegram",
                    SpaceBindingModel.external_space_id == "-20077",
                )
            )
        ) is None
        events_before = list(await session.scalars(select(ChatEventModel.id)))
    before_requests = len(env.model.requests)
    message = normalize_message(
        update(2, chat=-20077, text="/ai admin group current enable")["message"], env.gateway.client
    )
    assert message is not None
    result = await env.harness.processor.handle(message, TelegramSender(env.gateway, message))
    assert result.reason == "group_recovery"
    async with database.read_session() as session:
        binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == "telegram",
                SpaceBindingModel.external_space_id == "-20077",
            )
        )
        assert binding is not None and binding.status == "active"
        assert (await session.get(CanonicalSpaceModel, binding.space_id)).enabled
        ingest = await session.get(SpaceBindingIngestRouteModel, binding.id)
        route = await session.get(SpaceActiveRouteModel, binding.space_id)
        assert ingest.ingest_presence_id == env.gateway.presence_id and not ingest.paused
        assert route.presence_id == env.gateway.presence_id and not route.paused
        assert route.space_binding_id == binding.id
        assert list(await session.scalars(select(ChatEventModel.id))) == events_before
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.result_resource_id == binding.space_id
            )
        )
        assert receipt.status == "succeeded" and receipt.principal_id == person.id
    assert len(env.model.requests) == before_requests
    assert any(
        method == "getChatMember" and params["chat_id"] == "-20077" for method, params in env.wire
    )
    sent_texts = [params["text"] for method, params in env.wire if method == "sendMessage"]
    assert sent_texts[-1].startswith("已启用当前群")
