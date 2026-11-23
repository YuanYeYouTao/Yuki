"""SELF can wake without a group (#541 P3): relax two group-only constraints.

The owner decided (2026-11-23) that every SELF wake uses the same initiative chain and
that SELF need not belong to a group. A timed wake runs in a private Conversation, so:

``autonomy_initiative_runs``
  * ``space_id`` becomes nullable (a group run keeps its Space; a private run has none);
  * ``owner`` also admits ``timer``, the explicit-cadence proposer next to legacy/semantic;
  * the composite foreign key to ``autonomy_bindings`` (a group controller's selector row)
    becomes a plain foreign key to ``canonical_conversations``: a timed run has no group
    controller. Legacy and semantic admissions still read their binding before writing.

``memory_tool_receipts``
  * ``ck_memory_tool_receipts_initiative_call`` no longer requires an initiative receipt to
    have no Person owner. A receipt keeps its partition owner (Person XOR Space, unchanged
    ``ck_memory_tool_receipts_owner``): a wake in a private Conversation is owned by that
    Conversation's Person, as its Memory partition is.

Rows, indexes and triggers are copied unchanged. Frozen SQL only; this migration imports
no runtime module. Downgrade restores the 0167 shapes and refuses rows they cannot hold.
"""

from alembic import op

revision = "0168"
down_revision = "0167"
branch_labels = None
depends_on = None

RUN_COLUMNS = (
    "id, proposal_id, conversation_id, generation, owner, controller_epoch, space_id, "
    "presence_id, target_person_id, payload_hash, sources_json, support_refs_json, state, "
    "feedback_sequence, created_at, updated_at, trigger_kind, thread_key"
)

RECEIPT_COLUMNS = (
    "id, conversation_key_hash, trigger_event_id, bot_user_id, canonical_person_id, "
    "canonical_space_id, provider_id, tool_name, success, result_excerpt, result_characters, "
    "error_category, created_at, expires_at, initiative_run_id, tool_call_id, execution_id, "
    "source_call_key, source_event_ids_json"
)


def _runs(*, space: str, owners: str, conversation_fk: str) -> str:
    return (
        "CREATE TABLE {name} (\n"
        "\tid VARCHAR(36) NOT NULL, \n"
        "\tproposal_id TEXT NOT NULL, \n"
        "\tconversation_id VARCHAR(36) NOT NULL, \n"
        "\tgeneration INTEGER NOT NULL, \n"
        "\towner VARCHAR(16) NOT NULL, \n"
        "\tcontroller_epoch INTEGER NOT NULL, \n"
        f"\tspace_id VARCHAR(36){space}, \n"
        "\tpresence_id VARCHAR(36) NOT NULL, \n"
        "\ttarget_person_id VARCHAR(36), \n"
        "\tpayload_hash VARCHAR(64) NOT NULL, \n"
        "\tsources_json TEXT NOT NULL, \n"
        "\tsupport_refs_json TEXT NOT NULL, \n"
        "\tstate VARCHAR(16) NOT NULL, \n"
        "\tfeedback_sequence INTEGER NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\ttrigger_kind VARCHAR(16) DEFAULT 'source' NOT NULL, \n"
        "\tthread_key TEXT, \n"
        "\tPRIMARY KEY (id), \n"
        f"\t{conversation_fk}, \n"
        "\tCONSTRAINT uq_autonomy_proposal_run UNIQUE (conversation_id, generation, owner, "
        "controller_epoch, proposal_id), \n"
        "\tCHECK (generation >= 1 AND controller_epoch >= 0 AND feedback_sequence >= 0), \n"
        f"\tCHECK (owner IN ({owners})), \n"
        "\tCHECK (state IN ('accepted', 'running', 'completed', 'no_reply', 'interrupted', "
        "'failed')), \n"
        "\tFOREIGN KEY(space_id) REFERENCES spaces (id) ON DELETE RESTRICT, \n"
        "\tFOREIGN KEY(presence_id) REFERENCES account_assignments (id) ON DELETE RESTRICT, \n"
        "\tFOREIGN KEY(target_person_id) REFERENCES persons (id) ON DELETE RESTRICT\n"
        ")"
    )


