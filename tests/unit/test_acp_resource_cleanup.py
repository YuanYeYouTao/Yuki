"""Privacy and real archive retire the original ACP resources before losing owners."""

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.support.canonical_ingress import fixture_ingress
from tests.support.client_identity import client_identity_service
from tests.support.event_seed import seed_scoped_event
from tests.support.subagents_helpers import stack

from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.operations.base import CommandExecutor
from yuki.admin.operations.identity import IdentityOperations
from yuki.domain.conversations import ConversationScope
from yuki.domain.identity import PrincipalId
from yuki.identity.db_models import CanonicalPersonModel
from yuki.identity.person_identity import merge_persons
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.persistence.models import AdminOperationEventModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.db_models import SandboxTaskRunModel


async def seed_task(
    database,
    conversation,
    work_id,
    *,
    request="original-prompt",
    status="completed",
    acp=True,
    run_id=True,
):
    progress = (
        {
            "kind": "acp_prompt",
            "resource_request_id": f"{request}:peer",
            "peer_id": str(uuid4()),
            "session_id": "original-session",
        }
        if acp
        else {}
    )
    async with database.immediate_session() as writer:
        row = SandboxTaskRunModel(
            request_id=request,
            source_conversation_id=conversation,
            source_json=json.dumps(
                {"conversation_id": conversation, "generation": 1, "work_id": work_id}
            ),
            payload_hash="a" * 64,
            run_id=str(uuid4()) if run_id else None,
            status=status,
            progress_json=json.dumps(progress),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        writer.add(row)
    return row


async def privacy_case(database, *, status="completed", acp=True, run_id=True):
    appended = await seed_scoped_event(
        ScopedEventLedgerUnitOfWork(database),
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="private-cleanup",
        sender_user_id="1001",
        direction="inbound",
        content="private original context",
    )
    event = appended.event
    repository = WorkRepository(database)
    lease = await repository.acquire(event.canonical_conversation_id, 1)
    original = await repository.accept(
        lease,
        source_key="privacy-work",
        source={"actor_person_id": event.author_person_id},
        goal="original",
    )
    await repository.release(lease)
    task = await seed_task(
        database,
        event.canonical_conversation_id,
        original["id"],
        status=status,
        acp=acp,
        run_id=run_id,
    )
    return SimpleNamespace(
        person=event.author_person_id,
        conversation=event.canonical_conversation_id,
        work_id=original["id"],
        task=task,
    )


class CleanupClient:
    def __init__(self, database, callback=None, *, fail=False):
        self.database, self.callback, self.fail = database, callback, fail
        self.calls = []

    async def cleanup_tasks(self, tasks, operation_id, *, purge):
        self.calls.append((tuple(task.request_id for task in tasks), operation_id, purge))
        # A second connection can acquire the writer while the external ACK waits.
        async with self.database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalPersonModel)
                .where(CanonicalPersonModel.id != "missing")
                .values(display_name=CanonicalPersonModel.display_name)
            )
        if self.callback is not None:
            await self.callback()
        if self.fail:
            raise ValueError("external_agent_cleanup_unconfirmed")
        return [{"admitted": True, "pending": True, "peer_id": "original-peer"}]


def forget(database, person, entry):
    if entry == "personal":
        return lambda: PeopleRepository(database).delete_person_id(person)
    commands = IdentityOperations(
        CommandExecutor(database),
        client_identity_service(database, fixture_ingress(database)._router),
        PeopleRepository(database, memory_rebuilds=MemoryRebuildRepository(database)),
        router=fixture_ingress(database)._router,
    )
    principal = ControlPrincipal(
        principal_id=PrincipalId(uuid4()),
        person_id=None,
        source=PrincipalSource.CLI,
        granted_capabilities=("identity.person.forget",),
        authenticated=True,
        active=True,
    )
    request = str(uuid4())
    return lambda: commands.forget_person(principal, person, request)


@pytest.mark.parametrize("entry", ["personal", "admin"])
@pytest.mark.parametrize("status", ["waiting", "completed"])
async def test_forget_closes_original_active_or_idle_peers_before_deleting_owner(
    database, entry, status
):
    original = await privacy_case(database, status=status)
    client = CleanupClient(database)
    database.external_agents = client
    erase = forget(database, original.person, entry)
    result = await erase()
    assert result
    assert client.calls == [(("original-prompt",), client.calls[0][1], True)]
    async with database.sessions() as reader:
        assert await reader.get(CanonicalPersonModel, original.person) is None
        assert await reader.get(SandboxTaskRunModel, original.task.request_id) is None
        assert await reader.scalar(select(work.c.id).where(work.c.id == original.work_id)) is None
    if entry == "admin":
        assert result["peer_cleanup"] == (
            {"admitted": True, "pending": True, "peer_id": "original-peer"},
        )
        assert await erase() == {**json.loads(json.dumps(result)), "replay": True}
        assert len(client.calls) == 1


