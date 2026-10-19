"""Real personal identity, Bot receipts and Manager SQLite across a Unix socket."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_client_api import pair, person_and_code
from tests.unit.test_client_api import personal as personal_fixture
from tests.unit.test_computer_control import manager_at

from yuki.character.repository import CharacterRepository
from yuki.client_api.computer_commands import ComputerCommands, manager_key
from yuki.client_api.errors import ClientError
from yuki.client_api.requests import ClientRequests
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.sandbox.client import SandboxClient

admin = admin_fixture
personal = personal_fixture


@pytest.fixture
async def computer(personal, admin, tmp_path):
    identity = admin[1].identities
    manager = manager_at(tmp_path)
    short = tempfile.TemporaryDirectory(prefix="yc-", dir="/tmp")
    socket = Path(short.name) / "m.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    sandbox = SandboxClient(socket)
    commands = ComputerCommands(identity, sandbox)

    async def paired(*, owner=True, person=None):
        if person is None:
            person, code = await person_and_code(personal)
        else:
            code = (await identity.issue_for_operator(person, str(uuid4()), str(uuid4())))["code"]
        device = await pair(personal, code, "actual device")
        if owner:
            await CharacterRepository(identity).set_owner(
                str(uuid4()), person, str(uuid4()), granted=True
            )
        return await identity.authenticate(device["token"]), device

    try:
        yield SimpleNamespace(
            identity=identity,
            manager=manager,
            sandbox=sandbox,
            commands=commands,
            paired=paired,
            admin=admin[0],
        )
    finally:
        server.close()
        await server.wait_closed()
        short.cleanup()
        await manager.close()


async def test_owner_required_and_original_actor_is_not_a_user_payload(computer):
    actor, _ = await computer.paired(owner=False)
    with pytest.raises(ClientError, match="owner_required"):
        await computer.commands.execute(actor, str(uuid4()), "take", {})
    assert (
        computer.manager.db.execute("SELECT COUNT(*) FROM environment_mutations").fetchone()[0] == 0
    )


async def test_namespaces_with_same_uuid_do_not_mix_receipts_or_actual_device(computer):
    first, _ = await computer.paired()
    second, _ = await computer.paired()
    request = str(uuid4())
    take = await computer.commands.execute(first, request, "take", {"note": None, "lease_id": None})
    lease = take["control"]["lease_id"]
    transfer = await computer.commands.execute(
        second, request, "transfer", {"lease_id": lease, "note": "second operator"}
    )
    assert transfer["control"]["device_id"] == second.device_id
    assert transfer["by"]["device_id"] == second.device_id
    assert transfer["control"]["lease_id"] != lease
    assert "person_id" not in transfer["control"] and "run_id" not in transfer["control"]
    replay = await computer.commands.execute(
        first, request, "take", {"note": None, "lease_id": None}
    )
    assert replay["control"]["lease_id"] == lease
    assert computer.manager.computer.control()["device_id"] == second.device_id
    assert manager_key(first.request_scope, request) != manager_key(second.request_scope, request)
    with pytest.raises(ClientError, match="request_conflict"):
        await computer.commands.execute(
            second, request, "transfer", {"lease_id": lease, "note": "changed"}
        )


async def test_original_manager_commit_with_lost_response_is_settled_by_another_device_retry(
    computer, monkeypatch
):
    first, _ = await computer.paired()
    second, _ = await computer.paired(person=first.person_id)
    request = str(uuid4())
    real = computer.sandbox.execute

    async def lose(name, args, **kwargs):
        value = await real(name, args, **kwargs)
        return {"error": "sandbox_unavailable"} if name == "control_set" else value

    monkeypatch.setattr(computer.sandbox, "execute", lose)
    with pytest.raises(ClientError, match="unknown"):
        await computer.commands.execute(
            first, request, "take", {"note": "original", "lease_id": None}
        )
    original = computer.manager.computer.control()
    observed = await ClientRequests(computer.identity).get(first, request)
    assert observed["found"] and observed["status"] == "unknown"
    monkeypatch.setattr(computer.sandbox, "execute", real)
    restarted = ComputerCommands(computer.identity, computer.sandbox)
    settled = await restarted.execute(
        second, request, "take", {"note": "original", "lease_id": None}
    )
    assert settled["control"]["lease_id"] == original["lease_id"]
    assert settled["control"]["device_id"] == first.device_id
    assert (
        computer.manager.db.execute("SELECT COUNT(*) FROM environment_mutations").fetchone()[0] == 1
    )
    assert (await ClientRequests(computer.identity).get(second, request))["status"] == "succeeded"


async def test_reservation_before_manager_call_survives_cancel_and_does_not_hold_bot_writer(
    computer, monkeypatch
):
    actor, _ = await computer.paired()
    real = computer.sandbox.execute
    entered = asyncio.Event()

    async def wait_before_manager(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(computer.sandbox, "execute", wait_before_manager)
    request = str(uuid4())
    task = asyncio.create_task(computer.commands.execute(actor, request, "take", {}))
    await asyncio.wait_for(entered.wait(), 3)
    # This is an actual independent domain write while the external call waits.
    profile = await CharacterRepository(computer.identity).get(actor)
    async with asyncio.timeout(3):
        await CharacterRepository(computer.identity).update(
            str(uuid4()), profile["revision"], {"bio": "writer remains available"}, principal=actor
        )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await computer.identity.receipts.recover_interrupted_controls() == 1
    assert (
        computer.manager.db.execute("SELECT COUNT(*) FROM environment_mutations").fetchone()[0] == 0
    )
    monkeypatch.setattr(computer.sandbox, "execute", real)
    recovered = await computer.commands.execute(actor, request, "take", {})
    assert recovered["control"]["device_id"] == actor.device_id
    async with computer.identity.database.sessions() as reader:
        rows = list(
            await reader.scalars(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == request
                )
            )
        )
    assert len(rows) == 1 and rows[0].status == "succeeded"


async def test_two_actual_returns_end_one_lease_and_late_lease_does_not_clear_new_control(computer):
    first, _ = await computer.paired()
    second, _ = await computer.paired()
    lease = (await computer.commands.execute(first, str(uuid4()), "take", {}))["control"][
        "lease_id"
    ]
    values = await asyncio.gather(
        *(
            computer.commands.execute(
                who, str(uuid4()), "return", {"lease_id": lease, "note": None}
            )
            for who in (first, second)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in values) == 1
    assert (
        sum(
            isinstance(value, ClientError) and value.code == "precondition_failed"
            for value in values
        )
        == 1
    )
    new = (await computer.commands.execute(second, str(uuid4()), "take", {}))["control"]
    with pytest.raises(ClientError, match="precondition_failed"):
        await computer.commands.execute(first, str(uuid4()), "return", {"lease_id": lease})
    assert computer.manager.computer.control()["lease_id"] == new["lease_id"]


@pytest.mark.parametrize("committed", [False, True])
async def test_revoked_original_device_cannot_start_new_effect_but_original_fact_survives(
    computer, monkeypatch, committed
):
    first, _ = await computer.paired()
    second, _ = await computer.paired(person=first.person_id)
    request = str(uuid4())
    real = computer.sandbox.execute
    entered = asyncio.Event()

    async def interrupt(name, args, **kwargs):
        if (committed and name == "control_set") or (not committed):
            if committed:
                await real(name, args, **kwargs)
            entered.set()
            await asyncio.Future()
        return await real(name, args, **kwargs)

    monkeypatch.setattr(computer.sandbox, "execute", interrupt)
    task = asyncio.create_task(computer.commands.execute(first, request, "take", {}))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await computer.identity.revoke(second, first.device_id, str(uuid4()))
    monkeypatch.setattr(computer.sandbox, "execute", real)
    if committed:
        saved = await computer.commands.execute(second, request, "take", {})
        assert saved["control"]["device_id"] == first.device_id
    else:
        with pytest.raises(ClientError, match="unauthenticated"):
            await computer.commands.execute(second, request, "take", {})
        assert computer.manager.computer.control()["holder"] == "none"
        assert (await ClientRequests(computer.identity).get(second, request))["status"] == "failed"
