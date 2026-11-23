"""Scripted ACP agent: modes, permission requests and agent-initiated turns.

Each prompt's text is a JSON script. ``during`` steps run while the prompt is in
flight, ``after`` steps run after its response (an agent-initiated turn, task book
§15 E5). Every outcome is appended to FAKE_ACP_TRACE as one JSON line.

FAKE_ACP_STEERING advertises the ``_session/steering`` extension (top-level
``_meta.steering.supported``, as codex-acp 2.9 and claude-agent-acp 0.109 do);
FAKE_ACP_NO_LIST leaves out session/list and session/resume (Gemini CLI declares
only loadSession).
"""

import json
import os
import sys
import time
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


def say(session, text):
    content = {"type": "text", "text": text}
    update(session, {"sessionUpdate": "agent_message_chunk", "content": content})


def steering(message):
    """Answer one _session/steering request; the outcome follows the script."""
    params = message["params"]
    text = params["prompt"][0]["text"]
    record(method="_session/steering", text=text, meta=params.get("_meta"))
    return text


def run(session, steps):
    """Run the steps; a returned stop reason ends the prompt early."""
    for step in steps:
        if "say" in step:
            say(session, step["say"])
        elif "wait" in step:
            # Hold the turn until the test creates this file (e.g. after a detach).
            deadline = time.monotonic() + 10
            while not Path(step["wait"]).exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        elif "steer" in step:
            # Wait for the client's steer while this prompt is in flight.
            message = json.loads(sys.stdin.readline())
            assert message["method"] == "_session/steering", message
            text = steering(message)
            if step["steer"] == "promptRequired":
                # This turn already settled at the agent: the steer is not consumed.
                outcome = {"outcome": "promptRequired", "reason": "noRunningTurn"}
                send({"id": message["id"], "result": outcome})
                continue
            send({"id": message["id"], "result": {"outcome": "injected"}})
            say(session, f"steered:{text}")
        elif "tool" in step:
            update(session, {"sessionUpdate": "tool_call", **step["tool"]})
        elif "update" in step:
            update(session, step["update"])
        elif "permission" in step:
            response = request(
                "session/request_permission", {"sessionId": session, **step["permission"]}
            )
            outcome = response.get("result", {}).get("outcome")
            record(
                method="session/request_permission",
                tool=step["permission"]["toolCall"]["toolCallId"],
                outcome=outcome,
            )
            rejects = {
                item["optionId"]
                for item in step["permission"]["options"]
                if item["kind"].startswith("reject")
            }
            rejected = outcome["outcome"] == "cancelled" or outcome.get("optionId") in rejects
            if rejected and step.get("rejected") == "cancel_turn":
                # codex-acp: this reject ends the whole prompt.
                return "cancelled"
            if rejected and step.get("rejected") == "fail_tool":
                # Gemini CLI: only this tool call fails; the turn goes on.
                update(
                    session,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": step["permission"]["toolCall"]["toolCallId"],
                        "status": "failed",
                    },
                )
        elif "write" in step:
            response = request(
                "fs/write_text_file",
                {"sessionId": session, "path": step["write"], "content": "x"},
            )
            record(method="fs/write_text_file", path=step["write"], error="error" in response)
        elif "read" in step:
            response = request("fs/read_text_file", {"sessionId": session, "path": step["read"]})
            record(method="fs/read_text_file", path=step["read"], error="error" in response)
        elif "terminal" in step:
            params = {
                "sessionId": session,
                "command": step["terminal"],
                "args": step.get("args", []),
            }
            if "cwd" in step:
                params["cwd"] = step["cwd"]
            response = request("terminal/create", params)
            row = {"method": "terminal/create", "error": "error" in response}
            if step.get("output") and "result" in response:
                terminal = {"sessionId": session, "terminalId": response["result"]["terminalId"]}
                request("terminal/wait_for_exit", terminal)
                row["output"] = request("terminal/output", terminal)["result"]["output"]
                if step.get("release"):
                    request("terminal/release", terminal)
            if "cwd" in step or step.get("output"):
                row["cwd"] = step.get("cwd")
            record(**row)
    return None


record(method="started", pid=os.getpid())
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    parameters = message.get("params", {})
    result = {}
    after = []
    if method == "initialize":
        session_capabilities = {}
        if not os.environ.get("FAKE_ACP_NO_LIST"):
            session_capabilities.update(list={}, resume={})
        if not os.environ.get("FAKE_ACP_NO_CLOSE"):
            session_capabilities["close"] = {}
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": session_capabilities,
            },
        }
        if os.environ.get("FAKE_ACP_STEERING"):
            result["_meta"] = {"steering": {"supported": True}}
    elif method == "_session/steering":
        # No prompt of this session is in flight here.
        steering(message)
        result = {"outcome": "promptRequired", "reason": "noRunningTurn"}
    elif method in {"session/new", "session/load"}:
        if method == "session/new":
            result = {"sessionId": f"session-{sequence}"}
            sequence += 1
        session = parameters.get("sessionId") or result["sessionId"]
        record(method=method, cwd=parameters["cwd"], session=session)
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
        result = {"stopReason": run(session, script.get("during", [])) or "end_turn"}
        after = script.get("after", [])
    if "id" in message:
        send({"id": message["id"], "result": result})
    if after:
        run(parameters["sessionId"], after)
