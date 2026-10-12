"""Memory results keep real facts and evidence without repeating static rules."""

import json

import pytest
from tests.conftest import make_settings

from qq_ai_bot.capabilities.results import normalize_legacy_result
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.context import MEMORY_GROUNDING_RULE
from qq_ai_bot.memory.enums import MemoryEvidenceRelation, MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.repositories import AgentActionRepository, EventLedgerRepository
from qq_ai_bot.runtime.subagent_tools import worker_prompt
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime


@pytest.mark.parametrize("worker", [False, True])
async def test_memory_read_result_keeps_complete_fact_and_evidence_without_policy(database, worker):
    settings = make_settings(database.url, agent_tool_result_max_characters=2400)
    ledger = EventLedgerRepository(database)
    content = ("observatory history " * 45).strip()
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="original-memory-source",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content=content,
    )
    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="history:observatory",
            category="history",
            content=content,
            source_type=MemorySourceType.EXPLICIT,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            excerpt="observatory history",
        ),
    )
    tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=facts,
        actions=AgentActionRepository(database),
    )
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "current-query",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("1001"),
            "observatory",
            bot_user_id="8000",
        ),
        gateway=None,
        allow_generic_onebot=False,
        read_only=worker,
        trigger_event_id=event.id,
    )
    search = await tools.execute("search_memory", '{"query":"observatory"}', runtime)
    wire = search.model_payload()
    assert wire["ok"], wire
    assert wire["data"]["memories"][0]["content"] == content
    assert wire["data"]["memories"][0]["memory_ref"] == f"M{fact.id}"
    assert wire["evidence_state"]
    assert "memory_grounding_policy" not in wire
    assert len(json.dumps(wire, ensure_ascii=False)) <= settings.agent_tool_result_max_characters
    for name in ("get_memory_fact", "get_memory_evidence"):
        result = await tools.execute(name, json.dumps({"fact_id": fact.id}), runtime)
        payload = result.model_payload()
        assert payload["ok"], payload
        assert "memory_grounding_policy" not in payload
        if name == "get_memory_fact":
            assert payload["evidence_state"]
            assert payload["data"]["memory"]["content"] == content
        else:
            assert payload["data"]["fact_id"] == fact.id
            assert payload["data"]["evidence"][0]["relation"] == "self_statement"
    if worker:
        # Use the deployed builder in either tool mode; no frozen policy prose
        # or new per-result teaching mechanism is introduced.
        for code_enabled in (False, True):
            assert worker_prompt(code_enabled=code_enabled).count(MEMORY_GROUNDING_RULE) == 1


def test_original_memory_receipt_policy_and_evidence_still_decode():
    receipt = {
        "ok": True,
        "data": {"memory_ref": "M7", "content": "original paid fact"},
        "evidence_state": {"state": "original"},
        "memory_grounding_policy": "original saved rule",
    }
    result = normalize_legacy_result(receipt, provider_id="core", tool_name="search_memory")
    assert result.data == receipt["data"]
    assert result.memory_grounding_policy == receipt["memory_grounding_policy"]
    assert result.evidence_state == receipt["evidence_state"]
    assert receipt["memory_grounding_policy"] == "original saved rule"
