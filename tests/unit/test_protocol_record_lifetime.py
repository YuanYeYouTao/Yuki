import gc
from weakref import ref

from yuki.domain.messages import ChatMessage
from yuki.runtime.protocol_store import ProtocolStore


async def test_live_transcript_prefix_does_not_reencode_or_retain_released_records(
    database, monkeypatch
):
    store = ProtocolStore(database)
    original = store._encoded_record
    encoded = []

    def encode(record):
        encoded.append(id(record))
        return original(record)

    monkeypatch.setattr(store, "_encoded_record", encode)
    records = tuple(ChatMessage("user", f"{i}:" + "x" * 20000) for i in range(120))
    references = tuple(ref(record) for record in records)
    first = tuple([await store.put_record(record) for record in records])
    async with store.publication("original-work"):
        pass
    before = len(encoded)
    second = tuple([await store.put_record(record) for record in records])
    async with store.publication("original-work"):
        pass
    assert first == second
    assert len(encoded) == before
    del records
    gc.collect()
    assert all(reference() is None for reference in references)
