"""Preserve typed effect state and protect owned tool-result files."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None

INDEX_SQL = {
    "ix_runtime_effects_work_key": "CREATE INDEX ix_runtime_effects_work_key ON runtime_work_effects (work_id, effect_key)",
    "ix_runtime_effects_work_state": "CREATE INDEX ix_runtime_effects_work_state ON runtime_work_effects (work_id, state, effect_key)",
    "ix_runtime_effects_work_updated": "CREATE INDEX ix_runtime_effects_work_updated ON runtime_work_effects (work_id, updated DESC, effect_key DESC)",
    **{
        f"ix_runtime_effects_work_{field}": f"CREATE INDEX ix_runtime_effects_work_{field} ON runtime_work_effects (work_id, json_extract(receipt_json, '$.outcome.{field}'), effect_key)"
        for field in ("pending", "uncertain", "run_id")
    },
    "ix_tool_artifacts_work_id": "CREATE INDEX ix_tool_artifacts_work_id ON tool_artifacts (work_id)",
}


def _native_records(continuation):
    calls, outputs = {}, {}
    if not isinstance(continuation, dict) or not isinstance(continuation.get("payload"), list):
        return calls, outputs
    payload = continuation["payload"]
    if continuation.get("protocol") == "responses":
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("call_id"), str):
                continue
            identity = item["call_id"]
            if item.get("type") == "function_call":
                calls.setdefault(identity, []).append(
                    {"name": item.get("name"), "arguments": item.get("arguments")}
                )
            elif item.get("type") == "function_call_output":
                outputs.setdefault(identity, []).append(item.get("output"))
    elif continuation.get("provider") == "gemini" and continuation.get("protocol") == "gemini":
        native, responses = [], []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("parts"), list):
                continue
            ids = iter(item.get("_call_ids", []))
            for part in item["parts"]:
                if not isinstance(part, dict):
                    continue
                call = part.get("functionCall")
                if item.get("role") == "model" and isinstance(call, dict):
                    identity = next(ids, call.get("id"))
                    if isinstance(identity, str) and isinstance(call.get("args", {}), dict):
                        function = {
                            "name": call.get("name"),
                            "arguments": json.dumps(call.get("args", {})),
                        }
                        native.append((identity, call.get("id"), function))
                        calls.setdefault(identity, []).append(function)
                response = part.get("functionResponse")
                if item.get("role") == "user" and isinstance(response, dict):
                    responses.append(response)
        for response in responses:
            # Synthesized local IDs are recorded in _call_ids. A wire response
            # without an ID is accepted only when its function name is unique;
            # never guess among multiple calls to the same function.
            matched = [
                call
                for call in native
                if (
                    call[1] == response["id"]
                    if isinstance(response.get("id"), str)
                    else call[2]["name"] == response.get("name")
                )
            ]
            body = response.get("response")
            if len(matched) == 1 and isinstance(body, dict):
                outputs.setdefault(matched[0][0], []).append(body.get("output"))
    return calls, outputs


def _journal_record(bind, work_id, cache):
    if work_id in cache:
        return cache[work_id]
    raw = bind.execute(
        sa.text("SELECT chain_id, payload_json FROM runtime_work_journal WHERE work_id=:work_id"),
        {"work_id": work_id},
    ).first()
    record = None
    if raw is not None:
        try:
            payload = json.loads(raw[1])
        except (ValueError, TypeError):
            payload = {}
        transcript = payload.get("transcript", {}) if isinstance(payload, dict) else {}
        if isinstance(transcript, dict) and transcript.get("chain_id") == raw[0]:
            calls, outputs = _native_records(transcript.get("continuation"))
            for item in transcript.get("items", []):
                if not isinstance(item, dict) or not isinstance(item.get("value"), dict):
                    continue
                value = item["value"]
                if item.get("kind") == "message" and value.get("role") == "assistant":
                    for call in value.get("tool_calls", []):
                        if isinstance(call, dict) and isinstance(call.get("function"), dict):
                            calls.setdefault(call.get("id"), []).append(call["function"])
                elif item.get("kind") == "message" and value.get("role") == "tool":
                    outputs.setdefault(value.get("tool_call_id"), []).append(value.get("content"))
                elif item.get("kind") == "result":
                    outputs.setdefault(value.get("call_id"), []).append(value.get("output"))
            record = (raw[0], payload, calls, outputs)
    if len(cache) >= 8:
        cache.pop(next(iter(cache)))
    cache[work_id] = record
    return record


def _original_call(bind, key, work_id, receipt, cache=None):
    """Only the owned original public/private call and receipt prove arguments."""
    parts = key.rsplit(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    chain_id, sequence, call_id = parts
    record = _journal_record(bind, work_id, {} if cache is None else cache)
    if record is None or record[0] != chain_id:
        return None
    _, payload, calls, outputs = record
    candidates = []
    for function in calls.get(call_id, []):
        if function not in candidates:
            candidates.append(function)
    results = outputs.get(call_id, [])
    if (
        len(candidates) == 1
        and results
        and all(result == receipt.get("result") for result in results)
    ):
        return candidates[0]
    metadata, pending = payload.get("metadata", {}), payload.get("pending", [])
    if (
        not isinstance(metadata, dict)
        or metadata.get("sequence") != int(sequence)
        or not isinstance(pending, list)
    ):
        return None
    matched = [call for call in pending if isinstance(call, dict) and call.get("id") == call_id]
    if len(matched) == 1:
        function = {"name": matched[0].get("name"), "arguments": matched[0].get("arguments")}
        if not candidates or candidates == [function]:
            return function
    return None


def _delivery_proof(outcome, body, original, state):
    outcome.update(
        delivered_artifacts=[],
        caption_delivered=False,
        delivered_message=False,
        delivery_target=None,
    )
    if state != "accepted" or original is None or original.get("name") != "send_message":
        return
    try:
        args = json.loads(original.get("arguments", "{}"))
    except (ValueError, TypeError):
        return
    if not isinstance(args, dict):
        return
    text = isinstance(args.get("text"), str) and bool(args["text"].strip())
    artifact = args.get("artifact_id")
    file = body.get("file", body)
    caption = body.get("caption")
    if isinstance(artifact, str) and isinstance(file, dict) and file.get("status") == "succeeded":
        outcome["delivered_artifacts"] = [artifact]
        outcome["artifacts"] = list(dict.fromkeys([*outcome["artifacts"], artifact]))
        outcome["caption_delivered"] = bool(
            text
            and (
                (isinstance(caption, dict) and caption.get("status") == "succeeded")
                or (args.get("attachment_kind") == "image" and body.get("status") == "succeeded")
            )
        )
    outcome["delivered_message"] = bool(
        text
        and (
            outcome["caption_delivered"]
            if args.get("attachment_kind") == "file"
            else body.get("status") == "succeeded"
        )
    )
    if outcome["delivered_artifacts"] or outcome["delivered_message"]:
        outcome["delivery_target"] = body.get("target")


def _convert_outcome(bind, key, work_id, state, receipt, *, has_journal=True, journal_cache=None):
    try:
        result = json.loads(receipt.get("result", "{}"))
    except (ValueError, TypeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    body = result.get("progress", result.get("data", result.get("result", result)))
    body = body if isinstance(body, dict) else {}
    status = body.get("status")
    artifacts = []
    stack = [body]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("artifact_id"), str):
                artifacts.append(item["artifact_id"])
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    original = _original_call(bind, key, work_id, receipt, journal_cache) if has_journal else None
    receipt["outcome"] = {
        "tool": original.get("name", "legacy_tool")
        if original
        else result.get("tool_name", "legacy_tool"),
        "side_effecting": result.get("mutation_committed") is not False,
        "ok": bool(result.get("ok", receipt.get("transport_accepted", False)))
        and not body.get("error")
        and status not in ("failed", "cancelled", "unknown", "uncertain"),
        "run_id": body.get("run_id"),
        "pending": bool(body.get("pending")) or status in ("running", "queued", "waiting"),
        "uncertain": state in ("prepared", "unknown")
        or bool(result.get("uncertain"))
        or bool(body.get("uncertain"))
        or status in ("unknown", "uncertain"),
        "artifacts": list(dict.fromkeys(artifacts)),
        "delivered_artifacts": [],
        "status": status,
        "error_code": result.get("error_code"),
        "mutation_committed": result.get("mutation_committed"),
        "executed": result.get("executed", body.get("executed", True)),
    }
    _delivery_proof(receipt["outcome"], body, original, state)
    target = body.get("target")
    looks_like_delivery = result.get("tool_name") == "send_message" or (
        isinstance(target, dict)
        and target.get("kind") in ("space", "person")
        and isinstance(target.get("id"), str)
        and (
            status == "succeeded"
            or any(
                isinstance(body.get(part), dict) and body[part].get("status") == "succeeded"
                for part in ("file", "caption")
            )
        )
    )
    if (
        state == "accepted"
        and looks_like_delivery
        and not (
            receipt["outcome"]["delivered_artifacts"] or receipt["outcome"]["delivered_message"]
        )
    ):
        # Transport acceptance remains true. Missing original arguments require
        # reconciliation; they never authorize a new send under another call ID.
        receipt["outcome"]["uncertain"] = True
        receipt["outcome"]["delivery_verification_required"] = True

    return result, receipt["outcome"]


def upgrade() -> None:
    op.add_column("tool_artifacts", sa.Column("work_id", sa.String(36)))
    op.add_column("tool_artifacts", sa.Column("effect_key", sa.String(256)))
    op.add_column("tool_artifacts", sa.Column("sha256", sa.String(64)))
    op.add_column(
        "tool_artifacts", sa.Column("deleting", sa.Boolean(), nullable=False, server_default="0")
    )
    # Maintenance-only migration: bounded receipt pages and eight parsed journals
    # are converted under the offline schema transaction, with no file/network I/O.
    # Runtime writers never perform this historical compatibility scan.
    bind, cursor = op.get_bind(), ""
    has_journal = sa.inspect(bind).has_table("runtime_work_journal")
    journal_cache = {}
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT effect_key, work_id, state, receipt_json FROM runtime_work_effects "
                "WHERE effect_key > :cursor ORDER BY effect_key LIMIT 128"
            ),
            {"cursor": cursor},
        ).all()
        if not rows:
            break
        updates = []
        for key, work_id, state, raw in rows:
            receipt = json.loads(raw)
            if "outcome" in receipt:
                continue
            result, _ = _convert_outcome(
                bind,
                key,
                work_id,
                state,
                receipt,
                has_journal=has_journal,
                journal_cache=journal_cache,
            )
            handle = result.get("artifact_handle")
            if isinstance(handle, str):
                bind.execute(
                    sa.text(
                        "UPDATE tool_artifacts SET work_id=:work_id, effect_key=:key "
                        "WHERE handle_id=:handle AND work_id IS NULL"
                    ),
                    {"work_id": work_id, "key": key, "handle": handle},
                )
            updates.append({"key": key, "receipt": json.dumps(receipt, ensure_ascii=False)})
        if updates:
            bind.execute(
                sa.text(
                    "UPDATE runtime_work_effects SET receipt_json=:receipt WHERE effect_key=:key"
                ),
                updates,
            )
        cursor = rows[-1][0]
    for sql in INDEX_SQL.values():
        op.execute(sa.text(sql))


def downgrade() -> None:
    for name in reversed(INDEX_SQL):
        op.drop_index(name)
    for column in ("deleting", "sha256", "effect_key", "work_id"):
        op.drop_column("tool_artifacts", column)
