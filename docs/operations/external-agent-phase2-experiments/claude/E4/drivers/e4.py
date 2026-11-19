"""E4: an SDK (headless) Claude session sends a cross-session message to a live terminal session we started."""
import asyncio, json, os, sys, time, uuid
sys.path.insert(0, os.path.dirname(__file__))
import harness
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny

CASE, CWD, OUT, TARGET_NAME, TARGET_SID, TARGET_PID = sys.argv[1:7]
os.makedirs(CWD, exist_ok=True)
SESSION = str(uuid.uuid4())
ALLOWED = {TARGET_NAME, TARGET_SID, TARGET_PID, f"/tmp/cc-socks/{TARGET_PID}.sock"}

async def gate(tool, arguments, context):
    if tool == "SendMessage":
        target = next((str(v) for k, v in arguments.items() if k in ("to", "target", "recipient", "agent", "name", "session", "address")), "")
        ok = any(a and a in target for a in ALLOWED)
        harness.mark("can_use_tool", tool=tool, input_keys=sorted(arguments), target_is_our_session=ok)
        return PermissionResultAllow() if ok else PermissionResultDeny(message="only the experiment session may be messaged")
    harness.mark("can_use_tool", tool=tool, input_keys=sorted(arguments))
    if tool == "ListAgents":
        return PermissionResultAllow()
    return PermissionResultDeny(message="not needed")

PROMPT = (f"Use the SendMessage tool to send a message to the Claude session named {TARGET_NAME} (session id {TARGET_SID}). "
          "The message should ask it to reply with the single word pong. Do not message any other session. "
          "If you need to discover the address first, use ListAgents but only message that one session. Then end your turn.")

async def main():
    harness.start(OUT)
    harness.mark("case", case=CASE, session=SESSION)
    opts = ClaudeAgentOptions(cli_path="/opt/homebrew/bin/claude", cwd=CWD, model="haiku", session_id=SESSION,
                              system_prompt={"type": "preset", "preset": "claude_code"}, tools={"type": "preset", "preset": "claude_code"},
                              setting_sources=["user", "project", "local"], can_use_tool=gate, settings=json.dumps({"permissions": {"ask": ["SendMessage"]}}),
                              extra_args={"replay-user-messages": None})
    async with ClaudeSDKClient(options=opts) as client:
        async def drain():
            async for _ in client.receive_messages():
                pass
        reader = asyncio.create_task(drain())
        written = str(uuid.uuid4())
        harness.mark("write_input", input_uuid=written)
        async def one():
            yield {"type": "user", "uuid": written, "session_id": SESSION, "parent_tool_use_id": None, "message": {"role": "user", "content": PROMPT}}
        await client.query(one(), session_id=SESSION)
        await asyncio.sleep(int(os.environ.get("WATCH", "90")))
        harness.mark("watch_end")
        reader.cancel()
    print(SESSION)

asyncio.run(main())
