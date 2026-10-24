"""Opt-in Desktop management data through actual current domain services."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from yuki.container import ApplicationContainer
from yuki.domain.tool_actor import ToolActor
from yuki.identity.canonical_repository import ensure_account, replace_assignment
from yuki.identity.db_models import CanonicalPersonModel, PresenceModel
from yuki.identity.person_identity import person_family_condition
from yuki.memory.enums import MemoryScopeType
from yuki.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin


async def attach_character_fixture(
    app: FastAPI, container: ApplicationContainer, person: str
) -> None:
    import asyncio

    lose_memory = False
    blocked_request: str | None = None
    queries = asyncio.Event()
    queries.set()

    @app.middleware("http")
    async def original_response_loss(request: Request, call_next):
        nonlocal lose_memory, blocked_request
        candidate = None
        if lose_memory and request.method == "POST" and request.url.path.endswith("/corrections"):
            candidate = await request.json()
        if request.method == "GET" and request.url.path.endswith(f"/requests/{blocked_request}"):
            await queries.wait()
        response = await call_next(request)
        if (
            lose_memory
            and request.method == "POST"
            and request.url.path.endswith("/corrections")
            and response.status_code == 200
        ):
            # The actual domain write has completed. Discard only this HTTP acknowledgement.
            async for _ in response.body_iterator:
                pass
            assert candidate is not None
            blocked_request = candidate["request_id"]
            lose_memory = False
            queries.clear()
            return JSONResponse({"problem": {"code": "unknown"}}, status_code=503)
        return response

    @app.post("/__test/lose-next-memory-response")
    async def lose_next_memory_response() -> dict:
        nonlocal lose_memory
        lose_memory = True
        return {"armed": True}

    @app.post("/__test/release-original-queries")
    async def release_original_queries() -> dict:
        queries.set()
        return {"released": True}

    @app.post("/__test/revoke-character-owner")
    async def revoke_character_owner() -> dict:
        return await container.character.set_owner(
            str(uuid4()), person, str(uuid4()), granted=False
        )

    @app.post("/__test/qq-binding-without-destination")
    async def qq_binding_without_destination() -> dict:
        now = datetime.now(UTC)
        async with container.database.immediate_session() as session:
            account = await ensure_account(session, "100200", platform="qq", now=now)
            owner = await session.get(CanonicalPersonModel, person)
            assert owner is not None
            binding = await replace_assignment(session, account, None, owner, now=now)
            assert binding is not None
            return {
                "binding_id": binding.id,
                "person_id": binding.person_id,
                "platform": binding.platform,
                "external_account_id": binding.external_account_id,
            }

    @app.post("/__test/character-data")
    async def character_data() -> dict:
        async with container.database.read_session() as reader:
            event_id = await reader.scalar(
                select(ChatEventModel.id)
                .join(PresenceModel, PresenceModel.id == ChatEventModel.ingress_presence_id)
                .where(
                    person_family_condition(ChatEventModel.author_person_id, person),
                    ChatEventModel.direction == "inbound",
                    PresenceModel.platform == "app",
                    ChatEventModel.origin == "user_message",
                    ChatEventModel.suppression_status == "keeper",
                )
                .order_by(ChatEventModel.id.desc())
                .limit(1)
            )
        assert event_id is not None, "Character fixture needs an actual user message first"
        event = await container.ledger.get_event(event_id)
        assert event is not None
        context = MemoryMutationContext(
            event=event,
            conversation_key=f"conversation:{event.canonical_conversation_id}",
            turn_origin="user_message",
            delegation_mode="main_agent",
            trigger_actor_user_id=event.sender_user_id,
            actor_person_id=event.author_person_id,
            decision_actor_type=MemoryDecisionActorType.AGENT,
            decision_actor_id="yuki-main-agent",
            executed_by_bot_user_id=event.bot_user_id,
        )
        facts = []
        for index in range(52):
            result = await container.memory_mutations.mutate(
                MemoryMutationRequest(
                    operation="create",
                    target=MemoryMutationTarget(
                        subject_ref="current_speaker", scope_type=MemoryScopeType.PERSON
                    ),
                    new_content=f"已核對的記錄 {index + 1}",
                    memory_key=f"desktop-integration:{index}",
                    category="profile",
                ),
                replace(
                    context, operation_id=f"desktop-character-fixture:{event.id}:person:{index}"
                ),
            )
            assert result.ok, result
            facts.append(result.new_fact_id)
        own = await container.memory_mutations.mutate(
            MemoryMutationRequest(
                operation="create",
                target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
                new_content="我會先核對依據，再記錄結論。",
                memory_key="desktop-self:verify-first",
                category="self_preference",
            ),
            replace(context, operation_id=f"desktop-character-fixture:{event.id}:self"),
        )
        assert own.ok, own
        actor = ToolActor(
            user_id=event.sender_user_id,
            bot_user_id=event.bot_user_id,
            group_id=None,
            origin=TurnOrigin.USER_MESSAGE,
            instruction=event.content,
            event_id=event.id,
            person_id=event.author_person_id,
            conversation_id=event.canonical_conversation_id,
            presence_id=event.ingress_presence_id,
            platform="app",
        )
        arrangement = await container.automation.create_task(
            {
                "name": "整理下週資料",
                "goal": "核對資料並留下摘要",
                "trigger": {"type": "after", "seconds": 604800},
            },
            actor=actor,
            conversation_key=context.conversation_key,
            creation_source_key=f"desktop-character-fixture:{event.id}:automation",
        )
        return {
            "fact_ids": facts,
            "self_fact_id": own.new_fact_id,
            "source_event_id": event.id,
            "source_conversation_id": event.canonical_conversation_id,
            "automation_id": arrangement.id,
        }
