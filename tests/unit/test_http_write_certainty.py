"""Typed API errors retain the public writer outcome and original request recovery."""

from contextlib import asynccontextmanager

from sqlalchemy import func, select
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_client_api import person_and_code
from tests.unit.test_client_api import personal as personal_fixture

from yuki.admin.authority.command_types import ControlCommandError
from yuki.admin.authority.problems import Problem, ProblemCode
from yuki.client_api.db_models import ClientDeviceModel

admin = admin_fixture
personal = personal_fixture


async def test_confirmed_commit_ack_error_is_not_a_business_rejection(
    personal, database, monkeypatch
):
    person, code = await person_and_code(personal)
    error = ControlCommandError(Problem(ProblemCode.PRECONDITION_FAILED))
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
