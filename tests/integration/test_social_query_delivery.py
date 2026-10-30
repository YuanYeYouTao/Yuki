"""Real Main/Plugin calls keep discovery, validation and delivery contracts together.

Failure cases are specified before the product change: the retired view and mixed
selectors must be rejected before execution; missing/type/enum errors must stay
useful without echoing values; directory grants can be revoked between calls;
around must keep neighbours despite inapplicable filters; explicit Conversation
delivery must keep its original receipt rather than use a competing Binding.
"""

import json
from uuid import uuid4

import pytest
from tests.integration.test_plugin_agent_domain_grants import source_host
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.event_seed import seed_scoped_event
from tests.unit.test_history_dispatch_ownership import _tool

from yuki.domain.conversations import ConversationScope
from yuki.llm.fake import FakeLLMProvider
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki_plugin_sdk.permissions import PluginPermission


def request_evidence(request):
    return [
        {
            "role": message.role,
            "content": message.content,
            "tool_calls": [
                {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
                for call in message.tool_calls
            ],
        }
        for message in request.messages
    ]


@pytest.mark.parametrize("code_enabled", [False, True])
async def test_main_discovers_subject_and_conversation_reads_neighbours_and_sends(
    database, tmp_path, tool_delivery_evidence, code_enabled
):
    _, evidence = tool_delivery_evidence
    case = await setup_run(database, tmp_path, mode="silent")
    case.settings.code_mode_enabled = code_enabled
    case.chat.runtime.runner.main_contract = MainAgentContract(
        case.chat, ShortState(case.env.store), code_enabled=code_enabled
    )
    records = []
    original_events = []
    for index in range(3):
        added = await seed_scoped_event(
            case.env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            origin_key=f"query-contract-{index}",
            sender_user_id="10001",
            direction="inbound",
            content=f"original neighbour {index}",
        )
        records.append(added.event.id)
        original_events.append(
            {
                "event_id": added.event.id,
                "content": added.event.content,
                "conversation_id": added.event.canonical_conversation_id,
            }
        )
    replies = []
    evidence.update(
        code_enabled=code_enabled,
        source_event_ids=records,
        original_events=original_events,
        replies=replies,
    )
    calls = [
        ("find_contacts", {"kind": "person", "target": "PRIVATE_PARAMETER_VALUE"}),
        ("find_contacts", {}),
        ("find_contacts", {"kind": 7}),
        ("find_contacts", {"kind": "invalid"}),
        ("find_contacts", {"kind": "person", "view": "subjects"}),
        ("find_contacts", {"kind": "person", "subject_ref": "current_speaker"}),
        ("find_conversations", {"kind": "space"}),
        (
            "find_conversations",
            {"target": {"space_id": case.env.space}, "source": {"platform": "qq"}},
        ),
        (
            "query_chat_history",
            {
                "view": "around",
                "event_id": records[1],
                "before": 1,
                "after": 1,
                "keyword": "does-not-match",
                "start_at": "2100-01-01T00:00:00Z",
                "end_at": "2100-01-02T00:00:00Z",
                "order": "recent",
                "has": ["image"],
            },
        ),
        (
            "send",
            {
                "text": "discovery and original neighbours checked",
                "target": {
                    "kind": "space",
                    "target_id": case.env.space,
                    "conversation_id": case.env.context.conversation_id,
                    "binding_id": str(uuid4()),
                },
            },
        ),
    ]

    def respond(request):
        definitions = {tool.name: tool.parameters for tool in request.tools}
        assert evidence.setdefault("declarations", definitions) == definitions
        evidence.setdefault("requests", []).append(request_evidence(request))
        assert {"find_contacts", "find_conversations"} <= definitions.keys()
        for name in ("find_contacts", "find_conversations"):
            assert "view" not in definitions[name]["properties"]
            assert "oneOf" not in definitions[name]
        index = len(case.provider.requests) - 1
        if index:
            replies.append(json.loads(request.messages[-1].content))
        if index == len(calls):
            return "NO_REPLY"
        name, arguments = calls[index]
        return _tool(name, arguments, f"query-contract-{index}")

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    evidence["gateway_calls"] = case.env.bot.calls
    evidence["run"] = {"id": result.id, "status": str(result.status)}
    evidence["manifest_revision"] = case.chat.runtime.runner.main_contract.revision
    assert result.status.value == "succeeded"
    for index, field in (
        (0, "target"),
        (1, "kind"),
        (2, "kind"),
        (3, "kind"),
        (4, "view"),
        (6, "kind"),
    ):
        refused = replies[index]
        assert refused["error_code"] == "tool_input_validation_failed"
        assert refused["data"]["executed"] is False
        assert field in json.dumps(refused)
        assert "PRIVATE_PARAMETER_VALUE" not in json.dumps(refused)
    assert replies[5]["data"]["target_id"] == case.env.person
    directory = replies[7]["data"]
    assert case.env.context.conversation_id in {
        row["conversation_id"] for row in directory["items"]
    }
    around = replies[8]["data"]
    assert [row["event_id"] for row in around["events"]] == records
    assert around["order"] == "oldest"
    assert not around.get("start_at") and not around.get("end_at")
    assert replies[9]["data"]["status"] == "succeeded"
    assert [name for name, _ in case.env.bot.calls].count("send_group_msg") == 1
    if code_enabled:
        from yuki.codemode.tool_visibility import lookup_tools

        api = case.chat.runtime.runner.main_contract.script_api
        for name in ("find_contacts", "find_conversations"):
            found = json.loads(lookup_tools(api, json.dumps({"name": name})))
            assert found["data"]["direct"] is True
            assert found["data"]["parameters"] == evidence["declarations"][name]
            assert api.tool_for(found["data"]["script_name"]) == name
        evidence["script_names"] = {
            name: api.schemas[name] for name in ("find_contacts", "find_conversations")
        }


@pytest.mark.parametrize("name", ["find_contacts", "find_conversations"])
async def test_plugin_discovery_uses_current_history_grant_without_changing_declarations(
    database, tmp_path, tool_delivery_evidence, name
):
    _, evidence = tool_delivery_evidence
    provider = FakeLLMProvider()
    env, _, _, source, host = await source_host(database, tmp_path, provider, ())
    arguments = (
        {"kind": "space", "target_id": env.space}
        if name == "find_contacts"
        else {"target": {"space_id": env.space}}
    )
    replies = []
    evidence.update(tool=name, arguments=arguments, replies=replies)

    def respond(request):
        index = len(provider.requests) - 1
        evidence.setdefault("requests", []).append(request_evidence(request))
        evidence.setdefault("declarations", []).append([tool.name for tool in request.tools])
        assert {"find_contacts", "find_conversations"} <= {tool.name for tool in request.tools}
        if index:
            replies.append(json.loads(request.messages[-1].content))
        if index == 1:
            host._approved_permissions |= {PluginPermission.MESSAGE_HISTORY_READ}
        elif index == 2:
            host._approved_permissions = frozenset({PluginPermission.AGENT_RUN})
        elif index == 3:
            return _tool(
                "task_control", {"action": "complete", "result": "checked current grant"}, "finish"
            )
        return _tool(name, arguments, f"directory-grant-{index}")

    provider._responder = respond
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("Read the permitted original directory")
        evidence["work_result"] = result.data
        evidence["gateway_calls"] = env.bot.calls
        assert result.ok
        assert replies[0]["error_code"] == replies[2]["error_code"] == "permission_denied"
        assert replies[1]["ok"], replies
        assert all(tools == evidence["declarations"][0] for tools in evidence["declarations"])
        assert not any(action.startswith("send_") for action, _ in env.bot.calls)
    finally:
        await host.close_host_resources()
