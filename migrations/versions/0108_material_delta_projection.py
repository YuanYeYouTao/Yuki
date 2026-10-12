"""Retire public runtime packages while preserving private execution sources."""

import hashlib
import json
import logging

import sqlalchemy as sa
from alembic import op

from qq_ai_bot.conversation.observation_schema import PROJECTION_TRIGGERS_0089
from qq_ai_bot.conversation.projection_material_schema import PROJECTION_TRIGGERS_0108

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

_DYNAMIC_HEADER = "本轮运行资料（按 trust 字段区分可信度）："
_OBSERVATION_HEADER = "[Observation; derived notes]\n"
logger = logging.getLogger(__name__)


def upgrade() -> None:
    connection = op.get_bind()
    observations, affected, removable, selections = _prepare_retirement(connection)
    # Alembic's explicit SQLite writer covers DDL and data together. Removing the
    # old triggers here avoids collateral retirement of real chat selections;
    # the complete, prepared graph determines the only sources being deleted.
    for name in PROJECTION_TRIGGERS_0108:
        op.execute(f"DROP TRIGGER {name}")
    op.add_column(
        "prompt_projections",
        sa.Column("visible_material_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.execute(
        "DELETE FROM runtime_config_overrides WHERE config_key IN ("
        "'plugins.max_prompt_fragment_characters',"
        "'plugins.max_prompt_characters_per_plugin',"
        "'plugins.max_total_prompt_characters')"
    )
    _retire_selections(connection, affected, selections)
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
    # The old compiled input is a disposable public cache. Real first chat
    # expressions remain in selections, while private journals stay untouched.
    op.execute(
        "UPDATE prompt_projections SET payload_json='[]', visible_material_json='{}', "
        "byte_size=4, revision=revision+1, invalidated_reason='contract_changed'"
    )
    for statement in PROJECTION_TRIGGERS_0108.values():
        op.execute(statement)
    logger.info(
        "material_retirement public_sources=%s removed_sources=%s retained_sources=%s "
        "changed_owners=%s public_selections=%s",
        len(affected),
        len(removable),
        len(affected - set(removable)),
        len(changed_owners),
        len(selections),
    )


def _prepare_retirement(connection):
    observations = {
        row["id"]: dict(row)
        for row in connection.execute(
            sa.text(
                "SELECT id,conversation_id,source_key,parent_sources_json "
                "FROM model_context_observations"
            )
        ).mappings()
    }
    parents = {
        identity: {parent for parent, _ in json.loads(row["parent_sources_json"])}
        for identity, row in observations.items()
    }
    automatic = {
        identity
        for identity, row in observations.items()
        if row["source_key"].startswith("snapshot:")
    }
    pure = set(automatic)
    affected = set(automatic)
    while True:
        expanded = affected | {identity for identity, roots in parents.items() if roots & affected}
        pure_expanded = pure | {
            identity
            for identity, roots in parents.items()
            if roots
            and roots <= pure
            and observations[identity]["source_key"].startswith("scope-summary:")
        }
        if expanded == affected and pure_expanded == pure:
            break
        affected, pure = expanded, pure_expanded
    protected_owners = set(
        connection.execute(
            sa.text(
                "SELECT DISTINCT conversation_id FROM runtime_work AS w "
                "WHERE state NOT IN ('completed','failed','cancelled') "
                "OR EXISTS(SELECT 1 FROM runtime_work_journal AS j WHERE j.work_id=w.id) "
                "OR EXISTS(SELECT 1 FROM runtime_protocol_refs AS p WHERE p.work_id=w.id)"
            )
        ).scalars()
    )
    required = (
        (set(observations) - pure)
        | set(
            connection.execute(
                sa.text("SELECT owner_id FROM tool_artifact_refs WHERE owner_kind='observation'")
            ).scalars()
        )
        | {
            identity
            for identity, row in observations.items()
            if row["conversation_id"] in protected_owners
        }
    )
    while True:
        ancestors = required | {
            parent for identity in required for parent in parents.get(identity, ())
        }
        if ancestors == required:
            break
        required = ancestors
    removable = tuple(sorted(pure - required))
    selected_ids = []
    for identity, refs in connection.execute(
        sa.text("SELECT id,observation_sources_json FROM model_context_selections")
    ):
        if {source for source, _ in json.loads(refs)} & affected:
            selected_ids.append(identity)
    selections = []
    for start in range(0, len(selected_ids), 256):
        selections.extend(
            dict(row)
            for row in connection.execute(
                sa.text(
                    "SELECT id,source_key,event_ids_json,observation_sources_json,payload_json "
                    "FROM model_context_selections WHERE id IN :ids ORDER BY id"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": selected_ids[start : start + 256]},
            ).mappings()
        )
    # Only exact generated Observation copies require their source payload.
    # The rest of the graph preparation reads metadata, never private protocol.
    generic_sources = tuple(
        {
            json.loads(row["payload_json"])["observation_id"]
            for row in selections
            if not json.loads(row["event_ids_json"])
            and (json.loads(row["payload_json"])["message"].get("content") or "").startswith(
                _OBSERVATION_HEADER
            )
        }
    )
    payloads = {}
    for start in range(0, len(generic_sources), 256):
        payloads.update(
            connection.execute(
                sa.text(
                    "SELECT id,payload_json FROM model_context_observations WHERE id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": generic_sources[start : start + 256]},
            ).all()
        )
    for row in selections:
        row["source_payload"] = payloads.get(json.loads(row["payload_json"])["observation_id"])
    return observations, affected, removable, selections


def _retire_selections(connection, affected, selections):
    decoder = json.JSONDecoder()
    for row in selections:
        item = json.loads(row["payload_json"])
        content = item["message"].get("content") or ""
        generated = (
            row["source_payload"] is not None
            and content == _OBSERVATION_HEADER + row["source_payload"]
        )
        if not item["event_ids"] and generated:
            connection.execute(
                sa.text("DELETE FROM model_context_selections WHERE id=:id"), {"id": row["id"]}
            )
            continue
        if content.startswith(_DYNAMIC_HEADER):
            envelope, end = decoder.raw_decode(content[len(_DYNAMIC_HEADER) :])
            if not isinstance(envelope, list):
                raise ValueError("invalid original compiled material envelope")
            suffix = content[len(_DYNAMIC_HEADER) + end :]
            item["message"]["content"] = suffix[2:] if suffix.startswith("\n\n") else suffix
        if item.get("observation_id") in affected:
            item.pop("observation_id")
            item.pop("observation_version")
        refs = [
            [identity, version]
            for identity, version in json.loads(row["observation_sources_json"])
            if identity not in affected
        ]
        if not item["event_ids"] and not any(
            item["message"].get(key)
            for key in ("content", "tool_calls", "tool_call_id", "response_item")
        ):
            connection.execute(
                sa.text("DELETE FROM model_context_selections WHERE id=:id"), {"id": row["id"]}
            )
            continue
        key = row["source_key"]
        if not item["event_ids"]:
            # The original row is the trusted occurrence. Equal real inputs are
            # separate occurrences; neither guessed events nor value hashes own them.
            key = hashlib.sha256(f"selection:{row['id']}".encode()).hexdigest()
            item["selection_key"] = key
        connection.execute(
            sa.text(
                "UPDATE model_context_selections SET source_key=:key, "
                "observation_sources_json=:refs,payload_json=:payload WHERE id=:id"
            ),
            {
                "id": row["id"],
                "key": key,
                "refs": json.dumps(refs, separators=(",", ":")),
                "payload": json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            },
        )


def downgrade() -> None:
    for name in PROJECTION_TRIGGERS_0108:
        op.execute(f"DROP TRIGGER {name}")
    op.execute(
        "UPDATE prompt_projections SET byte_size=byte_size-length(CAST(visible_material_json AS BLOB))"
    )
    op.drop_column("prompt_projections", "visible_material_json")
    for statement in PROJECTION_TRIGGERS_0089.values():
        op.execute(statement)
