"""Focused `/ai mcp` command surface: call is fail-closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from qq_ai_bot.mcp.admin import MCPCommandHandler
from qq_ai_bot.services.command_service import CommandService
from qq_ai_bot.services.policies import CommandName

ADMIN_SOURCE = Path(__file__).resolve().parents[2] / "src" / "qq_ai_bot" / "mcp" / "admin.py"
_CALL_UNAVAILABLE = "MCP 任意调用当前不可用。"


class _ForbiddenManager:
    def __init__(self) -> None:
        self.invocations: list[str] = []

    def __getattr__(self, name: str) -> object:
        def _blocked(*_args: object, **_kwargs: object) -> object:
            self.invocations.append(name)
            raise AssertionError(f"MCP manager.{name} must not run for call")

        return _blocked


class _ListManager:
    def __init__(self) -> None:
        self.invocations: list[str] = []

    async def statuses(self) -> tuple[object, ...]:
        self.invocations.append("statuses")
        return ()

    def __getattr__(self, name: str) -> object:
        def _blocked(*_args: object, **_kwargs: object) -> object:
            self.invocations.append(name)
            raise AssertionError(f"unexpected manager.{name}")

        return _blocked


def test_mcp_admin_source_does_not_fabricate_superuser() -> None:
    source = ADMIN_SOURCE.read_text(encoding="utf-8")
    assert "actor_is_superuser=True" not in source
    assert "deterministic-superuser" not in source


def test_help_and_mutating_advertisement_omit_call() -> None:
    help_text = CommandService._help_text()
    assert (
        "/ai mcp list|show|status|tools|search|refresh|reconnect|enable|disable|doctor\n"
        in help_text
    )
    assert "|call" not in help_text
    assert CommandService.may_write(CommandName.MCP, "call server tool {}") is False
    assert CommandService.may_write(CommandName.MCP, "refresh server") is True
    assert CommandService.may_write(CommandName.MCP, "list") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "is_superuser"),
    [
        ("call", True),
        ("CALL server remote_tool {not-json", True),
        ('call server remote_tool {"query": "x"}', True),
        ("call server remote_tool []", False),
    ],
)
async def test_mcp_call_is_unavailable_before_json_or_manager(
    argument: str,
    is_superuser: bool,
) -> None:
    manager = _ForbiddenManager()
    handler = MCPCommandHandler(manager)  # type: ignore[arg-type]

    text = await handler.execute(argument, is_superuser=is_superuser)

    assert text == _CALL_UNAVAILABLE
    assert manager.invocations == []


@pytest.mark.asyncio
async def test_mcp_list_still_reads_manager() -> None:
    manager = _ListManager()
    handler = MCPCommandHandler(manager)  # type: ignore[arg-type]

    text = await handler.execute("list", is_superuser=False)

    assert text == "当前没有配置 MCP Server"
    assert manager.invocations == ["statuses"]
