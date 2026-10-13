"""Remove public activation material; retain original private execution evidence."""

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from alembic.util import load_python_file

from qq_ai_bot.conversation.projection_current_schema import PROJECTION_TRIGGERS_0109
from qq_ai_bot.conversation.projection_material_schema import PROJECTION_TRIGGERS_0108

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # The frozen 0108 preparation already walks observation dependencies and
    # protects original Work journals, protocol references and useful notes.
    retirement = load_python_file(Path(__file__).parent, "0108_material_delta_projection.py")
    observations, affected, removable, _ = retirement._prepare_retirement(connection)
    selections = [
        dict(row)
        for row in connection.execute(
            sa.text(
                "SELECT s.id,s.source_key,s.event_ids_json,s.observation_sources_json,"
                "s.payload_json,o.payload_json AS source_payload "
                "FROM model_context_selections AS s LEFT JOIN model_context_observations AS o "
                "ON o.id=json_extract(s.payload_json,'$.observation_id') ORDER BY s.id"
            )
        ).mappings()
    ]
    # A trigger without an event is execution input, not a public chat message.
    # Real notes keep their immutable observation source and first expression.
    snapshots = {
        identity
        for identity, row in observations.items()
        if row["source_key"].startswith("snapshot:")
    }
    execution_inputs = tuple(
        row["id"]
        for row in selections
        if not json.loads(row["event_ids_json"])
        and (
            not json.loads(row["observation_sources_json"])
            or json.loads(row["payload_json"]).get("observation_id") in snapshots
        )
    )
    selected_material = [
        row
        for row in selections
        if row["id"] not in execution_inputs
        and {identity for identity, _ in json.loads(row["observation_sources_json"])} & affected
    ]
    for name in PROJECTION_TRIGGERS_0108:
        op.execute(f"DROP TRIGGER {name}")
    retirement._retire_selections(connection, affected, selected_material)
    for start in range(0, len(execution_inputs), 256):
        connection.execute(
            sa.text("DELETE FROM model_context_selections WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": execution_inputs[start : start + 256]},
        )
    for start in range(0, len(removable), 256):
        connection.execute(
            sa.text("DELETE FROM model_context_observations WHERE id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": removable[start : start + 256]},
        )
    changed_owners = tuple({observations[identity]["conversation_id"] for identity in removable})
    for start in range(0, len(changed_owners), 256):
        connection.execute(
            sa.text(
                "UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1 "
                "WHERE id IN :ids"
            ).bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": changed_owners[start : start + 256]},
        )
    op.execute(
        "UPDATE prompt_projections SET payload_json='[]', byte_size=2, revision=revision+1, "
        "invalidated_reason='contract_changed'"
    )
    op.drop_column("prompt_projections", "visible_material_json")
    op.drop_table("autonomy_source_claims")
    for statement in PROJECTION_TRIGGERS_0109.values():
        op.execute(statement)


def downgrade() -> None:
    # Downgrade restores schema only. Retired public packages and auxiliary
    # claims are derived data; neither private journals nor effects are replayed.
    for name in PROJECTION_TRIGGERS_0109:
        op.execute(f"DROP TRIGGER {name}")
    op.add_column(
        "prompt_projections",
        sa.Column("visible_material_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.execute("UPDATE prompt_projections SET byte_size=byte_size+2")
    op.create_table(
        "autonomy_source_claims",
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(20), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.PrimaryKeyConstraint(
            "conversation_id", "generation", "source_kind", "source_id", "source_revision"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "generation"],
            ["autonomy_bindings.conversation_id", "autonomy_bindings.generation"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["autonomy_initiative_runs.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("source_kind IN ('event', 'memory')"),
    )
    op.create_index("ix_autonomy_sources_run", "autonomy_source_claims", ["run_id"])
    for statement in PROJECTION_TRIGGERS_0108.values():
        op.execute(statement)
