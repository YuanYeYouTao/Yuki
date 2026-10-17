"""Same-number QQ and Telegram inputs keep distinct real Memory owners."""

from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_telegram_runtime import drain
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.integration.test_telegram_runtime import update as telegram_update
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_memory_mutation import _context, _event, _service
from tests.unit.test_memory_worker_shutdown import _Provider as ExtractionProvider

from yuki.identity.canonical_repository import ensure_person
from yuki.identity.db_models import IdentityBindingModel
from yuki.memory.models import MemoryFactQuery
from yuki.memory.mutation.models import MemoryMutationRequest
from yuki.memory.repository import MemoryJobRepository
from yuki.memory.worker import MemoryWorker
from yuki.persistence.models import ChatEventModel, MemoryJobModel
from yuki.services.concurrency import ConcurrencyManager


async def test_native_telegram_and_qq_same_number_write_manual_and_automatic_facts(
    telegram_env, database
):
    env = telegram_env
    mutations, facts, ledger, _ = _service(database)
    async with database.immediate_session() as writer:
        qq_person = await ensure_person(writer, "42", now=datetime.now(UTC))
    text = "我准备考研，而且喜欢茶"
    qq = await _event(ledger, message_id="qq-memory-source", sender_user_id="42", content=text)
    env.model._responder = lambda _: "收到"
    await env.gateway.receive(telegram_update(text=text))
    await drain(env)
    async with database.sessions() as reader:
        telegram_binding = await reader.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "telegram",
                IdentityBindingModel.external_account_id == "42",
            )
        )
        telegram_id = await reader.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.author_person_id == telegram_binding.person_id,
                ChatEventModel.direction == "inbound",
            )
        )
        telegram_person = telegram_binding.person_id
    telegram = await ledger.get_event(telegram_id)
    assert telegram is not None and qq_person != telegram_person
    assert qq.author_person_id == qq_person and telegram.author_person_id == telegram_person
    manual = {}
    for source in (qq, telegram):
        result = await mutations.mutate(
            MemoryMutationRequest.model_validate(
                {
                    "operation": "create",
                    "target": {"subject_ref": "current_speaker", "scope_type": "person"},
                    "new_content": "喜欢茶",
                    "evidence_quote": "喜欢茶",
                    "memory_key": "preference:tea",
                    "category": "preference",
                }
            ),
            replace(
                _context(source),
                actor_person_id=source.author_person_id,
                operation_id=f"manual:{source.id}",
            ),
        )
        assert result.ok
        manual[source.author_person_id] = result.new_fact_id
    provider = ExtractionProvider()
    jobs = MemoryJobRepository(database)
    worker = MemoryWorker(
        settings=make_settings(
            database.url, memory_batch_trigger_count=1, memory_batch_max_wait_seconds=0
        ),
        jobs=jobs,
        facts=facts,
        ledger=ledger,
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
    )
    for source in (qq, telegram):
        await worker.enqueue(source.id, source.scope.key)
    assert await worker.process_once() == 1
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    assert provider.requests == 2
    all_ids = set()
    for person, source in ((qq_person, qq), (telegram_person, telegram)):
        stored = await facts.repository.list_facts(
            MemoryFactQuery(scope_type="person", canonical_subject_person_id=person)
        )
        assert {fact.content for fact in stored} == {"喜欢茶", "准备考研"}
        assert manual[person] in {fact.id for fact in stored}
        assert all(fact.canonical_subject_person_id == person for fact in stored)
        for fact in stored:
            assert [item.event_id for item in await facts.list_evidence(fact.id)] == [source.id]
            all_ids.add(fact.id)
    assert len(all_ids) == 4
    async with database.sessions() as reader:
        rows = list(await reader.scalars(select(MemoryJobModel)))
        assert {row.canonical_person_id for row in rows} == {qq_person, telegram_person}
        assert all(row.status == "done" for row in rows)
