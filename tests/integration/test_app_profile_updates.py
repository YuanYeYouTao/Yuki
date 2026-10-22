"""App observations contain real account names, never display placeholders."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner, seeded

from yuki.identity.db_models import (
    AccountModel,
    AccountNameObservationModel,
    CanonicalPersonModel,
    IdentityBindingModel,
)
from yuki.persistence.models import ChatEventModel, PersonAliasModel


@pytest.mark.parametrize("work_input", [False, True])
@pytest.mark.parametrize("name", ["", "Real App name"])
@pytest.mark.parametrize("person_name", ["", "Person chosen name"])
async def test_app_input_observes_only_the_real_name(
    app_env, database, work_input, name, person_name
):
    env = app_env
    device, person_id = await owner(env, database)
    async with database.immediate_session() as session:
        person = await session.get(CanonicalPersonModel, person_id)
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.person_id == person_id,
                IdentityBindingModel.platform == "app",
            )
        )
        person.display_name = person_name
        binding.account.display_name = name
        binding_id = binding.id
        account_id = binding.account_id

    if work_input:
        original = await seeded(env, database)
        url = f"/client/v1/works/{original.row['id']}/inputs"
    else:
        url = "/client/v1/messages"
    response = await env.client.post(
        url,
        headers=bearer(device["token"]),
        json={"request_id": str(uuid4()), "text": "App input"},
    )
    assert response.status_code == 200, response.text
    await drain(env)
    async with database.read_session() as session:
        assert (await session.get(CanonicalPersonModel, person_id)).display_name == (
            person_name or name
        )
        assert (await session.get(AccountModel, account_id)).display_name == name
        assert (await session.get(IdentityBindingModel, binding_id)).display_name == name
        aliases = set(
            await session.scalars(
                select(PersonAliasModel.alias).where(
                    PersonAliasModel.canonical_person_id == person_id
                )
            )
        )
        assert "本人" not in aliases
        assert name not in aliases
        observations = set(
            await session.scalars(
                select(AccountNameObservationModel.name).where(
                    AccountNameObservationModel.account_id == account_id,
                    AccountNameObservationModel.assignment_id == binding_id,
                    AccountNameObservationModel.space_binding_id.is_(None),
                )
            )
        )
        assert "本人" not in observations
        assert (name in observations) == bool(name)
        event = await session.get(ChatEventModel, response.json()["data"]["event_id"])
        assert event.author_person_id == person_id and event.sender_nickname == name
