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

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    PersonActiveRouteModel,
)
from yuki.conversation.hydrate import conversation_for_binding
from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.tool_actor import ToolActor
from yuki.identity.authority import person_is_superuser
from yuki.identity.person_identity import current_person_id, same_person_id
from yuki.identity.routing import RouteSendError
from yuki.persistence.unit_of_work import optional_session
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.proactive_schema import proactive_turns
from yuki.services.turn_transcript import dispatch_request
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
#: The Social receipt actions a ``send`` records (single, split parent, file caption).
_SEND_ACTIONS = ("send_message", "send_message_sequence", "send_file_caption")


@dataclass(frozen=True, slots=True)
class ProactiveSource:
    """What one source says and how its material is read and fixed at the start."""

    content: str
    # The runtime material still worth saying for this turn, or None (nothing left).
    material: Callable[[str], Awaitable[dict[str, Any] | None]]
    # Runs in the start writer: fix what this turn covers (None: nothing; no history reads).
    on_claim: Callable[[AsyncSession, str, dict[str, Any] | None], Awaitable[None]]
    # Model requests one attempt may pay; reaching it ends the turn without a retry.
    max_model_requests: int | None = None


class ProactiveRequestCapReached(RuntimeError):
    """The source's per-turn model request cap is spent; no further request is paid."""


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
        conversation = await reader.get(CanonicalConversationModel, conversation_id)
        if (
            conversation is None
            or conversation.person_id is None
            or not await same_person_id(reader, conversation.person_id, row["person_id"])
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

    async def recover(self, active: frozenset[str] = frozenset()) -> None:
        """Settle running turns no live attempt owns (a lost process or a lost attempt)."""
        async with self.database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(
                        select(proactive_turns).where(proactive_turns.c.state == "running")
                    )
                ).mappings()
            )
        for row in rows:
            if row["id"] in active:
                continue
            sent, claimed = await self._receipts(row["conversation_id"], row["id"])
            if sent:
                await self._settle(row["id"], "finished", "recovered_sent", None, expect="running")
            elif claimed:
                await self._settle(row["id"], "unknown", "delivery_unknown", None, expect="running")
            else:
                await self._settle(row["id"], "failed", "process_lost", None, expect="running")

    async def due(
        self, now: float, *, min_interval: float, retry_after: float, source_kind: str
    ) -> list[str]:
        """``source_kind``'s pending or once-failed turns whose person is free and past
        the minimum interval; a running turn and the last start of any source count."""
        async with self.database.read_session() as reader:
            rows = list(
                (
                    await reader.execute(
                        select(
                            proactive_turns.c.id,
                            proactive_turns.c.source_kind,
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
                if row["source_kind"] != source_kind or row["person_id"] in busy:
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
        # Only an already-set route, resolved without provisioning one (§9-3).
        if route is None:
            return await self._note(turn_id, "route_none")
        if route.paused:
            return await self._note(turn_id, "route_paused")
        try:
            resolved = await self._router.resolve_send_for_binding(
                person_id, route.identity_binding_id, preferred_presence_id=route.presence_id
            )
        except RouteSendError as exc:
            return await self._note(turn_id, f"route_{exc.category}"[:128])
        if resolved.presence_id != route.presence_id:
            # Only the route as set: another Presence of the Binding is not this route.
            return await self._note(turn_id, "route_presence_unavailable")
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
        key = state.runtime_scope_key or identity.key
        async with self._turns.background_turn(key, origin=TurnOrigin.PROACTIVE_PRIVATE) as token:
            if token is None:
                return await self._note(turn_id, "conversation_busy")
            async with self._concurrency.conversation(key):
                if not await self._claim(turn_id, conversation.id, resolved.presence_id):
                    return "not_due"
                # Claimed under the lock: the source attaches nothing more to it, so the
                # material read now is exactly what this turn covers.
                try:
                    material = await source.material(turn_id)
                    if material is None:
                        # Everything cleared before Yuki could say it: say nothing.
                        await self._start(turn_id, source, None)
                        return "cleared_before_start"
                    await self._start(turn_id, source, material)
                    result = await self._run(
                        turn_id, source, material, identity, state, key, token, resolved
                    )
                except ProactiveRequestCapReached:
                    return await self._capped(turn_id, conversation.id)
                except Exception as exc:
                    logger.warning(
                        "proactive_turn_failed turn=%s category=%s",
                        turn_id,
                        type(exc).__name__,
                        exc_info=exc,
                    )
                    return await self._fail(turn_id, conversation.id, type(exc).__name__)
        failure = result.outcome.failure if result.outcome is not None else None
        summary = {
            "model_requests": result.model_requests,
            "tool_calls": result.tool_calls_used,
            "messages_sent": result.messages_sent,
        }
        # Told only by a real delivery: a final text without send, or a refused send,
        # leaves the issue untold (retried once, then merged into the next batch).
        return await self._fail(
            turn_id,
            conversation.id,
            failure.code if failure is not None else "no_delivery",
            summary,
        )

    async def _run(
        self,
        turn_id: str,
        source: ProactiveSource,
        material: dict[str, Any],
        identity: ConversationScope,
        state: Any,
        key: str,
        token: Any,
        resolved: Any,
    ) -> Any:
        from yuki.services.agent_tools import ToolRuntime

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

        paid = 0

        async def validate() -> None:
            nonlocal paid
            await validate_proactive_turn(
                self.database, turn_id, conversation_id=state.id, presence_id=presence_id
            )
            if source.max_model_requests is None or dispatch_request() is None:
                return  # a tool call or preparation, not a model request
            if paid >= source.max_model_requests:
                raise ProactiveRequestCapReached(turn_id)
            paid += 1

        context = {"origin": TurnOrigin.PROACTIVE_PRIVATE.value, "proactive_turn_id": turn_id}
        context.update(material)
        return await self._generate(
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

    async def _claim(self, turn_id: str, conversation_id: str, presence_id: str) -> bool:
        """CAS pending/failed → running; a running turn takes no new source rows."""
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
                    updated=self._clock(),
                )
            )
            return bool(claimed.rowcount == 1)  # type: ignore[attr-defined]

    async def _start(
        self, turn_id: str, source: ProactiveSource, material: dict[str, Any] | None
    ) -> None:
        """Fix what the turn covers; only a real activation counts an attempt."""
        now = self._clock()
        async with self.database.immediate_session() as writer:
            await source.on_claim(writer, turn_id, material)
            if material is None:
                await writer.execute(
                    update(proactive_turns)
                    .where(proactive_turns.c.id == turn_id, proactive_turns.c.state == "running")
                    .values(state="finished", reason="cleared_before_start", updated=now)
                )
                return
            await writer.execute(
                update(proactive_turns)
                .where(proactive_turns.c.id == turn_id)
                .values(attempts=proactive_turns.c.attempts + 1, started=now, updated=now)
            )

    async def _fail(
        self,
        turn_id: str,
        conversation_id: str,
        code: str,
        summary: dict[str, Any] | None = None,
    ) -> str:
        """Settle a turn by its real Social receipts, whatever its run reported."""
        sent, claimed = await self._receipts(conversation_id, turn_id)
        if sent:
            # Told; a later failure does not make it untold.
            reason = None if code == "no_delivery" else code
            await self._settle(turn_id, "finished", reason, summary, expect="running")
            return "finished"
        if claimed:
            # A send was claimed with an unknown result: never resent.
            await self._settle(turn_id, "unknown", code, summary, expect="running")
            return "unknown"
        await self._settle(turn_id, "failed", code, summary, expect="running")
        return "failed"

    async def _capped(self, turn_id: str, conversation_id: str) -> str:
        """The cap ends this turn for good: what was sent stands, nothing is retried."""
        sent, claimed = await self._receipts(conversation_id, turn_id)
        state = "unknown" if claimed and not sent else "finished"
        await self._settle(turn_id, state, "model_request_cap", None, expect="running")
        return f"{state}:model_request_cap"

    async def _receipts(self, conversation_id: str | None, turn_id: str) -> tuple[bool, bool]:
        if conversation_id is None:
            return False, False
        async with self.database.read_session() as reader:
            statuses = set(
                await reader.scalars(
                    select(SocialOperationModel.status).where(
                        SocialOperationModel.source_turn_id
                        == turn_source_key(conversation_id, turn_id),
                        SocialOperationModel.action.in_(_SEND_ACTIONS),
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