@pytest.mark.parametrize("entry", ["personal", "admin"])
async def test_forget_unknown_close_keeps_original_owner_and_same_operation(database, entry):
    original = await privacy_case(database)
    client = CleanupClient(database, fail=True)
    database.external_agents = client
    erase = forget(database, original.person, entry)
    for _ in range(2):
        with pytest.raises(ValueError, match="external_agent_cleanup_unconfirmed"):
            await erase()
    assert client.calls[0] == client.calls[1]
    async with database.sessions() as reader:
        assert await reader.get(CanonicalPersonModel, original.person) is not None
        assert await reader.get(SandboxTaskRunModel, original.task.request_id) is not None
        assert (
            await reader.scalar(select(work.c.id).where(work.c.id == original.work_id))
            == original.work_id
        )


@pytest.mark.parametrize("entry", ["personal", "admin"])
@pytest.mark.parametrize("change", ["new_run", "work_source", "task_source", "merge"])
async def test_forget_does_not_expand_scope_after_original_close_admission(database, entry, change):
    original = await privacy_case(database)

    async def race():
        if change == "new_run":
            await seed_task(
                database, original.conversation, original.work_id, request="later-prompt"
            )
        else:
            async with database.immediate_session() as writer:
                if change == "work_source":
                    await writer.execute(
                        update(work)
                        .where(work.c.id == original.work_id)
                        .values(source_json='{"actor":"changed"}')
                    )
                elif change == "task_source":
                    await writer.execute(
                        update(SandboxTaskRunModel)
                        .where(SandboxTaskRunModel.request_id == "original-prompt")
                        .values(source_json='{"work_id":"changed"}')
                    )
                else:
                    other = await reader_person(writer, "1002")
                    await merge_persons(writer, original.person, other)

    client = CleanupClient(database, race)
    database.external_agents = client
    with pytest.raises(ValueError, match=r"cleanup_source_changed|privacy_persons_changed"):
        await forget(database, original.person, entry)()
    assert len(client.calls) == 1
    assert client.calls[0][0] == ("original-prompt",)
    async with database.sessions() as reader:
        assert await reader.get(SandboxTaskRunModel, "original-prompt") is not None
        assert (
            await reader.scalar(select(work.c.id).where(work.c.id == original.work_id))
            == original.work_id
        )


async def reader_person(session, account):
    from yuki.identity.db_models import IdentityBindingModel

    return await session.scalar(
        select(IdentityBindingModel.person_id).where(
            IdentityBindingModel.external_account_id == account
        )
    )


async def test_forget_accepts_same_request_identity_fill_and_real_progress(database):
    original = await privacy_case(database, status="waiting", run_id=False)
    await seed_task(
        database, original.conversation, original.work_id, request="ordinary-terminal", acp=False
    )

    async def progress():
        async with database.immediate_session() as writer:
            row = await writer.get(SandboxTaskRunModel, "original-prompt")
            row.run_id = str(uuid4())
            row.status = "completed"
            updated = json.loads(row.progress_json)
            updated["permission_requests"] = []
            updated["output"] = "normal final progress"
            row.progress_json = json.dumps(updated)
            terminal = await writer.get(SandboxTaskRunModel, "ordinary-terminal")
            terminal.progress_json = '{"kind":"terminal_progress","output":"ordinary output"}'

    database.external_agents = CleanupClient(database, progress)
    assert await PeopleRepository(database).delete_person_id(original.person)


async def test_plain_terminal_forget_does_not_require_manager_cleanup(database):
    original = await privacy_case(database, acp=False)
    client = CleanupClient(database, fail=True)
    database.external_agents = client
    assert await PeopleRepository(database).delete_person_id(original.person)
    assert not client.calls


@pytest.mark.parametrize("entry", ["personal", "admin"])
async def test_new_acp_after_plain_snapshot_is_seen_on_database_only_retry(
    database, monkeypatch, entry
):
    original = await privacy_case(database, acp=False)
    client = CleanupClient(database)
    database.external_agents = client
    prepare = PeopleRepository.prepare_person_forget
    attempts = 0

    async def race(repository, *args, **kwargs):
        nonlocal attempts
        plan = await prepare(repository, *args, **kwargs)
        attempts += 1
        # First is the external preview; second is the deferred WAL snapshot.
        # An actual concurrent writer commits after all its preparation reads.
        if attempts == 2:
            await seed_task(
                database, original.conversation, original.work_id, request="new-acp-prompt"
            )
        return plan

    monkeypatch.setattr(PeopleRepository, "prepare_person_forget", race)
    with pytest.raises(ValueError, match="external_agent_cleanup_unconfirmed"):
        await forget(database, original.person, entry)()
    assert attempts == 3
    assert client.calls == []
    async with database.sessions() as reader:
        assert await reader.get(CanonicalPersonModel, original.person) is not None
        assert await reader.get(SandboxTaskRunModel, "new-acp-prompt") is not None
        assert await reader.scalar(select(work.c.id).where(work.c.id == original.work_id))


