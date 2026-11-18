"""E8: SDK owner does one turn and exits at the turn boundary; resume while terminal is live (fork control); fork_session."""
import asyncio, json, os, sys, uuid
sys.path.insert(0, os.path.dirname(__file__))
import harness
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, SystemMessage

MODE, CWD, OUT = sys.argv[1:4]
TARGET = sys.argv[4] if len(sys.argv) > 4 else None

async def main():
    harness.start(OUT)
    sid = str(uuid.uuid4()) if MODE == "new" else None
    kw = dict(session_id=sid) if MODE == "new" else dict(resume=TARGET, fork_session=(MODE == "fork"))
    harness.mark("mode", mode=MODE, session=sid, target=TARGET)
    opts = ClaudeAgentOptions(cli_path="/opt/homebrew/bin/claude", cwd=CWD, model="haiku",
                              system_prompt={"type": "preset", "preset": "claude_code"}, tools={"type": "preset", "preset": "claude_code"},
                              setting_sources=["user", "project", "local"], extra_args={"replay-user-messages": None}, **kw)
    seen = None
    async with ClaudeSDKClient(options=opts) as client:
        await client.query("Reply with the single word: alpha" if MODE == "new" else "Reply with the single word: beta")
        async for m in client.receive_response():
            if isinstance(m, ResultMessage):
                seen = m.session_id
                break
        harness.mark("turn_boundary_close", session=seen)
    harness.mark("process_exited")
    print(seen)

asyncio.run(main())
