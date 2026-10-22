"""Typed API errors retain the public writer outcome and original request recovery."""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_client_api import pair, person_and_code
from tests.unit.test_client_api import personal as personal_fixture

from yuki.admin.authority.command_types import ControlCommandError
from yuki.admin.authority.problems import Problem, ProblemCode
from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.identity.errors import CanonicalIdentityError

admin = admin_fixture
personal = personal_fixture


@pytest.mark.parametrize("error_kind", ["client", "identity", "control"])
async def test_confirmed_commit_ack_error_is_not_a_business_rejection(
    personal, database, monkeypatch, error_kind
):
    person, code = await person_and_code(personal)
    error = (
        ClientError("unauthenticated")
        if error_kind == "client"
        else CanonicalIdentityError("missing_canonical_owner")
        if error_kind == "identity"
        else ControlCommandError(Problem(ProblemCode.PRECONDITION_FAILED))
    )
    error.add_note("database_write_phase:cleanup")
    error.add_note("database_commit:confirmed")
    original_writer = database.immediate_session

    @asynccontextmanager
    async def confirmed_lost_ack():
        async with original_writer() as writer:
            yield writer
        raise error

    with monkeypatch.context() as scoped:
        scoped.setattr(database, "immediate_session", confirmed_lost_ack)
        response = await personal.post(
            "/client/v1/pair", json={"code": code, "device_label": "confirmed original"}
        )
    assert response.status_code == 500 and response.json()["problem"]["code"] == "unknown"
    retry = await personal.post("/client/v1/pair", json={"code": code, "device_label": "same code"})
    assert retry.status_code == 401
    async with database.read_session() as reader:
        assert (
            await reader.scalar(
                select(func.count(ClientDeviceModel.id)).where(
                    ClientDeviceModel.person_id == person
                )
            )
            == 1
        )


async def test_rolled_back_pair_keeps_its_normal_business_rejection(personal):
    # No submitted effect or receipt is inferred from a normal one-time-code rejection.
    _person, code = await person_and_code(personal)
    await pair(personal, code)
    response = await personal.post(
        "/client/v1/pair", json={"code": code, "device_label": "same code"}
    )
    assert response.status_code == 401 and response.json()["problem"]["code"] == "unauthenticated"
