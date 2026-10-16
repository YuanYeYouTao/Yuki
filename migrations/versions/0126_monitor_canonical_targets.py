"""Upgrade bundled monitors using only their original approved delivery entries."""

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0126"
down_revision = "0124"
branch_labels = None
depends_on = None

_GITHUB = "github-monitor"
_FEED = "subscription-monitor"
logger = logging.getLogger("alembic.runtime.migration")


class _UnresolvedTarget(ValueError):
    pass


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prepared_hash(value):
    """Frozen v1 request bytes; payload_json is a string, never re-rendered."""
    payload = {
        name: value[name]
        for name in (
            "event_key",
            "event_type",
            "external_source",
            "target_type",
            "target_id",
            "summary",
            "payload_json",
            "text",
            "ask_agent",
            "agent_intent",
        )
    }
    payload["occurred_at"] = datetime.fromisoformat(value["occurred_at"]).isoformat()
    payload["media"] = [
        {
            "index": item["index"],
            "handle_id": item["handle_id"],
            "sha256": item["sha256"],
            "expires_at": (
                datetime.fromisoformat(item["expires_at"]).isoformat()
                if item["expires_at"]
                else None
            ),
        }
        for item in value["media"]
    ]
    if value.get("conversation_id") is not None:
        payload["conversation_id"] = value["conversation_id"]
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _target(value, grants):
    kind, target_id = value["target_type"], value["target_id"]
    conversation_id = value.get("conversation_id")
    matching = {
        (owner, conversation)
        for raw_kind, raw_id, owner, conversation in grants
        if raw_kind == kind
        and target_id in (raw_id, owner)
        and (conversation_id is None or conversation_id == conversation)
    }
    if not matching:
        raise _UnresolvedTarget("original_target_grant_missing")
    if len(matching) != 1:
        raise _UnresolvedTarget("original_target_grant_ambiguous")
    owner, conversation = next(iter(matching))
    if owner is None or conversation is None:
        raise _UnresolvedTarget("original_delivery_entry_missing")
    value["target_id"], value["conversation_id"] = owner, conversation


def _target_key(value):
    return f"{value['target_type']}:{value['target_id']}:conversation:{value['conversation_id']}"


def _targets(values, grants):
    for value in values:
        _target(value, grants)
    keys = [_target_key(value) for value in values]
    if len(keys) != len(set(keys)):
        raise _UnresolvedTarget("independent_target_records_collide")


def _deliveries(values, grants):
    for delivery in values:
        prepared = delivery.get("prepared")
        if prepared is not None and (
            prepared["target_type"],
            prepared["target_id"],
            prepared.get("conversation_id"),
        ) != (delivery["target_type"], delivery["target_id"], delivery.get("conversation_id")):
            raise _UnresolvedTarget("original_prepared_target_mismatch")
        _target(delivery, grants)
        delivery["target_key"] = _target_key(delivery)
        if prepared is not None:
            if prepared["request_hash"] != _prepared_hash(prepared):
                raise _UnresolvedTarget("original_prepared_request_hash_mismatch")
            _target(prepared, grants)
            if (prepared["target_type"], prepared["target_id"], prepared["conversation_id"]) != (
                delivery["target_type"],
                delivery["target_id"],
                delivery["conversation_id"],
            ):
                raise _UnresolvedTarget("original_prepared_target_mismatch")
            prepared["request_hash"] = _prepared_hash(prepared)
    keys = [value["target_key"] for value in values]
    if len(keys) != len(set(keys)):
        raise _UnresolvedTarget("independent_target_records_collide")


def _rewrite(value, *, kind, grants):
    value = deepcopy(value)
    if kind in ("repositories", "subscriptions"):
        for subscription in value:
            _targets(subscription["targets"], grants)
    elif kind == "github_monitor_queue_v1":
        for source in value.get("pending", ()):
            _targets(source["target_snapshot"], grants)
        inflight = value.get("inflight")
        if inflight is not None:
            for source in inflight["members"]:
                _targets(source["target_snapshot"], grants)
            _deliveries(inflight["deliveries"], grants)
        activation = value.get("activation")
        if activation is not None:
            _deliveries(activation["deliveries"], grants)
    elif kind == "feed_state_v1":
        for request in value.get("pending", ()):
            _target(request["target"], grants)
    return value


def upgrade():
    connection = op.get_bind()
    grants = {}
    for plugin in (_GITHUB, _FEED):
        grants[plugin] = list(
            connection.execute(
                sa.text("""
            SELECT target_type,target_id,
                   CASE WHEN target_type='private' THEN canonical_target_person_id
                        ELSE canonical_target_space_id END,
                   canonical_conversation_id
            FROM plugin_background_target_grants WHERE plugin_id=:plugin
        """),
                {"plugin": plugin},
            )
        )
    prepared = []
    for table, column in (("plugin_config_values", "key"), ("plugin_state", "namespace")):
        github_kind, feed_kind = (
            ("repositories", "subscriptions")
            if table == "plugin_config_values"
            else ("github_monitor_queue_v1", "feed_state_v1")
        )
        rows = (
            connection.execute(
                sa.text(f"""
            SELECT id,plugin_id,{column} AS kind,value_json FROM {table}
            WHERE (plugin_id=:github AND {column}=:github_kind)
               OR (plugin_id=:feed AND {column}=:feed_kind)
            ORDER BY id
        """),
                {
                    "github": _GITHUB,
                    "feed": _FEED,
                    "github_kind": github_kind,
                    "feed_kind": feed_kind,
                },
            )
            .mappings()
            .all()
        )
        for row in rows:
            try:
                value = _rewrite(
                    json.loads(row["value_json"]), kind=row["kind"], grants=grants[row["plugin_id"]]
                )
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                logger.warning(
                    "monitor_target_upgrade_unresolved plugin=%s table=%s row_id=%s category=%s",
                    row["plugin_id"],
                    table,
                    row["id"],
                    str(exc) if isinstance(exc, _UnresolvedTarget) else "legacy_shape_invalid",
                )
                continue
            encoded = _canonical_json(value)
            if json.loads(row["value_json"]) != value:
                prepared.append((table, row["id"], encoded))
    for table, row_id, encoded in prepared:
        connection.execute(
            sa.text(f"UPDATE {table} SET value_json=:value WHERE id=:id"),
            {"id": row_id, "value": encoded},
        )


def downgrade():
    raise RuntimeError("Monitor delivery entries cannot be inferred back to QQ targets")
