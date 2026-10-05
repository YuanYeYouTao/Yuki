"""Explicit trusted backend for isolated Runner fixtures, never production authority."""

from qq_ai_bot.services.agent_runner import AgentToolBackend


class StubAgentBackend(AgentToolBackend):
    def __init__(
        self,
        *,
        definitions=None,
        execute_call=None,
        parallel_safe=None,
        is_side_effecting=None,
        finalize=None,
        exhausted=None,
    ):
        # Fixed, test-only injection points replace SimpleNamespace fixtures.
        if definitions is not None:
            self.definitions = definitions
        if execute_call is not None:
            self.execute_call = execute_call
        if parallel_safe is not None:
            self.parallel_safe = parallel_safe
        if is_side_effecting is not None:
            self.is_side_effecting = is_side_effecting
        if finalize is not None:
            self.finalize = finalize
        if exhausted is not None:
            self.exhausted = exhausted

    # P10 retires legacy execute(name, arguments, runtime). Test fixtures now
    # implement execute_call and explicitly grant their fake lifecycle controls.
    def work_control_allowed(self, name: str) -> bool:
        return True

    def work_query_allowed(self, action: str) -> bool:
        return True
