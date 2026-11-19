"""E7: self-started turns under an SDK owner (background-task notification, stop-then-complete, ScheduleWakeup); X2: one long tool."""
import asyncio, json, os, sys, time, uuid
sys.path.insert(0, os.path.dirname(__file__))
import harness
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow

CASE, CWD, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(CWD, exist_ok=True)
SESSION = str(uuid.uuid4())

async def allow(tool, arguments, context):
    harness.mark("can_use_tool", tool=tool, tool_use_id=getattr(context, "tool_use_id", None))
    return PermissionResultAllow()

PROMPTS = {
    "bg_notify": [(0, "Use the Bash tool with run_in_background set to true to run: sleep 8; echo done . Then immediately end your turn with a one word reply. Do not wait for it and do not check on it.")],
    "bg_stop_then_complete": [(0, "Use the Bash tool with run_in_background set to true to run: sleep 6; echo done . Then, in the same turn, stop that background task with the tool for stopping background tasks. Then end your turn with a one word reply.")],
    "schedule_wakeup": [(0, "Call the ScheduleWakeup tool once to wake yourself up after the minimum allowed delay, with a short prompt that says to reply with the single word woke. Then end your turn with a one word reply.")],
    "x2_one_long_tool": [(0, "Use the Bash tool (not in background) to run exactly: sleep 20; echo ok . Then reply with one word."), (6, "Also reply with the word second.")],
}
WATCH = {"bg_notify": 60, "bg_stop_then_complete": 50, "schedule_wakeup": 420, "x2_one_long_tool": 70}

async def main():
    harness.start(OUT)
    harness.mark("case", case=CASE, session=SESSION)
    opts = ClaudeAgentOptions(cli_path="/opt/homebrew/bin/claude", cwd=CWD, model="haiku", session_id=SESSION,
                              system_prompt={"type": "preset", "preset": "claude_code"}, tools={"type": "preset", "preset": "claude_code"},
                              setting_sources=["user", "project", "local"], can_use_tool=allow,
                              extra_args={"replay-user-messages": None})
    async with ClaudeSDKClient(options=opts) as client:
        async def drain():
            async for _ in client.receive_messages():
                pass
        reader = asyncio.create_task(drain())
        start = time.time()
        for delay, text in PROMPTS[CASE]:
            await asyncio.sleep(max(0, start + delay - time.time()))
            written = str(uuid.uuid4())
            harness.mark("write_input", input_uuid=written)
            async def one():
                yield {"type": "user", "uuid": written, "session_id": SESSION, "parent_tool_use_id": None,
                       "message": {"role": "user", "content": text}}
            await client.query(one(), session_id=SESSION)
        await asyncio.sleep(WATCH[CASE])
        harness.mark("watch_end")
        reader.cancel()
    print(SESSION)

asyncio.run(main())
