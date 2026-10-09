"""Real DB/route host contracts with synthetic semantics, never a real Jev/QQ call."""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from yuki_participation.models import (
    CandidateKind,
    Choice,
    Observation,
)
from yuki_participation.rubric import CRITERIA, REVISION

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    SpaceActiveRouteModel,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.db_models import CanonicalSpaceModel, SpaceBindingModel
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.participation_snapshot import AsyncSnapshotStore
from qq_ai_bot.services.semantic_participation import SemanticParticipationService

pytestmark = pytest.mark.asyncio


def _choice(dimension, option):
    return Choice(
        choice=option,
        probabilities={key: float(key == option) for key in CRITERIA[dimension]},
    )


def _observation(snapshot, *, unknown=False, act="invite_yuki", unit=None):
    answers = {
        "interaction_mark": _choice("interaction_mark", "unknown" if unknown else act),
        "information_state": _choice("information_state", "unknown" if unknown else "refine"),
        "floor_state": _choice("floor_state", "unknown" if unknown else "yuki"),
        "boundary_scope": _choice("boundary_scope", "unknown" if unknown else "target_thread"),
    }
    if snapshot.kind != CandidateKind.CONVERSATION:
        answers["seed_fit"] = _choice("seed_fit", "unknown" if unknown else "appropriate")
    resolved = None
    if snapshot.focus.unit_ambiguous:
        selected = "unknown" if unknown else unit or snapshot.focus.unit_options[0].key
        answers["unit_selection"] = Choice(
            choice=selected,
            probabilities={
                **{
                    option.key: float(option.key == selected)
                    for option in snapshot.focus.unit_options
                },
                "unknown": float(selected == "unknown"),
            },
        )
        resolved = next((o for o in snapshot.focus.unit_options if o.key == selected), None)
    return Observation(
        observation_id=f"fixture:{snapshot.focus.ref.event_id}:{snapshot.sequence}",
        snapshot=snapshot,
        provider="synthetic_fixture",
        model_revision="test",
        rubric_revision=REVISION,
        received_at=max(time.time(), snapshot.issued_at),
        answers=answers,
        resolved_unit=resolved,
    )


class Observer:
    def __init__(self, *, unknown=False):
        self.unknown = unknown
        self.calls = []
        self.aclose = AsyncMock()

    async def evaluate(self, snapshot):
        self.calls.append(snapshot)
        return _observation(snapshot, unknown=self.unknown)


async def _event_and_route(database, ledger, *, group="2001", content="Yuki也来说说吧"):
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=str(uuid4()),
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        group_id=group,
        occurred_at=datetime.now(UTC) - timedelta(seconds=40),
    )
    async with database.immediate_session() as db:
        conv = await db.get(CanonicalConversationModel, event.canonical_conversation_id)
        space = await db.get(CanonicalSpaceModel, conv.space_id)
        space.enabled = space.autonomous_enabled = True
        binding = await db.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.space_id == conv.space_id,
                SpaceBindingModel.status == "active",
            )
        )
        route = await db.get(SpaceActiveRouteModel, conv.space_id)
        if route is None:
            db.add(
                SpaceActiveRouteModel(
                    space_id=conv.space_id,
                    space_binding_id=binding.id,
                    presence_id=event.ingress_presence_id,
                    route_generation=1,
                    paused=False,
                    revision=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    return event


def _message(event):
    return InboundMessage(
        message_id=event.platform_message_id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id=event.sender_user_id),
        text=event.content,
        bot_user_id=event.bot_user_id,
        group_id=event.group_id,
        conversation_id=event.canonical_conversation_id,
        presence_id=event.ingress_presence_id,
        source_event_id=event.id,
    )


async def _host(database, tmp_path, *, observer=True):
    policy = SimpleNamespace(autonomous_enabled=True, semantic_participation_enabled=True)
    app = SimpleNamespace(
        database=database,
        ledger=EventLedgerRepository(database),
        settings=SimpleNamespace(
            bot_aliases=("Yuki", "由纪"),
            semantic_participation_model_config_file=tmp_path / "autonomous-model.json",
        ),
        runtime_config=SimpleNamespace(
            snapshot=AsyncMock(
                return_value=SimpleNamespace(
                    conversation_policy=lambda: policy,
                )
            )
        ),
    )
    host = SemanticParticipationService(app)
    host._store = await AsyncSnapshotStore.open(tmp_path / f"participation-{uuid4()}.db")
    host._observer = Observer() if observer else None
    return host, policy


async def _item(host, event):
    scene = await host._scene(event.canonical_conversation_id)
    assert scene is not None, "fixture must use the real route resolver"
    item = await host._session(scene)
    await host._hydrate(item)
    return item
