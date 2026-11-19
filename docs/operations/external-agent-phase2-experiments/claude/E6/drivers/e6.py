"""E6: does the user's/project allow rule fire before can_use_tool, bypassing delegation scope?"""
import asyncio, json, os, shutil, sys
sys.path.insert(0, os.path.dirname(__file__))
import harness
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny, ResultMessage

ROOT = sys.argv[1]
VARIANT = sys.argv[2]
OUT = sys.argv[3]

base = os.path.join(ROOT, VARIANT)
shutil.rmtree(base, ignore_errors=True)
work, extra, outside = (os.path.join(base, n) for n in ("work", "extra", "outside"))
for d in (work, extra, outside):
    os.makedirs(d)
os.makedirs(os.path.join(work, ".claude"))
# "User's own" allow rules live in the project's local settings (stand-in for user rules; we must not touch ~/.claude).
json.dump({"permissions": {"allow": ["Bash(touch:*)", f"Write(/{outside}/**)", f"Edit(/{outside}/**)"]}},
          open(os.path.join(work, ".claude", "settings.local.json"), "w"))

calls = []


async def can_use_tool(tool, arguments, context):
    path = arguments.get("file_path") or ""
    calls.append({"tool": tool, "input_keys": sorted(arguments), "path_scope": (
        "work" if path.startswith(work) else "extra" if path.startswith(extra) else "outside" if path.startswith(outside) else ("none" if not path else "other"))})
    harness.mark("can_use_tool", tool=tool, path_scope=calls[-1]["path_scope"])
    return PermissionResultDeny(message="denied by experiment", interrupt=False)

common = dict(cli_path="/opt/homebrew/bin/claude", cwd=work, model="haiku", can_use_tool=can_use_tool,
              system_prompt={"type": "preset", "preset": "claude_code"}, tools={"type": "preset", "preset": "claude_code"})
variants = {
    # connector 0.8.0 today: no mode, no allowed tools, all setting sources
    "A_connector_today": dict(setting_sources=["user", "project", "local"]),
    "B_scope_all_sources": dict(setting_sources=["user", "project", "local"], add_dirs=[extra], permission_mode="acceptEdits", allowed_tools=["Bash(ls:*)"]),
    "C_scope_no_sources": dict(setting_sources=[], add_dirs=[extra], permission_mode="acceptEdits", allowed_tools=["Bash(ls:*)"]),
    "D_scope_disallow": dict(setting_sources=["user", "project", "local"], add_dirs=[extra], permission_mode="acceptEdits", allowed_tools=["Bash(ls:*)"], disallowed_tools=["Bash(touch:*)", f"Write(/{outside}/**)"]),
    "E_scope_settings_deny": dict(setting_sources=["user", "project", "local"], add_dirs=[extra], permission_mode="acceptEdits", allowed_tools=["Bash(ls:*)"],
                                  settings=json.dumps({"permissions": {"ask": ["Bash(touch:*)", f"Write(/{outside}/**)"]}})),
    "D2_scope_disallow_edit": dict(setting_sources=["user", "project", "local"], add_dirs=[extra], permission_mode="acceptEdits", allowed_tools=["Bash(ls:*)"], disallowed_tools=["Bash(touch:*)", f"Edit(/{outside}/**)"]),
    "E2_scope_settings_ask_edit": dict(setting_sources=["user", "project", "local"], add_dirs=[extra], permission_mode="acceptEdits", allowed_tools=["Bash(ls:*)"],
                                  settings=json.dumps({"permissions": {"ask": ["Bash(touch:*)", f"Edit(/{outside}/**)"]}})),
    "G_settings_ask_all": dict(setting_sources=["user", "project", "local"], settings=json.dumps({"permissions": {"ask": ["Bash", "Edit", "Write"]}})),
}
probes = [
    ("touch_user_allowed", "Use the Bash tool to run exactly this command and nothing else: touch probe_touch.txt"),
    ("ls_scope_allowed", "Use the Bash tool to run exactly this command and nothing else: ls"),
    ("mkdir_unlisted", "Use the Bash tool to run exactly this command and nothing else: mkdir probe_dir"),
    ("write_in_cwd", f"Use the Write tool to create the file {work}/in_cwd.txt with content x. Do not use Bash."),
    ("write_in_add_dir", f"Use the Write tool to create the file {extra}/in_extra.txt with content x. Do not use Bash."),
    ("write_outside_user_allowed", f"Use the Write tool to create the file {outside}/outside.txt with content x. Do not use Bash."),
]


async def main():
    harness.start(OUT)
    harness.mark("variant", name=VARIANT)
    results = []
    async with ClaudeSDKClient(options=ClaudeAgentOptions(**common, **variants[VARIANT])) as client:
        for name, prompt in probes:
            before = len(calls)
            harness.mark("probe", name=name)
            await client.query(prompt)
            async for m in client.receive_response():
                if isinstance(m, ResultMessage):
                    break
            effects = {
                "touch_user_allowed": os.path.exists(os.path.join(work, "probe_touch.txt")),
                "ls_scope_allowed": None,
                "mkdir_unlisted": os.path.isdir(os.path.join(work, "probe_dir")),
                "write_in_cwd": os.path.exists(os.path.join(work, "in_cwd.txt")),
                "write_in_add_dir": os.path.exists(os.path.join(extra, "in_extra.txt")),
                "write_outside_user_allowed": os.path.exists(os.path.join(outside, "outside.txt")),
            }[name]
            results.append({"probe": name, "can_use_tool_calls": calls[before:], "effect_happened": effects})
    print(json.dumps({"variant": VARIANT, "results": results}))

asyncio.run(main())
