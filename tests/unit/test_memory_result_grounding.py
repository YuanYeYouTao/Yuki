"""Memory results keep real facts and evidence without repeating static rules."""

import json

from tests.conftest import make_settings
from tests.support.event_seed import seed_event
from tests.support.service_dependencies import tool_dependencies

from yuki.capabilities.results import normalize_legacy_result
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.memory.context import MEMORY_GROUNDING_RULE
from yuki.memory.enums import MemoryEvidenceRelation, MemoryScopeType, MemorySourceType
from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.agent_tools import AgentToolService, ToolRuntime
from yuki.services.prompt_composer import PromptComposer


async def test_memory_read_result_keeps_complete_fact_and_evidence_without_policy(database):
    settings = make_settings(database.url, agent_tool_result_max_characters=2400)
    ledger = EventLedgerRepository(database)
    content = ("observatory history " * 45).strip()
    event, _ = await seed_event(
        ledger,
        bot_user_id="8000",
        origin_key="original-memory-source",
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
        **tool_dependencies(database, settings),
    )
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "current-query",
            "current-query",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("1001"),
            "observatory",
            bot_user_id="8000",
            person_id=event.author_person_id,
            conversation_id=event.canonical_conversation_id,
            presence_id=event.ingress_presence_id,
        ),
        trigger_event_id=event.id,
    )
    search = await tools.execute("search_memory", '{"query":"observatory"}', runtime)
    wire = search.model_payload()
    assert "ok" not in wire, wire
    (memory,) = wire["memories"]
    assert memory["content"] == content
    assert memory["fact_id"] == fact.id
    # Business data only: no aliases, envelope, provider or display-only policy.
    assert not {"memory_ref", "evidence_state", "memory_grounding_policy"} & {*wire, *memory}
    assert not {key for key, value in memory.items() if value is None}
    single = await tools.execute("search_memory", json.dumps({"fact_id": fact.id}), runtime)
    assert single.model_payload() == {"memories": [memory]}
    mixed = await tools.execute(
        "search_memory", json.dumps({"fact_id": fact.id, "query": "x"}), runtime
    )
    assert mixed.model_payload()["error"] == "invalid_arguments"
    # Use the deployed builder in either tool mode; no frozen policy prose
    # or new per-result teaching mechanism is introduced.
    for code_enabled in (False, True):
        prompt = PromptComposer(settings.model_copy(update={"code_mode_enabled": code_enabled}))
        assert sum(m.content.count(MEMORY_GROUNDING_RULE) for m in prompt.static_messages()) == 1


def test_original_memory_receipt_still_decodes_its_data():
    receipt = {
        "ok": True,
        "data": {"memory_ref": "M7", "content": "original paid fact"},
        "evidence_state": {"state": "original"},
        "memory_grounding_policy": "original saved rule",
    }
    result = normalize_legacy_result(receipt, provider_id="core", tool_name="read_memory")
    assert result.ok and result.data == receipt["data"]
    # The stored receipt itself is never rewritten.
    assert receipt["memory_grounding_policy"] == "original saved rule"
