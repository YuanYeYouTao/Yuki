"""Work-input preparation releases SQLite's writer until the receipt is claimed."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_client_works import app_env as app_env
from tests.unit.test_client_works import owner, seeded

from yuki.client_api.errors import ClientError
from yuki.client_api.works import ClientWorks
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.identity import canonical_uow
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_schema_v1 import inputs, work


async def test_work_completed_during_preparation_keeps_receipt_without_accepting_input(
    app_env, database, monkeypatch
):
    env = app_env
    device, _person = await owner(env, database)
    original = await seeded(env, database)
    principal = await env.identity.authenticate(device["token"])
    prepared, resume = asyncio.Event(), asyncio.Event()
    prepare = canonical_uow._prepare_observation

    async def paused_prepare(*args, **kwargs):
        result = await prepare(*args, **kwargs)
        prepared.set()
        await resume.wait()
        return result

    monkeypatch.setattr(canonical_uow, "_prepare_observation", paused_prepare)
    request_id = str(uuid4())
    submitted = asyncio.create_task(
        ClientWorks(env.identity, env.gateway).submit(
            principal, original.row["id"], request_id, "new input"
        )
    )
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        # A second real connection can commit while the input reads its source.
        async with database.immediate_session() as writer:
            await writer.execute(
                update(work)
                .where(work.c.id == original.row["id"])
                .values(state="completed", revision=work.c.revision + 1)
            )
        resume.set()
        with pytest.raises(ClientError, match="precondition_failed"):
            await asyncio.wait_for(submitted, 5)
    finally:
        resume.set()
        if not submitted.done():
            submitted.cancel()
        await asyncio.gather(submitted, return_exceptions=True)

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(inputs)) == 0
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == 1
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.request_id == request_id
            )
        )
        assert receipt is not None and receipt.problem_code == "precondition_failed"
    assert not env.calls and not env.gateway.tasks
