"""Original App sources survive child execution, plugin calls and offline transport."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.support.runtime_execution import make_child_executor
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner
from tests.unit.test_personal_entry_contract import admin as admin

from yuki.automation.repository import AutomationRepository
from yuki.automation.service import AutomationService
from yuki.automation.tools import AutomationToolService
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.db_models import CanonicalPersonModel, PresenceModel
from yuki.persistence.models import AutomationModel
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_repository import WorkRepository
from yuki.services.main_agent_contract import MainAgentContract
from yuki.time.service import TimeContextService
from yuki.workspace.short_state import ShortState
from yuki_plugin_sdk.permissions import PluginPermission


def tool(name, arguments, key):
    return ChatResponse(
        "", 0, tool_calls=(ToolCall(key, ToolFunction(name, json.dumps(arguments))),)
    )


async def app_source(env, database, monkeypatch):
    device, person = await owner(env, database)
    chat = env.harness.processor._chat
    captured = []
    original = chat._run_agent

    async def capture(conversation_key, messages, runtime, **kwargs):
        captured.append(runtime)
        return await original(conversation_key, messages, runtime, **kwargs)

    monkeypatch.setattr(chat, "_run_agent", capture)
    env.provider._responder = lambda _: "original App turn"
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "keep this original App source"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    await drain(env)
    assert len(captured) == 1 and captured[0].require_actor().person_id == person
    return chat, captured[0]


async def test_app_automation_create_keeps_canonical_owner_and_original_scene(app_env, database):
    env = app_env
    device, person = await owner(env, database)
    chat = env.harness.processor._chat
    service = AutomationService(
        settings=make_settings(database.url, automation_enabled=True),
        repository=AutomationRepository(database),
        time_service=TimeContextService(default_timezone="Asia/Shanghai"),
    )
    chat.set_automation_tools(AutomationToolService(service))
    arguments = {
        "task": {
            "name": "App reminder",
            "goal": "remind me of tea",
            "trigger": {"type": "after", "seconds": 3600},
            "delivery": {"target": "self_private"},
        }
    }
    results = []

    def respond(request):
        if request.messages[-1].role == "tool":
            results.append(json.loads(request.messages[-1].content))
            return "internal result"
        return tool("automation_manage", {"action": "create", **(arguments)}, "app-reminder")

    env.provider._responder = respond
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "remind me of tea in an hour"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200, response.text
    await drain(env)
    principal = await env.identity.authenticate(device["token"])
    async with database.sessions() as reader:
        row = (await reader.scalars(select(AutomationModel))).one()
    assert results and results[0]["ok"]
    assert row.canonical_creator_person_id == row.canonical_target_person_id == person
    assert (
        json.loads(row.authority_snapshot_json)["canonical_conversation_id"]
        == principal.conversation_id
    )
    from yuki.conversation.canonical_db_models import CanonicalConversationModel

    async with database.sessions() as reader:
        conversation = await reader.get(CanonicalConversationModel, principal.conversation_id)
        presence = await reader.get(PresenceModel, row.canonical_presence_id)
    assert conversation.entry_presence_id == presence.id == row.canonical_presence_id
    assert presence.platform == "app"


async def test_app_plugin_main_uses_real_person_without_qq_reinterpretation(
    app_env, database, monkeypatch
):
    env = app_env
    chat, source = await app_source(env, database, monkeypatch)
    host = HostPluginContext(
        plugin_id="test.app-original-source",
        approved_permissions=(
            PluginPermission.AGENT_RUN,
            PluginPermission.PERSON_CURRENT_READ,
            PluginPermission.GROUP_CURRENT_READ,
        ),
        services=PluginFacadeServices(
            approval_revision="approved",
            ledger=chat._ledger,
            people=env.harness.profiles,
            groups=env.harness.groups,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    before = len(env.provider.requests)
    try:
        with host.invocation_scope(host.plugin_id, source):
            profile = await host.people.get_current()
            assert profile is not None and profile["user_id"] == source.require_actor().person_id
            assert profile["group_id"] is None
            assert await host.groups.get_current() is None
            result = await host.agent.run("use the current App source")
        assert result.ok, result.model_dump()
        assert len(env.provider.requests) == before + 1
        assert any(t.name == "query_chat_history" for t in env.provider.requests[-1].tools)
    finally:
        await host.close_host_resources()


async def test_telegram_host_continuation_quiet_keeps_topics_independent(
    telegram_env, database, tmp_path
):
    from datetime import UTC, datetime

    from tests.integration.test_telegram_runtime import drain as drain_telegram
    from tests.integration.test_telegram_runtime import update
    from tests.support.semantic_participation_host_helpers import _host
    from yuki_participation.self_report import SelfDelta, SelfReport

    from yuki.admin.config_service import RuntimeConfigService
    from yuki.identity.canonical_repository import create_space_binding
    from yuki.persistence.models import ChatEventModel
    from yuki.services.participation_feedback import admission_unit_binding

    env = telegram_env
    host, _ = await _host(database, tmp_path)
    try:
        async with database.immediate_session() as session:
            await create_space_binding(
                session,
                external_space_id="-10042",
                name="explicit mention group",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                now=datetime.now(UTC),
                platform="telegram",
            )
        for identifier, topic in ((1, 10), (2, 20)):
            incoming = update(identifier, chat=-10042, topic=topic, text="original topic input")
            incoming["message"]["date"] = int(datetime.now(UTC).timestamp()) - 40
            await env.gateway.receive(incoming)
        await drain_telegram(env)
        async with database.sessions() as session:
            original_ids = tuple(
                await session.scalars(
                    select(ChatEventModel.id)
                    .where(ChatEventModel.direction == "inbound")
                    .order_by(ChatEventModel.id)
                )
            )
        records = [await env.harness.ledger.get_event(identity) for identity in original_ids]
        contexts = []
        for record in records:
            scene = await host._scene(record.canonical_conversation_id)
            assert scene.platform == "telegram" and scene.thread_id in {"10", "20"}
            item = await host._session(scene)
            await host._hydrate(item)
            binding = await host.binding_for_event(record.id)
            original = host.ordinary.prepare(record, scene.generation, 1, binding, reason="mention")
            assert await host.ordinary.commit(original)
            await host.on_ordinary_admitted(binding, original.admission)
            unit = admission_unit_binding(original.admission)
            focus = item.controller.state.events[f"event:{record.id}"]
            assert item.controller.observe_unit_hint(
                unit,
                SelfReport(
                    run_ref=original.admission.activation_id,
                    sequence=1,
                    response_id=f"synthetic-stay:{record.id}",
                    at=focus.at,
                    source=focus.ref,
                    delta=SelfDelta(engage="stay"),
                ),
            )
            await host._save(item)
            contexts.append((item, original, unit, focus))
        assert contexts[0][0].scene.space_id == contexts[1][0].scene.space_id
        assert contexts[0][0].scene.conversation_id != contexts[1][0].scene.conversation_id
        assert contexts[0][0].scene.scope != contexts[1][0].scene.scope
        for identifier, topic in ((3, 10), (4, 20)):
            incoming = update(
                identifier, chat=-10042, topic=topic, text="new unselected continuation"
            )
            incoming["message"]["date"] = int(datetime.now(UTC).timestamp()) - 20
            await env.gateway.receive(incoming)
        await drain_telegram(env)
        async with database.sessions() as session:
            next_ids = tuple(
                await session.scalars(
                    select(ChatEventModel.id)
                    .where(
                        ChatEventModel.direction == "inbound", ChatEventModel.id > original_ids[-1]
                    )
                    .order_by(ChatEventModel.id)
                )
            )
        before_requests, before_wire = len(env.model.requests), tuple(env.wire)
        first, second = [await host.continuation_for_event(identity) for identity in next_ids]
        assert first[1] == second[1] == "continuation"
        assert await host.continuation_current(first[0])
        assert await host.continuation_current(second[0])
        item, original, unit, focus = contexts[0]
        assert item.controller.observe_unit_hint(
            unit,
            SelfReport(
                run_ref=original.admission.activation_id,
                sequence=2,
                response_id="synthetic-quiet:topic-10",
                at=focus.at,
                source=focus.ref,
                delta=SelfDelta(engage="quiet"),
            ),
        )
        assert not await host.continuation_current(first[0])
        assert await host.continuation_current(second[0])
        processor = env.harness.processor
        processor.set_participation(host)
        processor._runtime_config = RuntimeConfigService(
            database=database,
            settings=make_settings(database.url, conversation_semantic_participation_enabled=True),
        )
        for identity, (binding, reason), expected in (
            (next_ids[0], first, False),
            (next_ids[1], second, True),
        ):
            record = await env.harness.ledger.get_event(identity)
            prepared = host.ordinary.prepare(record, 1, 2, binding, reason=reason)

            async def allowed(session, source=prepared, original_reason=reason):
                return await processor._ordinary_admission_allowed(source, original_reason, session)

            assert await host.ordinary.commit(prepared, admission_allowed=allowed) is expected
        assert await host.ordinary.get(next_ids[0]) is None
        assert (await host.ordinary.get(next_ids[1])).reason == "continuation"
        assert not host._observer.calls  # Synthetic local reports never buy a JEV request.
        assert len(env.model.requests) == before_requests and tuple(env.wire) == before_wire
    finally:
        await host.close()


@pytest.mark.parametrize("code_enabled", [False, True])
@pytest.mark.parametrize("transport", ["offline", "reconnect"])
async def test_app_child_keeps_entry_contract_and_runs_without_connection_gate(
    app_env, database, monkeypatch, code_enabled, transport
):
    env = app_env
    chat = env.harness.processor._chat
    contract = MainAgentContract(chat, ShortState(env.store), code_enabled=code_enabled)
    chat.runtime.runner.main_contract = contract
    chat, runtime = await app_source(env, database, monkeypatch)
    actor = runtime.require_actor()
    repository = WorkRepository(database)
    lease = await repository.acquire(actor.conversation_id, 1)
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_person_id": actor.person_id,
        "actor_user_id": actor.user_id,
        "trigger_event_id": actor.event_id,
        "presence_id": actor.presence_id,
        "bot_user_id": actor.bot_user_id,
        "conversation_id": actor.conversation_id,
        "generation": 1,
    }
    try:
        parent = await repository.accept(
            lease, source_key=f"app-child:{uuid4()}", source=source, goal="original"
        )
        database.subagents_enabled = True
        child_id = await SubagentRepository(repository).start(
            lease,
            parent["id"],
            f"spawn:{uuid4()}",
            {"goal": "check the original App entry"},
        )
    finally:
        await repository.release(lease)
    before = len(env.provider.requests)
    snapshots = []

    def respond(request):
        snapshots.append(request)
        if len(snapshots) == 1:
            if transport == "reconnect":
                env.gateway.registry.connect(
                    env.gateway.handle, provider_id="app", presence_id=actor.presence_id
                )
            return tool(
                "task_control", {"action": "update", "goal": "checked original App"}, "update"
            )
        return tool("task_control", {"action": "complete", "result": "done"}, "complete")

    env.provider._responder = respond
    if transport == "offline":
        env.gateway.registry.disconnect(env.gateway.handle)
    executor = make_child_executor(
        repository,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=contract.definitions,
        router=env.social.router,
    )
    assert await executor.run(child_id) is None
    child = await repository.get(child_id)
    assert child["state"] == "completed"
    assert len(env.provider.requests) == before + 2
    full = await contract.definitions()
    if code_enabled:
        assert "query_chat_history" in contract.script_api.schemas
        assert "read_personal_entry" not in contract.script_api.schemas
    else:
        assert tuple(snapshots[0].tools) == full
    async with database.sessions() as reader:
        presence = await reader.get(PresenceModel, actor.presence_id)
    assert presence.enabled and presence.platform == "app"
    assert json.loads(child["source_json"])["trigger_event_id"] == actor.event_id


async def test_unknown_sdk_send_does_not_block_next_explicit_send(app_env, database, monkeypatch):
    from yuki.gateway.providers.app.social import AppSocialOperations

    env = app_env
    chat, source = await app_source(env, database, monkeypatch)
    original_operations = env.social.router.operations
    attempts = []

    class Transport(AppSocialOperations):
        async def send(self, handle, target, message):
            attempts.append(message.text)
            if len(attempts) == 1:
                raise TimeoutError("synthetic lost native response")
            return await original_operations(route).send(handle, target, message)

    route = await env.social.router.resolve_send_for_conversation(source.effective_conversation_id)
    monkeypatch.setattr(env.social.router, "operations", lambda _: Transport())
    host = HostPluginContext(
        plugin_id="test.explicit-calls",
        approved_permissions=(PluginPermission.MESSAGE_PRIVATE_SEND,),
        services=PluginFacadeServices(ledger=chat._ledger, social=env.social),
    )
    try:
        with host.invocation_scope(host.plugin_id, source):
            first = await host.messages.send_text("first explicit call")
            second = await host.messages.send_text("second explicit call")
        assert first.data.get("uncertain") is True and not first.ok, first.model_dump()
        assert second.ok, second.model_dump()
        assert attempts == ["first explicit call", "second explicit call"]
        assert first.data["operation_id"] != second.data["operation_id"]
    finally:
        await host.close_host_resources()


async def test_telegram_sdk_current_profiles_use_original_person_and_space(
    telegram_env, monkeypatch
):
    from tests.integration.test_telegram_runtime import drain as drain_telegram
    from tests.integration.test_telegram_runtime import update

    env = telegram_env
    chat = env.harness.processor._chat
    captured = []
    original = chat._run_agent

    async def capture(key, messages, runtime, **kwargs):
        captured.append(runtime)
        return await original(key, messages, runtime, **kwargs)

    monkeypatch.setattr(chat, "_run_agent", capture)
    env.model._responder = lambda _: "synthetic private result"
    await env.gateway.receive(update(chat=-10042, topic=10, text="!ai current profile"))
    await drain_telegram(env)
    assert len(captured) == 1
    runtime = captured[0]
    from yuki.memory.models import MemoryFactCreate

    facts = chat._memories
    personal_fact = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            canonical_subject_person_id=runtime.require_actor().person_id,
            memory_key="canonical-person",
            category="test",
            content="TG personal canonical record",
            source_type="automatic",
        )
    )
    group_fact = await facts.remember(
        MemoryFactCreate(
            scope_type="group",
            canonical_subject_space_id=runtime.require_actor().space_id,
            memory_key="canonical-group",
            category="test",
            content="TG group canonical record",
            source_type="automatic",
        )
    )
    host = HostPluginContext(
        plugin_id="test.tg-profiles",
        approved_permissions=(
            PluginPermission.PERSON_CURRENT_READ,
            PluginPermission.PERSON_READ,
            PluginPermission.PERSON_ALIAS_READ,
            PluginPermission.GROUP_CURRENT_READ,
            PluginPermission.MESSAGE_CURRENT_READ,
            PluginPermission.MEMORY_PERSON_READ,
            PluginPermission.MEMORY_GROUP_READ,
            PluginPermission.MEMORY_SEARCH,
        ),
        services=PluginFacadeServices(
            people=env.harness.profiles,
            groups=env.harness.groups,
            ledger=chat._ledger,
            memories=facts,
            memory_context=chat._memory_context,
            runtime_config=chat._runtime_config,
        ),
    )
    try:
        with host.invocation_scope(host.plugin_id, runtime):
            person = await host.people.get_current()
            group = await host.groups.get_current()
            current = await host.messages.get_current()
            assert person is not None and person["user_id"] == runtime.require_actor().person_id
            assert person["group_id"] == runtime.require_actor().space_id
            assert group["group_id"] == runtime.require_actor().space_id
            assert current.person_id == runtime.require_actor().person_id
            assert (
                current.platform == "telegram"
                and current.conversation_id == runtime.effective_conversation_id
            )
            assert isinstance(await host.people.list_aliases(person["user_id"]), tuple)
            (personal,) = await host.memory.list_person(person["user_id"])
            (grouped,) = await host.memory.list_group(group["group_id"])
            assert personal["fact_id"] == personal_fact.id
            assert grouped["fact_id"] == group_fact.id
            assert personal["user_id"] == personal["subject_user_id"] == person["user_id"]
            assert grouped["group_id"] == group["group_id"]
            # SDK 4.0 output IDs remain usable as canonical input selectors.
            personal_search = await host.memory.search(
                "canonical record", scope_type="person", subject_id=personal["user_id"]
            )
            group_search = await host.memory.search(
                "canonical record", scope_type="group", subject_id=grouped["group_id"]
            )
            assert [row["fact_id"] for row in personal_search] == [personal_fact.id]
            assert [row["fact_id"] for row in group_search] == [group_fact.id]
            assert (await host.people.get(personal_search[0]["user_id"]))["user_id"] == person[
                "user_id"
            ]
    finally:
        await host.close_host_resources()


@pytest.mark.parametrize("origin", ["user_message", "self_initiative", "scheduled_automation"])
async def test_group_migration_preserves_original_work_and_native_send(
    telegram_env, database, origin
):
    from datetime import UTC, datetime

    from tests.integration.test_telegram_runtime import drain as drain_telegram
    from tests.integration.test_telegram_runtime import update

    from yuki.conversation.autonomy_repository import AutonomyRepository
    from yuki.services.execution_sources import recover_execution_source

    env = telegram_env
    env.model._responder = lambda _: "original internal activation"
    initial = update(chat=-42, text="!ai retain this original scene")
    initial["message"]["chat"]["type"] = "group"
    await env.gateway.receive(initial)
    await drain_telegram(env)
    chat = env.harness.processor._chat
    chat.runtime.runner.code_mode_settings = chat._settings  # application container binding
    async with database.sessions() as session:
        from yuki.conversation.canonical_db_models import CanonicalConversationModel
        from yuki.persistence.models import ChatEventModel

        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        event_id, conversation_id, generation = event.id, conversation.id, conversation.generation
        person, space = event.author_person_id, conversation.space_id
        binding_id = conversation.space_binding_id
    record = await env.harness.ledger.get_event(event_id)
    from yuki.services.execution_sources import recover_source

    base = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_person_id": person,
        "trigger_event_id": event_id,
        "conversation_id": conversation_id,
        "generation": generation,
        "presence_id": env.gateway.presence_id,
        "bot_user_id": env.gateway.client.self_id,
    }
    recovered = await recover_source(database, conversation_id, base, request_id="original")
    source = base
    if origin == "self_initiative":
        repository = AutonomyRepository(database)
        binding = await repository.ensure_binding(conversation_id, generation)
        binding = await repository.transition(binding, master_enabled=True, external_enabled=True)
        accepted = await repository.accept_host_proposal(
            proposal_id="original-self-before-migration",
            binding=binding,
            owner=binding.effective_owner,
            space_id=space,
            presence_id=env.gateway.presence_id,
            sources=(),
            trigger_kind="intrinsic",
        )
        source = {
            "origin": origin,
            "principal_kind": "self",
            "initiative_run_id": accepted.run.run_id,
            "conversation_id": conversation_id,
            "generation": generation,
            "space_id": space,
            "presence_id": env.gateway.presence_id,
            "bot_user_id": env.gateway.client.self_id,
            "group_id": "-42",
            "instruction": "original self purpose",
        }
    elif origin == "scheduled_automation":
        from yuki.domain.tool_actor import ToolActor

        repository = AutomationRepository(database)
        settings = make_settings(database.url, automation_enabled=True)
        service = AutomationService(
            settings=settings,
            repository=repository,
            time_service=TimeContextService(default_timezone="Asia/Taipei"),
        )
        actor = ToolActor.from_inbound(recovered.inbound(record))
        row = await service.create_task(
            {
                "name": "original schedule",
                "goal": "original purpose",
                "trigger": {"type": "after", "seconds": 3600},
                "delivery": {"target": "current_group"},
            },
            actor=actor,
            conversation_key="original-scene",
        )
        run = await repository.create_run(
            row.id, scheduled_for=row.next_run_at, actual_started_at=datetime.now(UTC)
        )
        source = {
            "owner": "automation",
            "origin": origin,
            "automation_id": row.id,
            "automation_run_id": run.id,
            "script_hash": row.script_hash,
            "parent_execution_id": f"automation:{run.id}:{row.script_hash}",
            "conversation_id": conversation_id,
            "generation": generation,
            "principal_kind": "person",
            "actor_person_id": person,
            "actor_user_id": "42",
            "presence_id": env.gateway.presence_id,
            "space_id": space,
            "bot_user_id": env.gateway.client.self_id,
            "current_group_id": "-42",
            "instruction": "original purpose",
        }
    works = WorkRepository(database)
    lease = await works.acquire(conversation_id, generation)
    try:
        parent = await works.accept(
            lease,
            source_key=(
                f"initiative:{source['initiative_run_id']}"
                if origin == "self_initiative"
                else f"original:{origin}"
            ),
            source=source,
            goal="original goal",
        )
        database.subagents_enabled = True
        child_id = await SubagentRepository(works).start(
            lease, parent["id"], "original-child", {"goal": "finish original goal"}
        )
    finally:
        await works.release(lease)
    before = await works.get(child_id)
    accepted_source = await recover_execution_source(
        database, conversation_id, source, request_id=child_id
    )
    await env.gateway.receive(
        {
            "update_id": 2,
            "message": {"chat": {"id": -42, "type": "group"}, "migrate_to_chat_id": -10042},
        }
    )
    after_source = await recover_execution_source(
        database, conversation_id, source, request_id=child_id
    )
    assert after_source == accepted_source
    env.model._responder = lambda _: ChatResponse(
        "",
        0,
        tool_calls=(
            ToolCall("original-effect", ToolFunction("send", '{"text":"original effect"}')),
            ToolCall("original-finish", ToolFunction("task_control", '{"action":"complete"}')),
        ),
    )
    executor = make_child_executor(
        works,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
    )
    if origin == "self_initiative":
        from yuki.tool_results.recorder import ToolReceiptRepository

        chat._tool_receipts = ToolReceiptRepository(database)
    requests_before_worker = len(env.model.requests)
    assert await executor.run(child_id) is None
    final = await works.get(child_id)
    assert final["state"] == "completed" and final["source_json"] == before["source_json"]
    assert final["model_requests"] == 1 and final["tool_calls"] == 1
    from yuki.runtime.work_budget_schema import budgets

    async with database.sessions() as reader:
        shared = (
            (await reader.execute(select(budgets).where(budgets.c.root_id == parent["id"])))
            .mappings()
            .one()
        )
        assert shared["models"] == shared["tools"] == 1
    async with database.sessions() as session:
        retained = await session.get(CanonicalConversationModel, conversation_id)
        original = await session.get(ChatEventModel, event_id)
        assert retained.space_binding_id == binding_id and retained.space_id == space
        assert original.group_id == "-42" and original.canonical_conversation_id == conversation_id
    sends = [params for method, params in env.wire if method == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == "-10042"
    if origin == "self_initiative":
        from yuki.persistence.models import MemoryToolReceiptModel

        async with database.sessions() as session:
            receipt = (
                await session.scalars(
                    select(MemoryToolReceiptModel).where(
                        MemoryToolReceiptModel.initiative_run_id == source["initiative_run_id"]
                    )
                )
            ).one()
            receipt_id = receipt.id
            assert receipt.tool_name == "send" and receipt.success
            assert receipt.execution_id == child_id and receipt.trigger_event_id is None
            assert receipt.canonical_space_id == space and receipt.canonical_person_id is None
        assert await executor.run(child_id) is None
        assert len(env.model.requests) == requests_before_worker + 1
        assert len([method for method, _ in env.wire if method == "sendMessage"]) == 1
        async with database.sessions() as session:
            assert (
                await session.scalars(
                    select(MemoryToolReceiptModel.id).where(
                        MemoryToolReceiptModel.initiative_run_id == source["initiative_run_id"]
                    )
                )
            ).all() == [receipt_id]


async def test_app_sdk_admin_grants_an_explicit_telegram_topic(
    app_env, telegram_env, database, tmp_path, monkeypatch
):
    from datetime import UTC, datetime

    from tests.integration.test_notification_platform_entries import install
    from tests.integration.test_telegram_runtime import drain as drain_telegram
    from tests.integration.test_telegram_runtime import update

    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
    from yuki.plugin_host.notification_delivery import (
        PlatformNotificationTransport,
        PluginNotificationOutboxWorker,
    )
    from yuki.services.effect_gate import ConversationEffectGate
    from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

    tg = telegram_env
    tg.model._responder = lambda _: "only the original internal result"
    await tg.gateway.receive(update(chat=-10042, topic=10, text="!ai known topic"))
    await drain_telegram(tg)
    async with database.sessions() as reader:
        conversation = await reader.scalar(
            select(CanonicalConversationModel).where(CanonicalConversationModel.kind == "space")
        )
    chat, source = await app_source(app_env, database, monkeypatch)
    async with database.immediate_session() as session:
        (
            await session.get(CanonicalPersonModel, source.require_actor().person_id)
        ).is_superuser = True
    repository = await install(database)
    host = HostPluginContext(
        plugin_id="native-notification",
        approved_permissions=(PluginPermission.NOTIFICATION_PUBLISH,),
        services=PluginFacadeServices(ledger=chat._ledger, notifications=repository),
    )
    target = NotificationTarget(
        target_type="group", target_id=conversation.space_id, conversation_id=conversation.id
    )
    try:
        with host.invocation_scope(host.plugin_id, source):
            grant = await host.notifications.grant_target(target, bot_user_id="")
            assert grant.conversation_id == conversation.id and grant.bot_user_id == "80001"
            assert grant.created_by_user_id == source.require_actor().person_id
            receipt = await host.notifications.publish(
                PublishNotificationRequest(
                    event_key="original-cross-entry",
                    event_type="fixture",
                    external_source="fixture",
                    target=target,
                    occurred_at=datetime.now(UTC),
                    summary="retained original topic",
                    text="topic effect",
                )
            )
        worker = PluginNotificationOutboxWorker(
            repository=repository,
            ledger=tg.harness.ledger,
            artifacts=PluginMediaArtifactStore(database, tmp_path / "cross-entry-media"),
            transport=PlatformNotificationTransport(database=database, router=tg.social.router),
            effect_gate=ConversationEffectGate(),
        )
        await worker._prepare_pending()
        row = await repository.claim_outbox()
        assert row.source_event_id == receipt.source_event_id
        assert row.canonical_presence_id == tg.gateway.presence_id
        await worker._deliver(row)
        sends = [params for method, params in tg.wire if method == "sendMessage"]
        assert (
            len(sends) == 1
            and sends[0]["chat_id"] == "-10042"
            and sends[0]["message_thread_id"] == 10
        )
    finally:
        await host.close_host_resources()
