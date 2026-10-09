"""Failed file publications are reclaimed without touching durable owners."""

import json
import os
import time
from uuid import uuid4

import pytest

from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository


def age(path):
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))


@pytest.mark.asyncio
async def test_failed_registration_and_crash_temporary_are_reclaimed(database, tmp_path):
    root = tmp_path / "results"
    store = ToolArtifactRepository(database, root, retention_seconds=60)
    with pytest.raises(ValueError, match="tool_artifact_work_unavailable"):
        await store.write_artifact(
            provider_id="core",
            tool_name="read",
            content="unregistered result",
            media_type="application/json",
            work_id="missing-original-work",
        )
    orphan = next(root.glob("*.json"))
    age(orphan)
    committed = await store.write_artifact(
        provider_id="core",
        tool_name="read",
        content="registered result",
        media_type="application/json",
    )
    age(root / f"{committed}.json")
    temporary = root / ".publishing-crashed"
    temporary.write_text("partial result", encoding="utf-8")
    age(temporary)
    fresh = root / f"{uuid4().hex}.json"
    fresh.write_text("still within publication grace", encoding="utf-8")
    unrelated = root / "keep.txt"
    unrelated.write_text("not an artifact", encoding="utf-8")
    age(unrelated)
    assert await store.cleanup() == 2
    assert not orphan.exists() and not temporary.exists()
    assert fresh.exists() and unrelated.exists()
    assert await store.read(committed) is not None


@pytest.mark.asyncio
async def test_orphan_scan_advances_in_bounded_pages(database, tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    store = ToolArtifactRepository(database, root, retention_seconds=60)
    for _ in range(129):
        path = root / f"{uuid4().hex}.json"
        path.write_text("unregistered", encoding="utf-8")
        age(path)
    assert await store.cleanup() == 128
    assert await store.cleanup() == 1
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_stored_result_can_be_read_by_deep_path_and_full_query(database, tmp_path):
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    query = "原始搜索词" * 60
    leaf = {"match": query, "large": "原" * (2 * 1024 * 1024)}
    value = leaf
    for _ in range(65):
        value = {"nested": value}
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="read",
        content=json.dumps(value, ensure_ascii=False),
        media_type="application/json",
    )
    path = ("nested",) * 65
    page = await store.read(handle, operation="get", path=(*path, "large"), limit=100)
    assert page["value"] == "原" * 100
    assert page["next_offset"] == 100
    assert page["total_characters"] == 2 * 1024 * 1024
    found = await store.read(handle, operation="search", query=query, max_characters=16000)
    assert found["matches"][0]["matched_path"] == [*path, "match"]
    assert found["matches"][0]["value_omitted"] is True
    remaining = await store.read(
        handle, operation="get", path=(*path, "match"), offset=100, limit=1000
    )
    assert remaining["value"] == query[100:]
    assert remaining["next_offset"] is None


@pytest.mark.asyncio
async def test_search_reaches_late_stored_nodes_and_respects_requested_page(database, tmp_path):
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    value = {"before": [[] for _ in range(50001)], "matches": ["needle"] * 129}
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="read",
        content=json.dumps(value),
        media_type="application/json",
    )
    found = await store.read(
        handle, operation="search", query="needle", limit=120, max_characters=50000
    )
    assert len(found["matches"]) == 120
    assert found["next_offset"] == 120
    remaining = await store.read(
        handle, operation="search", query="needle", offset=120, limit=120, max_characters=50000
    )
    assert len(remaining["matches"]) == 9
    assert remaining["next_offset"] is None
