"""Publish clean Main tasks once; retain every original version, occurrence and receipt."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def _task(row):
    raw = json.loads(row["script_json"])
    if "goal" in raw and "trigger" in raw:
        return None
    steps = raw.get("steps", [])
    goal = None
    constraints = []
    delivery = "current_group" if row["canonical_target_space_id"] else "self_private"
    if len(steps) == 1:
        call = steps[0].get("call")
        arguments = steps[0].get("arguments", {})
        if call == "yuki.agent" and isinstance(arguments.get("instruction"), str):
            instruction = arguments["instruction"]
            try:
                request = json.loads(instruction)
            except (TypeError, ValueError):
                request = None
            if (
                isinstance(request, dict)
                and set(request) == {"goal", "constraints", "delivery"}
                and isinstance(request["goal"], str)
                and isinstance(request["constraints"], list)
                and all(isinstance(value, str) for value in request["constraints"])
                and isinstance(request["delivery"], str)
                and request["delivery"] in {"self_private", "current_group", "none"}
            ):
                goal = request["goal"]
                constraints = request["constraints"]
                delivery = request["delivery"]
            else:
                if "{{" in instruction or "${" in instruction:
                    return False
                # Only the frozen compiler envelope is decoded. Other actual
                # Agent instructions retain every byte and requirement.
                goal = instruction
                delivery = arguments.get("delivery_target", delivery)
        elif (
            call == "social.send_message"
            and isinstance(arguments.get("text"), str)
            and (
                set(arguments) == {"text"}
                or (
                    set(arguments) == {"text", "target"}
                    and arguments["target"] == {"kind": "person", "subject_ref": "current_speaker"}
                )
            )
        ):
            text = arguments["text"]
            if "{{" not in text and "${" not in text:
                goal = "在安排到期时发送以下提醒原文：\n" + text
    if (
        not isinstance(goal, str)
        or not goal.strip()
        or delivery not in ("auto", "current_group", "self_private", "none")
        or not isinstance(constraints, (list, tuple))
        or any(not isinstance(value, str) for value in constraints)
    ):
        return False
    if delivery == "auto":
        delivery = "current_group" if row["canonical_target_space_id"] else "self_private"
    return {
        "version": 1,
        "name": raw["name"],
        "goal": goal,
        "trigger": {key: value for key, value in raw["schedule"].items() if value is not None},
        "timezone": raw["timezone"],
        "constraints": list(constraints),
        "delivery": {"target": delivery},
    }


def upgrade():
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT * FROM automations ORDER BY id")).mappings().all()
    # Prepare all JSON and hashes before first DML. Immutable old versions are untouched.
    plans = []
    for row in rows:
        task = _task(row)
        if task is None:
            continue
        if task is False:
            plans.append((row, None, None, None))
            continue
        task_json = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(
            json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        authority = json.loads(row["authority_snapshot_json"])
        for key in ("granted_capabilities", "capability_schema_versions", "capability_provenance"):
            authority.pop(key, None)
        plans.append((row, task_json, digest, json.dumps(authority, ensure_ascii=False)))
    versions = dict(
        connection.execute(
            sa.text(
                "SELECT automation_id, MAX(version) FROM automation_versions GROUP BY automation_id"
            )
        ).all()
    )
    for row, task_json, digest, authority in plans:
        if task_json is None:
            # A plugin/template/multi-step program with no recovered user goal is an archive,
            # never a natural-language request for a model to reinterpret.
            connection.execute(
                sa.text(
                    "UPDATE automations SET status='blocked', next_run_at=NULL, "
                    "claimed_by=NULL, claimed_until=NULL WHERE id=:id AND status IN ('active','paused','blocked','failed')"
                ),
                {"id": row["id"]},
            )
            continue
        connection.execute(
            sa.text(
                "INSERT INTO automation_versions (automation_id, version, script_json, script_hash, "
                "authority_snapshot_json, canonical_target_person_id, canonical_target_space_id, "
                "canonical_presence_id, updated_by, created_at) VALUES (:id,:version,:json,:hash, "
                ":authority,:person,:space,:presence,:actor,CURRENT_TIMESTAMP)"
            ),
            {
                "id": row["id"],
                "version": versions.get(row["id"], 0) + 1,
                "json": task_json,
                "hash": digest,
                "authority": authority,
                "person": row["canonical_target_person_id"],
                "space": row["canonical_target_space_id"],
                "presence": row["canonical_presence_id"],
                "actor": row["creator_user_id"],
            },
        )
        connection.execute(
            sa.text(
                "UPDATE automations SET script_json=:json, script_hash=:hash, "
                "required_capabilities_json='[]', authority_snapshot_json=:authority WHERE id=:id"
            ),
            {"id": row["id"], "json": task_json, "hash": digest, "authority": authority},
        )
    # The retired cursor and secondary budget rows are historical private records.
    # No new invocation is created, no old request/receipt is removed or recharged.


def downgrade():
    raise RuntimeError(
        "scheduled Main task publication is not reversible by replacing execution facts"
    )
