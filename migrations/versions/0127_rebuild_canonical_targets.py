"""Retain the original canonical subject of staged rebuild claims."""

import sqlalchemy as sa
from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("memory_rebuild_proposals") as batch:
        batch.add_column(sa.Column("canonical_subject_person_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("canonical_subject_space_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_rebuild_proposal_person",
            "persons",
            ["canonical_subject_person_id"],
            ["id"],
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_rebuild_proposal_space",
            "spaces",
            ["canonical_subject_space_id"],
            ["id"],
            onupdate="RESTRICT",
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_memory_rebuild_proposals_canonical_person", ["canonical_subject_person_id"]
        )
    # Only original internal identities are proof. Never resolve an old account
    # against today's binding, including when a third-party target is unknown.
    op.execute(
        sa.text("""
        UPDATE memory_rebuild_proposals AS proposal SET
            canonical_subject_person_id = CASE
                WHEN proposal.scope_type NOT IN ('person', 'person_group') THEN NULL
                WHEN proposal.actual_fact_id IS NOT NULL THEN (
                    SELECT canonical_subject_person_id FROM memory_facts
                    WHERE id = proposal.actual_fact_id)
                WHEN json_valid(proposal.claim_json) AND
                     json_extract(proposal.claim_json, '$.subject_ref') = 'speaker' THEN (
                    SELECT author_person_id FROM chat_events WHERE id = proposal.event_id)
                WHEN json_valid(proposal.claim_json) AND
                     substr(json_extract(proposal.claim_json, '$.subject_ref'), 1, 7) = 'member_' THEN (
                    SELECT id FROM persons WHERE id =
                        substr(json_extract(proposal.claim_json, '$.subject_ref'), 8))
                WHEN json_valid(proposal.claim_json) AND
                     json_extract(proposal.claim_json, '$.subject_ref') = 'reply_author' THEN (
                    SELECT reply.author_person_id FROM chat_events AS source
                    JOIN chat_events AS reply ON reply.id = source.reply_to_event_id
                    WHERE source.id = proposal.event_id
                        AND reply.canonical_conversation_id = source.canonical_conversation_id)
                ELSE NULL END,
            canonical_subject_space_id = CASE
                WHEN proposal.scope_type NOT IN ('group', 'person_group') THEN NULL
                WHEN proposal.actual_fact_id IS NOT NULL THEN (
                    SELECT canonical_subject_space_id FROM memory_facts
                    WHERE id = proposal.actual_fact_id)
                ELSE (
                    SELECT conversation.space_id FROM chat_events AS source
                    JOIN canonical_conversations AS conversation
                        ON conversation.id = source.canonical_conversation_id
                    WHERE source.id = proposal.event_id AND conversation.kind = 'space')
                END
    """)
    )


def downgrade() -> None:
    with op.batch_alter_table("memory_rebuild_proposals") as batch:
        batch.drop_index("ix_memory_rebuild_proposals_canonical_person")
        batch.drop_constraint("fk_rebuild_proposal_person", type_="foreignkey")
        batch.drop_constraint("fk_rebuild_proposal_space", type_="foreignkey")
        batch.drop_column("canonical_subject_person_id")
        batch.drop_column("canonical_subject_space_id")
