"""Cross-review management contracts through real application and SQLite owners."""

import argparse
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from tests.unit.test_admin_api import TOKEN, command
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_admin_operator_access import operator_file
from tests.unit.test_group_route_recovery import message, stack

from yuki.admin.authority.command_types import ControlCommandError
from yuki.admin.authority.operators import ControlOperatorAccess
from yuki.admin.control_resolution import ControlAccess
from yuki.admin_api.cli import add_admin_parser, admin_command
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
)
from yuki.domain.profiles import UserProfileSnapshot
from yuki.identity.authority import person_is_superuser
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.models import AdminOperationEventModel
from yuki.runtime.work_repository import WorkRepository
from yuki.services.policies import CommandName

admin = admin_fixture


async def test_role_replay_after_merge_never_revives_revoked_root(admin, database):
    client, *_ = admin
    source = (await client.post("/admin/v1/persons", json=command())).json()["data"]["resource_id"]
    target = (await client.post("/admin/v1/persons", json=command())).json()["data"]["resource_id"]
    request = command(1)
    url = f"/admin/v1/persons/{source}/superuser/grant"
    granted = await client.post(url, json=request)
    assert granted.status_code == 200, granted.text
    merged = await client.post(
        f"/admin/v1/persons/{source}/merge",
        json={"request_id": str(uuid4()), "to_person": target},
    )
    assert merged.status_code == 200, merged.text
    current = (await client.get(f"/admin/v1/persons/{target}")).json()["data"]
    assert current["is_superuser"]
    revoked = await client.post(
        f"/admin/v1/persons/{target}/superuser/revoke", json=command(current["revision"])
    )
    assert revoked.status_code == 200, revoked.text
    replayed = await client.post(url, json=request)
    assert replayed.status_code == 200 and replayed.json() == granted.json()
    assert not (await client.get(f"/admin/v1/persons/{source}")).json()["data"]["is_superuser"]
    async with database.sessions() as session:
        assert not await person_is_superuser(session, source)
        assert not await person_is_superuser(session, target)
        original = await session.get(CanonicalPersonModel, source)
        assert original is not None and original.merged_into_person_id == target
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == request["request_id"]
            )
        )
        assert receipt is not None and receipt.result_resource_id == source


async def test_operator_optional_person_link_preserves_its_explicit_grants(
    admin, database, tmp_path
):
    client, api, *_ = admin
    person = (await client.post("/admin/v1/persons", json=command())).json()["data"]["resource_id"]
    policy, _ = operator_file(
        tmp_path,
        capabilities=("identity.person.read", "identity.person.enable"),
        person=person,
    )
    object.__setattr__(api, "access", ControlOperatorAccess(database, policy))
    shown = await client.get(f"/admin/v1/persons/{person}")
    assert shown.status_code == 200, shown.text
    assert not shown.json()["data"]["is_superuser"]
    restored = await client.post(f"/admin/v1/persons/{person}/enable", json=command(1))
    assert restored.status_code == 200, restored.text
    assert TOKEN not in shown.text + restored.text


async def test_person_role_revocation_before_identity_writer_stops_new_mutation(
    admin, database, monkeypatch
):
    client, api, *_ = admin
    actor = (await client.post("/admin/v1/persons", json=command())).json()["data"]["resource_id"]
    victim = (await client.post("/admin/v1/persons", json=command(display_name="Original"))).json()[
        "data"
    ]["resource_id"]
    grant = await client.post(f"/admin/v1/persons/{actor}/superuser/grant", json=command(1))
    assert grant.status_code == 200, grant.text
    principal = await ControlAccess(database).principal_for_person(actor)
    arrived, proceed = asyncio.Event(), asyncio.Event()
    original = type(database).apply_snapshot_write

    async def delayed(instance, operation):
        arrived.set()
        await proceed.wait()
        return await original(instance, operation)

    monkeypatch.setattr(type(database), "apply_snapshot_write", delayed)
    pending = asyncio.create_task(
        api.commands.identity.change_person(
            principal, victim, str(uuid4()), display_name="After revoke"
        )
    )
    try:
        await asyncio.wait_for(arrived.wait(), timeout=5)
        revoked = await client.post(
            f"/admin/v1/persons/{actor}/superuser/revoke",
            json=command(grant.json()["data"]["revision"]),
        )
        assert revoked.status_code == 200, revoked.text
    finally:
        proceed.set()
    with pytest.raises(ControlCommandError) as error:
        await pending
    assert error.value.problem.code.value == "capability_denied"
    assert (await client.get(f"/admin/v1/persons/{victim}")).json()["data"][
        "display_name"
    ] == "Original"


