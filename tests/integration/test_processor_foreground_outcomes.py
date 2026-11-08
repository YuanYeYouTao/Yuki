"""Foreground admission: plain drops stay drops; writes with an open outcome propagate.

Failure matrix written before the B2 processor refactor (2026-11-07):

- a repeated delivery of an already admitted direct message is answered once;
- staging, human-input preparation, re-preparation and ready-marking each raise
  duplicate / conflict / interrupted / superseded / work-conflict errors;
- each error carries no write note, a confirmed rollback, a confirmed commit, or
  an unknown commit outcome. Only the last two propagate, and then without
  discarding the staged input; the others map to their original result.

Each case writes its observed outcome to ``outcome.json`` under its tmp path.
"""

import json
from unittest.mock import AsyncMock

import pytest
from tests.conftest import MemorySender, build_harness, make_settings

from yuki.conversation.ordinary_admission import (
    OrdinaryAdmissionConflict,
    OrdinaryAdmissionDuplicate,
)
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.llm.fake import FakeLLMProvider
from yuki.runtime.work_repository import WorkConflict
from yuki.services.turn_coordinator import TurnInterruptedError, TurnSupersededError

NOTES = {
    "plain": (),
    "rolled_back": ("database_write_phase:execute", "database_rollback:confirmed"),
    "committed": ("database_write_phase:cleanup", "database_commit:confirmed"),
    "unknown": ("database_write_phase:commit",),
}
ERRORS = {
    "duplicate": (OrdinaryAdmissionDuplicate, (False, "ordinary_already_admitted"), False),
    "conflict": (OrdinaryAdmissionConflict, (False, "ordinary_source_changed"), True),
    "interrupted": (TurnInterruptedError, (True, "turn_interrupted"), True),
    "superseded": (TurnSupersededError, (True, "turn_interrupted"), True),
    "work_conflict": (WorkConflict, (True, "turn_interrupted"), True),
}
# Which errors each foreground step maps; the rest are not raised there.
STEPS = {
    "stage": tuple(ERRORS),
    "human_input": ("interrupted", "superseded", "work_conflict"),
    "reprepare": ("conflict",),
    "ready": ("duplicate",),
}
CASES = [
    pytest.param(step, error, note, id=f"{step}-{error}-{note}")
    for step, errors in STEPS.items()
    for error in errors
    for note in NOTES
]


def _private(key="foreground-outcome", text="帮我看一下"):
    return InboundMessage(
        ingress_key=key,
        native_message_id=key,
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001"),
        bot_user_id="8000",
        text=text,
    )


async def test_repeated_delivery_of_admitted_direct_message_answers_once(database, tmp_path):
    provider = FakeLLMProvider(lambda _: "收到")
    processor = build_harness(database, make_settings(database.url), provider).processor
    sender = MemorySender()

    first = await processor.handle(_private(), sender)
    again = await processor.handle(_private(), sender)

    outcome = {"first": first.reason, "again": again.reason, "requests": len(provider.requests)}
    (tmp_path / "outcome.json").write_text(json.dumps(outcome, ensure_ascii=False))
    assert outcome == {"first": "chat", "again": "ordinary_already_admitted", "requests": 1}
    assert not again.handled and again.sent_messages == 0


@pytest.mark.parametrize(("step", "error", "note"), CASES)
async def test_foreground_write_outcome_matrix(database, monkeypatch, tmp_path, step, error, note):
    provider = FakeLLMProvider(lambda _: "收到")
    processor = build_harness(database, make_settings(database.url), provider).processor
    error_type, (handled, reason), discards = ERRORS[error]
    failure = error_type(error)
    for item in NOTES[note]:
        failure.add_note(item)

    discard = AsyncMock()
    monkeypatch.setattr(processor._chat, "discard_work_input", discard)
    staged_id = 7 if step == "ready" else None
    if step == "stage":
        monkeypatch.setattr(processor._chat, "stage_work_input", AsyncMock(side_effect=failure))
    else:
        monkeypatch.setattr(processor._chat, "stage_work_input", AsyncMock(return_value=staged_id))
    if step == "human_input":
        monkeypatch.setattr(processor, "_prepare_human_input", AsyncMock(side_effect=failure))
    if step == "reprepare":
        original_prepare = processor.ordinary_admissions.prepare
        calls = 0

        def prepare(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise failure
            return original_prepare(*args, **kwargs)

        monkeypatch.setattr(processor.ordinary_admissions, "prepare", prepare)
    if step == "ready":
        monkeypatch.setattr(processor._chat, "ready_work_input", AsyncMock(side_effect=failure))

    propagates = note in {"committed", "unknown"}
    raised = None
    result = None
    try:
        result = await processor.handle(_private(key=f"{step}-{error}-{note}"), MemorySender())
    except Exception as exc:
        raised = exc
    outcome = {
        "raised": raised is failure,
        "result": None if result is None else [result.handled, result.reason],
        "discarded": [call.args for call in discard.await_args_list],
        "requests": len(provider.requests),
    }
    (tmp_path / "outcome.json").write_text(json.dumps(outcome, ensure_ascii=False))

    if propagates:
        assert raised is failure and result is None
        assert not discard.await_count
    else:
        assert raised is None and result is not None
        assert (result.handled, result.reason) == (handled, reason)
        assert discard.await_args_list == ([((staged_id,),)] if discards else [])
    assert not provider.requests
