"""Operation identity supplied by the Host, never guessed from task wording."""

import hashlib
import json

from qq_ai_bot.capabilities.invocation import current_invocation


def creation_key(source_key: str) -> str:
    invocation = current_invocation.get()
    if invocation is None:
        return source_key
    call_key = invocation.call_id
    return "call:" + hashlib.sha256(json.dumps([source_key, call_key]).encode()).hexdigest()
