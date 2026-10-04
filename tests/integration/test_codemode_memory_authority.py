"""Real memory mutations retain their one source grant across fresh activations."""

import json
from dataclasses import asdict

from sqlalchemy import select
from tests.support.codemode_cases import build_host, requires_worker, run_code
from tests.unit.test_memory_mutation import _context, _service
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.memory.mutation.models import MemoryMutationRequest
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_schema_v1 import effects

pytestmark = requires_worker


def claim(content, key):
    return {
        "operation": "create",
        "target": {"subject_ref": "current_speaker", "scope_type": "person"},
        "new_content": content,
        "memory_key": key,
        "category": "preference",
        "reason": "用户明确要求记住",
        "confidence": 0.95,
    }


async def test_new_memory_service_and_composition_cannot_replenish_original_source_grant(
    database, tmp_path
):
    _, owner, runtime = await active_work(database, tmp_path)
    _, facts, ledger, _ = _service(database)
    event = await ledger.get_event(runtime.trigger_event_id)
    assert event is not None
    calls = []

    async def domain(name, arguments):
        assert name == "memory_change"
        # No in-memory lock/session/authority state is shared between activations.
        service, _, _, _ = _service(database)
        token = current_work_control.set(owner.control)
        try:
            result = await service.mutate(
                MemoryMutationRequest.model_validate_json(arguments), _context(event)
            )
        finally:
            current_work_control.reset(token)
        calls.append(result)
        return json.dumps({"ok": result.ok, "data": asdict(result)})

    env = build_host(owner, domain)
    first_code = "await yuki_memory_change(" + repr(claim("喜欢茶", "preference:tea")) + ")"
    first, outer = await run_code(env, first_code)
    assert first["stop_reason"] == "memory_observation_required"
    assert calls[-1].ok and calls[-1].new_fact_id is not None
    async with database.sessions() as reader:
        raw = await reader.scalar(
            select(effects.c.receipt_json).where(
                effects.c.effect_key == outer.identity.operation_id + "/c0"
            )
        )
    assert json.loads(raw)["invocation"]["original_domain_ref"] == (
        "memory:" + calls[-1].mutation_id
    )
    # A new outer call and fresh host share only the original durable Work/source.
    resumed = build_host(owner, domain)
    second, _ = await run_code(
        resumed,
        "await yuki_memory_change(" + repr(claim("喜欢咖啡", "preference:coffee")) + ")",
        call_id="code-after-recovery",
    )
    assert second["stop_reason"] == "memory_observation_required"
    assert calls[-1].reason_code == "memory_write_authority_consumed"
    assert not calls[-1].ok
    assert len(await facts.list_person(event.sender_user_id, limit=20)) == 1
    # Reentering the original parent only reads its receipt, never mutates again.
    replay, _ = await run_code(env, first_code)
    assert replay == first and len(calls) == 2
