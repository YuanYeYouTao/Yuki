"""Feedback publication uses prepared sources and payload rather than writer codecs."""

import json
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest
from sqlalchemy import update
from tests.unit.test_participation_feedback import set_work, setup, social

from yuki.conversation import autonomy_repository
from yuki.conversation.autonomy_db_models import InitiativeRunModel
from yuki.services.participation_feedback import reconcile_page


@pytest.mark.parametrize("irrelevant_json", [False, True])
async def test_feedback_sources_and_payload_are_decoded_before_writer(
    database, monkeypatch, irrelevant_json
):
    service, item, run, task = await setup(database)
    await set_work(database, task, state="completed", model_requests=1)
    await social(database, run, "feedback-prepared")
    if irrelevant_json:
        # Object key order and fields absent from the run contract do not change a source.
        source_json = json.dumps(
            [
                {
                    "source_id": source.source_id,
                    "revision": source.revision,
                    "kind": source.kind.value,
                    "irrelevant": "retained metadata",
                }
                for source in run.sources
            ],
            indent=2,
        )
        async with database.immediate_session() as writer:
            await writer.execute(
                update(InitiativeRunModel)
                .where(InitiativeRunModel.id == run.run_id)
                .values(sources_json=source_json)
            )
    active = ContextVar("feedback_writer", default=False)
    original_writer = database.immediate_session

    @asynccontextmanager
    async def writer():
        async with original_writer() as session:
            token = active.set(True)
            try:
                yield session
            finally:
                active.reset(token)

    monkeypatch.setattr(database, "immediate_session", writer)
    for name in ("_json", "_run"):
        original = getattr(autonomy_repository, name)

        def checked(*args, _original=original, **kwargs):
            assert not active.get(), "feedback codec ran while owning the writer"
            return _original(*args, **kwargs)

        monkeypatch.setattr(autonomy_repository, name, checked)
    await reconcile_page(service, (run,))
    settled = await service.repository.get_run(run.run_id)
    assert settled.state == "completed" and settled.feedback_sequence == 1
    assert item.controller.state.feedback[run.run_id].outcome == "completed"
    assert len(item.controller.state.effects) == 2
    service._dispatch.assert_not_awaited()
