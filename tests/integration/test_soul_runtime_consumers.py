"""SOUL edits affect new activations without mutating tools, history or paid inputs."""

from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.integration.test_self_reflection_checkpoint_recovery import reflection_case
from tests.integration.test_subagent_common_capabilities import _message_child
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.runtime_execution import make_child_executor
from tests.unit.test_dream_owner_projection import _scene as dream_scene
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from yuki.config import Settings
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel


def activate(settings: Settings, path, *, name, soul, revision):
    path.write_text(soul, encoding="utf-8")
    settings.soul_file = path
    settings.apply_soul_document(settings.read_soul_document())
    settings.apply_character_name(name, revision)


def prefix(request):
    return "\n".join(item.content or "" for item in request.messages if item.role == "system")


@pytest.mark.parametrize("soul", ["", "我的爱好是数学。", "我是{{name}}，喜欢数学。"])
async def test_main_rename_keeps_the_active_prefix_and_frozen_tool_contract(
    database, tmp_path, soul
):
    provider = FakeLLMProvider()
    _env, harness, chat, _state, message = await _scene(database, tmp_path, provider)
    settings = harness.settings
    path = tmp_path / "SOUL.md"
    activate(settings, path, name="初雪", soul=soul, revision=1)
    original = chat._prompt_composer.static_messages()
    observed = []

    def respond(request):
        observed.append(deepcopy(request))
        if len(observed) == 1:
            # A real dispatch has already captured its prefix. A role edit must
            # not alter the subsequent model/tool round inside that activation.
            activate(settings, path, name="Aurora", soul=soul, revision=2)
            return _tool("get_my_capabilities", {}, "original-read")
        return "内部结束"

    provider._responder = respond
    assert (await harness.processor.handle(message, MemorySender())).reason == "chat"
    assert len(observed) == 2
    assert all("当前角色的名称是 初雪。" in prefix(item) for item in observed)
    assert all("Aurora" not in prefix(item) for item in observed)
    assert observed[0].messages[0] == original[0]
    first_tools = deepcopy(observed[0].tools)
    manifest = chat.runtime.runner.main_contract
    original_revision = manifest.revision
    async with database.read_session() as reader:
        row = (await reader.scalars(select(PromptProjectionModel))).one()
        original_public = row.epoch_id
    assert (
        await harness.processor.handle(
            replace(
                message,
                ingress_key="after-rename",
                native_message_id="after-rename",
                text="继续处理",
            ),
            MemorySender(),
        )
    ).reason == "chat"
    assert "当前角色的名称是 Aurora。" in prefix(observed[-1])
    assert "当前角色的名称是 初雪。" not in prefix(observed[-1])
    assert observed[-1].tools == first_tools
    assert manifest.revision == original_revision
    assert provider.requests[0].messages == observed[0].messages
    async with database.read_session() as reader:
        assert (
            await reader.scalars(select(PromptProjectionModel))
        ).one().epoch_id == original_public
        events = tuple(await reader.scalars(select(ChatEventModel)))
    assert all("当前角色的名称是" not in row.content for row in events)
    assert "Aurora" not in "\n".join(
        item.content or "" for item in observed[-1].messages if item.role != "system"
    )


async def test_new_reflection_uses_the_same_identity_and_soul_as_main(database, tmp_path):
    service, _mutations, _repository, provider, batch, _source = await reflection_case(
        database, {"proposals": [], "episodes": []}
    )
    settings = service._settings
    activate(
        settings, tmp_path / "SOUL.md", name="小夏", soul="我是{{name}}，珍惜承诺。", revision=1
    )
    expected = settings.character_prompt
    assert await service.reflect(batch) == (0, 0)
    assert len(provider.requests) == 1
    assert expected in prefix(provider.requests[0])
    activate(settings, tmp_path / "SOUL.md", name="Aurora", soul="新的角色正文", revision=2)
    # The old batch is already complete; a role edit is no reason to buy it again.
    assert await service.reflect(batch) == (0, 0)
    assert len(provider.requests) == 1
    assert expected in prefix(provider.requests[0])


