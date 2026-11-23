"""Explicit Invocation construction for WorkSession test fixtures.

Fixtures run their tool effects through the real ``InvocationService`` entry,
exactly as direct calls do; WorkSession only owns the protocol.
"""

from types import SimpleNamespace

from yuki.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
)
from yuki.runtime.work_session import WorkSession
from yuki.services.invocation_service import InvocationService

__all__ = ["WorkSession", "invoke_tool"]


async def invoke_tool(session, call, invoke, *, side_effecting=True, invocation=None):
    """Execute ``invoke`` as the original Host invocation of ``call`` in ``session``."""
    control = session.control
    if invocation is None:
        assert session.transcript is not None
        invocation = Invocation(
            InvocationIdentity(
                session.call_key(call.id),
                str(control.current["id"]) if control.current else control.lease.owner,
                session.transcript.chain_id,
                session.sequence,
                call.id,
            ),
            call,
            TrustedInvocationContext(SimpleNamespace(work_control=control), session.contract),
        )

    async def typed_fixture():
        from yuki.capabilities.results import normalize_legacy_result
        from yuki.runtime.effect_outcomes import current_result_capture

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
