"""Frozen additive result migrations keep execution identities and exact counters."""

import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def load_migration(number):
    path = next(Path("migrations/versions").glob(f"{number}_*.py"))
    spec = importlib.util.spec_from_file_location(f"migration_{number}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0086_keeps_unknown_execution_and_binds_original_result_handle():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tool_artifacts (handle_id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO tool_artifacts VALUES ('original-handle')")
        connection.exec_driver_sql(
            "CREATE TABLE runtime_work_effects "
            "(effect_key TEXT PRIMARY KEY, work_id TEXT, state TEXT, "
            "receipt_json TEXT, updated REAL)"
        )
        raw = json.dumps(
            {
                "result": json.dumps(
                    {
                        "ok": False,
                        "uncertain": True,
                        "artifact_handle": "original-handle",
                        "error_code": "original_error",
                        "data": {
                            "run_id": "original-run",
                            "pending": True,
                            "artifact_id": "workspace-product",
                        },
                    }
                )
            }
        )
        connection.execute(
            sa.text("INSERT INTO runtime_work_effects VALUES (:key, :work, :state, :receipt, 1)"),
            {"key": "original-key", "work": "original-work", "state": "accepted", "receipt": raw},
        )
        migration = load_migration("0086")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        key, work, state, serialized = connection.exec_driver_sql(
            "SELECT effect_key, work_id, state, receipt_json FROM runtime_work_effects"
        ).one()
        evidence = json.loads(serialized)["outcome"]
        assert (key, work, state) == ("original-key", "original-work", "accepted")
        assert evidence["uncertain"] and evidence["pending"]
        assert evidence["run_id"] == "original-run" and evidence["error_code"] == "original_error"
        assert evidence["artifacts"] == ["workspace-product"]
        assert connection.exec_driver_sql(
            "SELECT work_id, effect_key FROM tool_artifacts"
        ).one() == ("original-work", "original-key")
        migration.downgrade()
        assert (
            connection.exec_driver_sql("SELECT effect_key FROM runtime_work_effects").scalar()
            == "original-key"
        )


def test_0088_counter_and_deletion_fence_upgrade_downgrade():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE runtime_work (id VARCHAR(36) PRIMARY KEY)")
        migration = load_migration("0088")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        connection.exec_driver_sql("INSERT INTO runtime_protocol_objects VALUES ('hash', 17, 1, 0)")
        assert (
            connection.exec_driver_sql("SELECT byte_size FROM runtime_protocol_usage").scalar()
            == 17
        )
        connection.exec_driver_sql("DELETE FROM runtime_protocol_objects")
        assert (
            connection.exec_driver_sql("SELECT byte_size FROM runtime_protocol_usage").scalar() == 0
        )
        migration.downgrade()
        assert not any(
            name.startswith("runtime_protocol_")
            for name in sa.inspect(connection).get_table_names()
        )


