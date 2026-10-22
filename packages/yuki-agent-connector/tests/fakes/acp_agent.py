"""Deterministic native ACP peer, exercising real SDK callbacks on its host."""

import json
import os
import sys
from pathlib import Path

sequence = 10


def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


def request(method, params):
    global sequence
    sequence += 1
    send({"id": sequence, "method": method, "params": params})
    response = json.loads(sys.stdin.readline())
    assert response["id"] == sequence, response
    if "error" in response:
        raise RuntimeError(response["error"])
    return response.get("result")


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    parameters = message.get("params", {})
    result = {}
    if method == "initialize":
        capabilities = {"loadSession": True}
        if not os.environ.get("WITHOUT_SESSION_LIST"):
            capabilities["sessionCapabilities"] = {"list": {}, "resume": {}}
        result = {"protocolVersion": 1, "agentCapabilities": capabilities}
    elif method == "session/list":
        result = {
            "sessions": [{"sessionId": "native-session", "cwd": os.getcwd(), "title": "Local"}]
        }
    elif method == "session/new":
        assert parameters["cwd"] == os.getcwd()
        result = {"sessionId": "native-session"}
    elif method == "session/prompt":
        session = parameters["sessionId"]
        send(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session,
                    "update": {
                        "sessionUpdate": "plan",
                        "entries": [
                            {
                                "content": "Use actual local callbacks",
                                "priority": "medium",
                                "status": "in_progress",
                            }
                        ],
                    },
                },
            }
        )
        permission = request(
            "session/request_permission",
            {
                "sessionId": session,
                "toolCall": {
                    "toolCallId": "tool",
                    "title": "Write local file",
                    "status": "pending",
                },
                "options": [
                    {"optionId": "allow-original", "name": "Allow once", "kind": "allow_once"}
                ],
            },
        )
        assert permission["outcome"]["optionId"] == "allow-original"
        target = str(Path.cwd() / "written-by-actual-client.txt")
        request(
            "fs/write_text_file",
            {"sessionId": session, "path": target, "content": "LOCAL_FILESYSTEM"},
        )
        read = request("fs/read_text_file", {"sessionId": session, "path": target})
        assert read["content"] == "LOCAL_FILESYSTEM"
        terminal = request(
            "terminal/create",
            {
                "sessionId": session,
                "command": sys.executable,
                "args": ["-c", "print('LOCAL_TERMINAL')"],
                "cwd": str(Path.cwd()),
            },
        )["terminalId"]
        request("terminal/wait_for_exit", {"sessionId": session, "terminalId": terminal})
        output = request("terminal/output", {"sessionId": session, "terminalId": terminal})
        assert output["output"].strip() == "LOCAL_TERMINAL"
        request("terminal/release", {"sessionId": session, "terminalId": terminal})
        for token in ["LOCAL_", "RESULT"]:
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": session,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": token},
                        },
                    },
                }
            )
        result = {"stopReason": "end_turn"}
    if "id" in message:
        send({"id": message["id"], "result": result})
