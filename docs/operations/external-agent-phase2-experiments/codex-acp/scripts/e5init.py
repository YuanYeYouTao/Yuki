import asyncio, json, os
from acpc import ACPClient, Log
HERE=os.path.dirname(os.path.abspath(__file__)); WS=os.path.join(HERE,"acpws")
async def probe(name, argv, env):
    log = Log(os.path.join(HERE,"artifacts","E5",f"frames-init-{name}.jsonl"))
    c = ACPClient(name, log, argv, WS, env, [WS]); await c.start()
    r = await c.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True}, "clientInfo": {"name": "p2exp", "version": "0"}}, timeout=60)
    caps = r.get("agentCapabilities", {})
    log.frame(name, "local", method="agentCapabilities", caps=caps, authMethods=[a.get("id") for a in r.get("authMethods", [])], agentInfo=r.get("agentInfo"))
    try:
        s = await c.call("session/new", {"cwd": WS, "mcpServers": []}, timeout=60)
        log.note(f"session/new ok; modes={[m.get('id') for m in (s.get('modes') or {}).get('availableModes',[])]}; models={[m.get('modelId') for m in (s.get('models') or {}).get('availableModels',[])][:8]}")
    except Exception as e:
        log.note(f"session/new failed: {str(e)[:200]}")
    await c.stop()
async def main():
    env = dict(os.environ)
    genv = dict(env, HOME=os.path.join(HERE,"acp","ghome")); genv.pop("GEMINI_API_KEY", None)
    await probe("gemini-0.68.0", [os.path.join(HERE,"acp/node_modules/.bin/gemini"), "--acp"], genv)
    await probe("claude-agent-acp-0.108.0", [os.path.join(HERE,"acp/node_modules/.bin/claude-agent-acp")], dict(env, ANTHROPIC_MODEL="haiku"))
asyncio.run(main())