@pytest.mark.parametrize("entry", ["personal", "admin"])
async def test_snapshot_retry_does_not_repeat_external_close(database, monkeypatch, entry):
    original = await privacy_case(database)
    client = CleanupClient(database)
    database.external_agents = client
    prepare = PeopleRepository.prepare_person_forget
    attempts = 0

    async def race(repository, *args, **kwargs):
        nonlocal attempts
        plan = await prepare(repository, *args, **kwargs)
        attempts += 1
        if attempts == 2:
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalPersonModel)
                    .where(CanonicalPersonModel.id == original.person)
                    .values(display_name="same original person")
                )
        return plan

    monkeypatch.setattr(PeopleRepository, "prepare_person_forget", race)
    assert await forget(database, original.person, entry)()
    assert attempts == 3
    assert len(client.calls) == 1


@pytest.mark.parametrize("entry", ["personal", "admin"])
async def test_forget_prepares_fresh_redactions_after_external_close(database, entry):
    original = await privacy_case(database)

    async def append_private_reference():
        async with database.immediate_session() as writer:
            writer.add(
                AdminOperationEventModel(
                    actor_user_id="1002",
                    capability="test",
                    operation="test",
                    target_type="test",
                    before_json=json.dumps({"reference": original.person}),
                    after_json="null",
                    success=True,
                    duration_seconds=0,
                    created_at=datetime.now(UTC),
                )
            )

    client = CleanupClient(database, append_private_reference)
    database.external_agents = client
    assert await forget(database, original.person, entry)()
    assert len(client.calls) == 1
    async with database.sessions() as reader:
        audit = await reader.scalar(
            select(AdminOperationEventModel).where(AdminOperationEventModel.operation == "test")
        )
        assert json.loads(audit.before_json) == {"reference": "[已删除用户]"}


async def archive_case(database, tmp_path):
    repository, workers, lease, parent, child = await stack(database, tmp_path)
    await repository.release(lease)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id.in_((parent["id"], child))).values(state="completed")
        )
        await writer.execute(
            update(children).where(children.c.work_id == child).values(notified_revision=100)
        )
    for request, identity in (("root-prompt", parent["id"]), ("child-prompt", child)):
        await seed_task(database, parent["conversation_id"], identity, request=request)
    return repository, workers, parent, child


async def age_tree(database, parent, child):
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id.in_((parent["id"], child)))
            .values(updated=time.time() - 8 * 86400)
        )


async def test_completion_keeps_peer_and_true_archive_closes_the_original_tree(database, tmp_path):
    _, workers, parent, child = await archive_case(database, tmp_path)
    client = CleanupClient(database)
    database.external_agents = client
    await workers.maintain()
    assert client.calls == []
    await age_tree(database, parent, child)
    await workers.maintain()
    assert client.calls[0][0] == ("child-prompt", "root-prompt")
    async with database.sessions() as reader:
        archived = await reader.scalar(
            select(children.c.archived_at).where(children.c.work_id == child)
        )
        assert archived is not None
        assert await reader.scalar(select(work.c.source_json).where(work.c.id == child)) == "{}"


@pytest.mark.parametrize("change", ["unknown", "new_run", "source"])
async def test_archive_keeps_owner_when_close_or_exact_snapshot_is_unconfirmed(
    database, tmp_path, change
):
    _, workers, parent, child = await archive_case(database, tmp_path)
    await age_tree(database, parent, child)

    async def race():
        if change == "new_run":
            await seed_task(database, parent["conversation_id"], child, request="late-prompt")
        elif change == "source":
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(work).where(work.c.id == child).values(source_json='{"changed":true}')
                )

    client = CleanupClient(database, race, fail=change == "unknown")
    database.external_agents = client
    with pytest.raises(ValueError, match="external_agent_cleanup_"):
        await workers.maintain()
    assert len(client.calls) == 1
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(children.c.archived_at).where(children.c.work_id == child))
            is None
        )
        assert await reader.scalar(select(work.c.source_json).where(work.c.id == child)) != "{}"
