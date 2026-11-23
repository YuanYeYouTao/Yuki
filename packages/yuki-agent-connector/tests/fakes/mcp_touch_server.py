"""Minimal stdio MCP server with one side-effect tool, ``touch``, for real-agent runs.

A real Claude (SDK or claude-agent-acp) loads it from a project ``.mcp.json``; the
test then checks whether the file outside the delegation's scope was written.
"""

import json
import sys
from pathlib import Path


def reply(identity, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identity, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method, identity = message.get("method"), message.get("id")
    if method == "initialize":
        reply(
            identity,
            {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "yukiprobe", "version": "1"},
            },
        )
    elif method == "tools/list":
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        tool = {"name": "touch", "description": "Create the file at an absolute path."}
        reply(identity, {"tools": [{**tool, "inputSchema": {**schema, "required": ["path"]}}]})
    elif method == "tools/call":
        path = Path(message["params"]["arguments"]["path"])
        path.write_text("touched\n")
        reply(identity, {"content": [{"type": "text", "text": f"created {path}"}]})
    elif identity is not None:
        reply(identity, {})
