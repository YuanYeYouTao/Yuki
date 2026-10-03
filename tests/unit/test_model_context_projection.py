"""Known prompt fields shrink while original records and frozen history stay intact."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from tests.conftest import make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.services import prompt_composer as composer_module
from qq_ai_bot.services.context_assembler import AssembledContext, ContextAssembler, ContextMetrics
from qq_ai_bot.services.model_context_projection import (
    project_people_and_scene,
    project_recent_delivery,
    project_short_state,
)
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.time.models import TimeContext
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


def _metadata():
    raw = {
        "current_person": {
            "user_id": "person-1",
            "nickname": "Winter",
            "display_name": "Winter",
            "aliases": ["Winter", "W", "W", ""],
            "facts": [],
        },
        "scene": {"type": "group", "group_id": "group-1", "group_card": "Winter"},
        "current_group": {"group_id": "group-1", "facts": []},
        "current_person_in_group": {
            "user_id": "person-1",
            "group_id": "group-1",
            "facts": [],
        },
        "event_bound_memory_refs": [{"event_id": 11, "person_id": "person-1"}],
    }
    # Exercise the actual production assembler's item IDs and aggregation.
    metadata, _ = ContextAssembler._render_metadata_selection(
        ContextAssembler._context_contributions(raw)
    )
    return metadata


def test_current_identity_and_alias_duplicates_are_only_prompt_projection():
    metadata = _metadata()
    original = copy.deepcopy(metadata)
    projected = project_people_and_scene(metadata)
    items = {item["id"]: item["data"] for item in projected["items"]}
    assert metadata == original
    assert items == {
        "current_person": {"user_id": "person-1", "display_name": "Winter"},
        "current_alias.1": "W",
        "scene": {"type": "group", "group_id": "group-1"},
        "event_bound_memory_refs": [{"event_id": 11, "person_id": "person-1"}],
    }


def test_facts_different_names_and_group_membership_are_not_lost():
    fact = {"fact_id": 7, "value": None, "evidence": [], "verified": False, "score": 0}
    metadata = {
        "items": [
            {
                "id": "current_person",
                "data": {"user_id": "p", "nickname": "A", "display_name": "B"},
            },
            {"id": "scene", "data": {"type": "group", "group_id": "g", "group_card": "C"}},
            {"id": "current_group", "data": {"group_id": "g", "facts": [fact]}},
            {"id": "current_person_in_group", "data": {"user_id": "other", "group_id": "other-g"}},
            {
                "id": "referenced_person.0",
                "data": {
                    "user_id": "r",
                    "group_id": "g",
                    "person_facts": [],
                    "group_facts": [fact],
                },
            },
            {"id": "plugin_business", "data": {"empty": [], "null": None, "value": False}},
        ]
    }
    original = copy.deepcopy(metadata)
    items = {item["id"]: item["data"] for item in project_people_and_scene(metadata)["items"]}
    assert metadata == original
    assert items["current_person"] == metadata["items"][0]["data"]
    assert items["scene"]["group_card"] == "C"
    assert items["current_group"] == {"facts": [fact]}
    assert items["current_person_in_group"] == {"user_id": "other", "group_id": "other-g"}
    assert items["referenced_person.0"] == {"user_id": "r", "group_id": "g", "group_facts": [fact]}
    assert items["plugin_business"] == metadata["items"][-1]["data"]


def test_actorless_private_target_remains_a_target_not_a_current_speaker():
    metadata = {
        "items": [
            {
                "id": "scene",
                "data": {
                    "type": "private",
                    "group_id": None,
                    "trigger": "external_event",
                    "current_actor": None,
                },
            },
            {
                "id": "conversation_target_person",
                "data": {
                    "user_id": "p",
                    "nickname": "A",
                    "display_name": "A",
                    "not_current_speaker": True,
                },
            },
            {"id": "conversation_target_alias.0", "data": "A"},
            {"id": "recent_external_events", "data": {"events": [], "content_trust": "untrusted"}},
        ]
    }
    items = {item["id"]: item["data"] for item in project_people_and_scene(metadata)["items"]}
    assert items["scene"] == {"type": "private", "trigger": "external_event", "current_actor": None}
    assert items["conversation_target_person"] == {
        "user_id": "p",
        "display_name": "A",
        "not_current_speaker": True,
    }
    assert "conversation_target_alias.0" not in items
    assert items["recent_external_events"] == metadata["items"][-1]["data"]


def test_unknown_metadata_shape_has_no_generic_empty_filter():
    metadata = {"schema": {"required": [], "default": None, "additionalProperties": False}}
    assert project_people_and_scene(metadata) == metadata


def test_recent_delivery_retains_transport_identity_and_false_has_text():
    rows = (
        {"platform_message_id": "receipt-1", "sent_at": "now", "has_text": True, "media_kinds": []},
        {
            "platform_message_id": "receipt-2",
            "sent_at": "now",
            "has_text": False,
            "media_kinds": ["image"],
        },
    )
    original = copy.deepcopy(rows)
    projected = project_recent_delivery(rows)
    assert rows == original
    assert "media_kinds" not in projected[0]
    assert projected[1] == rows[1]


def test_empty_short_state_cas_can_update_without_resurrecting_a_stale_writer(tmp_path):
    state = ShortState(WorkspaceStore(tmp_path / "state"))
    assert state.update({"slot": 1, "text": "remember", "expected_revision": 0})["ok"]
    assert state.update({"slot": 1, "text": "", "expected_revision": 1})["ok"]
    assert state.update({"slot": 2, "text": "still live", "expected_revision": 0})["ok"]
    snapshot = state.snapshot()
    projected = project_short_state(snapshot)
    assert projected[0] == {"slot": 1, "revision": 2}
    assert projected[1] == snapshot[1]
    assert state.snapshot() == snapshot
    assert not state.update({"slot": 1, "text": "stale", "expected_revision": 1})["ok"]
    assert state.update({"slot": 1, "text": "new", "expected_revision": projected[0]["revision"]})[
        "ok"
    ]


def test_compiled_request_shrinks_and_snapshot_matches_actual_projection(monkeypatch):
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    composer = PromptComposer(settings)
    now = datetime(2026, 10, 3, tzinfo=UTC)
    metadata = _metadata()
    delivery = (
        {"platform_message_id": "receipt", "sent_at": "now", "has_text": True, "media_kinds": []},
    )
    context = AssembledContext(
        metadata_payload=metadata,
        history_messages=(ChatMessage("user", "old frozen event"),),
        current_message=ChatMessage("user", "current event"),
        recent_delivery=delivery,
        current_time=TimeContext(now, now, "UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 0, 1, 13, False),
        visible_event_ids=frozenset({11}),
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(window_tokens=96_000),
        plugins=SimpleNamespace(max_total_prompt_characters=8_000),
        speech=SimpleNamespace(enabled=False),
    )
    short_state = [
        {"slot": slot, "text": "", "revision": slot, "expires_at": 1770000000} for slot in (1, 2, 3)
    ]

    def compose(selected_context=context):
        return composer.compose(
            inbound=None,
            context=selected_context,
            runtime=runtime,
            visual_observation=None,
            visual_failure=False,
            scope_type=ScopeType.GROUP,
            short_state=short_state,
        )

    with monkeypatch.context() as patch:
        patch.setattr(composer_module, "project_short_state", lambda rows: rows)
        patch.setattr(composer_module, "project_recent_delivery", lambda rows: list(rows))
        patch.setattr(composer_module, "project_people_and_scene", lambda data: data)
        before = compose()
    after = compose()
    assert after.messages[:-1] == before.messages[:-1]
    assert len(after.messages[-1].content) < len(before.messages[-1].content)
    envelopes, _ = json.JSONDecoder().raw_decode(after.messages[-1].content.split("：", 1)[1])
    data = {item["id"]: item for item in envelopes}
    assert data["runtime.short_state"]["data"] == [
        {"slot": slot, "revision": slot} for slot in (1, 2, 3)
    ]
    assert data["runtime.short_state"]["trust"] == "untrusted"
    assert data["runtime.recent_delivery"]["trust"] == "trusted"
    assert data["context.people_and_scene"]["trust"] == "untrusted"
    assert (
        after.current_snapshot["contributions"]["context.people_and_scene"]["payload"]
        == data["context.people_and_scene"]["data"]
    )
    # New projection does not rewrite an old compiled dynamic envelope in history.
    next_turn = compose(
        replace(context, history_messages=(*context.history_messages, before.messages[-1]))
    )
    assert next_turn.messages[:-1] == before.messages
    assert metadata == _metadata()
