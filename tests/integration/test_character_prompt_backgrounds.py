"""Hot edits reach actual plugin, reflection and Dream provider requests."""

from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI
from tests.integration.test_plugin_agent_domain_grants import source_host
from tests.integration.test_self_reflection_checkpoint_recovery import reflection_case
from tests.support.canonical_identity import ensure_person
from tests.support.client_identity import client_identity_service
from tests.unit.test_dream_owner_projection import _scene as dream_scene
from tests.unit.test_history_dispatch_ownership import _tool

from yuki.character.prompts import CharacterPrompts
from yuki.character.repository import CharacterRepository
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.config import read_prompt_documents
from yuki.llm.fake import FakeLLMProvider


@asynccontextmanager
async def editor(database, settings, root):
    system, persona = root / "system_prompt.md", root / "persona.md"
    system.write_text("OLD_SYSTEM {{YUKI_PERSONA_CORE}}", encoding="utf-8")
    persona.write_text("OLD_PERSONA", encoding="utf-8")
    previous_source = settings.resolved_bot_persona_file
    previous_bytes = previous_source.read_bytes()
    settings.system_prompt_file, settings.bot_persona_file = system, persona
    settings.apply_prompt_documents(
        read_prompt_documents(
            system, persona, system_prompt_template=settings.system_prompt_template
        )
    )
    assert settings.resolved_bot_persona_file == persona
    identity = client_identity_service(database)
    documents = CharacterPrompts(identity, settings)
    async with database.immediate_session() as writer:
        person = await ensure_person(writer, "background-prompt-owner")
    await CharacterRepository(identity).set_owner(str(uuid4()), person, str(uuid4()), granted=True)
    code = await identity.issue_for_operator(person, str(uuid4()), str(uuid4()))
    paired = await identity.pair(code["code"], "prompt activation test")
    headers = {"Authorization": "Bearer " + paired["token"]}
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(identity, documents=documents))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        before = await client.get("/client/v1/character/prompts", headers=headers)
        assert before.status_code == 200, before.text
        for document in before.json()["data"]["documents"]:
            content = (
                "NEW_SYSTEM {{YUKI_PERSONA_CORE}}"
                if document["key"] == "system_prompt"
                else "NEW_PERSONA"
            )
            saved = await client.put(
                f"/client/v1/character/prompts/{document['key']}",
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "expected_revision": document["revision"],
                    "content": content,
                },
            )
            assert saved.status_code == 200, saved.text
        current = await client.get("/client/v1/character/prompts", headers=headers)
        data = current.json()["data"]
        assert all(data["active"][item["key"]] == item["revision"] for item in data["documents"])
        try:
            yield client
        finally:
            if previous_source != persona:
                assert previous_source.read_bytes() == previous_bytes


async def test_plugin_main_activation_reads_hot_pair_at_provider(database, tmp_path):
    provider = FakeLLMProvider(
        lambda _: _tool("task_control", {"action": "complete", "result": "done"}, "done")
    )
    _, harness, _, source, host = await source_host(database, tmp_path, provider, ())
    try:
        async with editor(database, harness.settings, tmp_path):
            assert not provider.requests
            with host.invocation_scope(host.plugin_id, source):
                result = await host.agent.run("Use the current character prompt pair")
            assert result.ok and result.data["state"] == "completed", result
            assert len(provider.requests) == 1
            prefix = str(provider.requests[0].messages[0].content)
            assert "NEW_SYSTEM" in prefix and "NEW_PERSONA" in prefix
            assert "OLD_SYSTEM" not in prefix and "OLD_PERSONA" not in prefix
    finally:
        await host.close_host_resources()


async def test_new_self_reflection_batch_reads_hot_persona_at_provider(database, tmp_path):
    service, _, _, provider, batch, _ = await reflection_case(
        database, {"proposals": [], "episodes": []}
    )
    async with editor(database, service._settings, tmp_path):
        assert not provider.requests
        assert await service.reflect(batch) == (0, 0)
        assert len(provider.requests) == 1
        prefix = str(provider.requests[0].messages[0].content)
        assert "NEW_PERSONA" in prefix and "OLD_PERSONA" not in prefix


async def test_new_self_dream_batch_reads_hot_persona_at_provider(database, tmp_path):
    provider = FakeLLMProvider(
        lambda _: '{"actions":[{"operation":"keep","source_refs":["memory_1","memory_2"]}]}'
    )
    service, _, _, run, cluster, _ = await dream_scene(database, provider, owner="self_private")
    async with editor(database, service._settings, tmp_path):
        assert not provider.requests
        preview = await service.preview_cluster(run.public_id, cluster.id)
        assert preview.actions[0].source_refs == ("memory_1", "memory_2")
        assert len(provider.requests) == 1
        prefix = str(provider.requests[0].messages[0].content)
        assert "NEW_PERSONA" in prefix and "OLD_PERSONA" not in prefix
