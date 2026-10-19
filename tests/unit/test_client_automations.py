"""HTTP CAS commands use real domain scheduling, run history and original receipts."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer, pair
from tests.unit.test_client_works import owner

from yuki.character.repository import CharacterRepository
from yuki.client_api.automations import ClientAutomations, automation_revision
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.models import AutomationModel, AutomationVersionModel

admin = admin_fixture
app_env = app_fixture


@pytest.fixture
async def arrangement(app_env, database, tmp_path):
    case = await setup_run(database, tmp_path, delivery="none")
    issue = await app_env.identity.issue_for_operator(case.env.person, str(uuid4()), str(uuid4()))
    first = await pair(app_env.client, issue["code"])
    issue = await app_env.identity.issue_for_device(
        await app_env.identity.authenticate(first["token"]), str(uuid4())
    )
    second = await pair(app_env.client, issue["code"])
    service = ClientAutomations(app_env.identity, case.repository, case.service)
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(app_env.identity, automations=service))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client, case=case, first=first, second=second, service=service, app_env=app_env
        )


async def listed(env, *, scope="mine", device=None):
    return await env.client.get(
        "/client/v1/automations",
        params={"scope": scope},
        headers=bearer((device or env.first)["token"]),
    )


async def act(env, action, revision, *, device=None, request_id=None):
    request = {"request_id": request_id or str(uuid4()), "expected_revision": revision}
    result = await env.client.post(
        f"/client/v1/automations/{env.case.row.id}/actions/{action}",
        json=request,
        headers=bearer((device or env.first)["token"]),
    )
    return result, request


async def test_pause_prevents_due_dispatch_cas_resume_and_cancel_preserve_run(arrangement):
    env = arrangement
    initial = (await listed(env)).json()["data"]["items"][0]
    assert initial["conversation_id"] == env.case.actor.conversation_id
    assert initial["revision"] == automation_revision(
        await env.case.repository.get(env.case.row.id)
    )
    paused, command = await act(env, "pause", initial["revision"])
    assert paused.status_code == 200, paused.text
    view = paused.json()["data"]
    assert view["state"] == "paused" and view["revision"] != initial["revision"]
    env.case.clock.advance(2)
    assert await env.case.dispatch() is None
    assert not await env.case.repository.run_history(env.case.row.id)
    stale, _ = await act(env, "resume", initial["revision"], device=env.second)
    assert stale.status_code == 409 and stale.json()["problem"]["code"] == "version_conflict"
    replay, _ = await act(
        env, "pause", initial["revision"], device=env.second, request_id=command["request_id"]
    )
    assert replay.json()["data"] == {**view, "replay": True}
    resumed, _ = await act(env, "resume", view["revision"], device=env.second)
    assert resumed.status_code == 200, resumed.text
    run = await env.case.dispatch()
    assert run is not None and run.status.value == "succeeded"
    completed = (await listed(env)).json()["data"]["items"][0]
    cancelled, _ = await act(env, "delete", completed["revision"])
    assert cancelled.status_code == 200, cancelled.text
    current = cancelled.json()["data"]
    assert current["state"] == "cancelled" and current["next_run_at"] is None
    assert len(await env.case.repository.run_history(env.case.row.id)) == 1
    old_resume, _ = await act(env, "resume", view["revision"])
    assert (
        old_resume.status_code == 409 and old_resume.json()["problem"]["code"] == "version_conflict"
    )
    forbidden, _ = await act(env, "resume", current["revision"])
    assert (
        forbidden.status_code == 409
        and forbidden.json()["problem"]["code"] == "precondition_failed"
    )
    assert await env.case.dispatch() is not None  # Original stored history only.
    assert len(await env.case.repository.run_history(env.case.row.id)) == 1


async def test_two_devices_same_revision_only_one_action_and_original_definition(
    arrangement, database
):
    env = arrangement
    before = await env.case.repository.get(env.case.row.id)
    env.case.clock.advance(1)
    results = await asyncio.gather(
        act(env, "pause", automation_revision(before)),
        act(env, "pause", automation_revision(before), device=env.second),
    )
    assert sorted(result.status_code for result, _ in results) == [200, 409]
    after = await env.case.repository.get(env.case.row.id)
    assert after.version_id == before.version_id and after.script == before.script
    assert after.updated_at != before.updated_at
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AutomationVersionModel)) == 1


async def test_scope_and_self_owner_do_not_grant_foreign_mutation(arrangement, database, tmp_path):
    env = arrangement
    foreign, _ = await owner(env.app_env, database)
    assert not (await listed(env, device=foreign)).json()["data"]["items"]
    denied, _ = await act(env, "pause", automation_revision(env.case.row), device=foreign)
    assert denied.status_code == 404
    assert (await listed(env, scope="self")).status_code == 403
    from tests.integration.test_self_automation_delivery import self_actor

    actor = await self_actor(database, env.case.env)
    self_row = await env.case.service.create_task(
        {
            "name": "SELF arrangement",
            "goal": "整理自己的工作",
            "trigger": {"type": "after", "seconds": 60},
            "delivery": {"target": "none"},
        },
        actor=actor,
        conversation_key=actor.conversation_id,
    )
    await CharacterRepository(env.app_env.identity).set_owner(
        str(uuid4()),
        env.case.env.person,
        str(uuid4()),
        granted=True,
    )
    own_self = await listed(env, scope="self")
    assert own_self.status_code == 200, own_self.text
    assert [item["automation_id"] for item in own_self.json()["data"]["items"]] == [self_row.id]
    attempt = await env.client.post(
        f"/client/v1/automations/{self_row.id}/actions/pause",
        json={"request_id": str(uuid4()), "expected_revision": automation_revision(self_row)},
        headers=bearer(env.first["token"]),
    )
    assert attempt.status_code == 404


async def test_receipt_failure_rolls_back_domain_transition(arrangement, database, monkeypatch):
    env = arrangement
    before = await env.case.repository.get(env.case.row.id)

    async def crash(*args, **kwargs):
        raise RuntimeError("receipt interrupted")

    monkeypatch.setattr(env.app_env.identity.receipts, "record_personal_command", crash)
    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel))
    with pytest.raises(RuntimeError, match="receipt interrupted"):
        await act(env, "pause", automation_revision(before))
    assert await env.case.repository.get(env.case.row.id) == before
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ControlCommandReceiptModel))
            == count
        )
        assert (await session.get(AutomationModel, before.id)).status == "active"


async def test_new_definition_changes_snapshot_revision_and_rejects_old_action(arrangement):
    env = arrangement
    initial = (await listed(env)).json()["data"]["items"][0]
    updated = await env.case.service.update_task(
        env.case.row.id,
        {**env.case.row.script, "name": "更新後的安排"},
        actor=env.case.actor,
        conversation_key=env.case.actor.conversation_id,
    )
    current = (await listed(env)).json()["data"]["items"][0]
    assert updated.version_id != env.case.row.version_id
    assert current["revision"] == automation_revision(updated)
    assert current["revision"] != initial["revision"] and current["title"] == "更新後的安排"
    stale, _ = await act(env, "pause", initial["revision"])
    assert stale.status_code == 409 and stale.json()["problem"]["current"] == current
