"""Yuki-opened private turns and the watchdog's told-once issue rows.

``proactive_turns`` is the receipt of each private turn Yuki opens with a Person
(first source: the watchdog). ``watchdog_issues`` holds one row per superuser and
currently open problem; the row is the fact that this occurrence was registered,
and ``turn_id`` names the turn that told it. Rows are deleted once the problem has
stayed clear for the reporting interval, so a later recurrence is told again.
"""

from alembic import op

revision = "0160"
down_revision = "0159"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE proactive_turns (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            source_kind VARCHAR(32) NOT NULL,
            person_id VARCHAR(36) NOT NULL REFERENCES persons (id) ON DELETE RESTRICT,
            conversation_id VARCHAR(36),
            presence_id VARCHAR(36),
            state VARCHAR(16) NOT NULL,
            reason VARCHAR(128),
            attempts INTEGER DEFAULT '0' NOT NULL,
            result_json TEXT,
            created FLOAT NOT NULL,
            started FLOAT,
            updated FLOAT NOT NULL,
            CONSTRAINT ck_proactive_turns_state
                CHECK (state IN ('pending', 'running', 'finished', 'failed', 'unknown'))
        )"""
    )
    op.execute("CREATE INDEX ix_proactive_turns_state ON proactive_turns (state, updated)")
    op.execute(
        "CREATE INDEX ix_proactive_turns_person_started ON proactive_turns (person_id, started)"
    )
    op.execute(
        """CREATE TABLE watchdog_issues (
            person_id VARCHAR(36) NOT NULL REFERENCES persons (id) ON DELETE CASCADE,
            issue_key VARCHAR(256) NOT NULL,
            opened FLOAT NOT NULL,
            facts_json TEXT NOT NULL,
            turn_id VARCHAR(36) REFERENCES proactive_turns (id) ON DELETE SET NULL,
            PRIMARY KEY (person_id, issue_key)
        )"""
    )
    op.execute("CREATE INDEX ix_watchdog_issues_turn ON watchdog_issues (turn_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_watchdog_issues_turn")
    op.execute("DROP TABLE IF EXISTS watchdog_issues")
    op.execute("DROP INDEX IF EXISTS ix_proactive_turns_person_started")
    op.execute("DROP INDEX IF EXISTS ix_proactive_turns_state")
    op.execute("DROP TABLE IF EXISTS proactive_turns")
