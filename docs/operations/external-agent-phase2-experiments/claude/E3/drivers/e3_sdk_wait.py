"""E3 supplement: registry status of an SDK session while can_use_tool is pending."""
import asyncio, json, os, subprocess, sys, uuid
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny, ResultMessage
CWD = sys.argv[1]; os.makedirs(CWD, exist_ok=True)
SID = str(uuid.uuid4()); rows = []
def status():
    d = json.loads(subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True).stdout)
    return [{k: r.get(k) for k in ("status", "waitingFor")} for r in d if r.get("sessionId") == SID]
async def gate(tool, args, ctx):
    await asyncio.sleep(2); rows.append({"phase": "inside_can_use_tool", "registry": status()})
    return PermissionResultDeny(message="no")
async def main():
    opts = ClaudeAgentOptions(cli_path="/opt/homebrew/bin/claude", cwd=CWD, model="haiku", session_id=SID, can_use_tool=gate,
                              setting_sources=["user", "project", "local"], system_prompt={"type": "preset", "preset": "claude_code"})
    async with ClaudeSDKClient(options=opts) as c:
        await c.query("Use the Bash tool to run exactly: mkdir zz_probe")
        async for m in c.receive_response():
            if isinstance(m, ResultMessage): break
        rows.append({"phase": "after_result", "registry": status()})
    print(json.dumps({"session": SID, "rows": rows}))
asyncio.run(main())
