"""A delegation may name an existing session by its native ID under the chosen agent.

Production (2026-11-19, Work 8713d099): the user handed Yuki the Claude session ID
``9ea5c710-…`` to resume; ``start_subtask(session=<native id>)`` and
``get_tasks(agents={session})`` answered ``external_agent_not_found`` five times,
because only the internal session ID (a hash of instance and native ID) was looked up.
Failure cases written before the change:
1. ``start_subtask(executor={agent}, session=<native id>)`` delegates on that session,
   and the record keeps the internal session ID (the same one the directory lists).
2. ``get_tasks(agents={agent, session: <native id>})`` reads that session.
3. The internal session ID still names that same session (a second delegation on it
   meets the first record's ``conflict``).
4. A native ID unknown under that agent stays ``external_agent_not_found``.
5. A native ID is only resolved under the named agent: without an agent the browse
   read still requires the internal ID.

Real TLS device WebSocket, real SQLite Work rows, the real Work control tools; the
native peer is the bridge suite's protocol double (no model is called).
"""

import json

from sqlalchemy import select
from tests.integration.test_external_agent_bridge import NativeComputer, accepted_work, directory
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_external_agent_delegation import _delegate

from yuki.runtime.subagent_schema import children


async def _executor(bridge, identity):
    async with bridge.database.read_session() as reader:
        return json.loads(
            await reader.scalar(
                select(children.c.executor_json).where(children.c.work_id == identity)
            )
        )


async def test_native_session_id_resolves_under_the_named_agent(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, internal = await directory(bridge)
        native = computer.native_session
        assert native != internal
        async with accepted_work(bridge) as control:
            identity, _ = await _delegate(bridge, control, instance, native)
            assert (await _executor(bridge, identity))["session_id"] == internal
            # The internal ID names the very same session, now owned by that record.
            second = await control.tool_outcome(
                "start_subtask",
                {"goal": "x", "executor": {"agent": instance}, "session": internal},
                "call-internal",
            )
            assert not second.ok and second.error_code == "conflict"
            assert identity in second.public_message

            read = await control.tool_outcome(
                "get_tasks", {"agents": {"agent": instance, "session": native}}, "browse-native"
            )
            assert read.ok, read

            unknown = await control.tool_outcome(
                "start_subtask",
                {"goal": "x", "executor": {"agent": instance}, "session": "no-such-native"},
                "call-unknown",
            )
            assert not unknown.ok and unknown.error_code == "external_agent_not_found"

            unscoped = await control.tool_outcome(
                "get_tasks", {"agents": {"session": native}}, "browse-unscoped"
            )
            assert not unscoped.ok and unscoped.error_code == "external_agent_not_found"
    finally:
        await computer.close()
