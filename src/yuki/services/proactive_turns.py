"""Yuki opens one private turn with a Person through the Person's proactive route.

A source (first: the watchdog) registers a ``pending`` turn; this entry resolves the
Person's already-set active route to its private Conversation and runs one ordinary
Main through ``generate_origin_turn`` as SELF, with the source's facts as this
activation's runtime material. The turn only reports: no Work control is bound and
the Main backend refuses every non-read call except ``send`` for this origin.

The turn row is the receipt. A failed attempt is retried once by the caller's
schedule; a turn lost with its process is settled from its Social receipts, and an
unknown delivery is never resent.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import PersonActiveRouteModel
from yuki.conversation.hydrate import conversation_for_binding
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.tool_actor import ToolActor
from yuki.identity.authority import person_is_superuser
from yuki.identity.person_identity import current_person_id
from yuki.identity.routing import RouteSendError
from yuki.persistence.unit_of_work import optional_session
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.proactive_schema import proactive_turns
from yuki.social.db_models import SocialOperationModel

if TYPE_CHECKING:
    from yuki.admin.config_service import RuntimeConfigService
    from yuki.conversation.scope_repository import ConversationScopeRepository
    from yuki.identity.routing import PresenceRouter
    from yuki.persistence.database import Database
    from yuki.services.concurrency import ConcurrencyManager
    from yuki.services.turn_coordinator import ConversationTurnCoordinator

logger = logging.getLogger(__name__)

#: A failed attempt is retried once; a second failure waits for the source's next batch.
MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class ProactiveSource:
    """What one source says and how its material is read and fixed at the start."""

    content: str
    # The runtime material still worth saying for this turn, or None (nothing left).
    material: Callable[[str], Awaitable[dict[str, Any] | None]]
    # Runs in the claim writer: fix what this turn covers (no reads of history).
    on_claim: Callable[[AsyncSession, str, dict[str, Any]], Awaitable[None]]


async def validate_proactive_turn(
    database: Database,
    turn_id: str,
    *,
    conversation_id: str,
    presence_id: str | None,
    session: AsyncSession | None = None,
) -> None:
    """The running turn still owns this scene and its recipient is still a superuser."""
    async with optional_session(database, session, write=False) as reader:
        row = (
            (await reader.execute(select(proactive_turns).where(proactive_turns.c.id == turn_id)))
            .mappings()
            .first()
        )
        if (
            row is None
            or row["state"] != "running"
            or row["conversation_id"] != conversation_id
            or row["presence_id"] != presence_id
        ):
            raise PermissionError("proactive_turn_changed")
        if not await person_is_superuser(reader, row["person_id"]):
            raise PermissionError("proactive_recipient_changed")


def turn_source_key(conversation_id: str, turn_id: str) -> str:
    """The Social receipt key of this turn's sends (``SocialContext.turn_id``)."""
    return f"{conversation_id}:proactive:{turn_id}"


