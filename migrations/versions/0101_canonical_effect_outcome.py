"""Give every legacy effect receipt one canonical ``outcome``.

Receipts written before typed outcomes carried only a serialized ``result``
(or an empty ``outcome``). SQL then had to re-parse that text to decide pending
and unknown. This migration decodes it once, with a frozen copy of the pure
decoder, and stores the canonical projection; ``result``, media, opaque and
journal data are untouched. Undecodable rows become explicit unknown. Each row
is written with a CAS on its original bytes, paged by primary key, so re-running
is a no-op.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

PAGE = 256
_UNKNOWN = {"ok": False, "uncertain": True, "side_effecting": True, "executed": True}


def _bool(value, default=False):
    if value is None:
        return default
    if type(value) is not bool:
        raise ValueError("bad boolean")
    return value


def decode(receipt: dict, state: str) -> dict | None:
    """Frozen legacy decoder; returns None when the row already has an outcome."""
    outcome = receipt.get("outcome")
    if isinstance(outcome, dict) and outcome:
        return None
    if receipt.get("transport_accepted") is not None or (
        state == "failed" and receipt.get("error") == "delivery_not_dispatched"
    ):
        return None  # Final transport receipts have their own exact shape.
    payload = receipt.get("result")
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            raise ValueError("no ok")
        if payload.get("truncated") is True:
            raise ValueError("truncated")
        body = payload.get("data", payload)
        if not isinstance(body, dict):
            body = payload
        if isinstance(body.get("progress"), dict):
            body = body["progress"]
        if body.get("truncated") is True:
            raise ValueError("truncated")
        status = body.get("status")
        if status is not None and not isinstance(status, str):
            raise ValueError("bad status")
        uncertain = (
            _bool(payload.get("uncertain"))
            or _bool(body.get("uncertain"))
            or status in {"uncertain", "unknown"}
        )
        pending = _bool(body.get("pending")) or status in {"running", "queued", "waiting"}
        committed = payload.get("mutation_committed")
        if committed is not None and type(committed) is not bool:
            raise ValueError("bad committed")
        result = {
            "ok": payload["ok"]
            and not body.get("error")
            and status not in {"failed", "cancelled", "uncertain", "unknown"}
            and body.get("exit_code") in (None, 0),
            "pending": pending,
            "uncertain": uncertain,
            "side_effecting": True,
            "executed": _bool(body.get("executed"), True),
            "status": status,
            "mutation_committed": None if uncertain else committed,
            "run_id": body.get("run_id"),
            "legacy_migrated": True,
        }
    except (TypeError, ValueError):
        result = {**_UNKNOWN, "legacy_migrated": True}
    if state in {"prepared", "unknown"}:
        result["uncertain"] = True
    return result


def upgrade() -> None:
    bind = op.get_bind()
    cursor = ""
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT effect_key, state, receipt_json FROM runtime_work_effects "
                "WHERE effect_key > :cursor ORDER BY effect_key LIMIT :page"
            ),
            {"cursor": cursor, "page": PAGE},
        ).all()
        if not rows:
            return
        cursor = rows[-1][0]
        for key, state, raw in rows:
            try:
                receipt = json.loads(raw)
            except ValueError:
                receipt = None
            if not isinstance(receipt, dict):
                continue
            outcome = decode(receipt, state)
            if outcome is None:
                continue
            bind.execute(
                sa.text(
                    "UPDATE runtime_work_effects SET receipt_json = :new "
                    "WHERE effect_key = :key AND receipt_json = :old"
                ),
                {
                    "key": key,
                    "old": raw,
                    "new": json.dumps({**receipt, "outcome": outcome}, ensure_ascii=False),
                },
            )


def downgrade() -> None:
    # The added outcome is a pure projection of the untouched original result.
    pass