async def test_identity_restoration_and_paginated_discovery_are_read_only(admin, database):
    client, *_ = admin
    created = (await client.post("/admin/v1/persons", json=command())).json()["data"]
    person = created["resource_id"]
    disabled = await client.post(f"/admin/v1/persons/{person}/disable", json=command(1))
    assert disabled.status_code == 200, disabled.text
    disabled_revision = disabled.json()["data"]["revision"]
    restored = await client.post(
        f"/admin/v1/persons/{person}/enable", json=command(disabled_revision)
    )
    assert restored.status_code == 200, restored.text
    attached = await client.post(
        f"/admin/v1/persons/{person}/bindings",
        json=command(
            restored.json()["data"]["revision"],
            platform="telegram",
            external_account_id="review-account",
        ),
    )
    assert attached.status_code == 200, attached.text
    binding = attached.json()["data"]["resource_id"]
    disabled = await client.post(f"/admin/v1/bindings/{binding}/disable", json=command(1))
    assert disabled.status_code == 200, disabled.text
    rows = (await client.get("/admin/v1/presences")).json()["data"]["items"]
    presence = rows[0]
    retired = await client.post(
        f"/admin/v1/presences/{presence['id']}",
        json=command(presence["revision"], enabled=False, ingest_eligible=False),
    )
    assert retired.status_code == 200, retired.text

    writes = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(maxsplit=1)[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
        }:
            writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        discovered = []
        after = ""
        while True:
            response = await client.get("/admin/v1/persons", params={"limit": 1, "after": after})
            assert response.status_code == 200, response.text
            page = response.json()["data"]
            discovered.extend(row["id"] for row in page["items"])
            after = page["next_cursor"]
            if after is None:
                break
        assert len(discovered) == len(set(discovered)) and person in discovered
        binding_view = (await client.get(f"/admin/v1/bindings/{binding}")).json()["data"]
        filtered = (
            await client.get("/admin/v1/bindings", params={"person_id": person, "limit": 1})
        ).json()["data"]
        assert filtered["items"][0]["id"] == binding and binding_view["status"] == "disabled"
        presence_view = (await client.get(f"/admin/v1/presences/{presence['id']}")).json()["data"]
        assert not presence_view["enabled"] and not presence_view["ingest_eligible"]
        assert (await client.get("/admin/v1/works", params={"limit": 1})).status_code == 200
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert writes == []
    enabled = await client.post(
        f"/admin/v1/bindings/{binding}/enable", json=command(binding_view["revision"])
    )
    assert enabled.status_code == 200, enabled.text
    restored = await client.post(
        f"/admin/v1/presences/{presence['id']}",
        json=command(presence_view["revision"], enabled=True, ingest_eligible=True),
    )
    assert restored.status_code == 200 and restored.json()["data"]["resource_id"] == presence["id"]


async def test_cli_routes_role_presence_and_listing_to_real_operator_api(
    admin, monkeypatch, capsys
):
    client, *_ = admin

    @asynccontextmanager
    async def connected(_settings):
        yield client

    monkeypatch.setattr("yuki.admin_api.cli.admin_client", connected)
    parser = argparse.ArgumentParser()
    add_admin_parser(parser.add_subparsers(dest="command", required=True))
    created = (await client.post("/admin/v1/persons", json=command())).json()["data"]
    person = created["resource_id"]
    args = parser.parse_args(["admin", "person", "superuser", "grant", person, "--revision", "1"])
    assert await admin_command(None, args) == 0
    granted = json.loads(capsys.readouterr().out)
    assert granted["success"]
    args = parser.parse_args(["admin", "binding", "list", "--person", person, "--limit", "1"])
    assert await admin_command(None, args) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    presence = (await client.get("/admin/v1/presences")).json()["data"]["items"][0]
    args = parser.parse_args(
        [
            "admin",
            "presence",
            "ingest",
            presence["id"],
            "--revision",
            str(presence["revision"]),
            "off",
        ]
    )
    assert await admin_command(None, args) == 0
    assert json.loads(capsys.readouterr().out)["success"]
    actual = (await client.get(f"/admin/v1/presences/{presence['id']}")).json()["data"]
    assert not actual["ingest_eligible"]


