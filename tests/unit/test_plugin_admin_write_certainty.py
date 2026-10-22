"""An external config command retains an actual committed config with a lost ACK."""

import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.test_plugin_offline_configuration import manager_for, write_configured_plugin

from yuki.admin.authority.commands import ControlCommand
from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.operations.base import CommandExecutor
from yuki.admin.operations.plugins import PluginOperations
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.domain.identity import PrincipalId, RequestId
from yuki.plugin_host.configuration_service import PluginConfigurationError
from yuki.plugin_host.db_models import PluginConfigValueModel


async def test_committed_plugin_config_ack_is_unknown_and_original_command_is_not_replayed(
    database, tmp_path, monkeypatch
):
    root = tmp_path / "plugins"
    write_configured_plugin(root / "test.configured")
    manager, _registry, service = manager_for(database, root)
    await manager.start()
    try:
        await manager.approve("test.configured", actor_user_id="operator")
        before = await service.read("test.configured")
        principal = ControlPrincipal(
            principal_id=PrincipalId.parse(str(uuid4())),
            person_id=None,
            source=PrincipalSource.CLI,
            granted_capabilities=("control.plugin.config.mutate",),
            authenticated=True,
            active=True,
        )
        command = ControlCommand(
            request_id=RequestId.new(),
            expected_revision=before["revision"],
            payload={
                "scope_type": "global",
                "owner_id": None,
                "values": {"endpoint": "configured"},
            },
        )
        commit = AsyncSession.commit
        lost = False

        async def lost_config_ack(session):
            nonlocal lost
            configured = await session.scalar(
                select(PluginConfigValueModel.id).where(
                    PluginConfigValueModel.plugin_id == "test.configured",
                    PluginConfigValueModel.key == "endpoint",
                    PluginConfigValueModel.value_json == json.dumps("configured"),
                )
            )
            await commit(session)
            if configured is not None and not lost:
                lost = True
                raise PluginConfigurationError("version_conflict")

        operations = PluginOperations(CommandExecutor(database), manager)
        with monkeypatch.context() as injected:
            injected.setattr(AsyncSession, "commit", lost_config_ack)
            result = await operations.configure_plugin(
                principal, "test.configured", command, dict(command.payload)
            )
        assert lost
        assert result.operation.status.value == "unknown"
        current = await service.read("test.configured")
        assert current["values"]["endpoint"] == "configured"
        assert (
            await operations.configure_plugin(
                principal, "test.configured", command, dict(command.payload)
            )
            == result
        )
        assert (await service.read("test.configured"))["revision"] == current["revision"]
        async with database.read_session() as reader:
            receipt = await reader.scalar(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.request_id == command.request_id.text
                )
            )
            assert receipt.status == "unknown" and receipt.problem_code == "effect_unknown"
    finally:
        await manager.stop()
