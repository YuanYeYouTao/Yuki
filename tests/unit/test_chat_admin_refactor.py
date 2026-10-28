"""User-facing chat control behavior over the real container and SQLite."""

import json
import time
from dataclasses import replace

from sqlalchemy import func, select
from tests.unit.test_group_route_recovery import Sender, message, stack

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
)
from yuki.domain.messages import AttachmentKind, MessageAttachment
from yuki.domain.profiles import UserProfileSnapshot
from yuki.identity.db_models import CanonicalPersonModel
from yuki.services.policies import CommandName


def test_permission_directory_accepts_the_complete_search_text():
    from yuki.admin.permission_catalog import PermissionCatalogService

    report = PermissionCatalogService().report_for_message(
        message("/ai capabilities"),
        query="an ordinary complete directory search " * 4,
        actor_is_superuser=True,
    )
    assert report.capabilities == ()


def test_chat_renders_nested_original_receipt_as_json():
    from yuki.admin.authority.commands import ControlResult
    from yuki.services.commands.admin import render_result

    result = ControlResult(
        success=True,
        resource_id="fixture",
        revision=1,
        audit_id="1",
        effective_state={
            "resource": "fixture",
            "revision": 1,
            "status": "applied",
            "change": {"change_id": 42, "pending_restart": True},
        },
    )
    rendered = json.loads(render_result(result))
    assert rendered["effective_state"]["change"] == {
        "change_id": 42,
        "pending_restart": True,
    }


async def test_chat_transfer_rechecks_role_in_actual_writer(database, monkeypatch):
    import yuki.admin.operations.identity as domain
    from yuki.identity.canonical_repository import create_person_binding, find_identity_binding

    app, bot, *_ = await stack(database)
    try:
        from datetime import UTC, datetime

        async with database.immediate_session() as session:
            target = await create_person_binding(
                session,
                external_account_id="other-account",
                display_name="Other",
                now=datetime.now(UTC),
                platform="telegram",
            )
            original_binding = await find_identity_binding(session, "9000")
            binding_id, operator_id = original_binding.id, original_binding.person_id
        request = message(f"/ai admin binding transfer {binding_id} {target.person_id}")
        admitted = await app.canonical_ingress.pre_admit(bot, request)
        appended = await app.canonical_uow.append_inbound(admitted.message, admitted)
        request = replace(admitted.message, source_event_id=appended.event.id)
        authorize = domain.authorize_current

        async def revoke_before_writer(database_, principal, capability, *, session=None):
            await authorize(database_, principal, capability, session=session)
            if session is None:
                async with database.immediate_session() as writer:
                    person = await writer.get(CanonicalPersonModel, operator_id)
                    person.is_superuser = False

        monkeypatch.setattr(domain, "authorize_current", revoke_before_writer)
        result = await app.command_service.execute(
            CommandName.ADMIN,
            request,
            request.scope(),
            UserProfileSnapshot(
                request.sender.user_id, request.scope_type, group_id=request.group_id
            ),
            f"binding transfer {binding_id} {target.person_id}",
            time.perf_counter(),
        )
        assert "capability_denied" in result.text
        async with database.read_session() as session:
            original_binding = await find_identity_binding(session, "9000")
            assert original_binding.id == binding_id and original_binding.person_id == operator_id
            assert original_binding.status == "active"
    finally:
        await app.close()


async def test_group_new_rechecks_role_after_wait_before_generation_write(database, monkeypatch):
    from yuki.identity.canonical_repository import find_identity_binding
    from yuki.persistence.models import ChatEventModel

    app, bot, _presence, space, _binding = await stack(database)
    try:
        async with database.read_session() as session:
            operator = await find_identity_binding(session, "9000")
            operator_id = operator.person_id
            generation = await session.scalar(
                select(CanonicalConversationModel.generation).where(
                    CanonicalConversationModel.space_id == space
                )
            )
        cancel_before_boundary = app.turn_coordinator.cancel_running_before_boundary

        async def revoke_during_wait(key):
            await cancel_before_boundary(key)
            async with database.immediate_session() as session:
                person = await session.get(CanonicalPersonModel, operator_id)
                person.is_superuser = False

        monkeypatch.setattr(
            app.turn_coordinator, "cancel_running_before_boundary", revoke_during_wait
        )
        sender = Sender(bot)
        result = await app.processor.handle(message("/ai new", message_id="revoke-new"), sender)
        assert result.handled and "超级管理员" in sender.messages[-1].text
        async with database.read_session() as session:
            assert (
                await session.scalar(
                    select(CanonicalConversationModel.generation).where(
                        CanonicalConversationModel.space_id == space
                    )
                )
                == generation
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ChatEventModel)
                    .where(
                        ChatEventModel.origin_key == "revoke-new",
                        ChatEventModel.direction == "inbound",
                    )
                )
                == 1
            )
    finally:
        await app.close()


