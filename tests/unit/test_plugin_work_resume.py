"""An original plugin answer keeps its bytes, commit and exception through release."""

import asyncio
import json

import pytest
from sqlalchemy import select
from tests.integration.test_plugin_agent_domain_grants import source_host
from tests.unit.test_history_dispatch_ownership import _tool

from yuki.llm.fake import FakeLLMProvider
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs


@pytest.mark.parametrize("business", ["success", "commit_unknown", "cancelled"])
async def test_resume_keeps_original_outcome_when_release_fails(
    database, tmp_path, monkeypatch, business
):
    provider = FakeLLMProvider(lambda _: _tool("environment_status", {}, "original-status"))
    _, _, _, source, host = await source_host(database, tmp_path, provider, ())
    try:
        with host.invocation_scope(host.plugin_id, source):
            admitted = await host.agent.run(
                "Wait for an original plugin answer", max_model_requests=1
            )
        identity = admitted.data["work_id"]
        before = await WorkRepository(database).get(identity)
        assert before["state"] == "queued"
        release, enqueue = WorkRepository.release, WorkRepository.enqueue
        cleanup = OSError("original release failure")
        original = (
            asyncio.CancelledError("original input cancellation")
            if business == "cancelled"
            else RuntimeError("original input commit unknown")
        )
        if business == "commit_unknown":
            original.add_note("database_write_phase:commit")
            original.add_note("database_commit:unknown")

        async def release_then_fail(repository, lease):
            await release(repository, lease)
            raise cleanup

        async def original_enqueue(repository, *args, **kwargs):
            if business == "cancelled":
                raise original
            result = await enqueue(repository, *args, **kwargs)
            if business == "commit_unknown":
                raise original
            return result

        monkeypatch.setattr(WorkRepository, "release", release_then_fail)
        monkeypatch.setattr(WorkRepository, "enqueue", original_enqueue)
        text = "```python\n\tdef original():\n\t\treturn '原文'\n\n```\n- first\n- second"
        with host.invocation_scope(host.plugin_id, source):
            if business == "success":
                result = await host.agent.resume(identity, text, request_id="original-answer")
                assert result.ok and result.data["work_id"] == identity
            else:
                with pytest.raises(type(original)) as raised:
                    await host.agent.resume(identity, text, request_id="original-answer")
                assert raised.value is original
                assert any("lease release failed" in note for note in original.__notes__)
        if business == "success":
            with host.invocation_scope(host.plugin_id, source):
                replay = await host.agent.resume(identity, text, request_id="original-answer")
                assert replay.ok
        async with database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(select(inputs).where(inputs.c.work_id == identity))
                ).mappings()
            )
        answers = [row for row in rows if row["source_key"].startswith("plugin-answer:")]
        assert len(answers) == (0 if business == "cancelled" else 1)
        if answers:
            assert json.loads(answers[0]["payload_json"])["text"] == (
                "[原插件补充资料；不改变执行权限]\n" + text
            )
        after = await WorkRepository(database).get(identity)
        assert (after["model_requests"], after["tool_calls"]) == (
            before["model_requests"],
            before["tool_calls"],
        )
        assert len(provider.requests) == 1
    finally:
        await host.close_host_resources()
