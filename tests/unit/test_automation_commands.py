"""Deterministic commands retain owner scope while showing authorized full requests."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.support.commands_and_chat_helpers import inbound

from yuki.automation.models import AutomationStatus
from yuki.services.automation_commands import AutomationCommandHandler


async def test_command_lists_remain_owner_scoped_and_show_returns_authorized_body():
    row = SimpleNamespace(
        id=1,
        name="自动化 #1",
        status=AutomationStatus.ACTIVE,
        timezone="Asia/Shanghai",
        next_run_at=None,
        last_run_at=None,
        run_count=1,
        max_runs=7,
        script={"goal": "PRIVATE_REQUEST", "trigger": {"type": "after", "seconds": 1}},
    )
    service = SimpleNamespace(
        list_current=AsyncMock(return_value=(row,)),
        list_completed=AsyncMock(return_value=(row,)),
        require_manageable=AsyncMock(return_value=row),
    )
    handler = AutomationCommandHandler(settings=None, automation_service=service)
    message = replace(inbound("", message_id="command"), person_id="owner", source_event_id=1)
    for argument in ("list", "completed", "archive"):
        result = await handler.execute(message=message, identity=message.scope(), argument=argument)
        assert "PRIVATE_REQUEST" not in result
    service.list_current.assert_awaited_once_with("owner")
    assert service.list_completed.await_count == 2
    assert all(call.args == ("owner",) for call in service.list_completed.await_args_list)
    detail = await handler.execute(message=message, identity=message.scope(), argument="show 1")
    assert "PRIVATE_REQUEST" in detail and "次数上限：7" in detail
    assert service.require_manageable.await_args.args[1].person_id == "owner"
    service.require_manageable.side_effect = PermissionError("not owner")
    assert (
        await handler.execute(message=message, identity=message.scope(), argument="show 1")
        == "not owner"
    )
