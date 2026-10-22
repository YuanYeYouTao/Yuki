"""Personal projections retain authorization without reading each unrelated object."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from tests.support.event_seed import seed_scoped_event
from tests.support.sqlite_cost import capture_reads, read_vm_steps
from tests.support.workspace_snapshots import snapshot_bytes
from tests.unit.test_admin_api import admin as admin
from tests.unit.test_app_messages import app_env as app_env

from yuki.client_api.errors import ClientError
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import (
    ensure_account,
    find_identity_binding,
    replace_assignment,
)
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects, work
from yuki.social.db_models import SocialOperationModel


async def principal_for(env, database):
    async with database.read_session() as reader:
        person = (await find_identity_binding(reader, "1001")).person_id
    code = await env.identity.issue_for_operator(person, str(uuid4()), str(uuid4()))
    paired = await env.client.post(
        "/client/v1/pair", json={"code": code["code"], "device_label": "projection-cost"}
    )
    assert paired.status_code == 200
    return await env.identity.authenticate(paired.json()["data"]["token"])


async def completed_work_template(env, database):
    """Accept one real original Work; scale only its independent closed siblings."""
    original = (
        await seed_scoped_event(
            env.harness.scoped_events,
            scope=ConversationScope.group("8000", "2001"),
            platform_message_id=str(uuid4()),
            sender_user_id="1001",
            direction="inbound",
            content="original personal Work source",
        )
    ).event
    repository = WorkRepository(database)
    lease = await repository.acquire(original.canonical_conversation_id, 1)
    assert lease is not None
    try:
        accepted = await repository.accept(
            lease,
            source_key=str(uuid4()),
            source={
                "actor_person_id": original.author_person_id,
                "actor_user_id": "1001",
                "principal_kind": "person",
                "origin": "user_message",
                "trigger_event_id": original.id,
                "bot_user_id": "8000",
                "presence_id": original.ingress_presence_id,
                "generation": 1,
                "conversation_id": original.canonical_conversation_id,
            },
            goal="closed original Work",
        )
    finally:
        await repository.release(lease)
    async with database.immediate_session() as writer:
        await writer.execute(
            work.update().where(work.c.id == accepted["id"]).values(state="completed")
        )
    return await repository.get(accepted["id"])


@pytest.mark.parametrize(
    "operation",
    [
        "artifact",
        "home",
    ],
)
async def test_personal_reads_do_not_hydrate_every_unrelated_work(app_env, database, operation):
    env = app_env
    principal = await principal_for(env, database)
    original = await completed_work_template(env, database)
    views = ClientViews(env.identity, ClientWorks(env.identity, env.gateway))
    sizes = (8, 32)
    costs, current_size = [], 1
    for size in sizes:
        rows = [
            dict(original, id=str(uuid4()), source_key=str(uuid4()))
            for _ in range(size - current_size)
        ]
        async with database.immediate_session() as writer:
            await writer.execute(insert(work), rows)
        with capture_reads(database) as reads:
            if operation == "artifact":
                with pytest.raises(ClientError, match="not_found"):
                    await env.files.artifact(principal, str(uuid4()), "0" * 64)
            else:
                result = await views.home(principal)
                assert len(result["works"]) == size
                assert {item["state"] for item in result["works"]} == {"completed"}
        costs.append(len(reads))
        current_size = size
    async with database.read_session() as reader:
        assert (
            dict(
                (await reader.execute(select(work).where(work.c.id == original["id"])))
                .mappings()
                .one()
            )
            == original
        )
    assert not env.calls
    # Output may grow; reading each extra object's auxiliary tables is the regression.
    assert costs[1] - costs[0] < sizes[1] - sizes[0], (sizes, costs)


async def test_exact_artifact_authorization_does_not_scan_unrelated_private_history(
    app_env, database
):
    env = app_env
    principal = await principal_for(env, database)
    original = snapshot_bytes(env.store, "original.txt", b"immutable original artifact")
    source = (
        await seed_scoped_event(
            env.harness.scoped_events,
            scope=ConversationScope.private(
                "yuki-app",
                principal.external_account_id,
                platform="app",
                presence_id=principal.presence_id,
                conversation_id=principal.conversation_id,
            ),
            platform_message_id=str(uuid4()),
            sender_user_id="yuki-app",
            direction="outbound",
            content="original delivery",
            segments=(
                {
                    "type": "file",
                    "data": {
                        "artifact_id": original["artifact_id"],
                        "version": original["sha256"],
                    },
                },
            ),
        )
    ).event
    async with database.read_session() as reader:
        event = await reader.get(ChatEventModel, source.id)
        template = {
            column.name: getattr(event, column.name)
            for column in event.__table__.columns
            if column.name != "id"
        }
    sizes, costs, previous = (32, 128), [], 0
    for size in sizes:
        rows = [
            dict(
                template,
                canonical_event_id=str(uuid4()),
                platform_message_id=str(uuid4()),
                segments_json=json.dumps([{"type": "text", "data": {"text": "unrelated"}}]),
            )
            for _ in range(size - previous)
        ]
        async with database.immediate_session() as writer:
            await writer.execute(insert(ChatEventModel.__table__), rows)
        with capture_reads(database) as reads:
            metadata, body = await env.files.artifact(
                principal, original["artifact_id"], original["sha256"]
            )
        assert metadata["artifact_id"] == original["artifact_id"]
        assert body == b"immutable original artifact"
        costs.append(read_vm_steps(database, reads))
        previous = size
    async with database.read_session() as reader:
        event = await reader.get(ChatEventModel, source.id)
        assert event.author_person_id == source.author_person_id
        assert event.canonical_conversation_id == principal.conversation_id
        assert event.segments_json == template["segments_json"]
    assert not env.calls
    # Four times unrelated history must not cause a corresponding authorization scan.
    assert costs[1] < costs[0] * 2, (sizes, costs)


async def test_sync_batches_activity_for_the_same_persons_entries(app_env, database):
    env = app_env
    principal = await principal_for(env, database)
    views = ClientViews(env.identity, ClientWorks(env.identity, env.gateway))
    sizes, costs, previous = (8, 32), [], 1
    for size in sizes:
        now = datetime.now(UTC)
        async with database.immediate_session() as writer:
            person = await writer.get(CanonicalPersonModel, principal.person_id)
            for _ in range(size - previous):
                account = await ensure_account(writer, str(uuid4()), platform="app", now=now)
                binding = await replace_assignment(writer, account, None, person, now=now)
                await ensure_canonical_conversation(
                    writer,
                    kind="private",
                    primary_scope_key=f"binding:{binding.id}",
                    identity_binding_id=binding.id,
                    person_id=person.id,
                    presence_id=principal.presence_id,
                )
        with capture_reads(database) as reads:
            result = await views.sync(principal)
        assert len(result["conversations"]) == size
        assert {item["evidence"] for item in result["activity"]} == {"insufficient"}
        assert result["works"] == [] and not env.calls
        costs.append(len(reads))
        previous = size
    assert costs[1] - costs[0] < sizes[1] - sizes[0], (sizes, costs)


async def test_work_artifact_authorization_uses_its_original_effect_reference(app_env, database):
    env = app_env
    principal = await principal_for(env, database)
    original = await completed_work_template(env, database)
    artifact = snapshot_bytes(env.store, "work-original.txt", b"original Work artifact")
    event = (
        await seed_scoped_event(
            env.harness.scoped_events,
            scope=ConversationScope.group("8000", "2001"),
            platform_message_id=str(uuid4()),
            sender_user_id="8000",
            direction="outbound",
            content="confirmed original file",
            segments=(
                {
                    "type": "file",
                    "data": {"artifact_id": artifact["artifact_id"], "version": artifact["sha256"]},
                },
            ),
        )
    ).event
    operation = str(uuid4())
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        writer.add(
            SocialOperationModel(
                id=operation,
                source_turn_id=str(uuid4()),
                tool_call_id=str(uuid4()),
                source_conversation_id=event.canonical_conversation_id,
                action="send_message",
                payload_hash="a" * 64,
                target_kind="space",
                target_id=event.canonical_conversation_id,
                presence_id=event.ingress_presence_id,
                status="succeeded",
                event_id=event.id,
                created_at=now,
                updated_at=now,
            )
        )
        await writer.execute(
            insert(effects).values(
                effect_key=str(uuid4()),
                work_id=original["id"],
                kind="social",
                state="accepted",
                receipt_json=json.dumps(
                    {"invocation": {"original_domain_ref": f"social:{operation}"}}
                ),
                created=1.0,
                updated=1.0,
            )
        )
    sizes, costs, previous = (8, 512), [], 0
    for size in sizes:
        async with database.immediate_session() as writer:
            await writer.execute(
                insert(effects),
                [
                    dict(
                        effect_key=str(uuid4()),
                        work_id=original["id"],
                        kind="social",
                        state="accepted",
                        receipt_json=json.dumps(
                            {"invocation": {"original_domain_ref": "social:" + str(uuid4())}}
                        ),
                        created=1.0,
                        updated=1.0,
                    )
                    for _ in range(size - previous)
                ],
            )
        with capture_reads(database) as reads:
            metadata, body = await env.files.artifact(
                principal, artifact["artifact_id"], artifact["sha256"]
            )
        assert (
            metadata["artifact_id"] == artifact["artifact_id"] and body == b"original Work artifact"
        )
        costs.append(read_vm_steps(database, reads))
        previous = size
    assert costs[1] < costs[0] * 2, (sizes, costs)
    assert not env.calls
