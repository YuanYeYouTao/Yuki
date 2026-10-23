"""Use immutable versions as the only definitions; preserve accepted execution facts.

Offline, single transaction. The original versions, hashes, runs, Work and
receipts are never rewritten. This frozen migration imports no current ORM.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None

_OWNER_DDL = """CREATE TABLE automations_new (
 id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
 creator_kind VARCHAR(8) DEFAULT 'person' NOT NULL,
 creator_user_id VARCHAR(64) NOT NULL,
 bot_user_id VARCHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL,
 created_from_message_id VARCHAR(128) NOT NULL,
 creation_source_key VARCHAR(255),
 next_run_at DATETIME,
 last_run_at DATETIME,
 run_count INTEGER NOT NULL,
 max_runs INTEGER, creation_max_runs INTEGER,
 claimed_by VARCHAR(64), claimed_until DATETIME,
 created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
 canonical_creator_person_id VARCHAR(36) REFERENCES persons(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 canonical_presence_id VARCHAR(36) REFERENCES account_assignments(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 canonical_conversation_id VARCHAR(36) REFERENCES canonical_conversations(id) ON DELETE RESTRICT,
 source_event_id INTEGER,
 CONSTRAINT ck_automations_status CHECK (status IN ('active','paused','completed','cancelled','failed','blocked')),
 CONSTRAINT ck_automations_run_count CHECK (run_count >= 0),
 CONSTRAINT ck_automations_canonical_owner CHECK (
  (creator_kind='person' AND (canonical_creator_person_id IS NOT NULL OR status IN ('completed','cancelled','failed','blocked')))
  OR (creator_kind='self' AND canonical_creator_person_id IS NULL AND creator_user_id='' AND created_from_message_id=''))
)"""


def _clean(row, scene):
    raw = json.loads(row["script_json"])
    if "goal" not in raw or "trigger" not in raw:
        return None
    trigger = dict(raw["trigger"])
    zone = trigger.get("timezone") or raw.get("timezone") or "Asia/Shanghai"
    if trigger["type"] == "once":
        value = datetime.fromisoformat(trigger["local_datetime"])
        if value.utcoffset() is None:
            value = value.replace(tzinfo=ZoneInfo(zone), fold=0)
        trigger = {"type": "once", "local_datetime": value.isoformat()}
    elif trigger["type"] in ("daily", "weekly"):
        trigger["timezone"] = zone
    else:
        trigger.pop("timezone", None)
    goal = raw["goal"]
    constraints = raw.get("constraints", [])
    if constraints:
        goal += "\n\n原安排中的补充要求：\n" + "\n".join(constraints)
    target = raw.get("delivery", {}).get("target")
    if target == "self_private":
        person = row["canonical_target_person_id"] or row["canonical_creator_person_id"]
        goal += f"\n\n原交付要求：将需要交付的结果发送至人物 {person} 的私聊。原入口 Presence 为 {row['canonical_presence_id']}，原平台账号为 {row['bot_user_id']}。"
    elif target == "current_group":
        space = row["canonical_target_space_id"] or scene.get("canonical_space_id")
        goal += f"\n\n原交付要求：将需要交付的结果发送至群组空间 {space}。原入口 Presence 为 {row['canonical_presence_id']}，原平台账号为 {row['bot_user_id']}。"
    # none is a retired auxiliary mode, not a negation of the original goal.
    result = {"goal": goal, "trigger": trigger}
    if raw.get("name") is not None:
        result["name"] = raw["name"]
    return result


def upgrade():
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT * FROM automations ORDER BY id")).mappings().all()
    versions = (
        connection.execute(
            sa.text("SELECT * FROM automation_versions ORDER BY automation_id,version")
        )
        .mappings()
        .all()
    )
    by_owner = {}
    for version in versions:
        by_owner.setdefault(version["automation_id"], []).append(version)
    conversations = set(
        connection.execute(sa.text("SELECT id FROM canonical_conversations")).scalars()
    )
    plans = []
    for row in rows:
        scene = json.loads(row["authority_snapshot_json"])
        original = {
            "script_json": row["script_json"],
            "script_hash": row["script_hash"],
            "authority_snapshot_json": row["authority_snapshot_json"],
            "canonical_target_person_id": row["canonical_target_person_id"],
            "canonical_target_space_id": row["canonical_target_space_id"],
            "canonical_presence_id": row["canonical_presence_id"],
        }
        existing = by_owner.get(row["id"], [])
        found = bool(existing) and all(
            existing[-1][key] == value for key, value in original.items()
        )
        clean = _clean(row, scene) if row["status"] not in ("completed", "cancelled") else None
        encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":")) if clean else None
        digest = (
            hashlib.sha256(
                json.dumps(
                    clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if clean
            else None
        )
        minimal_scene = {
            key: value
            for key, value in scene.items()
            if key
            in {
                "creator_user_id",
                "bot_user_id",
                "created_from_message_id",
                "current_group_id",
                "principal_kind",
                "canonical_creator_person_id",
                "canonical_conversation_id",
                "conversation_generation",
                "canonical_presence_id",
                "canonical_space_id",
            }
        }
        plans.append(
            (
                row,
                original,
                found,
                max((v["version"] for v in existing), default=0),
                encoded,
                digest,
                json.dumps(minimal_scene, ensure_ascii=False, separators=(",", ":")),
                scene.get("canonical_conversation_id")
                if scene.get("canonical_conversation_id") in conversations
                else None,
            )
        )
    insert_version = sa.text("""INSERT INTO automation_versions
      (automation_id,version,script_json,script_hash,authority_snapshot_json,
       canonical_target_person_id,canonical_target_space_id,canonical_presence_id,updated_by,created_at)
      VALUES (:automation_id,:version,:script_json,:script_hash,:authority_snapshot_json,
       :canonical_target_person_id,:canonical_target_space_id,:canonical_presence_id,'',:created_at)""")
    for row, original, found, number, encoded, digest, scene_json, _ in plans:
        if not found:
            number += 1
            connection.execute(
                insert_version,
                {
                    **original,
                    "automation_id": row["id"],
                    "version": number,
                    "created_at": row["updated_at"],
                },
            )
        if encoded is not None:
            number += 1
            connection.execute(
                insert_version,
                {
                    "automation_id": row["id"],
                    "version": number,
                    "script_json": encoded,
                    "script_hash": digest,
                    "authority_snapshot_json": scene_json,
                    "canonical_target_person_id": None,
                    "canonical_target_space_id": None,
                    "canonical_presence_id": row["canonical_presence_id"],
                    "created_at": row["updated_at"],
                },
            )
    op.execute(_OWNER_DDL)
    owner_columns = (
        "id",
        "creator_kind",
        "creator_user_id",
        "bot_user_id",
        "status",
        "created_from_message_id",
        "creation_source_key",
        "next_run_at",
        "last_run_at",
        "run_count",
        "max_runs",
        "claimed_by",
        "claimed_until",
        "created_at",
        "updated_at",
        "canonical_creator_person_id",
        "canonical_presence_id",
    )
    statement = sa.text(
        "INSERT INTO automations_new ("
        + ",".join(owner_columns)
        + ",creation_max_runs,canonical_conversation_id,source_event_id) VALUES ("
        + ",".join(":" + name for name in owner_columns)
        + ",:creation_max_runs,:canonical_conversation_id,NULL)"
    )
    for row, _, _, _, _, _, _, conversation in plans:
        connection.execute(
            statement,
            {
                **{name: row[name] for name in owner_columns},
                "creation_max_runs": row["max_runs"],
                "canonical_conversation_id": conversation,
            },
        )
    op.execute("DROP TABLE automations")
    op.execute("ALTER TABLE automations_new RENAME TO automations")
    for suffix, columns in (
        ("status_next", "status,next_run_at"),
        ("creator_updated", "creator_user_id,updated_at"),
        ("claim", "claimed_until,claimed_by"),
        ("canonical_creator_person_id", "canonical_creator_person_id"),
        ("canonical_presence_id", "canonical_presence_id"),
        ("canonical_conversation_id", "canonical_conversation_id"),
        ("creation_source_key", "creation_source_key"),
    ):
        op.execute(f"CREATE INDEX ix_automations_{suffix} ON automations ({columns})")
    op.execute(
        "CREATE UNIQUE INDEX uq_automation_creation_call ON automations (canonical_creator_person_id,creation_source_key) WHERE creation_source_key LIKE 'call:%'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_automation_self_creation_call ON automations (creation_source_key) WHERE creator_kind='self' AND creation_source_key IS NOT NULL"
    )
    for operation in ("INSERT", "UPDATE"):
        op.execute(f"""CREATE TRIGGER trg_automations_canonical_presence_id_kind_{operation.lower()}
        BEFORE {operation} ON automations WHEN NEW.canonical_presence_id IS NOT NULL BEGIN
         SELECT RAISE(ABORT,'assignment kind mismatch') WHERE NOT EXISTS
          (SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'); END""")
    failures = connection.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if failures:
        raise RuntimeError(f"automation definition migration foreign key violations: {failures!r}")


def downgrade():
    raise RuntimeError("automation definitions cannot be downgraded by rewriting execution facts")
