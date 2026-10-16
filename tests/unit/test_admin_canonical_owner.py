"""Commands preserve canonical owners across transports with equal external IDs."""

import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from tests.conftest import build_harness, make_settings

from qq_ai_bot.admin.control_resolution import ControlAccess
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.identity.canonical_repository import (
    create_person_binding,
    create_space_binding,
    find_identity_binding,
    find_space_binding,
)
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.services.policies import CommandName


async def identities(database):
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        outsider = await create_person_binding(
            session,
            external_account_id="9000",
            display_name="TG user",
            now=now,
            platform="telegram",
        )
        operator = await find_identity_binding(session, "9000")
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=operator.person_id,
                platform="telegram",
                external_account_id="operator-account",
                display_name="Linked operator",
                status="active",
                revision=1,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
                history_starts_at=now,
            )
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
    harness = build_harness(database, make_settings(database.url))
    event = inbound(outsider, space=space)
    denied = await execute(harness, event, CommandName.GROUP, f"{space} off")
    assert "权限不足" in denied.text
    assert (await harness.groups.get(space)).enabled
    trusted = replace(event, person_id=operator, sender=SenderIdentity("operator-account"))
    changed = await execute(harness, trusted, CommandName.GROUP, f"{space} off")
    assert "停用" in changed.text
    assert not (await harness.groups.get(space)).enabled
    assert (await harness.groups.get(qq_space)).enabled
    assert not harness.provider.requests


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
    access = ControlAccess(database, superuser_ids={"9000"})
    assert "superuser" not in (await access.principal_for_person(outsider)).roles
    assert "superuser" in (await access.principal_for_person(operator)).roles
    assert not (await access.person_target(outsider)).lockout_protected
    assert (await access.person_target(operator)).lockout_protected
    assert (await access.space_target(space)).space_id.text == space
    with pytest.raises(ValueError):
        await access.person_target("9000")
    with pytest.raises(ValueError):
        await access.space_target("2001")
