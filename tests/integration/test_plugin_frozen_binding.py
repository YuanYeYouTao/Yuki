"""Offline real plugin lifecycle/registry/Host probe; synthetic files and SQLite only."""

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import build_harness, make_settings
from tests.support.codemode_cases import effect_rows, environment, outer_call, requires_worker

from qq_ai_bot import __version__
from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.codemode.driver import CodeModeDriver
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity, ToolCall, ToolFunction
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.capability_adapter import PluginCapabilityAdapter
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.invocation_service import InvocationService
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore
from yuki_plugin_sdk.api import PLUGIN_API_VERSION


def write_plugin(root, version, risk, field):
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("plugin.toml").write_text(f'''id = "test.contract"
name = "Offline contract fixture"
version = "{version}.0.0"
description = "Synthetic inert audit fixture"
entrypoint = "fixture{version}:Fixture"
plugin_api = "{PLUGIN_API_VERSION}"
yuki_requires = ">=3.8"
permissions = ["tool.register"]
''')
    root.joinpath(f"fixture{version}.py").write_text(f'''
from pydantic import BaseModel, ConfigDict
from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration
from yuki_plugin_sdk.models import RiskClass
from yuki_plugin_sdk.results import ToolResult
class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    {field}: str
class Fixture:
    async def register(self, registrar):
        registrar.register_tool(ToolRegistration(
            ToolMetadata(name="inspect", description="Read an inert fixture",
                         risk=RiskClass.{risk}), Arguments, ToolResult, self.handle))
    async def start(self, context):
        self.context = context
    async def stop(self):
        pass
    async def handle(self, arguments):
        self.context.calls.append({{"version": {version}, "args": arguments.model_dump(),
                                   "declared_risk": "{risk}"}})
        if {version} == 2:
            self.context.effect_file.write_text("committed synthetic fixture")
        return ToolResult(data={{"version": {version}, "value": arguments.{field}}},
                          mutation_committed=({version} == 2))
''')


