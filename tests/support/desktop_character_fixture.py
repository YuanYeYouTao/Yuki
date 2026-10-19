"""Opt-in, loopback-only data setup through the real domain services."""

from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import select

from yuki.character.repository import CharacterRepository
from yuki.container import ApplicationContainer
from yuki.domain.tool_actor import ToolActor
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
    database = container.database
    await CharacterRepository(container.client_api.identity).set_owner(
        str(uuid4()), person, str(uuid4()), granted=True
    )

    @app.post("/__test/revoke-character-owner")
    async def revoke_owner():
        return await CharacterRepository(container.client_api.identity).set_owner(
            str(uuid4()), person, str(uuid4()), granted=False
        )

    @app.post("/__test/character-data")
    async def character_data():
        async with database.sessions() as reader:
            event_id = await reader.scalar(
                select(ChatEventModel.id)
                .where(
                    ChatEventModel.author_person_id == person,
                    ChatEventModel.direction == "inbound",
                )
                .order_by(ChatEventModel.id.desc())
                .limit(1)
            )
        assert event_id is not None
        event = await container.ledger.get_event(event_id)
        assert event is not None
        context = MemoryMutationContext(
            event=event,
            conversation_key=f"private:{event.sender_user_id}",
            turn_origin="user_message",
            delegation_mode="main_agent",
            trigger_actor_user_id=event.sender_user_id,
            actor_person_id=person,
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
                    memory_key=f"desktop-integration:{uuid4()}",
                    category="profile",
                ),
                context,
            )
            assert result.ok, result
            facts.append(result.new_fact_id)
        result = await container.memory_mutations.mutate(
            MemoryMutationRequest(
                operation="create",
                target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
                new_content="我會先核對依據，再記錄結論。",
                memory_key=f"desktop-self:{uuid4()}",
                category="self_preference",
            ),
            context,
        )
        assert result.ok, result
        actor = ToolActor(
            user_id=event.sender_user_id,
            bot_user_id=event.bot_user_id,
            group_id=None,
            origin=TurnOrigin.USER_MESSAGE,
            instruction=event.content,
            event_id=event.id,
            person_id=person,
            conversation_id=event.canonical_conversation_id,
            presence_id=event.ingress_presence_id,
            platform="app",
        )
        arrangement = await container.automation.create_task(
            {
                "name": "整理下週資料",
                "goal": "核對資料並留下摘要",
                "trigger": {"type": "after", "seconds": 604800},
                "delivery": {"target": "none"},
            },
            actor=actor,
            conversation_key=context.conversation_key,
        )
        return {
            "fact_ids": facts,
            "self_fact_id": result.new_fact_id,
            "source_event_id": event.id,
            "source_conversation_id": event.canonical_conversation_id,
            "automation_id": arrangement.id,
        }
