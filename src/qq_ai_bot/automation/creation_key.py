"""Operation identity supplied by the Host, never guessed from task wording."""

import hashlib
import json

from qq_ai_bot.capabilities.invocation import current_invocation
from qq_ai_bot.runtime.work_activation import current_work_control


def creation_key(source_key: str) -> str:
    invocation = current_invocation.get()
    if invocation is None:
        return source_key
    work = current_work_control.get()
    call_key = (
        work.session.call_key(invocation.call_id)
        if work is not None and work.session is not None
        else f"{invocation.execution_key}:{invocation.call_id}"
    )
    return "call:" + hashlib.sha256(json.dumps([source_key, call_key]).encode()).hexdigest()
