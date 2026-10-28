"""Separate original envelopes, transport deduplication and native message references.

Historical source bytes and receipt identities survive this schema change. The
backfill only uses the original physical account and confirmed linked effects.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.exec_driver_sql(
        "ALTER TABLE chat_events RENAME COLUMN platform_message_id TO origin_key"
    )
    connection.exec_driver_sql(
        "ALTER TABLE canonical_event_receipts RENAME COLUMN platform_message_id TO ingress_key"
    )
    # SQLite updates dependent trigger SQL during RENAME COLUMN. Preserve those
    # exact definitions while rebuilding the two tables whose constraints change.
    triggers = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL"
    ).all()
    sequence = connection.exec_driver_sql(
        "SELECT seq FROM sqlite_sequence WHERE name='chat_events'"
    ).scalar()
    for name, _sql in triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + name.replace('"', '""') + '"')
    with op.batch_alter_table(
        "chat_events", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch:
        batch.alter_column(
            "origin_key", existing_type=sa.String(128), type_=sa.String(), nullable=True
        )
        batch.add_column(sa.Column("native_message_id", sa.String(), nullable=True))
    with op.batch_alter_table("canonical_event_receipts", recreate="always") as batch:
        batch.drop_constraint("ck_canonical_event_receipts_platform_message_id", type_="check")
        batch.alter_column(
            "ingress_key", existing_type=sa.String(128), type_=sa.String(), nullable=False
        )
    connection.exec_driver_sql(
        "CREATE INDEX ix_chat_events_native_reference ON chat_events "
        "(canonical_conversation_id, native_message_id) "
        "WHERE native_message_id IS NOT NULL AND suppression_status = 'keeper'"
    )
    if sequence is not None:
        connection.exec_driver_sql(
            "UPDATE sqlite_sequence SET seq=max(seq, ?) WHERE name='chat_events'", (sequence,)
        )

    # A truncated receipt is repairable only from its own unique canonical
    # keeper, original account and namespace. Unique-key conflicts abort the
    # migration rather than discarding a receipt or choosing a different owner.
    repairs = connection.exec_driver_sql("""
        SELECT r.id, e.origin_key, r.transport_namespace, e.scope_type,
            e.group_id, e.private_peer_user_id, e.segments_json FROM canonical_event_receipts r
        JOIN chat_events e ON e.canonical_event_id=r.canonical_event_id
            AND e.suppression_status='keeper'
        JOIN account_assignments p ON p.id=e.ingress_presence_id
        WHERE length(r.ingress_key)=128 AND length(e.origin_key)>128
            AND substr(e.origin_key,1,128)=r.ingress_key
            AND p.account_id=r.ingress_account_id
    """).all()
    for receipt_id, key, namespace, scope, group_id, peer_id, segments_json in repairs:
        context = next(
            (
                part["data"]
                for part in json.loads(segments_json)
                if part.get("type") == "yuki_context"
            ),
            {},
        )
        if json.loads(namespace) != [scope, group_id or peer_id, context.get("thread_id")]:
            continue
        try:
            connection.exec_driver_sql(
                "UPDATE canonical_event_receipts SET ingress_key=? WHERE id=?", (key, receipt_id)
            )
        except sa.exc.IntegrityError as exc:
            raise RuntimeError(
                f"0155 conflicting complete ingress key on receipt {receipt_id}"
            ) from exc

    # Rebuilt indexes have no planner statistics. Resolve each original event,
    # rather than scanning all receipts for its receiving account.
    connection.exec_driver_sql("""
        UPDATE chat_events AS e SET native_message_id=e.origin_key
        WHERE e.event_kind='message' AND e.direction='inbound' AND EXISTS (
            SELECT 1 FROM canonical_event_receipts r
                INDEXED BY ix_canonical_event_receipts_canonical_event_id
            JOIN account_assignments p ON p.id=e.ingress_presence_id
            JOIN accounts a ON a.id=p.account_id
            WHERE r.canonical_event_id=e.canonical_event_id
                AND r.ingress_account_id=p.account_id AND r.ingress_key=e.origin_key
                AND ((a.platform='qq' AND e.ingress_provider='snowluma'
                    AND length(e.origin_key)>0 AND e.origin_key NOT GLOB '*[^0-9-]*')
                OR (a.platform='telegram' AND e.ingress_provider='telegram'
                    AND e.origin_key GLOB '*:[0-9]*'
                    AND substr(e.origin_key,1,instr(e.origin_key,':')-1)
                        =coalesce(e.group_id,e.private_peer_user_id))
                OR (a.platform IN ('feishu','lark') AND e.ingress_provider IN ('feishu','lark')
                    AND e.origin_key GLOB 'om_*'))
        )
    """)
    connection.exec_driver_sql("""
        UPDATE chat_events AS e SET native_message_id=e.origin_key
        WHERE e.event_kind='message' AND e.direction='outbound' AND EXISTS (
            SELECT 1 FROM social_operation_receipts s
            JOIN account_assignments p ON p.id=s.presence_id
            JOIN accounts a ON a.id=p.account_id
            WHERE s.event_id=e.id AND s.status='succeeded'
                AND s.platform_reference=e.origin_key
                AND s.action IN ('send_message','send_file','send_file_caption','send_emoji')
                AND ((a.platform='qq' AND length(e.origin_key)>0
                    AND e.origin_key NOT GLOB '*[^0-9-]*')
                OR (a.platform='telegram' AND e.origin_key GLOB '*:[0-9]*')
                OR (a.platform IN ('feishu','lark') AND e.origin_key GLOB 'om_*'))
        )
    """)
    connection.exec_driver_sql("""
        UPDATE chat_events AS e SET native_message_id=e.origin_key
        WHERE e.event_kind='message' AND e.direction='outbound' AND EXISTS (
            SELECT 1 FROM plugin_notification_outbox n
            JOIN account_assignments p ON p.id=n.canonical_presence_id
            JOIN accounts a ON a.id=p.account_id
            WHERE n.event_id=e.id AND n.status='sent' AND n.platform_message_id=e.origin_key
                AND ((a.platform='qq' AND length(e.origin_key)>0
                    AND e.origin_key NOT GLOB '*[^0-9-]*')
                OR (a.platform='telegram' AND e.origin_key GLOB '*:[0-9]*')
                OR (a.platform IN ('feishu','lark') AND e.origin_key GLOB 'om_*'))
        )
    """)
    for _name, sql in triggers:
        connection.exec_driver_sql(sql)
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchmany(1)
    if violations:
        raise RuntimeError(f"0155 foreign key conflict: {violations[0]}")


def downgrade() -> None:
    raise RuntimeError(
        "0155 retains nullable local facts and complete ingress keys; restore code forward"
    )