async def run(*, native_child=False):
    with tempfile.TemporaryDirectory(prefix="yuki-r3-plugin-") as d:
        base = Path(d)
        root = base / "plugins" / "test.contract"
        write_plugin(root, 1, "READ", "value")
        db = Database(f"sqlite+aiosqlite:///{base}/fixture.db")
        await db.create_schema()
        registry = ExtensionRegistry()
        installs = PluginInstallationRepository(db)
        ctx = SimpleNamespace(calls=[], effect_file=base / "inert-effect.txt")
        contexts = {}

        class AuditContext(HostPluginContext):
            pass

        def context_factory(manifest, permissions):
            context = AuditContext(
                plugin_id=manifest.id,
                approved_permissions=permissions,
                services=PluginFacadeServices(approval_revision=manifest.manifest_hash),
            )
            context.calls = ctx.calls
            context.effect_file = ctx.effect_file
            contexts[manifest.id] = context
            return context

        manager = PluginManager(
            enabled=True,
            discovery=PluginDiscovery(base / "plugins", yuki_version=__version__),
            installations=installs,
            loader=PluginLoader(),
            extensions=registry,
            event_bus=PluginEventBus(),
            context_factory=context_factory,
        )
        try:
            await manager.start()
            await manager.approve("test.contract", actor_user_id="9000")
            await manager.enable("test.contract", actor_user_id="9000")
            adapter = PluginCapabilityAdapter(
                registry=registry,
                installations=installs,
                is_running=lambda p: p in manager.running_plugin_ids,
                invocation_scope=lambda p, r, **kw: contexts[p].invocation_scope(p, r, **kw),
            )
            chat = build_harness(db, make_settings(db.url)).processor._chat
            chat.set_plugin_tools(adapter)
            contract = MainAgentContract(chat, ShortState(WorkspaceStore(base / "workspace")))
            chat.runtime.runner.main_contract = contract
            before = await contract.definitions()
            original_revision = contract.revision
            name = registry.list(kind=ExtensionKind.TOOL)[0].model_name
            tr = ToolRuntime(
                inbound=InboundMessage(
                    message_id="1",
                    bot_user_id="9999",
                    sender=SenderIdentity("1001"),
                    text="inspect",
                    event_type="message",
                    scope_type=ScopeType.PRIVATE,
                ),
                gateway=None,
                allow_generic_onebot=False,
                execution_id="offline-fixture",
                runtime_config=await chat._runtime_config.snapshot(),
            )
            backend = MainAgentBackend(chat, tr)
            await backend.prepare()
            ar = SimpleNamespace(work_control=None, origin=TurnOrigin.USER_MESSAGE)

            async def execute(backend, args, seq):
                call = ToolCall(f"call-{seq}", ToolFunction(name, json.dumps(args)))
                inv = direct_invocations(
                    (call,),
                    ar,
                    chain_id="offline-chain",
                    request_sequence=seq,
                    manifest_revision=contract.revision,
                )[0]
                return json.loads(await backend.execute_call(inv))

            out = {
                "name": name,
                "before": {
                    "parallel": backend.parallel_safe(name, ar),
                    "side_effecting": backend.is_side_effecting(name, '{"value":"fixture"}', ar),
                    "result": await execute(backend, {"value": "fixture"}, 0),
                },
            }
            await manager.disable("test.contract", actor_user_id="9000")
            out["disabled_result"] = await execute(backend, {"value": "fixture"}, 1)
            # Identical approved code survives an unchanged lifecycle restart.
            await manager.enable("test.contract", actor_user_id="9000")
            assert (await execute(backend, {"value": "after-restart"}, 10))["ok"]
            assert contract.health()["restart_required"] is False
            # A call already waiting for its authorized scope cannot pick up a
            # newer registration. The Host must check again after that wait.
            entered, release = asyncio.Event(), asyncio.Event()
            original_scope = adapter._invocation_scope

            @asynccontextmanager
            async def waiting_scope(*args, **kwargs):
                entered.set()
                await release.wait()
                async with original_scope(*args, **kwargs) as context:
                    yield context

            adapter._invocation_scope = waiting_scope
            waiting = asyncio.create_task(execute(backend, {"value": "waiting"}, 11))
            await asyncio.wait_for(entered.wait(), 2)
            await manager.disable("test.contract", actor_user_id="9000")
            # Same name/schema; a newly approved implementation changes READ to MUTATE.
            write_plugin(root, 2, "MUTATE", "value")
            await manager.discover()
            await manager.approve("test.contract", actor_user_id="9000")
            await manager.enable("test.contract", actor_user_id="9000")
            release.set()
            waited = await asyncio.wait_for(waiting, 2)
            adapter._invocation_scope = original_scope
            assert waited["ok"] is False and not ctx.effect_file.exists()
            assert contract.health()["restart_required"] is True
            backend.refresh_catalog(ar, web_was_used=False)
            out["after_risk_change"] = {
                "running": manager.running_plugin_ids,
                "parallel": backend.parallel_safe(name, ar),
                "side_effecting": backend.is_side_effecting(name, '{"value":"fixture"}', ar),
                "current_adapter_read_only": adapter.is_read_only(name),
                "result": await execute(backend, {"value": "fixture"}, 2),
            }
            out["after_risk_change"]["fixture_file_committed"] = ctx.effect_file.exists()
            if native_child:
                # Real frozen API -> pinned VM -> typed child -> real Work journal.
                # The new MUTATE implementation must not run under old READ facts.
                env = await environment(db, base / "code-child")
                env.host.api = contract.script_api
                service = InvocationService()

                async def child(invocation, side_effecting):
                    return await service.invoke(
                        invocation,
                        lambda: backend.execute_call(invocation),
                        side_effecting=side_effecting,
                    )

                env.host.execute_business = child
                wrapper = next(key for key, value in env.host.api.wrappers.items() if value == name)
                outer = outer_call(env, f"await {wrapper}({{'value': 'synthetic'}})")
                raw = await CodeModeDriver(env.host, outer).run()
                body = json.loads(raw)
                rows, tools, budget = await effect_rows(db, env.control.current["id"])
                assert tools == budget == 1 and len(rows) == 2
                # A contract mismatch is rejected before the handler starts.
                assert body["operations"][0]["status"] == "not_executed"
                leaf = next(
                    row for row in rows.values() if row["effect_key"] != outer.identity.operation_id
                )
                receipt = json.loads(leaf["receipt_json"])
                refusal = json.loads(receipt["result"])
                assert refusal["error_code"] == "plugin_tool_contract_changed"
                assert refusal["executed"] is False and refusal["mutation_committed"] is False
                assert await CodeModeDriver(env.host, outer).run() == raw
                assert await effect_rows(db, env.control.current["id"]) == (rows, tools, budget)
                assert not ctx.effect_file.exists()
            # Again through supported lifecycle; schema is now different under the same model name.
            await manager.disable("test.contract", actor_user_id="9000")
            write_plugin(root, 3, "READ", "replacement")
            await manager.discover()
            await manager.approve("test.contract", actor_user_id="9000")
            await manager.enable("test.contract", actor_user_id="9000")
            fresh = MainAgentBackend(chat, tr)
            await fresh.prepare()
            after = await contract.definitions()
            out["schema_change"] = {
                "frozen_same": before == after,
                "revision_same": contract.revision == original_revision,
                "frozen_parameters": next(t.parameters for t in after if t.name == name),
                "current_parameters": next(
                    t.parameters
                    for t in adapter.definitions(tr, web_was_used=False)
                    if t.name == name
                ),
                "old_arguments_fresh_backend": await execute(fresh, {"value": "fixture"}, 3),
                "new_arguments_fresh_backend": await execute(fresh, {"replacement": "fixture"}, 4),
                "old_arguments_original_backend": await execute(backend, {"value": "fixture"}, 5),
            }
            out["handler_calls"] = ctx.calls
            assert out["before"]["result"]["ok"]
            assert not out["disabled_result"]["ok"]
            assert out["after_risk_change"]["result"]["ok"] is False
            assert (
                out["after_risk_change"]["result"]["error_code"] == "plugin_tool_contract_changed"
            )
            assert contract.health()["restart_required"] is True
            assert not out["after_risk_change"]["fixture_file_committed"]
            assert out["after_risk_change"]["result"]["mutation_committed"] is False
            assert out["schema_change"]["frozen_same"] and out["schema_change"]["revision_same"]
            assert ctx.calls == [
                {"version": 1, "args": {"value": "fixture"}, "declared_risk": "READ"},
                {"version": 1, "args": {"value": "after-restart"}, "declared_risk": "READ"},
            ]
            assert (
                out["schema_change"]["old_arguments_fresh_backend"]["error_code"]
                == "plugin_tool_contract_changed"
            )
            assert (
                out["schema_change"]["new_arguments_fresh_backend"]["error_code"]
                == "plugin_tool_contract_changed"
            )
            return out
        finally:
            await manager.stop()
            await db.close()


async def test_approved_hot_upgrade_does_not_replace_frozen_binding():
    await run()


@requires_worker
async def test_hot_upgrade_refusal_keeps_native_child_and_work_receipt():
    await run(native_child=True)
