"""Actorless plugin wakeups keep real sends, and cannot archive anonymously."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from tests.conftest import build_harness, make_settings
from tests.support.background_authority import approve_background_plugin
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel, ToolArtifactModel
from qq_ai_bot.runtime.effect_outcomes import ResultCapture, current_result_capture
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.trigger import ExternalEventTurnTrigger
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.tool_results.access import ArtifactAccess
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


@pytest.mark.parametrize(
    ("budget", "artifacts_enabled"), [(24000, True), (128, False), (128, True)]
)
async def test_plugin_wakeup_send_receipt_only_requires_actor_for_actual_archive(
    database, tmp_path, monkeypatch, budget, artifacts_enabled
):
    env = await social_env(database, tmp_path)
    # Background sends now recheck the current installation and canonical
    # target grant at the actual Social claim, including this direct fixture.
    await approve_background_plugin(
        database,
        plugin_id="test.result-access",
        bot_user_id="80001",
        group_id="20001",
        creator_user_id="10001",
    )
    scope = ConversationScope.group(env.bot.self_id, "20001")
    external = await env.service.writer.append_external(
        scope=scope,
        platform_message_id="plugin-result-source",
        source_plugin_id="test.result-access",
        external_source="fixture",
        external_event_key="one-original-event",
        external_event_type="fixture.created",
        external_payload={"id": "one-original-event"},
        external_target_id="20001",
        content="A plugin asks the main agent to acknowledge this update.",
        occurred_at=datetime.now(UTC),
    )
    responses = iter(
        (
            ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "original-send", ToolFunction("send_message", '{"text":"已收到更新。"}')
                    ),
                ),
            ),
            ChatResponse("", 0),
        )
    )
    provider = FakeLLMProvider(lambda _: next(responses))
    harness = build_harness(
        database,
        make_settings(
            database.url,
            runtime_work_enabled=False,
            agent_tool_result_max_characters=24000,
            tooling_result_token_budget=budget // 4,
            tooling_result_artifact_enabled=artifacts_enabled,
        ),
        provider,
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    store = ToolArtifactRepository(database, tmp_path / "tool-artifacts", retention_seconds=60)
    chat._tool_artifacts = store
    event = await harness.ledger.get_event(external.event.id)
    state = await harness.conversation_scopes.get(scope)
    config = await chat._runtime_config.snapshot(group_id="20001")
    actor_checks = []
    original = ToolRuntime.require_actor

    def require_actor(runtime):
        if runtime.origin is TurnOrigin.PLUGIN_BACKGROUND:
            actor_checks.append(runtime)
            assert runtime.inbound is None and runtime.actor_context is None
            assert not runtime.actor_user_id and not runtime.actor_is_superuser
        return original(runtime)

    monkeypatch.setattr(ToolRuntime, "require_actor", require_actor)
    async with chat._turn_coordinator.background_turn(scope.key) as token:
        snapshot = ConversationTurnSnapshot(
            state.id,
            scope.key,
            state.generation,
            event.id,
            token.version,
            transport_scope_key=scope.key,
        )
        invocation = chat.generate_main_agent_wakeup(
            event=event,
            trigger=ExternalEventTurnTrigger(
                plugin_id="test.result-access",
                source_event_id=event.id,
                target_type="group",
                target_id="20001",
                agent_intent="Acknowledge once with the send tool.",
            ),
            identity=scope,
            runtime=config,
            turn_token=token,
            turn_snapshot=snapshot,
            gateway=env.bot,
            space_id=env.space,
            presence_id=env.presence,
            conversation_id=event.canonical_conversation_id,
        )
        if artifacts_enabled and budget == 128:
            with pytest.raises(PermissionError, match="tool_actor_unavailable"):
                await invocation
            assert len(provider.requests) == 1 and len(actor_checks) == 1
        else:
            result = await invocation
            assert result.model_requests == 2 and result.tool_calls_used == 1
            assert not actor_checks
            replies = [
                message for message in provider.requests[1].messages if message.role == "tool"
            ]
            assert len(replies) == 1 and replies[0].tool_call_id == "original-send"
            receipt = json.loads(replies[0].content)
            assert receipt["ok"] is True
            assert "artifact_handle" not in receipt
            if artifacts_enabled:
                assert receipt["data"]["status"] == "succeeded"
            else:
                assert receipt["truncated"] is True
    # Rendering cannot invalidate or repeat the already confirmed transport effect.
    assert [action for action, _ in env.bot.calls].count("send_group_msg") == 1
    async with database.sessions() as session:
        operations = (await session.scalars(select(SocialOperationModel))).all()
        assert len(operations) == 1
        operation = operations[0]
        assert operation.status == "succeeded"
        assert (
            operation.source_turn_id == f"{event.canonical_conversation_id}:plugin-event:{event.id}"
        )
        assert operation.event_id is not None and operation.platform_reference
        delivered = await session.get(ChatEventModel, operation.event_id)
        assert delivered.content == "已收到更新。" and delivered.caused_by_event_id == event.id
        assert not (await session.scalars(select(ToolArtifactModel))).all()
    assert not (tmp_path / "tool-artifacts").exists()


@pytest.mark.parametrize("name", ["web_search", "read_webpage"])
async def test_small_research_resolves_original_scope_and_archives_complete_result(name):
    access = ArtifactAccess("original-conversation", 3, "original-person")
    checks = []

    def resolve():
        checks.append(access)
        return access

    writer = SimpleNamespace(write_artifact=AsyncMock(return_value="immutable-original"))
    outcome = ToolExecutionResult(ok=True, data={"text": "complete research"}, tool_name=name)
    rendered = await ToolResultBudgeter(
        max_characters=24000, artifacts=writer, artifact_access_resolver=resolve
    ).render(outcome)
    assert checks == [access] and rendered.artifact_id == "immutable-original"
    writer.write_artifact.assert_awaited_once()
    written = writer.write_artifact.await_args.kwargs
    assert written["access"] is access
    assert json.loads(written["content"])["data"] == outcome.data


async def test_oversized_artifact_read_does_not_resolve_or_recursively_archive():
    def resolve():
        raise AssertionError("paged artifact reads must not acquire a new archive identity")

    writer = SimpleNamespace(write_artifact=AsyncMock())
    rendered = await ToolResultBudgeter(
        max_characters=512, artifacts=writer, artifact_access_resolver=resolve
    ).render(
        ToolExecutionResult(
            ok=True,
            data="original-page " * 1000,
            provider_id="artifacts",
            tool_name="read_tool_artifact",
        )
    )
    assert rendered.truncated and rendered.artifact_id is None
    writer.write_artifact.assert_not_awaited()


async def test_archive_identity_rejection_keeps_original_typed_outcome_without_anonymous_write():
    def resolve():
        raise PermissionError("tool_actor_unavailable")

    writer = SimpleNamespace(write_artifact=AsyncMock())
    outcome = ToolExecutionResult(
        ok=True, data="complete original receipt " * 1000, mutation_committed=True
    )
    capture = ResultCapture("original-work", "original-effect")
    token = current_result_capture.set(capture)
    try:
        with pytest.raises(PermissionError, match="tool_actor_unavailable"):
            await ToolResultBudgeter(
                max_characters=512, artifacts=writer, artifact_access_resolver=resolve
            ).render(outcome)
        assert capture.outcome is outcome and capture.outcome.mutation_committed is True
        assert capture.artifact_handle is None
        writer.write_artifact.assert_not_awaited()
    finally:
        current_result_capture.reset(token)


@pytest.mark.parametrize(
    "web_kind",
    [
        "pages",
        "gemini_summary",
        "gemini_run",
        "gemini_revoked",
        "gemini_late_revoked",
        "gemini_deadline",
    ],
)
async def test_automation_research_keeps_source_tail_and_filters_history_before_requested_limit(
    database, tmp_path, web_kind
):
    import asyncio
    from datetime import timedelta

    from tests.support.automation_unified_delivery_helpers import setup_run

    from qq_ai_bot.automation.models import AutomationScript, RunStatus
    from qq_ai_bot.automation.service import AutomationService
    from qq_ai_bot.automation.work_cursor import load
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.domain.messages import CitationOrigin, ResponseCitation
    from qq_ai_bot.domain.tool_actor import ToolActor
    from qq_ai_bot.identity.db_models import CanonicalPersonModel
    from qq_ai_bot.model_runtime.models import ModelProfile
    from qq_ai_bot.runtime.work_budget_schema import automation_budgets
    from qq_ai_bot.web.bridge_state import BridgeState
    from qq_ai_bot.web.gemini_bridge import GeminiSearchBridge
    from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource

    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    writer = case.chat._ledger._writer
    scope = ConversationScope.group("80001", "20001")
    occurred = datetime(2026, 10, 11, 8, tzinfo=UTC)
    body = "needle " + "history original " * 200 + "HISTORY_TAIL"
    for index in range(101):
        await writer.append(
            scope=scope,
            platform_message_id=f"matching-{index}",
            sender_user_id="10001",
            direction="inbound",
            content=body,
            occurred_at=occurred,
        )
    suppressed = await writer.append(
        scope=scope,
        platform_message_id="suppressed-match",
        sender_user_id="10001",
        direction="inbound",
        content="needle excluded",
        occurred_at=occurred,
    )
    async with database.immediate_session() as session:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == suppressed.event.id)
            .values(suppression_status="duplicate", utterance_fingerprint="fixture-duplicate")
        )
    await writer.append(
        scope=scope,
        platform_message_id="newest-nonmatch",
        sender_user_id="10001",
        direction="inbound",
        content="newer unrelated message",
        occurred_at=occurred + timedelta(minutes=1),
    )
    event = await writer.append(
        scope=scope,
        platform_message_id="research-source",
        sender_user_id="10001",
        direction="inbound",
        content="Read the original evidence",
        occurred_at=occurred + timedelta(minutes=2),
    )
    title = "original title " * 80
    content = "original source " * 700 + "SOURCE_TAIL"
    summary = "provider original " * 700 + "SUMMARY_TAIL"
    url = "https://example.com/" + "x" * 2200
    requests = []
    reads = []
    queued = asyncio.Event()
    queued_request = 1 if web_kind == "gemini_revoked" else 2
    sources = tuple(
        WebSearchSource(
            str(index),
            title,
            f"https://example.com/{index}",
            "example.com",
            content,
            content,
        )
        for index in range(7)
    )
    cancelled = []

    class NativeProvider(FakeLLMProvider):
        async def complete(self, request):
            try:
                return await super().complete(request)
            except asyncio.CancelledError:
                cancelled.append(request)
                raise

    search_provider = NativeProvider(
        lambda _: ChatResponse(
            summary,
            0,
            citations=tuple(
                ResponseCitation(
                    url=source.url,
                    title=title,
                    origin=CitationOrigin.ANNOTATION,
                )
                for source in sources
            ),
        ),
        delay_seconds=2 if web_kind == "gemini_deadline" else 0,
    )
    bridge = GeminiSearchBridge(
        profile=ModelProfile(
            id="actual-search",
            provider="fake",
            model="fake",
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=32768,
        ),
        credential="synthetic",
        provider=search_provider,
        state=BridgeState(tmp_path / "search.sqlite3"),
    )

    class Web:
        async def search(self, request):
            requests.append(request)
            if web_kind in {"gemini_revoked", "gemini_late_revoked"} and (
                len(requests) == queued_request
            ):
                await bridge.slot.acquire()
                queued.set()
            if web_kind != "pages":
                return await bridge.search(request)
            return WebSearchResponse(request.query, sources, None, 0, provider_summary=summary)

        async def extract(self, target, question):
            reads.append((target, question))
            return WebSearchSource("read", title, target, "example.com", content, content)

    handlers = case.executor._registry.require("yuki.agent").handler.__self__
    handlers._web = Web()
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "original research results",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "current_group"},
            "limits": {"max_llm_calls": 1}
            if web_kind == "gemini_run"
            else {"timeout_seconds": 1}
            if web_kind == "gemini_deadline"
            else {},
            "steps": [
                {"id": "search", "call": "web.search", "arguments": {"query": "q" * 701}},
                {
                    "id": "read",
                    "call": "web.read_page",
                    "arguments": {"url": url, "question": "q" * 1201},
                },
                {
                    "id": "history",
                    "call": "history.search",
                    "arguments": {
                        "keyword": "needle",
                        "after": "2026-10-11T16:00:00",
                        "before": "2026-10-11T16:00:30",
                        "limit": 101,
                    },
                },
                {
                    "id": "tail",
                    "call": "web.search",
                    "arguments": {"query": "${search.provider_summary}"},
                },
            ],
        }
    )
    service = AutomationService(
        settings=case.executor._settings,
        repository=case.repository,
        registry=case.executor._registry,
        time_service=case.chat._time,
    )
    row = await service.create(
        script,
        actor=ToolActor(
            user_id="10001",
            bot_user_id="80001",
            group_id="20001",
            origin=TurnOrigin.USER_MESSAGE,
            instruction="Read the original evidence",
            event_id=event.event.id,
            person_id=case.env.person,
            conversation_id=case.env.context.conversation_id,
        ),
        conversation_key=scope.key,
    )
    run = await case.repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=case.clock.now(),
    )
    try:
        if web_kind in {"gemini_revoked", "gemini_late_revoked"}:
            executing = asyncio.create_task(case.executor.execute(row, run))
            await queued.wait()
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalPersonModel)
                    .where(CanonicalPersonModel.id == case.env.person)
                    .values(enabled=False)
                )
            bridge.slot.release()
            result = await executing
        else:
            result = await case.executor.execute(row, run)
    finally:
        await bridge.close()
    expected_models = {
        "pages": 0,
        "gemini_summary": 2,
        "gemini_run": 1,
        "gemini_revoked": 0,
        "gemini_late_revoked": 1,
        "gemini_deadline": 1,
    }[web_kind]
    assert result.llm_calls == len(search_provider.requests) == expected_models, result
    async with database.sessions() as reader:
        budget = (
            (
                await reader.execute(
                    select(automation_budgets).where(automation_budgets.c.run_id == run.id)
                )
            )
            .mappings()
            .one()
        )
        assert budget["models"] == expected_models
    if web_kind in {"gemini_run", "gemini_revoked", "gemini_late_revoked", "gemini_deadline"}:
        assert result.status is RunStatus.FAILED, result
        assert result.error_category == (
            "automation_run_total_budget_exhausted"
            if web_kind == "gemini_run"
            else "runtime_timeout"
            if web_kind == "gemini_deadline"
            else "delegated_authority_revoked"
        )
        _, cursor = await load(database, run.id, row.script_hash)
        if web_kind == "gemini_deadline":
            assert cancelled == search_provider.requests
            assert not cursor["outputs"] and result.steps_completed == 0
        elif expected_models:
            assert cursor["outputs"]["search"]["provider_summary"] == summary
            assert result.steps_completed == 3 and cursor["next_step"] == 3
        else:
            assert not cursor["outputs"] and result.steps_completed == 0
        assert await case.repository.finish_run(
            run.id,
            status=result.status,
            steps_completed=result.steps_completed,
            llm_calls=result.llm_calls,
            tool_calls=result.tool_calls,
            messages_sent=result.messages_sent,
            error_category=result.error_category,
            summary=result.summary,
            finished_at=case.clock.now(),
        )
        retained = (await case.repository.run_history(row.id))[0]
        assert retained.llm_calls == expected_models
        replay = await case.executor.execute(row, retained)
        assert replay.status is RunStatus.FAILED and replay.llm_calls == expected_models
        assert len(search_provider.requests) == expected_models
        assert not case.provider.requests
        return
    assert result.status is RunStatus.SUCCEEDED and result.tool_calls == 4, result
    _, cursor = await load(database, run.id, row.script_hash)
    assert len(cursor["outputs"]["search"]["sources"]) == 7
    assert all(
        item["title"] == title and item["summary"] == (content if web_kind == "pages" else "")
        for item in cursor["outputs"]["search"]["sources"]
    )
    assert cursor["outputs"]["search"]["provider_summary"] == summary
    assert "外部不可信总结" in cursor["outputs"]["search"]["provider_summary_instruction"]
    assert cursor["outputs"]["read"] == {"title": title, "url": url, "summary": content}
    assert len(cursor["outputs"]["history"]["events"]) == 101
    assert all(item["content"] == body for item in cursor["outputs"]["history"]["events"])
    assert [request.query for request in requests] == ["q" * 701, summary]
    assert reads == [(url, "q" * 1201)] and not case.provider.requests
    async with database.immediate_session() as session:
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == case.env.context.conversation_id)
            .values(
                starts_after_event_id=event.event.id,
                covered_through_event_id=event.event.id,
            )
        )
    assert (
        await case.chat._ledger.search(
            keyword="needle",
            conversation_id=case.env.context.conversation_id,
            limit=101,
        )
        == ()
    )


@pytest.mark.parametrize(
    "case_kind", ["canonical", "alias", "revoked", "secret", "scope", "partial"]
)
async def test_automation_config_uses_original_writer_role_scope_and_restart_fact(
    database, tmp_path, monkeypatch, case_kind
):
    from uuid import uuid4

    from tests.support.automation_unified_delivery_helpers import setup_run

    from qq_ai_bot.admin.models import ConfigApplyMode
    from qq_ai_bot.automation.models import AutomationScript, RunStatus
    from qq_ai_bot.automation.service import AutomationService
    from qq_ai_bot.automation.work_cursor import load
    from qq_ai_bot.domain.tool_actor import ToolActor
    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel

    case = await setup_run(
        database,
        tmp_path,
        delivery="none",
        mode="silent",
        superusers_csv="10001",
    )
    key = (
        "定时任务开关"
        if case_kind == "alias"
        else "llm.api_key"
        if case_kind == "secret"
        else "automation.enabled"
    )
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "original authorized config writer",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "current_group"},
            "steps": [
                {
                    "id": "configure",
                    "call": "config.set",
                    "arguments": {
                        "key": key,
                        "value": "synthetic-secret" if case_kind == "secret" else False,
                        "scope_type": "group" if case_kind == "scope" else "global",
                        "scope_id": "20001" if case_kind == "scope" else "",
                    },
                },
                *(
                    [
                        {
                            "id": "read",
                            "call": "history.search",
                            "arguments": {"keyword": "Save", "limit": 1},
                        }
                    ]
                    if case_kind == "partial"
                    else []
                ),
            ],
        }
    )
    event = await case.chat._ledger._writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="original-config-source",
        sender_user_id="10001",
        direction="inbound",
        content="Save the explicitly delegated configuration change",
    )
    service = AutomationService(
        settings=case.executor._settings,
        repository=case.repository,
        registry=case.executor._registry,
        time_service=case.chat._time,
    )
    row = await service.create(
        script,
        actor=ToolActor(
            user_id="10001",
            bot_user_id="80001",
            group_id="20001",
            origin=TurnOrigin.USER_MESSAGE,
            instruction="Save the explicitly delegated configuration change",
            event_id=event.event.id,
            person_id=case.env.person,
            conversation_id=case.env.context.conversation_id,
        ),
        conversation_key="group:80001:20001",
    )
    run = await case.repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=case.clock.now(),
    )
    if case_kind == "partial":
        from qq_ai_bot.automation import work_cursor

        save = work_cursor.save

        async def stop_after_saved_step(database, run_id, script_hash, phase, payload, **kwargs):
            await save(database, run_id, script_hash, phase, payload, **kwargs)
            if phase == "ready" and payload["next_step"] == 1:
                raise RuntimeError("Host stopped after the original step cursor was saved")

        with monkeypatch.context() as interrupted:
            interrupted.setattr(work_cursor, "save", stop_after_saved_step)
            first = await case.executor.execute(row, run)
        assert first.status is RunStatus.FAILED and first.steps_completed == first.tool_calls == 1
    if case_kind in {"revoked", "partial"}:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(IdentityBindingModel)
                .where(
                    IdentityBindingModel.person_id == case.env.person,
                )
                .values(status="disabled")
            )
            writer.add(
                IdentityBindingModel(
                    id=str(uuid4()),
                    person_id=case.env.person,
                    platform="qq",
                    external_account_id="19002",
                    status="active",
                    revision=1,
                    first_seen_at=case.clock.now(),
                    last_seen_at=case.clock.now(),
                    created_at=case.clock.now(),
                    updated_at=case.clock.now(),
                )
            )
    result = await case.executor.execute(row, run)
    success = case_kind in {"canonical", "alias", "partial"}
    assert result.status is (RunStatus.SUCCEEDED if success else RunStatus.FAILED), result
    async with database.sessions() as reader:
        overrides = (await reader.scalars(select(RuntimeConfigOverrideModel))).all()
        assert len(overrides) == int(success)
        if success:
            assert overrides[0].config_key == "automation.enabled"
            assert json.loads(overrides[0].value_json) is False
    if success:
        _, cursor = await load(database, run.id, row.script_hash)
        assert cursor["outputs"]["configure"] == {
            "key": "automation.enabled",
            "after": False,
            "ok": True,
        }
        effective = await case.chat._runtime_config.get_effective("automation.enabled")
        assert effective.value is True
        assert len(await case.chat._runtime_config.pending_restart_entries()) == 1
        assert effective.apply_mode is ConfigApplyMode.RESTART_REQUIRED
        assert case.executor._settings.automation_enabled is True
        if case_kind == "partial":
            assert (
                cursor["outputs"]["read"]["events"][0]["content"]
                == "Save the explicitly delegated configuration change"
            )
            assert result.steps_completed == result.tool_calls == 2
        await case.executor.execute(row, run)
    assert not case.provider.requests and not [
        action for action, _ in case.env.bot.calls if action.startswith("send_")
    ]


@pytest.mark.parametrize("scope_kind", ["person", "group"])
async def test_automation_memory_reads_original_requested_101_facts(database, tmp_path, scope_kind):
    from tests.support.automation_unified_delivery_helpers import setup_run

    from qq_ai_bot.automation.models import AutomationScript, RunStatus
    from qq_ai_bot.automation.service import AutomationService
    from qq_ai_bot.automation.work_cursor import load
    from qq_ai_bot.domain.tool_actor import ToolActor
    from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, MemorySourceType
    from qq_ai_bot.memory.models import MemoryFactCreate

    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    originals = []
    for index in range(101):
        content = f"original {scope_kind} fact {index}"
        await case.chat._memories.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON
                if scope_kind == "person"
                else MemoryScopeType.GROUP,
                subject_user_id="10001" if scope_kind == "person" else None,
                group_id="20001" if scope_kind == "group" else None,
                kind=MemoryKind.FACT,
                memory_key=f"original-{index}",
                category="test",
                content=content,
                importance=4,
                confidence=0.9,
                source_type=MemorySourceType.AUTOMATIC,
            )
        )
        originals.append(content)
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "original requested memory page",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "current_group"},
            "steps": [
                {
                    "id": "facts",
                    "call": f"memory.get_{scope_kind}",
                    "arguments": {
                        "user_id" if scope_kind == "person" else "group_id": "10001"
                        if scope_kind == "person"
                        else "20001",
                        "limit": 101,
                    },
                }
            ],
        }
    )
    event = await case.chat._ledger._writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="original-memory-source",
        sender_user_id="10001",
        direction="inbound",
        content="Read the original requested memory page",
    )
    service = AutomationService(
        settings=case.executor._settings,
        repository=case.repository,
        registry=case.executor._registry,
        time_service=case.chat._time,
    )
    row = await service.create(
        script,
        actor=ToolActor(
            user_id="10001",
            bot_user_id="80001",
            group_id="20001",
            origin=TurnOrigin.USER_MESSAGE,
            instruction="Read the original requested memory page",
            event_id=event.event.id,
            person_id=case.env.person,
            conversation_id=case.env.context.conversation_id,
        ),
        conversation_key="group:80001:20001",
    )
    run = await case.repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=case.clock.now(),
    )
    result = await case.executor.execute(row, run)
    assert result.status is RunStatus.SUCCEEDED and result.tool_calls == 1, result
    _, cursor = await load(database, run.id, row.script_hash)
    returned = cursor["outputs"]["facts"]["memories"]
    assert len(returned) == 101 and {item["content"] for item in returned} == set(originals)
    assert not case.provider.requests