@pytest.mark.parametrize("owner", ["self_private", "person_group"])
async def test_dream_uses_soul_only_when_interpreting_own_memories(database, tmp_path, owner):
    provider = FakeLLMProvider(
        lambda _: '{"actions":[{"operation":"keep","source_refs":["memory_1","memory_2"]}]}'
    )
    service, _facts, _dreams, run, cluster, _rows = await dream_scene(
        database, provider, owner=owner
    )
    settings = service._settings
    activate(settings, tmp_path / "SOUL.md", name="小夏", soul="人格独有标记", revision=1)
    expected = settings.character_prompt
    await service.preview_cluster(run.public_id, cluster.id)
    assert len(provider.requests) == 1
    assert (expected in prefix(provider.requests[0])) == (owner == "self_private")


@pytest.mark.parametrize("principal", ["person", "self"])
async def test_new_scheduled_main_uses_the_shared_soul(database, tmp_path, principal):
    case = await setup_run(database, tmp_path, mode="silent", principal=principal)
    settings = case.chat._settings
    activate(settings, tmp_path / "SOUL.md", name="小夏", soul="共同人格正文", revision=1)
    case.clock.advance(2)
    await case.dispatch()
    assert len(case.provider.requests) == 1
    assert settings.character_prompt in prefix(case.provider.requests[0])
    assert "共同人格正文" in prefix(case.provider.requests[0])


async def test_child_uses_the_shared_character_source_at_its_own_activation(database, tmp_path):
    case, repository, _parent, child = await _message_child(database, tmp_path)
    settings = case.chat._settings
    activate(settings, tmp_path / "SOUL.md", name="小夏", soul="子任务同源人格", revision=1)
    case.provider._responder = lambda _: "子任务内部结果"
    executor = make_child_executor(
        repository,
        chat=case.chat,
        config=case.chat._runtime_config,
        runner=case.chat.runtime.runner,
        load_tools=case.chat.runtime.runner.main_contract.definitions,
        router=case.env.router,
    )
    assert await executor.run(child) is None
    assert len(case.provider.requests) == 1
    assert settings.character_prompt in prefix(case.provider.requests[0])
    assert (await repository.get(child))["state"] == "completed"


async def test_public_compact_adoption_keeps_the_original_activation_soul(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider(lambda _: "内部结束")
    _env, harness, chat, _state, message = await _scene(database, tmp_path, provider)
    settings = harness.settings
    path = tmp_path / "SOUL.md"
    activate(settings, path, name="初雪", soul="原人格{{name}}", revision=1)
    await harness.processor.handle(message, MemorySender())
    previous_count = len(provider.requests)
    run = chat._run_agent
    public_inputs = []

    async def low_trigger(key, messages, runtime, **kwargs):
        public_inputs.append(runtime.public_compaction)
        config = runtime.runtime_config
        return await run(
            key,
            messages,
            replace(
                runtime,
                runtime_config=replace(
                    config, context=replace(config.context, compact_trigger_tokens=1)
                ),
            ),
            **kwargs,
        )

    monkeypatch.setattr(chat, "_run_agent", low_trigger)
    compacted = []

    def respond(request):
        if "Host 会话上下文压缩要求" in (request.messages[-1].content or ""):
            compacted.append(deepcopy(request))
            activate(settings, path, name="Aurora", soul="新人格{{name}}", revision=2)
            return "原会话决定仍然有效。"
        return "内部结束"

    provider._responder = respond
    await harness.processor.handle(
        replace(
            message,
            ingress_key="compact-during-rename",
            native_message_id="compact-during-rename",
            text="继续原决定",
        ),
        MemorySender(),
    )
    assert compacted
    active = provider.requests[previous_count:]
    assert all("当前角色的名称是 初雪。" in prefix(request) for request in active)
    assert all(
        "新人格" not in prefix(request) and "Aurora" not in prefix(request) for request in active
    )
    assert any("原会话决定仍然有效。" in str(request.messages) for request in active)
    assert settings.character_identity.display_name == "Aurora"
    # The adopted public prefix is the real source of later compaction requests,
    # even when the immediate Main transcript still carries its original prefix.
    for context in public_inputs:
        assert "当前角色的名称是 初雪。" in context.messages[0].content
        assert "新人格" not in context.messages[0].content
