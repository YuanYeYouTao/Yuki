"""Remove implicit reply effects and migrate scheduled text delivery."""

import hashlib
import json

from alembic import op
from sqlalchemy import text

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("automations", "automation_versions"):
        rows = connection.execute(text(f"SELECT id, script_json FROM {table}")).fetchall()
        for row_id, raw in rows:
            script = json.loads(raw)
            changed = False
            for step in script.get("steps", []):
                arguments = step.get("arguments")
                if isinstance(arguments, dict) and "reply_state" in arguments:
                    arguments.pop("reply_state")
                    changed = True
            if changed:
                encoded = json.dumps(
                    script, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                hash_input = json.loads(encoded)
                limits = hash_input.get("limits")
                if isinstance(limits, dict) and not limits.get("agent_budget_managed", False):
                    limits.pop("agent_budget_managed", None)
                script_hash = hashlib.sha256(
                    json.dumps(
                        hash_input,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    text(
                        f"UPDATE {table} "
                        "SET script_json = :script, script_hash = :script_hash WHERE id = :id"
                    ),
                    {
                        "id": row_id,
                        "script": encoded,
                        "script_hash": script_hash,
                    },
                )
    op.execute("DROP TABLE IF EXISTS reply_effect_events")
    op.execute(
        "UPDATE runtime_config_overrides AS retired "
        "SET config_key = 'speech.agent_delivery_enabled' "
        "WHERE config_key = 'speech.agent_effects_enabled' AND NOT EXISTS ("
        "SELECT 1 FROM runtime_config_overrides AS current "
        "WHERE current.config_key = 'speech.agent_delivery_enabled' "
        "AND current.scope_type = retired.scope_type "
        "AND (current.scope_type = 'global' "
        "OR current.canonical_person_id = retired.canonical_person_id "
        "OR current.canonical_space_id = retired.canonical_space_id))"
    )
    op.execute(
        "DELETE FROM runtime_config_overrides WHERE config_key = 'speech.agent_effects_enabled'"
    )
    op.execute(
        "DELETE FROM runtime_config_overrides WHERE config_key IN ("
        "'speech.spontaneous_frequency', 'emoji.spontaneous_frequency', "
        "'emoji.max_effects_per_reply', 'reply.cancel_on_new_message')"
    )


def downgrade() -> None:
    # Cadence events and retired policy overrides cannot be reconstructed.
    pass