async def test_manual_group_compact_rechecks_role_after_build(database, tmp_path, monkeypatch):
    from tests.conftest import MemorySender, build_harness, make_settings
    from tests.support.commands_and_chat_helpers import inbound

    from yuki.identity.canonical_repository import find_identity_binding
    from yuki.services.main_agent_contract import MainAgentContract
    from yuki.workspace.short_state import ShortState
    from yuki.workspace.store import WorkspaceStore

    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    chat.runtime.runner.main_contract = MainAgentContract(
        chat, ShortState(WorkspaceStore(tmp_path / "compact-workspace"))
    )
    await harness.processor.handle(
        inbound(
            "hello",
            message_id="compact-history",
            user_id="9000",
            group_id="2001",
            mentions_bot=True,
        ),
        MemorySender(),
    )
    harness.provider.requests.clear()
    build_messages = harness.processor._chat._build_messages
    async with database.read_session() as session:
        operator = await find_identity_binding(session, "9000")
        operator_id = operator.person_id

    async def revoke_after_build(*args, **kwargs):
        built = await build_messages(*args, **kwargs)
        async with database.immediate_session() as session:
            person = await session.get(CanonicalPersonModel, operator_id)
            person.is_superuser = False
        return built

    monkeypatch.setattr(harness.processor._chat, "_build_messages", revoke_after_build)
    sender = MemorySender()
    result = await harness.processor.handle(
        inbound("/ai compact", message_id="revoke-compact", user_id="9000", group_id="2001"),
        sender,
    )
    assert result.handled and "需要当前超级管理员资格" in sender.messages[-1].text
    assert not harness.provider.requests


async def test_authorized_config_command_with_image_is_executed(database):
    app, bot, *_ = await stack(database)
    try:
        request = replace(
            message("/ai admin config set context.compact_trigger_tokens 12345"),
            attachments=(MessageAttachment(AttachmentKind.IMAGE, "an image"),),
        )
        result = await app.processor.handle(request, Sender(bot))
        assert result.handled
        assert (
            await app.runtime_config.get_effective("context.compact_trigger_tokens")
        ).value == 12345
    finally:
        await app.close()


async def test_chat_person_creation_replays_original_internal_event(database):
    app, bot, *_ = await stack(database)
    try:
        request = message("/ai admin person create 新人物")
        admitted = await app.canonical_ingress.pre_admit(bot, request)
        appended = await app.canonical_uow.append_inbound(admitted.message, admitted)
        request = replace(admitted.message, source_event_id=appended.event.id)
        profile = UserProfileSnapshot(
            request.sender.user_id, request.scope_type, group_id=request.group_id
        )
        before = None
        async with database.read_session() as session:
            before = await session.scalar(select(func.count()).select_from(CanonicalPersonModel))
        first = await app.command_service.execute(
            CommandName.ADMIN,
            request,
            request.scope(),
            profile,
            "person create 新人物",
            time.perf_counter(),
        )
        second = await app.command_service.execute(
            CommandName.ADMIN,
            request,
            request.scope(),
            profile,
            "person create 新人物",
            time.perf_counter(),
        )
        first_result, second_result = json.loads(first.text), json.loads(second.text)
        assert first_result["success"] and second_result == first_result
        async with database.read_session() as session:
            assert (
                await session.scalar(select(func.count()).select_from(CanonicalPersonModel))
                == before + 1
            )
            rows = list(
                (
                    await session.scalars(
                        select(ControlCommandReceiptModel).where(
                            ControlCommandReceiptModel.source_event_id == appended.event.id
                        )
                    )
                ).all()
            )
            assert len(rows) == 1
            assert rows[0].status == "succeeded"
    finally:
        await app.close()


async def test_real_container_rebuild_command_uses_original_service_and_current_role(database):
    from yuki.identity.canonical_repository import find_identity_binding

    app, bot, *_ = await stack(database)
    try:
        sender = Sender(bot)
        first = await app.processor.handle(
            message("/ai admin memory rebuild list", message_id="rebuild-list"), sender
        )
        assert first.reason == "command_admin"
        assert sender.messages[-1].text == "暂无历史记忆重建任务。"
        async with database.immediate_session() as session:
            binding = await find_identity_binding(session, "9000")
            person = await session.get(CanonicalPersonModel, binding.person_id)
            person.is_superuser = False
        second = await app.processor.handle(
            message("/ai admin memory rebuild list", message_id="rebuild-list-after-revoke"), sender
        )
        assert second.reason == "command_admin"
        assert "current real superuser" in sender.messages[-1].text
        assert "暂无历史记忆重建任务" not in sender.messages[-1].text
    finally:
        await app.close()
