"""Artifact pages retain original ordering, coordinates, and the outer budget."""

import asyncio
import hashlib
import threading

import pytest
from sqlalchemy import update
from tests.support.social_identity_cases import social_env

from yuki.capabilities.results import artifact_page_size
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatImage
from yuki.tool_results import artifacts
from yuki.tool_results.access import ArtifactAccess
from yuki.tool_results.artifacts import (
    ToolArtifactRepository,
    _get_json,
    _inspect_json,
    _read_text_artifact,
    _read_text_file,
    _search_json,
)

BASE = {"handle": "h", "mode": "json", "logical_root": "data", "provider_id": "p", "tool_name": "t"}


@pytest.mark.parametrize("text_mode", [False, True])
def test_eof_metadata_can_fit_when_the_preceding_cursor_does_not(text_mode):
    total = 100001 if text_mode else 1000001
    original = "x" * total
    offset = total - 2

    def read(budget):
        if text_mode:
            return _read_text_artifact(
                "h", original, offset=offset, limit=2, query="", max_characters=budget
            )
        return _get_json(
            original, path=(), offset=offset, limit=2, base=BASE, max_characters=budget
        )

    final_page = read(10000)
    budget = artifact_page_size(final_page)
    result = read(budget)
    assert result == final_page
    assert result["next_offset"] is None
    assert result["content" if text_mode else "value"] == "xx"


@pytest.mark.parametrize("operation", [_get_json, _inspect_json])
def test_collection_pages_choose_the_maximal_prefix_in_the_real_envelope(operation):
    value = ["中\n" * 3, {"x": [1, 2, 3]}, True, "😀" * 20, None]
    for offset in (0, 1, 3):
        full = operation(
            value, path=("rows",), offset=offset, limit=4, base=BASE, max_characters=10000
        )
        field = "children" if operation is _inspect_json else "value"
        # Get may return its original complete-value shape without page metadata.
        if "next_offset" not in full:
            continue
        for budget in range(300, 650):
            candidates = []
            for length in range(1, len(full[field]) + 1):
                page = {**full, field: full[field][:length]}
                end = offset + length
                page["next_offset"] = end if end < len(value) else None
                if artifact_page_size(page) <= budget:
                    candidates.append(page)
            result = operation(
                value, path=("rows",), offset=offset, limit=4, base=BASE, max_characters=budget
            )
            if candidates:
                assert result == candidates[-1]
            else:
                assert result["error_code"] in {
                    "artifact_value_too_large",
                    "artifact_budget_too_small",
                }


def test_key_selection_is_stable_and_complete_get_keeps_original_order():
    value = {"ß": 1, "SS": 2, "ss": 3, "a": 4, "A": 5, "İ": 6, "i\u0307": 7}
    complete = _get_json(value, path=(), offset=0, limit=8, base=BASE, max_characters=10000)
    assert list(complete["value"]) == list(value)
    page = _get_json(value, path=(), offset=1, limit=4, base=BASE, max_characters=10000)
    assert list(page["value"]) == sorted(value, key=str.casefold)[1:5]
    empty = _inspect_json(value, path=(), offset=100, limit=3, base=BASE, max_characters=10000)
    assert empty["children"] == [] and empty["next_offset"] is None


def test_search_deduplicates_parent_records_but_preserves_first_match_and_array_paths():
    value = {"item": {"b": "hit", "a": "hit", "nested": ["hit", "hit"]}}
    result = _search_json(
        value, path=(), query="hit", offset=0, limit=20, base=BASE, max_characters=10000
    )
    assert [(match["path"], match["matched_path"]) for match in result["matches"]] == [
        (["item"], ["item", "a"]),
        (["item", "nested", 0], ["item", "nested", 0]),
        (["item", "nested", 1], ["item", "nested", 1]),
    ]


def test_search_includes_nonterminal_cursor_in_its_budget():
    result = _search_json(
        ["hit"] * 10002, path=(), query="hit", offset=9999, limit=1, base=BASE, max_characters=351
    )
    assert (
        result.get("error_code") == "artifact_budget_too_small" or artifact_page_size(result) <= 351
    )


