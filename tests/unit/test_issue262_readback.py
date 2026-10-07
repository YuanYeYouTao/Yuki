"""#262: real file pages, scalar artifact paging and encoded receipt bounds."""

import hashlib
import json
from dataclasses import replace

import pytest

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.services.chat import _fit_artifact_page_result
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.workspace.files import FileWorkspace
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("limit", [4, 97, 32768])
def test_utf8_pages_reconstruct_source_and_distinguish_eof(tmp_path, published, limit):
    text = '資料🙂\\"\n' * 6000
    data = text.encode()
    version = hashlib.sha256(data).hexdigest()
    if published:
        store = WorkspaceStore(tmp_path / "published")
        key = store.write("source.txt", data)["artifact_id"]
    else:
        tmp_path.joinpath("source.txt").write_bytes(data)
        store = FileWorkspace(tmp_path)
        key = "source.txt"
    offset, pieces = 0, []
    while True:
        page = store.read(key, offset=offset, limit=limit, expected_version=version)
        assert page["size"] == len(data) and page["version"] == version
        assert page["offset_unit"] == "bytes" and page["read_state"] == "inline"
        assert page["next_offset"] == offset + len(page["text"].encode())
        assert page["eof"] is (not page["truncated"])
        pieces.append(page["text"])
        if page["eof"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(pieces) == text
    eof = store.read(key, offset=len(data), expected_version=version)
    assert eof["text"] == "" and eof["eof"] and eof["size"] > 0


def test_empty_binary_version_conflict_and_change_during_page(tmp_path, monkeypatch):
    store = FileWorkspace(tmp_path)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    page = store.read("empty")
    assert page["text"] == "" and page["size"] == 0 and page["eof"]
    empty.write_bytes(b"\x00\xff")
    binary = store.read("empty")
    assert binary["read_state"] == "binary" and binary["text"] is None
    with pytest.raises(WorkspaceError, match="version_conflict"):
        store.read("empty", expected_version=page["version"])
    empty.write_text("before")
    fingerprint = store.fingerprint

    def change_after_fingerprint(fd):
        original = fingerprint(fd)
        empty.write_text("changed after fingerprint")
        return original

    monkeypatch.setattr(store, "fingerprint", change_after_fingerprint)
    with pytest.raises(WorkspaceError, match="file_changed_during_read"):
        store.read("empty")


@pytest.mark.parametrize("budget", [1000, 24000])
async def test_externalized_file_never_looks_empty(database, tmp_path, budget):
    source = tmp_path / "source"
    source.write_text("source-page\n" * 12000)
    original = FileWorkspace(tmp_path).read("source")
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    result = await ToolResultBudgeter(max_characters=budget, artifacts=store).render(
        ToolExecutionResult(ok=True, data=original, provider_id="core", tool_name="workspace_read")
    )
    body = json.loads(result.text)
    assert len(result.text) <= budget
    assert body["truncated"] and body["artifact_handle"] == result.artifact_id
    assert body["data"]["read_state"] == "externalized" and body["data"]["text"] is None
    for key in ("size", "offset", "next_offset", "version", "eof", "offset_unit"):
        assert body["data"][key] == original[key]


@pytest.mark.parametrize("budget", [1000, 24000, 32000])
async def test_scalar_string_pages_fit_real_envelope_without_nested_artifacts(
    database, tmp_path, budget
):
    text = '中文🙂\\"\n' * 5000
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="workspace_read",
        media_type="application/json",
        content=json.dumps({"ok": True, "data": {"text": text}}),
    )
    offset, pieces = 0, []
    while True:
        page = await store.read(
            handle,
            operation="get",
            path=("text",),
            offset=offset,
            limit=32000,
            max_characters=budget,
        )
        assert page is not None and "error_code" not in page
        typed = _fit_artifact_page_result(page, max_characters=budget)
        assert typed.ok
        # MainAgentBackend adds mutation evidence before result budgeting.
        typed = replace(typed, mutation_committed=False)
        bounded = await ToolResultBudgeter(max_characters=budget, artifacts=store).render(typed)
        body = json.loads(bounded.text)
        assert not bounded.truncated and bounded.artifact_id is None
        assert len(bounded.text) <= budget
        assert len(json.dumps({"result": bounded.text}, ensure_ascii=False).encode()) <= 49152
        data = body["data"]
        assert data["offset_unit"] == "characters" and data["total_characters"] == len(text)
        pieces.append(data["value"])
        if data["next_offset"] is None:
            break
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    assert "".join(pieces) == text
    too_small = await store.read(handle, operation="get", path=("text",), max_characters=1)
    assert too_small["error_code"] == "artifact_budget_too_small"


async def test_json_read_rechecks_authority_after_file_io(database, tmp_path, monkeypatch):
    import asyncio

    from sqlalchemy import update
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.tool_results.access import ArtifactAccess

    env = await social_env(database, tmp_path)
    access = ArtifactAccess(env.context.conversation_id, 1, env.person)
    store = ToolArtifactRepository(database, tmp_path / "results", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core",
        tool_name="workspace_read",
        media_type="application/json",
        content=json.dumps({"ok": True, "data": {"text": "private"}}),
        access=access,
    )
    original = asyncio.to_thread

    async def revoke_after_io(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        if function == store._read_bounded:
            async with database.sessions() as writer, writer.begin():
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == access.conversation_id)
                    .values(generation=2)
                )
        return result

    monkeypatch.setattr(asyncio, "to_thread", revoke_after_io)
    page = await store.read(handle, operation="get", path=("text",), access=access)
    assert page["error_code"] == "artifact_not_authorized" and "value" not in page
