"""Content revision shared by Dream and authorized personal memory clients."""

import hashlib
import json

from yuki.memory.models import MemoryFact


def fact_signature(fact: MemoryFact) -> str:
    payload = {
        "id": fact.id,
        "content": fact.content,
        "memory_key": fact.memory_key,
        "category": fact.category,
        "importance": fact.importance,
        "confidence": fact.confidence,
        "source_type": fact.source_type.value,
        "authority": fact.authority.value,
        "status": fact.status.value,
        "conflict_state": fact.conflict_state.value,
        "valid_from": fact.valid_from.isoformat() if fact.valid_from else None,
        "valid_until": fact.valid_until.isoformat() if fact.valid_until else None,
        "evidence_count": fact.evidence_count,
        "updated_at": fact.updated_at.isoformat(),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
