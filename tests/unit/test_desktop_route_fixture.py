"""The unavailable route fixture creates a real own binding without a destination."""

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select
from tests.support.desktop_character_fixture import attach_character_fixture
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_management import management as management_fixture
from tests.unit.test_client_works import owner

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import IdentityBindingModel
from yuki.persistence.models import ChatEventModel

admin = admin_fixture
app_env = app_fixture
management = management_fixture


async def test_actual_own_qq_binding_without_destination_preserves_accepted_route(
    management, database
):
    case = management
    device, person = await owner(case.env, database)
    principal = await case.env.identity.authenticate(device["token"])
    headers = bearer(device["token"])
    await attach_character_fixture(
        case.app, SimpleNamespace(database=database, character=case.character), person
    )
    before = (await case.client.get("/client/v1/me/proactive-route", headers=headers)).json()[
        "data"
    ]
    original = {
        "request_id": str(uuid4()),
        "expected_revision": before["revision"],
        "binding_id": principal.binding_id,
    }
    accepted = await case.client.put(
        "/client/v1/me/proactive-route", headers=headers, json=original
    )
    assert accepted.status_code == 200, accepted.text
    receipt = await case.client.get(
        f"/client/v1/requests/{original['request_id']}", headers=headers
    )
    async with database.read_session() as reader:
        events_before = await reader.scalar(select(func.count()).select_from(ChatEventModel))
    created = await case.client.post("/__test/qq-binding-without-destination")
    assert created.status_code == 200, created.text
    binding = created.json()
    assert binding["person_id"] == person and binding["platform"] == "qq"
    assert binding["external_account_id"] == "100200"
    me = (await case.client.get("/client/v1/me", headers=headers)).json()["data"]
    assert any(item["binding_id"] == binding["binding_id"] for item in me["bindings"])
    async with database.read_session() as reader:
        actual = await reader.get(IdentityBindingModel, binding["binding_id"])
        assert actual is not None and actual.person_id == person and actual.status == "active"
        assert not await reader.scalar(
            select(func.count())
            .select_from(CanonicalConversationModel)
            .where(CanonicalConversationModel.identity_binding_id == actual.id)
        )
        assert await reader.scalar(select(func.count()).select_from(ChatEventModel)) == (
            events_before
        )
    unavailable = await case.client.put(
        "/client/v1/me/proactive-route",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "expected_revision": accepted.json()["data"]["revision"],
            "binding_id": binding["binding_id"],
        },
    )
    assert unavailable.status_code == 409, unavailable.text
    assert unavailable.json()["problem"] == {
        "code": "precondition_failed",
        "reason": "route_unavailable",
    }
    assert (await case.client.get("/client/v1/me/proactive-route", headers=headers)).json() == (
        accepted.json()
    )
    assert (
        await case.client.get(f"/client/v1/requests/{original['request_id']}", headers=headers)
    ).json() == receipt.json()