@pytest.mark.parametrize(
    "proof",
    [
        "paired",
        "pending",
        "forged_summary",
        "wrong_work",
        "wrong_result",
        "wrong_chain",
        "responses_paired",
        "responses_delta",
        "gemini_paired",
        "gemini_delta",
        "gemini_ambiguous",
    ],
)
def test_0086_only_migrates_delivery_from_original_owned_call_and_receipt(proof):
    engine = sa.create_engine("sqlite://")
    arguments = json.dumps(
        {"artifact_id": "original-product", "attachment_kind": "file", "text": "caption"}
    )
    result = json.dumps(
        {
            "ok": True,
            "data": {
                "target": {"kind": "space", "id": "original-space"},
                "status": "succeeded",
                "file": {"status": "succeeded"},
                "caption": {"status": "succeeded"},
            },
        }
    )
    payload = {
        "transcript": {
            "chain_id": "original-chain",
            "items": [
                {
                    "kind": "message",
                    "value": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "original-call",
                                "function": {"name": "send_message", "arguments": arguments},
                            }
                        ],
                    },
                },
                {"kind": "result", "value": {"call_id": "original-call", "output": result}},
            ],
        },
        "pending": [],
        "metadata": {
            "sequence": 3,
            "effects": [
                {
                    "tool": "send_message",
                    "delivered_artifacts": ["FORGED"],
                    "caption_delivered": True,
                }
            ],
        },
    }
    if proof.startswith("responses"):
        function = {
            "type": "function_call",
            "call_id": "original-call",
            "name": "send_message",
            "arguments": arguments,
        }
        native = [function]
        payload["transcript"]["items"] = [payload["transcript"]["items"][1]]
        if proof == "responses_paired":
            native.append(
                {"type": "function_call_output", "call_id": "original-call", "output": result}
            )
            payload["transcript"]["items"] = []
        payload["transcript"]["continuation"] = {
            "provider": "openai",
            "protocol": "responses",
            "payload": native,
        }
    elif proof.startswith("gemini"):
        native = [
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {"name": "send_message", "args": json.loads(arguments)},
                        "thoughtSignature": "private-signature",
                    }
                ],
                "_call_ids": ["original-call"],
            }
        ]
        payload["transcript"]["items"] = [payload["transcript"]["items"][1]]
        if proof in {"gemini_paired", "gemini_ambiguous"}:
            native.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "send_message",
                                "response": {"output": result},
                            }
                        }
                    ],
                }
            )
            payload["transcript"]["items"] = []
        if proof == "gemini_ambiguous":
            native[0]["parts"].append(
                {"functionCall": {"name": "send_message", "args": {"artifact_id": "different"}}}
            )
            native[0]["_call_ids"].append("different-call")
        payload["transcript"]["continuation"] = {
            "provider": "gemini",
            "protocol": "gemini",
            "payload": native,
        }
    elif proof == "pending":
        payload["transcript"]["items"] = []
        payload["pending"] = [
            {"id": "original-call", "name": "send_message", "arguments": arguments}
        ]
    elif proof == "forged_summary":
        payload["transcript"]["items"] = []
    elif proof == "wrong_result":
        payload["transcript"]["items"][1]["value"]["output"] = "different backend receipt"
    elif proof == "wrong_chain":
        payload["transcript"]["chain_id"] = "different-chain"
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tool_artifacts (handle_id TEXT PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE runtime_work_effects "
            "(effect_key TEXT PRIMARY KEY, work_id TEXT, state TEXT, "
            "receipt_json TEXT, updated REAL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE runtime_work_journal "
            "(work_id TEXT PRIMARY KEY, chain_id TEXT, payload_json TEXT)"
        )
        connection.execute(
            sa.text("INSERT INTO runtime_work_journal VALUES (:work, :chain, :payload)"),
            {
                "work": "different-work" if proof == "wrong_work" else "original-work",
                "chain": "original-chain",
                "payload": json.dumps(payload),
            },
        )
        connection.execute(
            sa.text("INSERT INTO runtime_work_effects VALUES (:key, :work, 'accepted', :raw, 1)"),
            {
                "key": "original-chain:3:original-call",
                "work": "original-work",
                "raw": json.dumps({"result": result}),
            },
        )
        migration = load_migration("0086")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        evidence = json.loads(
            connection.exec_driver_sql("SELECT receipt_json FROM runtime_work_effects").scalar()
        )["outcome"]
        if proof in {
            "paired",
            "pending",
            "responses_paired",
            "responses_delta",
            "gemini_paired",
            "gemini_delta",
        }:
            assert evidence["tool"] == "send_message"
            assert evidence["delivered_artifacts"] == ["original-product"]
            assert evidence["caption_delivered"] and evidence["delivered_message"]
            assert evidence["delivery_target"] == {"kind": "space", "id": "original-space"}
        else:
            assert evidence["delivered_artifacts"] == []
            assert not evidence["caption_delivered"] and not evidence["delivered_message"]
            assert evidence["uncertain"] and evidence["delivery_verification_required"]
