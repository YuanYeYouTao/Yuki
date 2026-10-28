"""Code's real driver, child journal and archives retain original history sources."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.integration.test_codemode_lifecycle_recovery import FakeEngine, FakeRun, environment
from tests.support.codemode_cases import TOOLS, effect_rows, outer_call, requires_worker
from tests.support.event_seed import seed_scoped_event

from yuki.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from yuki.codemode.api_projection import project
from yuki.codemode.driver import CodeModeDriver
from yuki.codemode.driver_types import EngineOutcome
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatTool
from yuki.persistence.models import ToolArtifactModel
from yuki.runtime.effect_outcomes import current_result_capture
from yuki.services.history_query import history_schema
from yuki.services.invocation_service import InvocationService
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.social.tools import social_tool_definitions
from yuki.tool_results.artifacts import ToolArtifactRepository


class HistoryRun(FakeRun):
    def event(self):
        value = super().event()
        return (
            replace(
                value, output={"copied": ["source-alpha", "source-beta"], "padding": "x" * 3000}
            )
            if value.status == "completed"
            else value
        )


class HistoryEngine(FakeEngine):
    def run(self, _names):
        return HistoryRun(calls=self.calls)


async def source_environment(database, tmp_path, native):
    env = await environment(database, tmp_path)
    social = env.test_case.env
    sources = []
    for name in ("source-alpha", "source-beta"):
        row = await seed_scoped_event(
            social.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            origin_key=name,
            sender_user_id="10001",
            direction="inbound",
            content=name,
        )
        sources.append(row.event.id)
    env.host.api = project(
        (
            *TOOLS,
            next(tool for tool in social_tool_definitions() if tool.name == "find_contacts"),
            ChatTool("query_chat_history", "history", history_schema()),
        ),
        "manifest-test",
    )
    store = ToolArtifactRepository(database, tmp_path / "code-artifacts", retention_seconds=600)
    runtime = SimpleNamespace(
        runtime_config=SimpleNamespace(
            tooling=SimpleNamespace(
                result_artifact_enabled=True,
                result_artifact_retention_seconds=600,
            )
        ),
        require_actor=lambda: SimpleNamespace(person_id=social.person, principal_kind="person"),
        turn_snapshot=SimpleNamespace(generation=1),
        effective_conversation_id=social.context.conversation_id,
        memory_allowed_scopes=(),
        context_plugin_id=None,
        context_read_contract=None,
    )
    backend = MainAgentBackend(SimpleNamespace(_tool_artifacts=store), runtime)
    env.host.archive = backend.archive_code_result
    env.host.result_limit = 1000
    service = InvocationService()
    dispatched = []

    async def business(invocation, side_effecting):
        async def invoke():
            dispatched.append(invocation.call.id)
            data = await social.service.execute(
                invocation.call.function.name,
                json.loads(invocation.call.function.arguments),
                replace(social.context, call_id=invocation.call.id),
            )
            result = ToolExecutionResult(
                ok=True,
                data=data,
                provider_id="core",
                tool_name=invocation.call.function.name,
                source_event_ids=getattr(data, "source_event_ids", ()),
                source_privacy_generation=getattr(data, "source_privacy_generation", None),
            )
            return (await ToolResultBudgeter(max_characters=None).render(result)).text

        return await service.invoke(invocation, invoke, side_effecting=side_effecting)

    env.host.execute_business = business
    if not native:
        env.host.worker = SimpleNamespace(execution_digest=lambda: "fake-history-engine")
        calls = [
            ("yuki_query_chat_history", {"view": "search", "keyword": name})
            for name in ("source-alpha", "source-beta")
        ]
        env.host.engine_factory = lambda *_args: HistoryEngine(calls=calls)
    code = """
a = await yuki_query_chat_history({'view': 'search', 'keyword': 'source-alpha'})
b = await yuki_query_chat_history({'view': 'search', 'keyword': 'source-beta'})
{'copied': [a['data']['events'][0]['content'], b['data']['events'][0]['content']],
 'padding': 'x' * 3000}
