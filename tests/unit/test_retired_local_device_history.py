"""Retired executor effects keep original facts without any live dispatch chain."""

import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.conftest import build_harness, make_settings
from tests.unit.test_tool_effect_audit import active_work
from tests.unit.test_work_domain_receipts import admitted, read_result

from yuki.client_api.http import attach_client_api
from yuki.client_api.openapi import client_openapi
from yuki.executor.db_models import ExecutorOperationModel
from yuki.external_agents.db_models import AgentOperationModel
from yuki.runtime.effect_queries import RuntimeEffectQueries
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


async def original_effect(database, tmp_path, state):
    _, owner, _ = await active_work(database, tmp_path)
    control = owner.control
    key = await admitted(control, "local_device", {"action": "execute"})
    op_id = str(uuid4())
    async with database.immediate_session() as writer:
        writer.add(
            ExecutorOperationModel(
                op_id=op_id,
                executor_id=None,
                person_id=None,
                work_id=control.current["id"],
                tool_call_key=key,
                capability="notification",
                params_json='{"text":"original notification"}',
                state=state,
                level="submitted" if state == "succeeded" else None,
                result_json='{"original_receipt":"untouched"}',
                observation_json='{"system_accepted":true}',
                receipt_hash="original-hash",
            )
        )
        await WorkRepository.bind_domain_receipt(
            writer, control.current["id"], key, "local_device:" + op_id
        )
    return control, key, op_id


async def original_rows(database):
    async with database.read_session() as reader:
        receipt = (await reader.execute(select(ExecutorOperationModel.__table__))).mappings().all()
        effect = (await reader.execute(select(effects))).mappings().all()
        native_count = await reader.scalar(select(func.count()).select_from(AgentOperationModel))
    return receipt, effect, native_count


@pytest.mark.parametrize("state", ["succeeded", "failed", "unknown", "acknowledged"])
async def test_original_work_receipt_survives_retirement_and_reopen(database, tmp_path, state):
    control, key, op_id = await original_effect(database, tmp_path, state)
    before = await original_rows(database)
    # Reopen the database; no LocalExecutors instance or connection is constructed.
    await database.engine.dispose()
    first = await read_result(database, control, key)
    result, evidence, _ = first
    assert result["original_domain_ref"] == "local_device:" + op_id
    assert result["data"]["operation_id"] == op_id
    assert result["data"]["state"] == state
    assert result["data"]["result"] == {"original_receipt": "untouched"}
    assert result["replay_forbidden"] is True
    assert result.get("uncertain", False) is (state == "unknown")
    assert result.get("mutation_committed") is (
        None if state in {"unknown", "acknowledged"} else state == "succeeded"
    )
    assert evidence["pending"] is (state == "acknowledged")
    assert evidence["uncertain"] is (state == "unknown")
    assert await read_result(database, control, key) == first
    assert await original_rows(database) == before


@pytest.mark.parametrize("mismatch", ["reference", "work_id", "operation_key"])
async def test_retired_history_requires_exact_original_identity(database, tmp_path, mismatch):
    control, key, op_id = await original_effect(database, tmp_path, "succeeded")
    before = await original_rows(database)
    query = {
        "reference": "local_device:" + op_id,
        "work_id": control.current["id"],
        "operation_key": key,
    }
    query[mismatch] = "different-original-identity"
    assert await RuntimeEffectQueries(database).inspect_local_operation(**query) is None
    assert await original_rows(database) == before


async def test_retired_routes_are_absent_and_native_and_remote_computer_remain():
    document = client_openapi()
    assert document["info"]["x-yuki-api-revision"] == 7
    assert "/agents/v1/devices" in document["paths"]
    assert "/client/v1/computer/files" in document["paths"]
    assert not any(
        "/executors" in path or path.startswith("/executor/") for path in document["paths"]
    )

    def no_service():
        raise AssertionError("retired routes must not resolve an active service")

    app = FastAPI()
    attach_client_api(app, no_service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/client/v1/executors")).status_code == 404
        assert (await client.post("/executor/v1/heartbeat")).status_code == 404


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_fixed_contract_and_execution_retire_only_local_device(
    database, tmp_path, code_enabled
):
    _, _, runtime = await active_work(database, tmp_path)
    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    contract = MainAgentContract(
        chat, ShortState(WorkspaceStore(tmp_path / "short-state")), code_enabled=code_enabled
    )
    names = {tool.name for tool in await contract.definitions()}
    assert "local_device" not in names
    assert {"external_agent", "terminal_exec", "send"} <= names
    runtime = replace(runtime, runtime_config=await chat._runtime_config.snapshot())
    result = await chat._tools.execute("local_device", json.dumps({"action": "execute"}), runtime)
    assert result.ok is False
    assert result.error_code == "unknown_tool"
    assert await original_rows(database) == ([], [], 0)
