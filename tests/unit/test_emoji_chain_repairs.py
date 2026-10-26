"""Corrected emoji-chain facts: local identity, TGS sniff, and the artifact lock."""

import gzip
import io
import json
import threading
from pathlib import Path

from yuki.conversation.media_index import attachment_origin, same_attachment_origin
from yuki.emoji.media import media_facts
from yuki.gateway.social import TransportEffectError
from yuki.workspace.artifact_lock import ArtifactLockBusy, ArtifactRootLock


def test_attachment_origin_ignores_caption_and_segment_text() -> None:
    original = {"file_id": "photo-1", "caption": "午饭"}
    edited = {"caption": "晚饭", "file_id": "photo-1"}
    replaced = {"file_id": "photo-2", "caption": "午饭"}
    assert attachment_origin(original) == attachment_origin(edited)
    assert attachment_origin(original) != attachment_origin(replaced)
    previous = {"type": "file", "data": {"file": "animated", "preview": {"file": "old"}}}
    caption = {"type": "file", "data": {"file": "animated", "preview": {"file": "old"}}}
    thumbnail = {"type": "file", "data": {"file": "animated", "preview": {"file": "new"}}}
    assert same_attachment_origin(previous, caption)
    assert not same_attachment_origin(previous, thumbnail)


def test_tgs_sniff_reads_top_level_fields_and_stops_before_a_huge_string() -> None:
    prefix = b'{"w":32,"h":48,"v":"5.5.2","layers":[1],'
    filler = b'"unused":"' + (b"x" * 8_000_000) + b'"}'
    blob = gzip.compress(prefix + filler)

    class Limited(io.BytesIO):
        def read(self, size: int = -1) -> bytes:  # type: ignore[override]
            if self.tell():
                raise AssertionError("sniff kept reading after the header")
            return super().read(8192)

    facts = media_facts(Limited(blob))
    assert facts is not None
    assert facts["image_format"] == "TGS"
    assert facts["width"] == 32 and facts["height"] == 48


def test_nested_tgs_names_do_not_count() -> None:
    payload = json.dumps({"meta": {"v": "no", "layers": [1], "w": 9, "h": 9}}).encode()
    assert media_facts(io.BytesIO(gzip.compress(payload))) is None


def test_damaged_tgs_tail_keeps_a_recognized_prefix() -> None:
    payload = b'{"v":"5.5.2","layers":[],"w":8,"h":8,"rest":"' + (b"y" * 1000)
    raw = gzip.compress(payload + b'"}')
    damaged = raw[:-8] + b"bad-tail"
    facts = media_facts(io.BytesIO(damaged))
    assert facts is not None and facts["image_format"] == "TGS"


def test_transport_effect_before_message_call_is_not_uncertain() -> None:
    error = TransportEffectError("artifact_transport_unavailable", uncertain=False)
    assert error.uncertain is False


def test_shared_artifact_lock_allows_parallel_holders_and_blocks_cleanup(tmp_path: Path) -> None:
    lock = ArtifactRootLock(tmp_path)
    first = lock.acquire(exclusive=False)
    entered = threading.Event()
    release = threading.Event()

    def second() -> None:
        held = lock.acquire(exclusive=False)
        entered.set()
        assert release.wait(5)
        lock.release(held)

    worker = threading.Thread(target=second)
    worker.start()
    assert entered.wait(5)
    try:
        lock.acquire(exclusive=True, wait=False)
    except ArtifactLockBusy:
        blocked = True
    else:
        blocked = False
    assert blocked
    release.set()
    worker.join(5)
    lock.release(first)
    exclusive = lock.acquire(exclusive=True, wait=False)
    lock.release(exclusive)
