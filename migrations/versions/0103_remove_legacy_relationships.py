"""Remove retired relationship storage and automation context declarations."""

import hashlib
import json

from alembic import op
from sqlalchemy import text

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

_CONFIG_KEYS = (
    "relationship.initial_affection",
    "relationship.initial_trust",
    "relationship.confidence_threshold",
    "relationship.max_auto_delta",
    "relationship.daily_positive_cap",
    "relationship.daily_negative_cap",
    "relationship.conflict_preference_min_gap",
)


def upgrade() -> None:
    connection = op.get_bind()
    for table in ("relationship_jobs", "relationship_events", "person_relationships"):
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
    connection.exec_driver_sql('DROP INDEX IF EXISTS "ix_chat_events_conversation_author_id"')
    for key in _CONFIG_KEYS:
        connection.execute(
            text("DELETE FROM runtime_config_overrides WHERE config_key = :key"), {"key": key}
        )

    for table in ("automations", "automation_versions"):
        rows = connection.execute(text(f"SELECT id, script_json FROM {table}")).fetchall()
        for row_id, raw in rows:
            script = json.loads(raw)
            context = script.get("context")
            if not isinstance(context, dict) or "include_relationship" not in context:
                continue
            del context["include_relationship"]
            encoded = json.dumps(script, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            # Repository JSON already contains the validated DSL defaults and
            # excludes optional null fields. Match its existing canonical hash.
            hash_input = json.loads(encoded)
            limits = hash_input.get("limits")
            if isinstance(limits, dict) and not limits.get("agent_budget_managed", False):
                limits.pop("agent_budget_managed", None)
            digest = hashlib.sha256(
                json.dumps(
                    hash_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                text(
                    f"UPDATE {table} SET script_json = :script, script_hash = :hash WHERE id = :id"
                ),
                {"id": row_id, "script": encoded, "hash": digest},
            )


def downgrade() -> None:
    raise RuntimeError("Retired relationship data cannot be reconstructed")
