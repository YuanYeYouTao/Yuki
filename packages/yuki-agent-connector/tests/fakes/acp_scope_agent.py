"""Scripted ACP agent: modes, permission requests and agent-initiated turns.

Each prompt's text is a JSON script. ``during`` steps run while the prompt is in
flight, ``after`` steps run after its response (an agent-initiated turn, task book
§15 E5). Every outcome is appended to FAKE_ACP_TRACE as one JSON line.
"""

import json
import os
import sys
from pathlib import Path

sequence = 100
trace = Path(os.environ["FAKE_ACP_TRACE"])
modes = [item for item in os.environ.get("FAKE_ACP_MODES", "").split(",") if item]


def record(**value):
    with trace.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value) + "\n")


def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


def request(method, params):
    global sequence
    sequence += 1
    send({"id": sequence, "method": method, "params": params})
    response = json.loads(sys.stdin.readline())
    assert response["id"] == sequence, response
    return response


def update(session, value):
    send({"method": "session/update", "params": {"sessionId": session, "update": value}})


def run(session, steps):
    for step in steps:
        if "tool" in step:
            update(session, {"sessionUpdate": "tool_call", **step["tool"]})
        elif "update" in step:
            update(session, step["update"])
        elif "permission" in step:
            response = request(
                "session/request_permission", {"sessionId": session, **step["permission"]}
            )
            record(
                method="session/request_permission",
                tool=step["permission"]["toolCall"]["toolCallId"],
                outcome=response.get("result", {}).get("outcome"),
            )
        elif "write" in step:
            response = request(
                "fs/write_text_file",
                {"sessionId": session, "path": step["write"], "content": "x"},
            )
            record(method="fs/write_text_file", path=step["write"], error="error" in response)
        elif "terminal" in step:
            response = request(
                "terminal/create",
                {"sessionId": session, "command": step["terminal"], "args": []},
            )
            record(method="terminal/create", error="error" in response)


record(method="started", pid=os.getpid())
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    parameters = message.get("params", {})
    result = {}
    after = []
    if method == "initialize":
        session_capabilities = {"list": {}, "resume": {}}
        if not os.environ.get("FAKE_ACP_NO_CLOSE"):
            session_capabilities["close"] = {}
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": session_capabilities,
            },
        }
    elif method == "session/new":
        result = {"sessionId": f"session-{sequence}"}
        sequence += 1
        if modes:
            result["modes"] = {
                "currentModeId": os.environ.get("FAKE_ACP_CURRENT_MODE", modes[-1]),
                "availableModes": [{"id": mode, "name": mode} for mode in modes],
            }
    elif method == "session/set_mode":
        record(method=method, mode=parameters["modeId"], session=parameters["sessionId"])
    elif method == "session/close":
        record(method=method, session=parameters["sessionId"])
    elif method == "session/prompt":
        session = parameters["sessionId"]
        script = json.loads(parameters["prompt"][0]["text"])
        record(method=method, session=session)
        run(session, script.get("during", []))
        result = {"stopReason": "end_turn"}
        after = script.get("after", [])
    if "id" in message:
        send({"id": message["id"], "result": result})
    if after:
        run(parameters["sessionId"], after)
