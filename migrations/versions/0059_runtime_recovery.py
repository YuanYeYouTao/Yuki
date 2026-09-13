"""Store activation recovery and delivery intent independently of model prose."""

import json
from collections import defaultdict

from alembic import op

from qq_ai_bot.conversation.rollup.signals import signals
from qq_ai_bot.runtime.work_recovery_schema import (
    deliveries,
    install_quota,
    invocations,
    quota,
    recovery,
)

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    recovery.create(op.get_bind(), checkfirst=True)
    deliveries.create(op.get_bind(), checkfirst=True)
    signals.create(op.get_bind(), checkfirst=True)
    invocations.create(op.get_bind(), checkfirst=True)
    quota.create(op.get_bind(), checkfirst=True)
    install_quota(op.get_bind())
    reconcile_legacy_progress(op.get_bind())


def reconcile_legacy_progress(connection) -> None:
    """Old snapshots are cumulative observations, never additional charges."""
    from sqlalchemy import text

    groups = defaultdict(dict)
    owners = defaultdict(set)
    for row in connection.execute(
        text("SELECT source_json, progress_json FROM sandbox_task_runs WHERE progress_json != '{}'")
    ):
        try:
            source, progress = json.loads(row.source_json), json.loads(row.progress_json)
            identity, group = source.get("work_id"), progress.get("group_id")
            if not identity or not group:
                continue
            values = tuple(
                max(0, int(progress.get(key, 0)))
                for key in ("models_used", "tools_used", "messages_used")
            )
        except (ValueError, TypeError, AttributeError):
            continue
        owners[group].add(identity)
        previous = groups[identity].get(group, (0, 0, 0))
        groups[identity][group] = tuple(max(a, b) for a, b in zip(values, previous, strict=True))
    for identity, snapshots in groups.items():
        values = tuple(max(v[index] for v in snapshots.values()) for index in range(3))
        connection.execute(
            text(
                "UPDATE runtime_work SET model_requests=MAX(model_requests,:m), tool_calls=MAX(tool_calls,:t), sent_messages=MAX(sent_messages,:s) WHERE id=:id"
            ),
            dict(id=identity, m=values[0], t=values[1], s=values[2]),
        )
        if len(snapshots) > 1 or any(len(owners[group]) > 1 for group in snapshots):
            connection.execute(
                text(
                    "UPDATE runtime_work SET state='suspended', reason='legacy_budget_ownership_ambiguous', revision=revision+1 WHERE id=:id AND state NOT IN ('completed','failed','cancelled')"
                ),
                dict(id=identity),
            )
    connection.execute(
        text("""UPDATE runtime_work_budgets SET
        models=MAX(models, COALESCE((SELECT SUM(w.model_requests) FROM runtime_work w
            WHERE w.id=root_id OR w.id IN (SELECT work_id FROM runtime_subagents WHERE root_id=runtime_work_budgets.root_id)),0)),
        tools=MAX(tools, COALESCE((SELECT SUM(w.tool_calls) FROM runtime_work w
            WHERE w.id=root_id OR w.id IN (SELECT work_id FROM runtime_subagents WHERE root_id=runtime_work_budgets.root_id)),0))""")
    )


def downgrade() -> None:
    # Runtime rollback cannot discard accepted receipts or reset task budgets.
    pass