"""
    return env, tuple(sources), store, dispatched, code


@pytest.mark.parametrize(
    "native", [False, pytest.param(True, marks=[requires_worker, pytest.mark.native_worker])]
)
async def test_code_discovers_entries_and_queries_same_source_with_projected_schemas(
    database, tmp_path, native
):
    env, sources, _store, dispatched, _code = await source_environment(database, tmp_path, native)
    social = env.test_case.env
    source = {"platform": "qq", "scope": "group"}
    directory_args = {
        "view": "entries",
        "source": source,
        "target": {"space_id": social.space},
        "limit": 1,
    }
    history_args = {
        "view": "search",
        "source": source,
        "target": {"conversation_id": social.context.conversation_id},
        "keyword": "source-alpha source-beta",
        "match": "any",
    }
    business = env.host.execute_business
    observed = []

    async def observe(invocation, side_effecting):
        raw = await business(invocation, side_effecting)
        observed.append((invocation.call.function.name, json.loads(raw)["data"]))
        return raw

    env.host.execute_business = observe
    if not native:
        calls = [("yuki_find_contacts", directory_args), ("yuki_query_chat_history", history_args)]
        env.host.engine_factory = lambda *_args: HistoryEngine(calls=calls)
    code = f"""
directory = await yuki_find_contacts({directory_args!r})
entry = directory['data']['items'][0]
history = await yuki_query_chat_history({{
    'view': 'search', 'source': {source!r},
    'target': {{'conversation_id': entry['conversation_id']}},
    'keyword': 'source-alpha source-beta', 'match': 'any'
}})
{{'copied': [row['content'] for row in history['data']['events']], 'padding': 'x' * 3000}}
"""
    outer = outer_call(env, code)
    result = json.loads(await CodeModeDriver(env.host, outer).run())
    assert result["status"] == "completed" and len(dispatched) == 2
    assert [name for name, _ in observed] == ["find_contacts", "query_chat_history"]
    directory, history = (data for _, data in observed)
    assert len(directory["items"]) == 1
    entry = directory["items"][0]
    assert entry["conversation_id"] == social.context.conversation_id
    assert entry["platform"] == "qq" and entry["scope"] == "group"
    assert {row["event_id"] for row in history["events"]} == set(sources)
    assert {row["conversation_id"] for row in history["sources"]} == {entry["conversation_id"]}
    rows, _, _ = await effect_rows(database, env.control.current["id"])
    outcome = json.loads(rows[outer.identity.operation_id]["receipt_json"])["outcome"]
    assert set(outcome["source_event_ids"]) == set(sources)


@pytest.mark.parametrize(
    "native", [False, pytest.param(True, marks=[requires_worker, pytest.mark.native_worker])]
)
@pytest.mark.parametrize("entry", ["driver", "runner"])
async def test_code_history_parent_and_real_backend_archive_keep_sources(
    database, tmp_path, native, entry, monkeypatch
):
    env, sources, store, dispatched, code = await source_environment(database, tmp_path, native)
    outer = outer_call(env, code)
    assert current_result_capture.get() is None
    if entry == "driver":
        result = json.loads(await CodeModeDriver(env.host, outer).run())
    else:
        runner = env.test_case.runner
        monkeypatch.setattr(runner, "_code_host", lambda *_args, **_kwargs: env.host)
        batch = await runner._execute_tool_batch(
            (outer.call,),
            env.test_case.backend,
            env.test_case.runtime,
            remaining_calls=None,
            max_parallel_calls=2,
            declared_names=frozenset({"execute_code"}),
        )
        result = json.loads(batch.calls[0][1])
    assert result["status"] == "completed" and len(dispatched) == 2
    rows, _, _ = await effect_rows(database, env.control.current["id"])
    outcome = json.loads(rows[outer.identity.operation_id]["receipt_json"])["outcome"]
    assert set(outcome["source_event_ids"]) == set(sources)
    assert outcome["source_privacy_generation"] == 0
    async with database.read_session() as reader:
        artifact = await reader.get(ToolArtifactModel, result["result_ref"])
        assert artifact.work_id == env.control.current["id"]
        assert artifact.effect_key == outer.identity.operation_id
        access = json.loads(artifact.access_json)
        assert access["conversation_id"] == env.control.lease.conversation_id
        assert set(access["source_event_ids"]) == set(sources)
        assert access["privacy_generation"] == 0
    page = await store.read(result["result_ref"], operation="get")
    assert page.source_event_ids == sources
    assert current_result_capture.get() is None


async def test_code_resume_reads_original_child_sources_without_dispatch(
    database, tmp_path, monkeypatch
):
    env, sources, _store, dispatched, code = await source_environment(database, tmp_path, False)
    outer = outer_call(env, code)
    settle = CodeModeDriver._settle
    crashed = False

    async def lose_final_publication(driver, state, **kwargs):
        nonlocal crashed
        outcome: EngineOutcome | None = kwargs.get("outcome")
        if outcome is not None and outcome.status == "completed" and not crashed:
            crashed = True
            raise RuntimeError("before parent publication")
        return await settle(driver, state, **kwargs)

    monkeypatch.setattr(CodeModeDriver, "_settle", lose_final_publication)
    with pytest.raises(RuntimeError, match="before parent publication"):
        await CodeModeDriver(env.host, outer).run()
    assert len(dispatched) == 2
    result = json.loads(await CodeModeDriver(env.host, outer).resume())
    assert result["status"] == "completed" and len(dispatched) == 2
    rows, _, _ = await effect_rows(database, env.control.current["id"])
    outcome = json.loads(rows[outer.identity.operation_id]["receipt_json"])["outcome"]
    assert set(outcome["source_event_ids"]) == set(sources)
    assert outcome["source_privacy_generation"] == 0


@pytest.mark.parametrize(
    "native", [False, pytest.param(True, marks=[requires_worker, pytest.mark.native_worker])]
)
async def test_empty_history_page_does_not_age_later_code_sources(database, tmp_path, native):
    from yuki.execution_trace.db_models import ExecutionTraceStateModel

    env, sources, _store, dispatched, _code = await source_environment(database, tmp_path, native)
    business = env.host.execute_business
    reads = 0

    async def erase_between_reads(invocation, side_effecting):
        nonlocal reads
        result = await business(invocation, side_effecting)
        reads += 1
        if reads == 1:
            assert json.loads(result)["data"]["events"] == []
            async with database.immediate_session() as writer:
                writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        return result

    env.host.execute_business = erase_between_reads
    if not native:
        calls = [
            ("yuki_query_chat_history", {"view": "search", "keyword": keyword})
            for keyword in ("absent-source", "source-alpha")
        ]
        env.host.engine_factory = lambda *_args: HistoryEngine(calls=calls)
    code = """
