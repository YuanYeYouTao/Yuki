"""Raw-frame tee for claude-agent-sdk 0.2.172: logs body-free frames in both directions."""
import json, time
from claude_agent_sdk._internal.transport import subprocess_cli as sc

T0 = time.time()
LOG = None


def start(path):
    global LOG, T0
    LOG = open(path, "w")
    T0 = time.time()


def mark(_mark, **extra):
    _emit({"side": "mark", "type": _mark, **extra})


def _emit(row):
    row = {"t": round(time.time() - T0, 2), **row}
    if LOG:
        LOG.write(json.dumps(row) + "\n"); LOG.flush()


def _blocks(content):
    out = []
    if isinstance(content, list):
        for b in content:
            e = {"type": b.get("type")}
            if b.get("type") == "tool_use":
                e |= {"id": b.get("id"), "name": b.get("name"), "input_keys": sorted((b.get("input") or {}).keys())}
            if b.get("type") == "tool_result":
                e |= {"tool_use_id": b.get("tool_use_id"), "is_error": b.get("is_error")}
            out.append(e)
    elif isinstance(content, str):
        out.append({"type": "text"})
    return out


KEEP = ("type", "subtype", "uuid", "session_id", "parent_tool_use_id", "isReplay", "is_error", "state",
        "status", "task_id", "tool_use_id", "permissionMode", "num_turns", "stop_reason", "request_id",
        "model", "isSynthetic", "origin", "priority", "lifecycle", "command_uuid", "event", "hook_event_name")


def sanitize(d):
    row = {k: d[k] for k in KEEP if k in d and not isinstance(d[k], (dict, list))}
    msg = d.get("message")
    if isinstance(msg, dict):
        row["blocks"] = _blocks(msg.get("content"))
    req = d.get("request")
    if isinstance(req, dict):
        row["request"] = {k: req.get(k) for k in ("subtype", "tool_name", "tool_use_id", "blocked_path", "decision_reason", "mode", "agent_id") if req.get(k) is not None}
        if "input" in req and isinstance(req["input"], dict):
            row["request"]["input_keys"] = sorted(req["input"].keys())
        if "permission_suggestions" in req:
            row["request"]["suggestion_types"] = [s.get("type") for s in req.get("permission_suggestions") or []]
    resp = d.get("response")
    if isinstance(resp, dict):
        inner = resp.get("response") if isinstance(resp.get("response"), dict) else {}
        row["response"] = {"subtype": resp.get("subtype"), "request_id": resp.get("request_id"), "behavior": inner.get("behavior")}
    if d.get("type") == "system" and d.get("subtype") == "init":
        row["init"] = {"permissionMode": d.get("permissionMode"), "cwd_set": bool(d.get("cwd")), "tools_count": len(d.get("tools") or []),
                       "has_ScheduleWakeup": "ScheduleWakeup" in (d.get("tools") or []),
                       "has_SendMessage": "SendMessage" in (d.get("tools") or []),
                       "has_ListAgents": "ListAgents" in (d.get("tools") or []),
                       "claude_code_version": d.get("claude_code_version")}
    return row


_orig_read = sc.SubprocessCLITransport._read_messages_impl
_orig_write = sc.SubprocessCLITransport.write


async def _read(self):
    async for d in _orig_read(self):
        try:
            _emit({"side": "cli->sdk", **sanitize(d)})
        except Exception as e:  # logging must not break the run
            _emit({"side": "cli->sdk", "log_error": str(e)})
        yield d


async def _write(self, data):
    try:
        for line in data.splitlines():
            if line.strip():
                _emit({"side": "sdk->cli", **sanitize(json.loads(line))})
    except Exception as e:
        _emit({"side": "sdk->cli", "log_error": str(e)})
    return await _orig_write(self, data)


sc.SubprocessCLITransport._read_messages_impl = _read
sc.SubprocessCLITransport.write = _write
