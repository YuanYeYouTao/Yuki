"""Failed file publications are reclaimed without touching durable owners."""

import os
import time
from uuid import uuid4

import pytest

from qq_ai_bot.mcp.repository import ToolArtifactRepository


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
