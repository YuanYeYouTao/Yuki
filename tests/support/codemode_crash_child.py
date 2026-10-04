"""Child process for the Code Mode hard-kill test; exits with os._exit(9)."""

import asyncio
import json
import os
import sys


def _append(path: str, entry: list[object]) -> None:
    with open(path, "a") as handle:
        handle.write(json.dumps(entry) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


async def main() -> None:
    from qq_ai_bot.codemode.driver import CodeModeDriver
    from qq_ai_bot.domain.messages import ChatMessage
    from qq_ai_bot.persistence.database import Database
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_repository import WorkLease, WorkRepository
    from qq_ai_bot.runtime.work_session import WorkSession
    from qq_ai_bot.services.turn_transcript import TurnTranscript
    from tests.support import codemode_cases as cases

    url, lease_json, work_id, chain, outer_json, log_path = sys.argv[2:]
    repository = WorkRepository(Database(url))
    lease = WorkLease(**json.loads(lease_json))

    async def valid() -> None:
        return None

    control = WorkControl(repository, lease, "tool-audit", {}, valid)
    control.current = await repository.get(work_id)
    session = WorkSession(control, "contract")
    control.session = session
    session.transcript = TurnTranscript((ChatMessage("system", "test"),))
    session.transcript.chain_id = chain

    class Logged(cases.FakeDomain):
        async def __call__(self, name: str, arguments: str) -> str:
            # The downstream fact is durable before the Host learns anything.
            _append(log_path, [name, json.loads(arguments)])
            if json.loads(arguments).get("q") == 2:
                os._exit(9)  # Killed after the second call reached downstream.
            return json.dumps({"ok": True, "data": json.loads(arguments)})

    built = cases.build_host(session, Logged())
    outer = cases.outer_call(built, json.loads(outer_json)["code"])
    await CodeModeDriver(built.host, outer).run()


if __name__ == "__main__":
    os.environ["YUKI_MONTY_BINARY"] = sys.argv[1]
    asyncio.run(main())
