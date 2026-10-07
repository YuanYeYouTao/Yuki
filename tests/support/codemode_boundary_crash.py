"""Kill a non-production Host immediately after an accepted native child receipt."""

import asyncio
import json
import os
import sys


def append(path, entry):
    with open(path, "a") as output:
        output.write(json.dumps(entry) + "\n")
        output.flush()
        os.fsync(output.fileno())


async def main():
    from qq_ai_bot.codemode.driver import CodeModeDriver
    from qq_ai_bot.domain.messages import ChatMessage
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_repository import WorkLease, WorkRepository
    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.services.turn_transcript import TurnTranscript
    from qq_ai_bot.services.work_reporting import before_work_tool
    from tests.support.codemode_cases import FakeDomain, build_host, outer_call

    url, lease_json, work_id, chain, code, log, stop_tool = sys.argv[2:]
    repository = WorkRepository(Database(url))
    lease = WorkLease(**json.loads(lease_json))

    async def valid():
        assert await repository.valid(lease)

    row = await repository.get(work_id)
    control = WorkControl(
        repository, lease, row["source_key"], json.loads(row["source_json"]), valid
    )
    control.current = row
    owner = WorkSession(control, "contract")
    control.session = owner
    owner.transcript = TurnTranscript((ChatMessage("system", "test"),))
    owner.transcript.chain_id = chain

    class Logged(FakeDomain):
        async def __call__(self, name, arguments):
            append(log, [name, json.loads(arguments)])
            return await super().__call__(name, arguments)

    env = build_host(owner, Logged())
    env.host.before_dispatch = lambda call: before_work_tool(control, call)
    outer = outer_call(env, code)
    owner.transcript.append(ChatMessage("assistant", tool_calls=(outer.call,)))
    await owner.save("response", (outer.call,))
    original = repository.record_effect

    async def accepted_then_exit(key, state, receipt, **kwargs):
        await original(key, state, receipt, **kwargs)
        if receipt.get("outcome", {}).get("tool") == stop_tool:
            append(log, ["accepted", key, stop_tool])
            os._exit(91)

    repository.record_effect = accepted_then_exit
    await CodeModeDriver(env.host, outer).run()
    raise AssertionError("the requested crash window was not reached")


if __name__ == "__main__":
    os.environ["YUKI_MONTY_BINARY"] = sys.argv[1]
    asyncio.run(main())
