"""A user PreToolUse hook that allows every tool, for real-agent runs (#650).

It stands in for the user's own hook in a project's local settings and records each
call next to itself, so a run shows that the hook was consulted.
"""

import json
import sys
from pathlib import Path

event = json.load(sys.stdin)
with (Path(sys.argv[1])).open("a") as log:
    log.write(json.dumps({"tool": event.get("tool_name")}) + "\n")
json.dump(
    {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "user hook allows everything",
        }
    },
    sys.stdout,
)
