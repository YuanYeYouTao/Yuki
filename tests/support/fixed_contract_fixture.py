"""Use the production fixed declaration in main Agent integration fixtures."""

from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


def bind_main_contract(harness, tmp_path):
    chat = harness.processor._chat
    chat.runtime.runner.main_contract = MainAgentContract(
        chat,
        ShortState(WorkspaceStore(tmp_path / "short-state")),
        code_enabled=harness.settings.code_mode_enabled,
    )
