"""Safety invariants for canonical social effects."""

import asyncio
from uuid import UUID

import pytest

from qq_ai_bot.capabilities.catalog import UnifiedToolCatalog, UnifiedToolCatalogEntry
from qq_ai_bot.capabilities.exposure import AuthorityFirstExposurePlanner
from qq_ai_bot.capabilities.models import CapabilityExposure, CapabilityTrustSource
from qq_ai_bot.capabilities.provider import ChatToolCapabilityProvider
from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.social.models import OperationStatus, SocialError, SocialTarget
from qq_ai_bot.social.repository import SocialOperationRepository
from qq_ai_bot.social.tools import social_tool_definitions


@pytest.mark.asyncio
async def test_social_receipt_claim_replay_and_interrupted_delivery(database: Database) -> None:
    definitions = social_tool_definitions()
    assert definitions == social_tool_definitions()
    descriptors = ChatToolCapabilityProvider(
        definitions, source=CapabilityTrustSource.CORE
    ).descriptors()
    assert len(descriptors) == 6
    assert all(
        descriptor.exposure is CapabilityExposure.DIRECT_ALWAYS for descriptor in descriptors
    )
    assert all(not descriptor.required_permissions for descriptor in descriptors)
    assert all(
        descriptor.allowed_origins
        == frozenset(
            {TurnOrigin.USER_MESSAGE, TurnOrigin.AUTONOMOUS_GROUP, TurnOrigin.SCHEDULED_AUTOMATION}
        )
        for descriptor in descriptors
    )
    catalog = UnifiedToolCatalog(
        entries=tuple(
            UnifiedToolCatalogEntry(
                descriptor=item,
                provider_id="core",
                scope_ids=item.scope_ids,
                compact_description=item.description,
                tags=(),
                searchable_text=item.model_name,
                estimated_schema_tokens=500,
                available=True,
                revision="1",
            )
            for item in descriptors
        ),
        scopes=(),
        revision="1",
    )
    planner = AuthorityFirstExposurePlanner(first_round_hard_cap=1, schema_token_budget=1)
    plan = planner.plan_initial(
        catalog=catalog,
        requestable_ids=frozenset({"find_contacts"}),
        hits=(),
        memory_view=None,
        kernel_tools=(),
        query="unrelated",
        artifact_available=False,
        reply_target_available=False,
    )
    assert {item.descriptor.model_name for item in plan.entries} == {
        tool.name for tool in definitions
    }
    assert plan.callable_ids == frozenset({"find_contacts"})
    async with database.sessions() as session, session.begin():
        person = await ensure_person(session, "10001")
        presence = await ensure_presence(session, "80001")
        conversation = await ensure_canonical_conversation(
            session, kind="private", primary_scope_key="private:80001:10001", person_id=person
        )
    repository = SocialOperationRepository(database)
    target = SocialTarget(kind="person", id=UUID(person))

    async def prepare(text: str = "hello"):
        return await repository.prepare(
            source_turn_id="turn-1",
            tool_call_id="call-1",
            source_conversation_id=conversation.conversation_id,
            action="send_private_message",
            target=target,
            payload={"text": text},
        )

    receipt = await prepare()
    assert await prepare() == receipt
    with pytest.raises(SocialError, match="idempotency_conflict"):
        await prepare("changed")
    claims = await asyncio.gather(
        *[repository.claim(receipt.operation_id, presence_id=presence) for _ in range(2)]
    )
    assert sum(claims) == 1
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.sessions() as session, session.begin():
            await repository.finish(
                receipt.operation_id,
                status=OperationStatus.SUCCEEDED,
                platform_reference="123",
                session=session,
            )
            raise RuntimeError("rollback")
    assert (await repository.get(receipt.operation_id)).status == OperationStatus.EXECUTING
    assert await repository.recover_interrupted() == 1
    recovered = await prepare()
    assert recovered.status == OperationStatus.UNCERTAIN
    assert recovered.error_category == "process_interrupted"
    assert not await repository.claim(receipt.operation_id, presence_id=presence)
    assert await repository.recover_interrupted() == 0
    async with database.sessions() as session, session.begin():
        with pytest.raises(SocialError, match="invalid_transition"):
            await repository.finish(
                receipt.operation_id,
                status=OperationStatus.SUCCEEDED,
                platform_reference="123",
                session=session,
            )