def _receipts(*, initiative_owner: str) -> str:
    return (
        "CREATE TABLE {name} (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tconversation_key_hash VARCHAR(64) NOT NULL, \n"
        "\ttrigger_event_id INTEGER, \n"
        "\tbot_user_id VARCHAR(64) NOT NULL, \n"
        "\tcanonical_person_id VARCHAR(36), \n"
        "\tcanonical_space_id VARCHAR(36), \n"
        "\tprovider_id VARCHAR(128) NOT NULL, \n"
        "\ttool_name VARCHAR(255) NOT NULL, \n"
        "\tsuccess BOOLEAN NOT NULL, \n"
        "\tresult_excerpt TEXT NOT NULL, \n"
        "\tresult_characters INTEGER NOT NULL, \n"
        "\terror_category VARCHAR(128), \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\texpires_at DATETIME NOT NULL, \n"
        "\tinitiative_run_id VARCHAR(36), \n"
        "\ttool_call_id VARCHAR(255), \n"
        "\texecution_id VARCHAR(255), \n"
        "\tsource_call_key VARCHAR(64), \n"
        "\tsource_event_ids_json TEXT DEFAULT '[]' NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT fk_tool_receipt_initiative FOREIGN KEY(initiative_run_id) REFERENCES "
        "autonomy_initiative_runs (id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT ck_memory_tool_receipts_initiative_call CHECK (initiative_run_id IS NULL "
        "OR (tool_call_id IS NOT NULL AND execution_id IS NOT NULL AND source_call_key IS NOT "
        f"NULL{initiative_owner})), \n"
        "\tCONSTRAINT ck_memory_tool_receipts_size CHECK (result_characters >= 0), \n"
        "\tCONSTRAINT ck_memory_tool_receipts_owner CHECK ((canonical_person_id IS NOT NULL "
        "AND canonical_space_id IS NULL) OR (canonical_person_id IS NULL AND "
        "canonical_space_id IS NOT NULL)), \n"
        "\tFOREIGN KEY(trigger_event_id) REFERENCES chat_events (id) ON DELETE CASCADE, \n"
        "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE RESTRICT "
        "ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE RESTRICT "
        "ON UPDATE RESTRICT\n"
        ")"
    )


RUN_SCHEMA = (
    "CREATE INDEX ix_autonomy_runs_active ON autonomy_initiative_runs "
    "(conversation_id, generation, state)",
    "CREATE INDEX ix_autonomy_runs_reconcile ON autonomy_initiative_runs (created_at) "
    "WHERE state IN ('accepted', 'running')",
    *(
        f"CREATE TRIGGER trg_autonomy_initiative_runs_presence_id_kind_{event}\n"
        f"BEFORE {event.upper()} ON autonomy_initiative_runs\n"
        "WHEN NEW.presence_id IS NOT NULL\n"
        "BEGIN\n"
        " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
        "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
        "  );\n"
        "END"
        for event in ("insert", "update")
    ),
)

RECEIPT_SCHEMA = (
    "CREATE INDEX ix_memory_tool_receipts_initiative ON memory_tool_receipts "
    "(initiative_run_id, id)",
    "CREATE INDEX ix_memory_tool_receipts_canonical_person_id ON memory_tool_receipts "
    "(canonical_person_id)",
    "CREATE UNIQUE INDEX uq_memory_tool_receipts_source_call ON memory_tool_receipts "
    "(source_call_key)",
    "CREATE INDEX ix_memory_tool_receipts_canonical_space_id ON memory_tool_receipts "
    "(canonical_space_id)",
    "CREATE INDEX ix_memory_tool_receipts_conversation_created ON memory_tool_receipts "
    "(conversation_key_hash, created_at)",
    "CREATE INDEX ix_memory_tool_receipts_expires ON memory_tool_receipts (expires_at)",
)


def _rebuild(table: str, create: str, columns: str, schema: tuple[str, ...]) -> None:
    # Foreign keys are off for the whole migration run (migrations/env.py). Build the new
    # shape under a temporary name and rename it last, as batch mode does: renaming the
    # old table would rewrite the referencing tables' declarations to the old name.
    temporary = f"{table}_0168"
    op.execute(create.format(name=temporary))
    op.execute(f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {table}")
    op.execute(f"DROP TABLE {table}")
    op.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
    for statement in schema:
        op.execute(statement)


def upgrade() -> None:
    _rebuild(
        "autonomy_initiative_runs",
        _runs(
            space="",
            owners="'legacy', 'semantic', 'timer'",
            conversation_fk="FOREIGN KEY(conversation_id) REFERENCES canonical_conversations "
            "(id) ON DELETE RESTRICT",
        ),
        RUN_COLUMNS,
        RUN_SCHEMA,
    )
    _rebuild(
        "memory_tool_receipts", _receipts(initiative_owner=""), RECEIPT_COLUMNS, RECEIPT_SCHEMA
    )


def downgrade() -> None:
    bind = op.get_bind()
    for check in (
        "SELECT 1 FROM autonomy_initiative_runs WHERE owner='timer' OR space_id IS NULL",
        "SELECT 1 FROM memory_tool_receipts "
        "WHERE initiative_run_id IS NOT NULL AND canonical_person_id IS NOT NULL",
    ):
        if bind.exec_driver_sql(check + " LIMIT 1").first() is not None:
            raise RuntimeError("groupless SELF runs cannot return to the 0167 shape")
    _rebuild(
        "memory_tool_receipts",
        _receipts(initiative_owner=" AND canonical_person_id IS NULL"),
        RECEIPT_COLUMNS,
        RECEIPT_SCHEMA,
    )
    _rebuild(
        "autonomy_initiative_runs",
        _runs(
            space=" NOT NULL",
            owners="'legacy', 'semantic'",
            conversation_fk="FOREIGN KEY(conversation_id, generation) REFERENCES "
            "autonomy_bindings (conversation_id, generation) ON DELETE RESTRICT",
        ),
        RUN_COLUMNS,
        RUN_SCHEMA,
    )
