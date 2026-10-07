"""SELF code is resumed from the original initiative without adopting a person."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings
from tests.support.codemode_cases import BINARY, requires_worker
from tests.support.parent_receipts import parent_receipts
from tests.support.runtime_execution import make_work_resumer
from tests.unit.test_self_initiative_runtime import self_source

from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore

pytestmark = requires_worker


@pytest.mark.parametrize("scenario", ["normal", "refused", "cancelled", "resumed"])
async def test_self_code_uses_original_initiative_and_shared_main_service(
    database, tmp_path, scenario
):
    source, _, _ = await self_source(database)
    count = 36 if scenario == "resumed" else 1
    code = (
        f"for i in range({count}):\n"
        "    await yuki_update_short_state({'slot': 1, 'text': str(i), 'expected_revision': i})"
    )
    if scenario == "refused":
        code = (
            "await yuki_send_message({'text': 'MUST_NOT_SEND', "
            "'target': {'kind': 'person', 'subject_ref': 'current_speaker'}})"
        )
    if scenario == "cancelled":
        code += (
            "\nawait yuki_update_short_state({'slot': 1, 'text': 'LATE', 'expected_revision': 1})"
        )

    def respond(request):
        if len(provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("outer", ToolFunction("execute_code", json.dumps({"code": code}))),
                ),
            )
        assert len(parent_receipts(request, "outer")) == 1
        return "NO_REPLY"

    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        enabled_groups_csv="2001",
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest(),
    )
    chat = build_harness(database, settings, provider).processor._chat
    state = ShortState(WorkspaceStore(tmp_path / "state"))
    chat._tools.short_state = state
    chat.runtime.runner.main_contract = MainAgentContract(chat, state)
    chat.runtime.runner.code_mode_settings = settings
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    registry.connect(
        SimpleNamespace(self_id="8000"), provider_id="snowluma", presence_id=source["presence_id"]
    )
    repository = WorkRepository(database)
    lease = await repository.acquire(source["conversation_id"], 1)
    item = await repository.accept(
        lease,
        source_key=f"initiative:{source['initiative_run_id']}",
        source=source,
        goal=source["instruction"],
        output_kind="answer",
        deliver_artifacts=False,
    )
    await repository.release(lease)
    writes = []
    execute = state.execute

    async def record(arguments):
        result = await execute(arguments)
        if json.loads(result)["ok"]:
            writes.append(json.loads(arguments))
            if scenario == "cancelled":
                await repository.cancel(source["conversation_id"])
        return result

    state.execute = record
    resumer = make_work_resumer(
        repository,
        ledger=chat._ledger,
        scopes=chat._conversation_scopes,
        turns=chat._turn_coordinator,
        router=PresenceRouter(database, registry),
        config=chat._runtime_config,
        generate_self=chat.generate_self_initiative,
        generate_wakeup=chat.generate_main_agent_wakeup,
        validate_snapshot=chat.validate_turn_snapshot,
        run_effect=chat.run_effect,
        bindings=chat.runtime.bindings,
    )
    await resumer.resume(item)
    row = await repository.get(item["id"])
    if scenario == "resumed":
        assert row["state"] == "queued"
        assert len(writes) == 32 and len(provider.requests) == 1
        await resumer.resume(row)
        row = await repository.get(item["id"])
    assert row["state"] == ("cancelled" if scenario == "cancelled" else "completed"), (
        resumer.last_error
    )
    assert len(writes) == (0 if scenario == "refused" else count)
    actual_source = json.loads(row["source_json"])
    assert actual_source["initiative_run_id"] == source["initiative_run_id"]
    assert actual_source["principal_kind"] == "self" and actual_source["actor_user_id"] == ""
    before = len(provider.requests)
    await resumer.resume(row)
    assert len(provider.requests) == before
