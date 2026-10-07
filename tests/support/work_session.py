"""Explicit Invocation construction for legacy WorkSession test fixtures."""

from qq_ai_bot.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
)
from qq_ai_bot.runtime.work_session import WorkSession as RuntimeWorkSession


class WorkSession(RuntimeWorkSession):
    async def execute(self, call, invoke, *, invocation=None, **kwargs):
        if invocation is None:
            assert self.transcript is not None
            invocation = Invocation(
                InvocationIdentity(
                    self.call_key(call.id),
                    str(self.control.current["id"])
                    if self.control.current
                    else self.control.lease.owner,
                    self.transcript.chain_id,
                    self.sequence,
                    call.id,
                ),
                call,
                TrustedInvocationContext(self.control, self.contract),
            )

        async def typed_fixture():
            from qq_ai_bot.capabilities.results import normalize_legacy_result
            from qq_ai_bot.runtime.effect_outcomes import current_result_capture

            result = await invoke()
            capture = current_result_capture.get()
            if capture is not None and capture.outcome is None:
                capture.outcome = normalize_legacy_result(
                    result, provider_id="fixture", tool_name=call.function.name
                )
            return result

        return await super().execute(call, typed_fixture, invocation=invocation, **kwargs)


def observe_fixture_result(control, name, result, executed, *, side_effecting=True, arguments="{}"):
    from qq_ai_bot.capabilities.results import normalize_legacy_result
    from qq_ai_bot.runtime.effect_outcomes import execution_evidence

    if executed:
        control.observe_evidence(
            execution_evidence(
                normalize_legacy_result(result, provider_id="fixture", tool_name=name),
                tool=name,
                side_effecting=side_effecting,
                arguments=arguments,
            )
        )
