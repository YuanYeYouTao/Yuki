"""Use the production fixed declaration in main Agent integration fixtures."""

from types import SimpleNamespace

from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


def bind_main_contract(harness, tmp_path):
    chat = harness.processor._chat
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), ShortState(WorkspaceStore(tmp_path / "short-state"))
    )
