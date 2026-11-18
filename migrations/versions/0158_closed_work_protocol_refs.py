"""Release private protocol refs of Works that can no longer resume.

Completed and cancelled Works have no journal reader; the existing protocol GC
reclaims their objects once unowned. Failed Works and completed (unarchived)
workers stay resumable. runtime_work, journal, effects, inputs and waits remain.
"""

from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TRIGGER IF NOT EXISTS protocol_refs_closed_work AFTER UPDATE OF state ON runtime_work
    WHEN NEW.state IN ('completed', 'cancelled') AND OLD.state IS NOT NEW.state
    BEGIN DELETE FROM runtime_protocol_refs WHERE work_id=NEW.id AND (NEW.state='cancelled'
        OR NOT EXISTS (SELECT 1 FROM runtime_subagents WHERE work_id=NEW.id)); END""")
    op.execute(
        "DELETE FROM runtime_protocol_refs WHERE work_id IN ("
        "SELECT id FROM runtime_work AS w WHERE w.state='cancelled' OR (w.state='completed'"
        " AND NOT EXISTS (SELECT 1 FROM runtime_subagents AS s WHERE s.work_id=w.id)))"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS protocol_refs_closed_work")
