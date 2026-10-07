"""Bound receipt storage keys without changing the originating execution identity."""

import hashlib


def social_call_key(operation_id: str) -> str:
    """Bound only the receipt representation; the Work keeps its full identity."""
    if len(operation_id) <= 128:
        return operation_id
    return "social-call:v1:sha256:" + hashlib.sha256(operation_id.encode()).hexdigest()


def social_source_key(source_turn_id: str) -> str:
    """Preserve existing short keys and digest the complete UTF-8 source when needed.

    The input remains the Host's conversation-scoped event/execution identity.
    Only its receipt lookup representation changes; callers must not substitute
    this key for the original execution ID or use it to reconstruct ownership.
    """
    if len(source_turn_id) <= 128:
        return source_turn_id
    return "social-source:v1:sha256:" + hashlib.sha256(source_turn_id.encode("utf-8")).hexdigest()
