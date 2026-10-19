"""Commands preserve canonical owners across transports with equal external IDs."""

import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.conftest import build_harness, make_settings
from tests.unit.test_group_route_recovery import stack

from yuki.admin.control_resolution import ControlAccess
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.domain.profiles import UserProfileSnapshot
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
    ensure_account,
    find_identity_binding,
    find_space_binding,
    replace_assignment,
)
from yuki.identity.db_models import CanonicalPersonModel
from yuki.services.policies import CommandName


async def identities(database):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        await create_presence(
            session, external_account_id="telegram-bot", now=now, platform="telegram"
        )
        outsider = await create_person_binding(
            session,
            external_account_id="9000",
            display_name="TG user",
            now=now,
            platform="telegram",
        )
        operator = await find_identity_binding(session, "9000")
        account = await ensure_account(
            session,
            "operator-account",
            platform="telegram",
            display_name="Linked operator",
            now=now,
        )
        await replace_assignment(
            session,
            account,
            None,
            await session.get(CanonicalPersonModel, operator.person_id),
            now=now,
        )
        space = await create_space_binding(
            session,
            external_space_id="2001",
            name="TG group",
            enabled=True,
            autonomous_enabled=True,
            require_mention=True,
            now=now,
            platform="telegram",
        )
        qq_space = await find_space_binding(session, "2001")
        return outsider.person_id, operator.person_id, space.space_id, qq_space.space_id


def inbound(person, *, user="9000", space=None):
    return InboundMessage(
        message_id=str(uuid4()),
        event_type="message:telegram",
        scope_type=ScopeType.GROUP if space else ScopeType.PRIVATE,
        sender=SenderIdentity(user),
        text="/ai command",
        platform="telegram",
        bot_user_id="telegram-bot",
        group_id="2001" if space else None,
        person_id=person,
        space_id=space,
    )


async def execute(harness, event, command, argument):
    event = replace(event, message_id=str(uuid4()), text=f"/ai {command.value} {argument}")
    appended = await harness.ledger._writer.append(
        scope=event.scope(),
        platform_message_id=event.message_id,
        sender_user_id=event.sender.user_id,
        direction="inbound",
        content=event.text,
    )
    event = replace(
        event,
        source_event_id=appended.event.id,
        conversation_id=appended.event.canonical_conversation_id,
        legacy_conversation_key=event.scope().key,
    )
    return await harness.processor._commands.execute(
        command,
        event,
        event.scope(),
        UserProfileSnapshot(event.sender.user_id, event.scope_type, group_id=event.group_id),
        argument,
        time.perf_counter(),
    )


async def test_group_command_uses_real_person_grant_and_internal_space(database):
    outsider, operator, space, qq_space = await identities(database)
    app, *_ = await stack(database)
    try:
        event = inbound(outsider, space=space)
        profile = UserProfileSnapshot(
            event.sender.user_id, event.scope_type, group_id=event.group_id
        )
        denied = await app.command_service.execute(
            CommandName.ADMIN,
            event,
            event.scope(),
            profile,
            f"group {space} disable",
            time.perf_counter(),
        )
        assert "capability_denied" in denied.text
        assert (await app.groups.get(space)).enabled
        trusted = replace(event, person_id=operator, sender=SenderIdentity("operator-account"))
        appended = await app.ledger._writer.append(
            scope=trusted.scope(),
            platform_message_id=trusted.message_id,
            sender_user_id=trusted.sender.user_id,
            direction="inbound",
            content=f"/ai admin group {space} disable",
        )
        trusted = replace(trusted, source_event_id=appended.event.id)
        changed = await app.command_service.execute(
            CommandName.ADMIN,
            trusted,
            trusted.scope(),
            profile,
            f"group {space} disable",
            time.perf_counter(),
        )
        assert "停用" in changed.text
        assert not (await app.groups.get(space)).enabled
        assert (await app.groups.get(qq_space)).enabled
    finally:
        await app.close()


async def test_preferences_for_tg_person_do_not_require_a_qq_binding(database):
    outsider, _operator, _space, _qq_space = await identities(database)
    harness = build_harness(database, make_settings(database.url))
    event = inbound(outsider)
    changed = await execute(harness, event, CommandName.PREFERENCE, "set tone concise")
    assert "已设置" in changed.text
    rows = await harness.processor._memories.list_preferences(person_id=outsider)
    assert len(rows) == 1 and rows[0].content == "concise"
    assert rows[0].canonical_subject_person_id == outsider
    shown = await execute(harness, event, CommandName.PREFERENCE, "list")
    assert "tone = concise" in shown.text
    assert not harness.provider.requests


async def test_canonical_control_does_not_accept_external_ids_as_targets(database):
    outsider, operator, space, _qq_space = await identities(database)
    access = ControlAccess(database)
    assert "superuser" not in (await access.principal_for_person(outsider)).roles
    assert "superuser" in (await access.principal_for_person(operator)).roles
    assert (await access.space_target(space)).space_id.text == space
    with pytest.raises(ValueError):
        await access.person_target("9000")
    with pytest.raises(ValueError):
        await access.space_target("2001")
