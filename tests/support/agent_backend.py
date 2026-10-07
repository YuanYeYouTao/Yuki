"""Explicit trusted backend for isolated Runner fixtures, never production authority."""

from qq_ai_bot.services.agent_runner import AgentToolBackend


def _typed_fixture(call):
    async def execute(invocation):
        from qq_ai_bot.capabilities.results import normalize_legacy_result
        from qq_ai_bot.runtime.effect_outcomes import current_result_capture

        result = await call(invocation)
        capture = current_result_capture.get()
        if capture is not None and capture.outcome is None:
            capture.outcome = normalize_legacy_result(
                result, provider_id="fixture", tool_name=invocation.call.function.name
            )
        return result

    return execute


class StubAgentBackend(AgentToolBackend):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        implementation = cls.__dict__.get("execute_call")
        if implementation is not None:

            async def execute_call(self, invocation):
                return await _typed_fixture(lambda bound: implementation(self, bound))(invocation)

            cls.execute_call = execute_call

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
            self.execute_call = _typed_fixture(execute_call)
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