async def test_doctor_query_and_operation_preserve_full_current_diagnostics(admin):
    client, _, manager, *_ = admin
    report = (await manager.doctor("test.contract")).model_dump(mode="json")
    queried = await client.get("/admin/v1/plugins/test.contract")
    assert queried.status_code == 200, queried.text
    shown = queried.json()["data"]
    assert {key: shown[key] for key in report} == report
    assert report["problems"]
    result = await client.post(
        "/admin/v1/plugins/test.contract/actions/doctor", json=command(shown["revision"])
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["effective_state"]["diagnostics"] == report


async def test_work_discovery_detail_and_cancel_use_original_revision_and_budget(
    admin, database, tmp_path
):
    from tests.support.social_identity_cases import social_env

    client, *_ = admin
    scene = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    async with database.sessions() as session:
        generation = await session.scalar(
            select(CanonicalConversationModel.generation).where(
                CanonicalConversationModel.id == scene.context.conversation_id
            )
        )
    lease = await repository.acquire(scene.context.conversation_id, generation)
    assert lease is not None
    original = await repository.accept(
        lease,
        source_key="admin-review-original-work",
        source={"origin": "user_message", "principal_kind": "person"},
        goal="original managed task",
    )
    listed = (await client.get("/admin/v1/works", params={"state": "running", "limit": 1})).json()[
        "data"
    ]
    assert listed["items"][0]["id"] == original["id"]
    assert listed["items"][0]["source"] == {
        "origin": "user_message",
        "owner": None,
        "initiative_run_id": None,
    }
    detail = (await client.get(f"/admin/v1/works/{original['id']}")).json()["data"]
    assert detail["source"] == listed["items"][0]["source"]
    assert detail["actions"]["cancel"] is True
    assert detail["actions"]["resume"] is False
    assert "source_json" not in detail and "checkpoint_json" not in detail
    body = command(detail["revision"])
    cancelled = await client.post(f"/admin/v1/works/{detail['id']}/cancel", json=body)
    assert cancelled.status_code == 200, cancelled.text
    assert (
        await client.post(f"/admin/v1/works/{detail['id']}/cancel", json=body)
    ).json() == cancelled.json()
    current = await repository.get(detail["id"])
    assert current["state"] == "cancelled" and current["id"] == original["id"]
    assert current["model_requests"] == original["model_requests"]
    assert current["source_json"] == original["source_json"]


async def test_chat_config_original_writer_and_replay_keep_usable_change_identifier(database):
    app, bot, *_ = await stack(database)

    async def submit(value, identity):
        argument = f"config set context.compact_trigger_tokens {value}"
        request = message("/ai admin " + argument, message_id=identity)
        admitted = await app.canonical_ingress.pre_admit(bot, request)
        appended = await app.canonical_uow.append_inbound(admitted.message, admitted)
        request = replace(admitted.message, source_event_id=appended.event.id)
        profile = UserProfileSnapshot(
            request.sender.user_id, request.scope_type, group_id=request.group_id
        )

        async def execute():
            return await app.command_service.execute(
                CommandName.ADMIN, request, request.scope(), profile, argument, time.perf_counter()
            )

        return await execute(), execute, appended.event.id

    try:
        first, replay, first_event = await submit(12345, "review-config-1")
        later, _, _ = await submit(23456, "review-config-2")
        assert "已处理" in first.text and "已处理" in later.text
        repeated = await replay()
        assert repeated.text == first.text
        assert (
            await app.runtime_config.get_effective("context.compact_trigger_tokens")
        ).value == 23456
        async with database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ControlCommandReceiptModel).where(
                        ControlCommandReceiptModel.source_event_id == first_event
                    )
                )
            )
            assert len(rows) == 1
            audits = list(
                await session.scalars(
                    select(AdminOperationEventModel).where(
                        AdminOperationEventModel.trigger_message_id == "review-config-1",
                        AdminOperationEventModel.capability == "runtime_config",
                    )
                )
            )
            assert len(audits) == 1
            assert f"变更编号：{audits[0].id}" in first.text
    finally:
        await app.close()


async def test_config_rollback_of_real_failed_change_returns_domain_rejection(database):
    app, bot, *_ = await stack(database)
    try:
        failed = await app.runtime_config.set_override(
            "context.compact_trigger_tokens",
            "invalid",
            scope_type="global",
            scope_id="",
            actor_user_id="9000",
            trigger_message_id="review-failed-config",
        )
        assert not failed.success
        async with database.sessions() as session:
            failed_id = await session.scalar(
                select(AdminOperationEventModel.id).where(
                    AdminOperationEventModel.trigger_message_id == "review-failed-config"
                )
            )
        argument = f"config rollback {failed_id}"
        request = message("/ai admin " + argument, message_id="review-rollback-failed")
        admitted = await app.canonical_ingress.pre_admit(bot, request)
        appended = await app.canonical_uow.append_inbound(admitted.message, admitted)
        request = replace(admitted.message, source_event_id=appended.event.id)
        profile = UserProfileSnapshot(
            request.sender.user_id, request.scope_type, group_id=request.group_id
        )
        rejected = await app.command_service.execute(
            CommandName.ADMIN, request, request.scope(), profile, argument, time.perf_counter()
        )
        assert "未完成" in rejected.text or "未修改" in rejected.text
    finally:
        await app.close()
