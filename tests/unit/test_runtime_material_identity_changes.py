"""Real Main and provider wire inputs retain only identity and plugin changes."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.unit.test_history_dispatch_ownership import _scene
from tests.unit.test_runtime_material_delta import _changes

from qq_ai_bot.conversation.observation_models import (
    ContextObservationModel,
    ContextSelectionModel,
)
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import ChatMessage, SenderIdentity
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.memory.enums import MemoryScopeType, MemorySourceType
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import SubjectResolver
from qq_ai_bot.plugin_host.extension_registry import ExtensionRegistry
from qq_ai_bot.plugin_host.prompt_adapter import PluginPromptAdapter
from yuki_plugin_sdk.models import PromptFragment, PromptStage
from yuki_plugin_sdk.permissions import PluginPermission


@pytest.fixture
async def wire_main(database, tmp_path):
    wire = []
    responses = []

    async def transport(request):
        wire.append(json.loads(request.content))
        if responses:
            return httpx.Response(200, json=responses.pop(0))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "内部处理，无须发送。"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
        )
        env, harness, chat, state, message = await _scene(database, tmp_path, provider)
        yield SimpleNamespace(
            env=env,
            harness=harness,
            chat=chat,
            state=state,
            message=message,
            wire=wire,
            responses=responses,
        )


def _current(body):
    return ChatMessage("user", body["messages"][-1]["content"])


def _people(material):
    return {item["id"]: item["data"] for item in material["context.people_and_scene"]["items"]}


async def _sources(database):
    async with database.sessions() as reader:
        return tuple(
            await reader.scalars(
                select(ContextObservationModel.id).order_by(ContextObservationModel.id)
            )
        )


async def _projection(database, actor_id):
    async with database.sessions() as reader:
        views = select(ContextSelectionModel.view_key).where(
            ContextSelectionModel.actor_id == actor_id
        )
        return (
            await reader.scalars(
                select(PromptProjectionModel).where(PromptProjectionModel.view_key.in_(views))
            )
        ).one()


async def _handle(scene, message, *, expected_requests=1):
    attempts = len(scene.wire)
    assert (await scene.harness.processor.handle(message, MemorySender())).reason == "chat"
    assert len(scene.wire) == attempts + expected_requests
    return scene.wire[attempts]


async def _assert_frozen_wire(database, actor_id, body):
    saved = await _projection(database, actor_id)
    assert json.loads(saved.payload_json)[-1]["message"]["content"] == _current(body).content
    return saved


async def test_name_changes_remain_real_events_without_publishing_material(database, wire_main):
    scene = wire_main
    message = replace(
        scene.message,
        sender=replace(
            scene.message.sender,
            nickname="caller-nickname-A",
            group_card="caller-group-card",
            nickname_known=True,
            group_card_known=True,
        ),
    )
    first = await _handle(scene, message)
    initial = _people(_changes(_current(first)))
    assert initial == {"scene": {"type": "group", "group_id": "20001"}}
    assert "caller-group-card|QQ:10001]" in _current(first).content
    saved = await _assert_frozen_wire(database, scene.env.person, first)
    original_items = json.loads(saved.payload_json)
    original_sources = await _sources(database)

    unchanged = await _handle(scene, replace(message, message_id="people-unchanged", text="继续"))
    assert _changes(_current(unchanged)) == {}
    assert await _sources(database) == original_sources
    # A new activation inserts its own Host initialization. The frozen public
    # current expression remains identical in the actual provider history.
    assert unchanged["messages"][:-1].count(first["messages"][-1]) == 1

    changed = replace(
        message,
        message_id="people-nickname-B",
        text="名字资料已更新，继续处理。",
        sender=replace(message.sender, nickname="caller-nickname-B"),
    )
    changed_wire = await _handle(scene, changed)
    assert _changes(_current(changed_wire)) == {}
    assert "caller-group-card|QQ:10001]" in _current(changed_wire).content
    assert (await scene.harness.profiles.get(user_id="10001", group_id="20001")).nickname == (
        "caller-nickname-B"
    )
    changed_sources = await _sources(database)
    assert changed_sources == original_sources
    saved = await _assert_frozen_wire(database, scene.env.person, changed_wire)
    assert json.loads(saved.payload_json)[: len(original_items)] == original_items
    assert _people(json.loads(saved.visible_material_json)) == initial

    removed = replace(
        changed,
        message_id="people-clear-nickname",
        text="群名片已清空，昵称仍有效。",
        sender=replace(changed.sender, group_card="", group_card_known=True),
    )
    removed_wire = await _handle(scene, removed)
    assert _changes(_current(removed_wire)) == {}
    assert "caller-nickname-B|QQ:10001]" in _current(removed_wire).content
    removed_sources = await _sources(database)
    assert removed_sources == original_sources
    saved = await _assert_frozen_wire(database, scene.env.person, removed_wire)
    assert _people(json.loads(saved.visible_material_json)) == initial

    still_removed = await _handle(
        scene, replace(removed, message_id="people-still-empty", text="继续没有昵称的场景。")
    )
    assert _changes(_current(still_removed)) == {}
    assert await _sources(database) == removed_sources
    assert still_removed["messages"][:-1].count(removed_wire["messages"][-1]) == 1
    await _assert_frozen_wire(database, scene.env.person, still_removed)


async def test_subject_switch_uses_its_own_material_and_source_owner(database, wire_main):
    scene = wire_main
    original = replace(
        scene.message,
        sender=replace(
            scene.message.sender,
            nickname="person-A-nickname",
            group_card="person-A-card",
            group_card_known=True,
        ),
    )
    await _handle(scene, original)
    first = await _projection(database, scene.env.person)
    first_material = first.visible_material_json
    async with database.sessions() as writer, writer.begin():
        second_person = await ensure_person(writer, "10002")
    second = replace(
        original,
        message_id="second-real-person",
        person_id=second_person,
        sender=SenderIdentity(
            "10002",
            nickname="",
            group_card="person-B-card",
            nickname_known=True,
            group_card_known=True,
        ),
        text="另一个真实用户提出自己的问题。",
    )
    second_wire = await _handle(scene, second)
    second_people = _people(_changes(_current(second_wire)))
    assert second_people == {"scene": {"type": "group", "group_id": "20001"}}
    assert "person-B-card|QQ:10002]" in _current(second_wire).content
    second_saved = await _assert_frozen_wire(database, second_person, second_wire)
    assert second_saved.view_key != first.view_key
    assert _people(json.loads(second_saved.visible_material_json)) == second_people
    assert (await _projection(database, scene.env.person)).visible_material_json == first_material
    async with database.sessions() as reader:
        event_id = json.loads(second_saved.payload_json)[-1]["event_ids"][0]
        source = (
            await reader.scalars(
                select(ContextObservationModel).where(
                    ContextObservationModel.source_event_id == event_id
                )
            )
        ).one()
        assert source.actor_id == second_person
        assert (
            _people(
                {
                    "context.people_and_scene": json.loads(source.payload_json)["contributions"][
                        "context.people_and_scene"
                    ]["payload"]
                }
            )
            == second_people
        )

    sources = await _sources(database)
    unchanged_second = await _handle(
        scene, replace(second, message_id="same-second-person", text="同一人继续自己的问题。")
    )
    assert _changes(_current(unchanged_second)) == {}
    assert await _sources(database) == sources
    assert _people(
        json.loads((await _projection(database, second_person)).visible_material_json)
    ) == (second_people)

    facts = MemoryFactService(MemoryFactRepository(database))
    for account in ("10001", "10002"):
        await facts.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=account,
                memory_key="hobby:astronomy",
                category="hobby",
                content=f"astronomy belongs to {account}",
                source_type=MemorySourceType.EXPLICIT,
            )
        )
    scene.responses.append(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "current-speaker-memory",
                                "type": "function",
                                "function": {
                                    "name": "search_memory",
                                    "arguments": json.dumps(
                                        {
                                            "query": "astronomy",
                                            "target": {
                                                "scope": "person",
                                                "subject_ref": "current_speaker",
                                            },
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                }
            ]
        }
    )
    returning = replace(
        original,
        message_id="first-person-returns",
        text="[提及10002] 第一位用户继续原问题。",
        mentioned_user_ids=("10002",),
        reply_to_event_id=event_id,
        reply_to_message_id=second.message_id,
        reply_sender_user_id="10002",
    )
    restored_first = await _handle(scene, returning, expected_requests=2)
    current = _current(restored_first).content
    assert "person-A-card|QQ:10001]" in current
    assert "[提及person-B-card/QQ:10002]" in current
    assert f"回复:#{event_id}/person-B-card/QQ:10002" in current
    memory_result = json.loads(scene.wire[-1]["messages"][-1]["content"])
    assert memory_result["ok"], memory_result
    assert [item["content"] for item in memory_result["data"]["memories"]] == [
        "astronomy belongs to 10001"
    ]
    latest = await scene.harness.ledger.get_event(
        json.loads((await _projection(database, scene.env.person)).payload_json)[-1]["event_ids"][0]
    )
    subjects = SubjectResolver.context(latest)
    assert subjects.resolve("speaker:person").subject_user_id == "10001"
    assert subjects.resolve("reply_author:person_group").subject_user_id == "10002"
    assert _changes(_current(restored_first)) == {}
    assert await _sources(database) == sources
    assert (await _projection(database, scene.env.person)).visible_material_json == first_material
    await _assert_frozen_wire(database, scene.env.person, restored_first)


async def test_registered_plugin_fragment_change_exit_and_return_a_are_real_deltas(
    database, wire_main
):
    scene = wire_main
    extensions = ExtensionRegistry()
    adapter = PluginPromptAdapter(extensions, scene.chat._prompt_composer._registry)

    def register(plugin_id, content):
        extensions.registrar(
            plugin_id, (PluginPermission.PROMPT_CONTEXT_REGISTER,)
        ).register_prompt_fragment(
            PromptFragment(id="context", stage=PromptStage.PLUGIN_CONTEXT, content=content)
        )
        assert adapter.activate(plugin_id) == 1

    changing_plugin = "com.example.changing"
    stable_plugin = "com.example.stable"
    changing_id = f"plugin.{changing_plugin}.context"
    stable_id = f"plugin.{stable_plugin}.context"
    content_a = "插件的实际资料 A。"
    content_b = "插件的实际资料 B。"
    register(changing_plugin, content_a)
    register(stable_plugin, "另一个插件保持不变。")
    first = await _handle(scene, scene.message)
    assert _changes(_current(first))["context.plugins"] == [
        {"fragment_id": changing_id, "content": content_a, "plugin_id": changing_plugin},
        {
            "fragment_id": stable_id,
            "content": "另一个插件保持不变。",
            "plugin_id": stable_plugin,
        },
    ]
    saved = await _assert_frozen_wire(database, scene.env.person, first)
    original_items = json.loads(saved.payload_json)
    first_sources = await _sources(database)

    unchanged = await _handle(
        scene, replace(scene.message, message_id="plugin-A-unchanged", text="插件资料未变。")
    )
    assert _changes(_current(unchanged)) == {}
    assert await _sources(database) == first_sources
    assert unchanged["messages"][:-1].count(first["messages"][-1]) == 1

    assert extensions.remove_plugin(changing_plugin) == 1
    register(changing_plugin, content_b)
    changed = await _handle(
        scene, replace(scene.message, message_id="plugin-B", text="采用插件的新资料。")
    )
    assert _changes(_current(changed)) == {
        "context.plugins": [
            {"fragment_id": changing_id, "content": content_b, "plugin_id": changing_plugin}
        ]
    }
    changed_sources = await _sources(database)
    assert len(changed_sources) == len(first_sources) + 1
    saved = await _assert_frozen_wire(database, scene.env.person, changed)
    assert json.loads(saved.payload_json)[: len(original_items)] == original_items

    assert adapter.deactivate(changing_plugin) == 1
    assert extensions.remove_plugin(changing_plugin) == 1
    exited = await _handle(
        scene, replace(scene.message, message_id="plugin-exits", text="这个插件已退出。")
    )
    assert _changes(_current(exited)) == {
        "context.plugins": [{"fragment_id": changing_id, "content": None}]
    }
    exited_sources = await _sources(database)
    assert len(exited_sources) == len(changed_sources) + 1
    saved = await _assert_frozen_wire(database, scene.env.person, exited)
    assert json.loads(saved.visible_material_json)["context.plugins"] == [
        {"fragment_id": stable_id, "content": "另一个插件保持不变。", "plugin_id": stable_plugin}
    ]

    still_exited = await _handle(
        scene, replace(scene.message, message_id="plugin-still-exited", text="继续当前问题。")
    )
    assert _changes(_current(still_exited)) == {}
    assert await _sources(database) == exited_sources
    assert still_exited["messages"][:-1].count(exited["messages"][-1]) == 1

    register(changing_plugin, content_a)
    returned = await _handle(
        scene, replace(scene.message, message_id="plugin-A-returns", text="插件恢复了原资料。")
    )
    assert _changes(_current(returned)) == {
        "context.plugins": [
            {"fragment_id": changing_id, "content": content_a, "plugin_id": changing_plugin}
        ]
    }
    returned_sources = await _sources(database)
    assert len(returned_sources) == len(exited_sources) + 1
    saved = await _assert_frozen_wire(database, scene.env.person, returned)
    assert {
        item["fragment_id"]: item["content"]
        for item in json.loads(saved.visible_material_json)["context.plugins"]
    } == {changing_id: content_a, stable_id: "另一个插件保持不变。"}

    unchanged_return = await _handle(
        scene, replace(scene.message, message_id="plugin-restored-unchanged", text="再继续。")
    )
    assert _changes(_current(unchanged_return)) == {}
    assert await _sources(database) == returned_sources
    await _assert_frozen_wire(database, scene.env.person, unchanged_return)
