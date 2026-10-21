"""Text artifact search exposes coordinates in the original Unicode text."""

import pytest

from yuki.tool_results.artifacts import ToolArtifactRepository


@pytest.mark.parametrize("prefix", ["ßx", "İx", "ﬃx"], ids=["sharp-s", "dotted-i", "ligature"])
async def test_search_after_casefold_expansion_preserves_original_coordinates(
    database, tmp_path, prefix
):
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    original = prefix + "TARGETend"
    handle = await store.write_artifact(
        provider_id="core", tool_name="read", content=original, media_type="text/plain"
    )
    result = await store.read(handle, operation="text", query="target", limit=6)
    assert result["query_matched"] is True
    assert result["total_characters"] == len(original)
    assert result["offset"] == len(prefix)
    assert result["content"] == "TARGET"
    assert result["next_offset"] == len(prefix) + 6


@pytest.mark.parametrize(
    ("original", "query", "offset", "expected_start", "expected"),
    [
        ("中xTARGETend", "target", 0, 2, "TARGET"),
        ("Straße!", "STRASSE", 0, 0, "Straße"),
        ("first TARGET last TARGET", "target", 10, 18, "TARGET"),
        ("ßTARGETxTARGETend", "target", 8, 8, "TARGET"),
        ("ßxTARGETend", "", 2, 2, "TARGET"),
    ],
    ids=[
        "chinese-prefix",
        "expanded-match",
        "original-offset",
        "expanded-before-offset",
        "plain-page",
    ],
)
async def test_text_search_and_paging_keep_original_bytes(
    database, tmp_path, original, query, offset, expected_start, expected
):
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core", tool_name="read", content=original, media_type="text/plain"
    )
    result = await store.read(handle, operation="text", query=query, offset=offset, limit=6)
    assert result["offset"] == expected_start
    assert result["content"] == expected
    assert result["total_characters"] == len(original)
    assert result["next_offset"] == (
        expected_start + len(expected) if expected_start + len(expected) < len(original) else None
    )


async def test_missing_text_query_does_not_return_an_unrelated_slice(database, tmp_path):
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core", tool_name="read", content="原文ßtext", media_type="text/plain"
    )
    result = await store.read(handle, operation="text", query="absent", offset=2)
    assert result["query_matched"] is False
    assert result["offset"] == 2
    assert result["content"] == "" and result["next_offset"] is None
    assert result["total_characters"] == len("原文ßtext")
