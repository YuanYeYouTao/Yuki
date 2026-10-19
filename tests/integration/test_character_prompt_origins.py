"""Owner file edits affect real new SELF and scheduled Main activations."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from tests.integration.test_current_material_main_contract import _deployment
from tests.integration.test_self_common_capabilities import runtime, self_actor
from tests.support.automation_runtime_helpers import FakeClock

from yuki.character.repository import CharacterRepository
from yuki.client_api.http import attach_client_api
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope
from yuki.domain.tool_actor import ToolActor
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.trigger import SelfInitiativeTrigger


@pytest.mark.parametrize("origin", ["self", "person_automation", "self_automation"])
async def test_new_origin_uses_hot_pair_at_actual_provider_boundary(database, tmp_path, origin):
    system, persona = tmp_path / "system.md", tmp_path / "persona.md"
    system.write_text("OLD_SYSTEM {{YUKI_PERSONA_CORE}}", encoding="utf-8")
    persona.write_text("OLD_PERSONA", encoding="utf-8")
    provider = FakeLLMProvider(lambda _request: "NO_REPLY")
    app, env, _message, _sender = await _deployment(
        database,
        tmp_path,
        provider,
        system_prompt_file=system,
        bot_persona_file=persona,
    )
    clock = FakeClock(datetime.now(UTC))
    app.time_context._clock = clock
    await app.app_gateway.start()
    await app._freeze_main_manifest()
    try:
        identity = app.client_api.identity
        await CharacterRepository(identity).set_owner(
            str(uuid4()), env.person, str(uuid4()), granted=True
        )
        issue = await identity.issue_for_operator(env.person, str(uuid4()), str(uuid4()))
        device = await identity.pair(issue["code"], "real prompt editor")
        headers = {"Authorization": "Bearer " + device["token"]}
        actor = await self_actor(database, env) if origin != "person_automation" else None
        if origin != "self":
            if actor is None:
                async with database.sessions() as session:
                    event = (await session.scalars(select(ChatEventModel))).first()
                actor = ToolActor(
                    user_id=event.sender_user_id,
                    bot_user_id=event.bot_user_id,
                    group_id=event.group_id,
                    origin=TurnOrigin.USER_MESSAGE,
                    instruction=event.content,
                    event_id=event.id,
                    person_id=event.author_person_id,
                    conversation_id=event.canonical_conversation_id,
                    presence_id=event.ingress_presence_id,
                )
            scheduled = await app.automation.create_task(
                {
                    "name": "new origin",
                    "goal": "Use the current prompt pair",
                    "trigger": {"type": "after", "seconds": 1},
                    "delivery": {"target": "none"},
                },
                actor=actor,
                conversation_key=f"conversation:{actor.conversation_id}",
            )
        api = FastAPI()
        attach_client_api(api, lambda: app.client_api)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            current = await client.get("/client/v1/character/prompts", headers=headers)
            assert current.status_code == 200, current.text
            for document in current.json()["data"]["documents"]:
                content = (
                    "NEW_SYSTEM {{YUKI_PERSONA_CORE}}"
                    if document["key"] == "system_prompt"
                    else "NEW_PERSONA"
                )
                saved = await client.put(
                    f"/client/v1/character/prompts/{document['key']}",
                    headers=headers,
                    json={
                        "request_id": str(uuid4()),
                        "expected_revision": document["revision"],
                        "content": content,
                    },
                )
                assert saved.status_code == 200, saved.text
        assert not provider.requests
        if origin == "self":
            source = runtime(actor, env)
            assert source.inbound is None and source.require_actor().person_id is None
            scope = await app.conversation_scopes.get(ConversationScope.group("80001", "20001"))
            config = await app.runtime_config.snapshot(group_id=env.space)
            token = await app.turn_coordinator.notify_message(
                scope.runtime_scope_key, TurnOrigin.SELF_INITIATIVE
            )
            snapshot = ConversationTurnSnapshot(
                conversation_id=scope.id,
                scope_key=scope.runtime_scope_key,
                generation=scope.generation,
                trigger_event_id=None,
                coordinator_version=token.version,
                initiative_run_id=actor.initiative_run_id,
            )

            async def validate():
                assert app.turn_coordinator.is_current(token)

            await app.chat.generate_self_initiative(
                trigger=SelfInitiativeTrigger(
                    run_id=actor.initiative_run_id,
                    conversation_id=scope.id,
                    generation=scope.generation,
                    space_id=env.space,
                    presence_id=env.presence,
                    group_id="20001",
                    bot_user_id="80001",
                    instruction=actor.instruction,
                ),
                runtime=config,
                turn_token=token,
                turn_snapshot=snapshot,
                before_model_request=validate,
                source_runtime=source,
            )
        else:
            observed = []
            generate = app.automation_worker._generate

            async def actual_source(**kwargs):
                source = kwargs["source_runtime"].require_actor()
                observed.append((source.origin, source.principal_kind))
                return await generate(**kwargs)

            app.automation_worker._generate = actual_source
            clock.advance(2)
            due = await app.automation_repository.claim_due(
                worker_id="real-origin-fixture",
                now=clock.now(),
                clock=clock.now,
                lease_seconds=app.settings.automation_lease_seconds,
            )
            assert [row.id for row in due] == [scheduled.id]
            for row in due:
                await app.automation_worker._process_guarded(row)
            history = await app.automation_repository.run_history(scheduled.id)
            assert len(history) == 1 and history[0].status.value == "succeeded"
            assert history[0].version_id == scheduled.version_id
            assert observed == [
                (
                    TurnOrigin.SCHEDULED_AUTOMATION,
                    "person" if origin == "person_automation" else "self",
                )
            ]
        assert len(provider.requests) == 1
        prefix = "\n".join(
            message.content or ""
            for message in provider.requests[0].messages
            if message.role == "system"
        )
        assert "NEW_SYSTEM" in prefix and "NEW_PERSONA" in prefix
        assert "OLD_SYSTEM" not in prefix and "OLD_PERSONA" not in prefix
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)