class ProactivePrivateTurns:
    def __init__(
        self,
        database: Database,
        *,
        router: PresenceRouter,
        scopes: ConversationScopeRepository,
        turn_coordinator: ConversationTurnCoordinator,
        concurrency: ConcurrencyManager,
        runtime_config: RuntimeConfigService,
        generate: Callable[..., Awaitable[Any]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self._router = router
        self._scopes = scopes
        self._turns = turn_coordinator
        self._concurrency = concurrency
        self._config = runtime_config
        self._generate = generate
        self._clock = clock
        self._sources: dict[str, ProactiveSource] = {}

    def register(self, kind: str, source: ProactiveSource) -> None:
        self._sources[kind] = source

    @staticmethod
    async def enqueue(session: AsyncSession, person_id: str, kind: str, now: float) -> str:
        """Register a pending turn inside the caller's short writer."""
        identity = str(uuid.uuid4())
        await session.execute(
            proactive_turns.insert().values(
                id=identity,
                source_kind=kind,
                person_id=person_id,
                state="pending",
                attempts=0,
                created=now,
                updated=now,
            )
        )
        return identity

    async def recover(self) -> None:
        """Settle turns a previous process left running, from their Social receipts."""
        async with self.database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(
                        select(proactive_turns).where(proactive_turns.c.state == "running")
                    )
                ).mappings()
            )
        for row in rows:
            sent, claimed = await self._receipts(row["conversation_id"], row["id"])
            if sent:
                await self._settle(row["id"], "finished", "recovered_sent", None, expect="running")
            elif claimed:
                await self._settle(row["id"], "unknown", "delivery_unknown", None, expect="running")
            else:
                await self._settle(row["id"], "failed", "process_lost", None, expect="running")

    async def due(self, now: float, *, min_interval: float, retry_after: float) -> list[str]:
        """Pending or once-failed turns whose person is past the minimum interval."""
        async with self.database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(
                        select(
                            proactive_turns.c.id,
                            proactive_turns.c.person_id,
                            proactive_turns.c.state,
                            proactive_turns.c.attempts,
                            proactive_turns.c.updated,
                        )
                        .where(
                            proactive_turns.c.state.in_(("pending", "running", "failed")),
                            or_(
                                proactive_turns.c.state != "failed",
                                proactive_turns.c.attempts < MAX_ATTEMPTS,
                            ),
                        )
                        .order_by(proactive_turns.c.created, proactive_turns.c.id)
                    )
                ).mappings()
            )
            busy = {row["person_id"] for row in rows if row["state"] == "running"}
            chosen: list[str] = []
            for row in rows:
                if row["person_id"] in busy:
                    continue
                if row["state"] == "failed" and now - row["updated"] < retry_after:
                    continue
                if row["state"] == "running":
                    continue
                last = await reader.scalar(
                    select(func.max(proactive_turns.c.started)).where(
                        proactive_turns.c.person_id == row["person_id"]
                    )
                )
                if last is not None and now - last < min_interval:
                    continue
                busy.add(row["person_id"])
                chosen.append(row["id"])
        return chosen

    async def attempt(self, turn_id: str) -> str:
        """One attempt of one turn; returns the outcome label (for health and tests)."""
        async with self.database.read_session() as reader:
            row = (
                (
                    await reader.execute(
                        select(proactive_turns).where(proactive_turns.c.id == turn_id)
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["state"] not in {"pending", "failed"}:
                return "not_due"
            if not await person_is_superuser(reader, row["person_id"]):
                await self._settle(turn_id, "finished", "recipient_not_superuser", None)
                return "recipient_not_superuser"
            person_id = await current_person_id(reader, row["person_id"])
            route = await reader.get(PersonActiveRouteModel, person_id)
        # Only an already-set route: this entry never provisions one.
        if route is None:
            return await self._note(turn_id, "route_none")
        if route.paused:
            return await self._note(turn_id, "route_paused")
        try:
            resolved = await self._router.resolve_send_for_person(person_id)
        except RouteSendError as exc:
            return await self._note(turn_id, f"route_{exc.category}"[:128])
        async with self.database.read_session() as reader:
            conversation = await conversation_for_binding(
                reader, resolved.binding_id, presence_id=resolved.presence_id
            )
        if conversation is None:
            return await self._note(turn_id, "conversation_missing")
        identity = ConversationScope.private(
            resolved.sender_account_id,
            resolved.external_target_id,
            platform=resolved.platform,
            presence_id=resolved.presence_id,
            conversation_id=conversation.id,
        )
        state = await self._scopes.get(identity)
        if state is None or state.id != conversation.id:
            return await self._note(turn_id, "conversation_missing")
        source = self._sources[row["source_kind"]]
        material = await source.material(turn_id)
        if material is None:
            # Everything cleared before Yuki could say it: say nothing.
            await self._settle(turn_id, "finished", "cleared_before_start", None)
            return "cleared_before_start"
        key = state.runtime_scope_key or identity.key
        async with self._turns.background_turn(key, origin=TurnOrigin.PROACTIVE_PRIVATE) as token:
            if token is None:
                return await self._note(turn_id, "conversation_busy")
            async with self._concurrency.conversation(key):
                if not await self._claim(
                    turn_id, conversation.id, resolved.presence_id, source, material
                ):
                    return "not_due"
                snapshot = ConversationTurnSnapshot(
                    state.id,
                    key,
                    state.generation,
                    None,
                    token.version,
                    identity.key,
                    proactive_turn_id=turn_id,
                )
                actor = ToolActor(
                    user_id="",
                    bot_user_id=resolved.sender_account_id,
                    group_id=None,
                    origin=TurnOrigin.PROACTIVE_PRIVATE,
                    instruction=source.content,
                    execution_id=f"proactive:{turn_id}",
                    conversation_id=state.id,
                    presence_id=resolved.presence_id,
                    principal_kind="self",
                    platform=resolved.platform,
                    proactive_turn_id=turn_id,
                )
                presence_id = resolved.presence_id

                async def validate() -> None:
                    await validate_proactive_turn(
                        self.database,
                        turn_id,
                        conversation_id=state.id,
                        presence_id=presence_id,
                    )

                context = {
                    "origin": TurnOrigin.PROACTIVE_PRIVATE.value,
                    "proactive_turn_id": turn_id,
                }
                context.update(material)
                from yuki.services.agent_tools import ToolRuntime

                try:
                    result = await self._generate(
                        content=source.content,
                        identity=identity,
                        runtime=await self._config.snapshot(),
                        turn_token=token,
                        turn_snapshot=snapshot,
                        before_model_request=validate,
                        source_runtime=ToolRuntime(
                            inbound=None,
                            actor_context=actor,
                            actor_is_superuser=False,
                            conversation_key=key,
                            execution_id=actor.execution_id,
                            origin=TurnOrigin.PROACTIVE_PRIVATE,
                            conversation_id=state.id,
                            scope_type=ScopeType.PRIVATE,
                            external_target_id=resolved.external_target_id,
                            activation_context=lambda: context,
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "proactive_turn_failed turn=%s category=%s", turn_id, type(exc).__name__
                    )
                    return await self._fail(turn_id, conversation.id, type(exc).__name__)
        failure = result.outcome.failure if result.outcome is not None else None
        summary = {
            "model_requests": result.model_requests,
            "tool_calls": result.tool_calls_used,
            "messages_sent": result.messages_sent,
        }
        if failure is not None:
            return await self._fail(turn_id, conversation.id, failure.code, summary)
        await self._settle(turn_id, "finished", None, summary, expect="running")
        return "finished"

    async def _claim(
        self,
        turn_id: str,
        conversation_id: str,
        presence_id: str,
        source: ProactiveSource,
        material: dict[str, Any],
    ) -> bool:
        now = self._clock()
        async with self.database.immediate_session() as writer:
            claimed = await writer.execute(
                update(proactive_turns)
                .where(
                    proactive_turns.c.id == turn_id,
                    proactive_turns.c.state.in_(("pending", "failed")),
                )
                .values(
                    state="running",
                    reason=None,
                    conversation_id=conversation_id,
                    presence_id=presence_id,
                    attempts=proactive_turns.c.attempts + 1,
                    started=now,
                    updated=now,
                )
            )
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return False
            await source.on_claim(writer, turn_id, material)
        return True

    async def _fail(
        self,
        turn_id: str,
        conversation_id: str,
        code: str,
        summary: dict[str, Any] | None = None,
    ) -> str:
        sent, _ = await self._receipts(conversation_id, turn_id)
        if sent:
            # Already told: a later failure does not make it untold.
            await self._settle(turn_id, "finished", code, summary, expect="running")
            return "finished"
        await self._settle(turn_id, "failed", code, summary, expect="running")
        return "failed"

    async def _receipts(self, conversation_id: str | None, turn_id: str) -> tuple[bool, bool]:
        if conversation_id is None:
            return False, False
        async with self.database.read_session() as reader:
            statuses = set(
                await reader.scalars(
                    select(SocialOperationModel.status).where(
                        SocialOperationModel.source_turn_id
                        == turn_source_key(conversation_id, turn_id),
                        SocialOperationModel.action == "send",
                    )
                )
            )
        return "succeeded" in statuses, bool(statuses - {"failed"})

    async def _note(self, turn_id: str, reason: str) -> str:
        """Leave the turn pending with why it could not start; write only on change."""
        async with self.database.read_session() as reader:
            current = await reader.scalar(
                select(proactive_turns.c.reason).where(proactive_turns.c.id == turn_id)
            )
        if current != reason:
            async with self.database.immediate_session() as writer:
                await writer.execute(
                    update(proactive_turns)
                    .where(
                        proactive_turns.c.id == turn_id,
                        proactive_turns.c.state.in_(("pending", "failed")),
                    )
                    .values(reason=reason, updated=self._clock())
                )
        return reason

    async def _settle(
        self,
        turn_id: str,
        state: str,
        reason: str | None,
        summary: dict[str, Any] | None,
        *,
        expect: str | None = None,
    ) -> None:
        async with self.database.immediate_session() as writer:
            query = update(proactive_turns).where(proactive_turns.c.id == turn_id)
            if expect is not None:
                query = query.where(proactive_turns.c.state == expect)
            await writer.execute(
                query.values(
                    state=state,
                    reason=reason[:128] if reason else None,
                    result_json=json.dumps(summary, sort_keys=True) if summary else None,
                    updated=self._clock(),
                )
            )

    async def health(self) -> dict[str, Any]:
        async with self.database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(
                        select(
                            proactive_turns.c.state,
                            proactive_turns.c.reason,
                            func.count(),
                        )
                        .where(
                            proactive_turns.c.state.in_(("pending", "running", "failed")),
                            or_(
                                proactive_turns.c.state != "failed",
                                proactive_turns.c.attempts < MAX_ATTEMPTS,
                            ),
                        )
                        .group_by(proactive_turns.c.state, proactive_turns.c.reason)
                    )
                ).all()
            )
            last = (
                (
                    await reader.execute(
                        select(proactive_turns.c.state, proactive_turns.c.reason)
                        .where(proactive_turns.c.started.is_not(None))
                        .order_by(proactive_turns.c.started.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        return {
            "open_turns": [
                {"state": state, "reason": reason, "count": count} for state, reason, count in rows
            ],
            "last_turn": dict(last) if last is not None else None,
        }
