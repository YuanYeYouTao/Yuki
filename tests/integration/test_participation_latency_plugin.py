"""Actual direct registration/router/authorization with simulated plugin lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict
from tests.conftest import MemorySender, build_harness, make_settings
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.canonical_identity import ensure_person
from tests.support.commands_and_chat_helpers import inbound
from tests.support.semantic_participation_host_helpers import _host

from yuki.admin.control_resolution import ControlAccess
from yuki.identity.db_models import CanonicalPersonModel
from yuki.plugin_host.command_adapter import PluginCommandAdapter
from yuki.plugin_host.direct_command_router import DirectCommandRouter
from yuki.plugin_host.extension_registry import ExtensionRegistry
from yuki_plugin_sdk.models import PermissionLevel
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import CommandMetadata, CommandRegistration
from yuki_plugin_sdk.results import CommandResult


class SyntheticArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = ""


@pytest.mark.parametrize("superuser", [False, True])
async def test_registered_direct_command_does_not_wait_for_participation(
    database, tmp_path, monkeypatch, latency_evidence, superuser
):
    async with database.immediate_session() as writer:
        person = await ensure_person(writer, "1001")
        (await writer.get(CanonicalPersonModel, person)).is_superuser = superuser
    calls = []

    async def handler(arguments):
        calls.append(arguments.text)
        return CommandResult(text="synthetic plugin result")

    registry = ExtensionRegistry()
    registry.registrar("fixture", [PluginPermission.COMMAND_REGISTER]).register_command(
        CommandRegistration(
            metadata=CommandMetadata(
                name="echo",
                description="synthetic direct command",
                permission=PermissionLevel.SUPERUSER,
            ),
            argument_model=SyntheticArguments,
            handler=handler,
        )
    )
    manager = SimpleNamespace(running_plugin_ids=("fixture",))
    router = DirectCommandRouter(
        bindings={"!fixture": "fixture:echo"}, registry=registry, manager=manager
    )
    harness = build_harness(
        database,
        make_settings(database.url, conversation_semantic_participation_enabled=True),
        direct_plugin_commands=router,
    )
    harness.processor._commands._plugin_commands = PluginCommandAdapter(
        manager=manager, registry=registry, control=ControlAccess(database), direct_commands=router
    )
    host, _ = await _host(database, tmp_path, observer=False)
    harness.processor.set_participation(host)
    real_binding = host.binding_for_event
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_binding(*args, **kwargs)

    monkeypatch.setattr(host, "binding_for_event", blocked)
    sender = MemorySender()
    task = asyncio.create_task(
        harness.processor.handle(
            inbound("!fixture hello", message_id=f"synthetic-plugin-{superuser}"), sender
        )
    )
    waited = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait_for(
            asyncio.wait((task, waited), return_when=asyncio.FIRST_COMPLETED), 30
        )
        finished = task in done
        release.set()
        result = await asyncio.wait_for(task, 30)
        latency_evidence.update(
            boundary=(
                "real Processor, registered command/router/adapter, Person auth and SQLite; "
                "fake plugin running lifecycle and sender"
            ),
            superuser=superuser,
            finished_before_binding_release=finished,
            binding_entered=entered.is_set(),
            command_handler_calls=len(calls),
            simulated_sends=sender.calls,
            handled=result.handled,
        )
        assert result.handled and sender.calls == 1
        assert calls == (["hello"] if superuser else [])
        assert ("synthetic plugin result" in sender.messages[0].text) == superuser
        assert finished, "direct plugin command waited for unconsumed participation binding"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        if not waited.done():
            waited.cancel()
        await asyncio.gather(task, waited, return_exceptions=True)
        await host.close()
