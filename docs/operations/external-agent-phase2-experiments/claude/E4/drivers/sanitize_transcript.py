"""Emit a body-free structural view of a Claude transcript JSONL (types, uuids, parent chain)."""
import json, sys
from datetime import datetime
path = sys.argv[1]
t0 = None
for line in open(path):
    o = json.loads(line)
    ts = o.get("timestamp")
    t = None
    if ts:
        v = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        t0 = t0 if t0 is not None else v
        t = round(v - t0, 2)
    m = o.get("message") or {}
    c = m.get("content")
    blocks = []
    if isinstance(c, list):
        for b in c:
            e = {"type": b.get("type")}
            if b.get("type") == "tool_use":
                e |= {"id": b.get("id"), "name": b.get("name")}
            if b.get("type") == "tool_result":
                e |= {"tool_use_id": b.get("tool_use_id"), "is_error": b.get("is_error")}
            blocks.append(e)
    elif isinstance(c, str):
        blocks = ["<text>"]
    row = {"t": t, "type": o.get("type"), "subtype": o.get("subtype"), "uuid": o.get("uuid"),
           "parentUuid": o.get("parentUuid"), "sessionId": o.get("sessionId"), "entrypoint": o.get("entrypoint"),
           "isSidechain": o.get("isSidechain"), "promptSource": o.get("promptSource"), "turnOrigin": o.get("turnOrigin"),
           "permissionMode": o.get("permissionMode"), "blocks": blocks}
    att = o.get("attachment")
    if isinstance(att, dict):
        row["attachment_type"] = att.get("type")
    if o.get("type") in ("api-request-blob", "api-request", "file-history-snapshot"):
        row = {"t": t, "type": o.get("type")}
    print(json.dumps({k: v for k, v in row.items() if v not in (None, [], "")}))