def test_search_does_not_omit_a_value_to_fill_a_partially_used_page():
    value = [{"hit": "x" * 30}, {"hit": "y" * 30}]
    full = _search_json(
        value, path=(), query="hit", offset=0, limit=2, base=BASE, max_characters=10000
    )
    first = {**full, "matches": full["matches"][:1], "next_offset": 1}
    result = _search_json(
        value,
        path=(),
        query="hit",
        offset=0,
        limit=2,
        base=BASE,
        max_characters=artifact_page_size(first),
    )
    assert result == first
    assert "value_omitted" not in result["matches"][0]


def test_an_oversized_first_member_does_not_prepare_later_large_values(monkeypatch):
    value = ["first" * 200000, "later" * 200000]
    error = artifacts._oversized_value((), value, base=BASE, max_characters=1)
    original = artifacts.json_character_count

    def counted(item, **kwargs):
        assert item is not value[1]
        return original(item, **kwargs)

    monkeypatch.setattr(artifacts, "json_character_count", counted)
    result = _get_json(
        value, path=(), offset=0, limit=2, base=BASE, max_characters=artifact_page_size(error)
    )
    assert result == error


def test_text_file_stream_decodes_split_unicode_and_keeps_only_requested_page(tmp_path):
    text = "a" * 65535 + "中😀\n" + "尾" * 50000
    raw = text.encode("utf-8")
    path = tmp_path / "text"
    path.write_bytes(raw)
    valid, result = _read_text_file(
        path,
        len(raw),
        hashlib.sha256(raw).hexdigest(),
        handle_id="h",
        offset=65534,
        limit=5,
        max_characters=1000,
    )
    assert valid
    assert result["content"] == text[65534:65539]
    assert result["total_characters"] == len(text)
    assert result["next_offset"] == 65539


@pytest.mark.parametrize("change", ["size", "digest", "utf8"])
def test_integrity_errors_precede_utf8_errors(tmp_path, change):
    raw = b"invalid\xff"
    path = tmp_path / "text"
    path.write_bytes(raw)
    valid, result = _read_text_file(
        path,
        len(raw) + (change == "size"),
        "bad" if change == "digest" else hashlib.sha256(raw).hexdigest(),
        handle_id="h",
        offset=0,
        limit=2,
        max_characters=1000,
    )
    assert not valid
    assert result["error_code"] == "artifact_corrupt"
    assert result["detail"] == (
        "Artifact 不是有效 UTF-8" if change == "utf8" else "Artifact 完整性校验失败"
    )


@pytest.mark.parametrize("operation", ["get", "text"])
async def test_source_is_rechecked_after_worker_page_preparation(
    database, tmp_path, monkeypatch, operation
):
    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="read",
        content='{"x":"original"}',
        media_type="application/json",
        access=access,
    )
    started = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    function_name = "_get_json" if operation == "get" else "_string_page"
    original = getattr(artifacts, function_name)

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert proceed.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(artifacts, function_name, blocked)
    reading = asyncio.create_task(store.read(handle, operation=operation, access=access))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == env.context.conversation_id)
                .values(generation=2)
            )
    finally:
        proceed.set()
    page = await reading
    assert page["error_code"] == "artifact_not_authorized"


async def test_media_publication_passes_the_once_encoded_bytes_to_storage(
    database, tmp_path, monkeypatch
):
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    captured = {}

    async def write(**kwargs):
        captured.update(kwargs)
        return "original-media-handle"

    monkeypatch.setattr(store, "write_artifact", write)
    handle = await store.write_media_artifact(
        provider_id="core",
        tool_name="inspect",
        images=(ChatImage(data_url="data:image/png;base64,YWJj"),),
        access=ArtifactAccess("conversation", 1, "person"),
    )
    assert handle == "original-media-handle"
    assert isinstance(captured["content"], bytes)
    assert b"YWJj" in captured["content"]