empty = await yuki_query_chat_history({'view': 'search', 'keyword': 'absent-source'})
current = await yuki_query_chat_history({'view': 'search', 'keyword': 'source-alpha'})
{'copied': current['data']['events'][0]['content'], 'padding': 'x' * 3000}
"""
    outer = outer_call(env, code)
    result = json.loads(await CodeModeDriver(env.host, outer).run())
    assert result["status"] == "completed" and len(dispatched) == 2
    rows, _, _ = await effect_rows(database, env.control.current["id"])
    outcome = json.loads(rows[outer.identity.operation_id]["receipt_json"])["outcome"]
    assert outcome["source_event_ids"] == [sources[0]]
    assert outcome["source_privacy_generation"] == 1
    async with database.read_session() as reader:
        artifact = await reader.get(ToolArtifactModel, result["result_ref"])
        access = json.loads(artifact.access_json)
        assert access["source_event_ids"] == [sources[0]]
        assert access["privacy_generation"] == 1


async def test_old_code_body_cannot_publish_after_privacy_change_but_real_send_stays(
    database, tmp_path, monkeypatch
):
    from yuki.execution_trace.db_models import ExecutionTraceStateModel
    from yuki.runtime.work_repository import WorkConflict
    from yuki.social.db_models import SocialOperationModel

    env, _sources, _store, dispatched, code = await source_environment(database, tmp_path, False)
    calls = [
        ("yuki_query_chat_history", {"view": "search", "keyword": "source-alpha"}),
        ("yuki_query_chat_history", {"view": "search", "keyword": "source-beta"}),
        ("yuki_send", {"text": "already delivered"}),
    ]
    env.host.engine_factory = lambda *_args: HistoryEngine(calls=calls)
    env.host.result_limit = 24000
    settle = CodeModeDriver._settle

    async def erased_before_parent(driver, state, **kwargs):
        outcome = kwargs.get("outcome")
        if outcome is not None and outcome.status == "completed":
            async with database.immediate_session() as writer:
                writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        return await settle(driver, state, **kwargs)

    monkeypatch.setattr(CodeModeDriver, "_settle", erased_before_parent)
    outer = outer_call(env, code)
    with pytest.raises(WorkConflict, match="work_effect_history_source_changed"):
        await CodeModeDriver(env.host, outer).run()
    assert len(dispatched) == 3
    rows, _, _ = await effect_rows(database, env.control.current["id"])
    assert rows[outer.identity.operation_id]["state"] == "prepared"
    assert "result" not in json.loads(rows[outer.identity.operation_id]["receipt_json"])
    sent = [
        json.loads(row["receipt_json"])["outcome"]
        for row in rows.values()
        if json.loads(row["receipt_json"]).get("outcome", {}).get("tool") == "send"
    ]
    assert len(sent) == 1 and sent[0]["delivered_message"] is True
    async with database.read_session() as reader:
        operation = await reader.scalar(
            select(SocialOperationModel).where(SocialOperationModel.action == "send_message")
        )
        assert operation.status == "succeeded" and operation.original_event_id is not None
