"""Explicit Invocation construction for WorkSession test fixtures.

Fixtures run their tool effects through the real ``InvocationService`` entry,
exactly as direct and Code calls do; WorkSession only owns the protocol.
"""

from types import SimpleNamespace

from qq_ai_bot.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
    child_operation_id,
)
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.invocation_service import InvocationService

__all__ = ["WorkSession", "invoke_tool", "observe_fixture_result"]


async def invoke_tool(
    session, call, invoke, *, side_effecting=True, invocation=None, child_ordinal=None
):
    """Execute ``invoke`` as the original Host invocation of ``call`` in ``session``.

    ``child_ordinal`` makes it a Code composition child of ``call``'s operation,
    the real path on which even ``send_message`` keeps the pending/unresolved fences.
    """
    control = session.control
    if invocation is None:
        assert session.transcript is not None
        parent = session.call_key(call.id) if child_ordinal is not None else None
        invocation = Invocation(
            InvocationIdentity(
                session.call_key(call.id)
                if parent is None
                else child_operation_id(parent, child_ordinal),
                str(control.current["id"]) if control.current else control.lease.owner,
                session.transcript.chain_id,
                session.sequence,
                call.id,
                parent_operation_id=parent,
                child_ordinal=child_ordinal,
            ),
            call,
            TrustedInvocationContext(SimpleNamespace(work_control=control), session.contract),
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

    # The production turn binds exactly this session to its control before
    # any tool call; fixtures that juggle sessions get the same binding here.
    previous, control.session = control.session, session
    try:
        return await InvocationService().invoke(
            invocation, typed_fixture, side_effecting=side_effecting
        )
    finally:
        control.session = previous


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
