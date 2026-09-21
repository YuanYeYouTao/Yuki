"""Remove retired per-task Agent tool-selection metadata."""

import hashlib
import json

from alembic import op
from sqlalchemy import text

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def _script_hash(script: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(script, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    hash_input = json.loads(encoded)
    limits = hash_input.get("limits")
    if isinstance(limits, dict) and not limits.get("agent_budget_managed", False):
        limits.pop("agent_budget_managed", None)
    digest = hashlib.sha256(
        json.dumps(
            hash_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return encoded, digest


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("automations", "automation_versions"):
        rows = connection.execute(text(f"SELECT id, script_json FROM {table}")).fetchall()
        for row_id, raw in rows:
            script = json.loads(raw)
            changed = False
            for step in script.get("steps", []):
                if not isinstance(step, dict) or step.get("call") != "yuki.agent":
                    continue
                arguments = step.get("arguments")
                if isinstance(arguments, dict) and "allowed_capabilities" in arguments:
                    arguments.pop("allowed_capabilities")
                    changed = True
            if not changed:
                continue
            encoded, digest = _script_hash(script)
            connection.execute(
                text(
                    f"UPDATE {table} "
                    "SET script_json = :script, script_hash = :script_hash WHERE id = :id"
                ),
                {"id": row_id, "script": encoded, "script_hash": digest},
            )


def downgrade() -> None:
    # Removed inert metadata carried no executable authority and is not reconstructed.
    pass
