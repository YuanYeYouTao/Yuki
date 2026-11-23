"""Timed SELF wake (#541 P3): one more trigger for the same SELF initiative chain.

Only the opportunity is new. Admission writes the same ``autonomy_initiative_runs`` row,
the participation host's ``_dispatch`` creates the same ``self_initiative`` Work, and the
WorkScheduler runs it through the fixed Main tools, Social targets and receipts. The wake
does not belong to a group: it runs in the private Conversation of each superuser's set,
unpaused active route (the scene Yuki already uses to speak up on her own), so a ``send``
without target reaches that owner and any other target goes through the common routing.

The cadence is explicit (``conversation.self_wakeup_interval_seconds``, 0 = off) and is
gated by the existing SELF switches (``autonomous_enabled``, ``proactive_wakeup_enabled``),
never by group activity, ``auto_reply_enabled`` or ``semantic_participation_enabled``.
A slot is ``floor(now / interval)``; its proposal key is unique per Conversation, so the
same slot after a repeat or a restart returns the original run. A slot that passed while
the process was down is not caught up. An admitted run whose Work was never created is
dispatched by the host's outbox reconciliation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.memory_config import MemoryConfigScope

if TYPE_CHECKING:
    from yuki.services.semantic_participation import SemanticParticipationService

CONCERNS_PATH = "/workspace/notes/concerns.md"

TIMER_NOTE = (
    "定时自主唤醒：这是按设定节奏的唤醒，不属于任何群，也没有新的真人请求。"
    "现在在主人的私聊里，省略 target 的 send 发给主人；要找别人或发到群里，用 send 的 target。"
    f"你在意的事记在 {CONCERNS_PATH}，自己用文件工具维护，可以先看看。"
)

_KEYS = (
    "conversation.autonomous_enabled",
    "conversation.proactive_wakeup_enabled",
    "conversation.self_wakeup_interval_seconds",
)

# Superusers' set, unpaused routes and the private Conversation each route resolves to.
# Runs once per slot (and once after a restart), not per tick.
SCENES_SQL = (
    "SELECT c.id AS conversation_id, c.generation AS generation, "
    "r.presence_id AS presence_id "
    "FROM person_active_routes r "
    "JOIN persons p ON p.id = r.person_id AND p.enabled = 1 "
    "JOIN persons root ON root.id = coalesce(p.merged_into_person_id, p.id) "
    "AND root.enabled = 1 AND root.is_superuser = 1 "
    "JOIN canonical_conversations c ON c.kind = 'private' "
    "AND c.identity_binding_id = r.identity_binding_id "
    "AND c.entry_presence_id = r.presence_id AND coalesce(c.thread_id, '') = '' "
    "WHERE r.paused = 0"
)

# Scenes whose run for this slot already exists: a full uq_autonomy_proposal_run lookup.
ADMITTED_SQL = (
    "SELECT conversation_id FROM autonomy_initiative_runs "
    "WHERE (conversation_id, generation) IN ("
    "SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]') "
    "FROM json_each(:scenes)) "
    "AND owner = 'timer' AND controller_epoch = 0 AND proposal_id = :proposal"
)


async def _cadence(service: SemanticParticipationService, session: AsyncSession) -> float:
    """The current interval, or 0 when off or a SELF switch is off (global scope)."""
    master, wakeup, interval = await service.runtime_config.inspect_configs(
        _KEYS, scope=MemoryConfigScope(), session=session
    )
    if not (master.effective.value and wakeup.effective.value):
        return 0.0
    return float(interval.effective.value or 0)


async def timer_wakeup(service: SemanticParticipationService) -> None:
    async with service.database.read_session() as reader:
        interval = await _cadence(service, reader)
    if interval <= 0:
        return
    proposal = f"timer:{interval:g}:{int(service._clock() // interval)}"
    if service._timer_slot == proposal:
        return
    async with service.database.read_session() as reader:
        scenes = (await reader.execute(text(SCENES_SQL))).mappings().all()
        admitted = set(
            (
                await reader.execute(
                    text(ADMITTED_SQL),
                    {
                        "scenes": json.dumps(
                            [[row["conversation_id"], row["generation"]] for row in scenes]
                        ),
                        "proposal": proposal,
                    },
                )
            ).scalars()
        )

    async def allowed(session: AsyncSession) -> bool:
        return await _cadence(service, session) == interval

    for scene in scenes:
        if scene["conversation_id"] in admitted:
            continue
        result = await service.repository.accept_timer_wakeup(
            proposal_id=proposal,
            conversation_id=scene["conversation_id"],
            generation=scene["generation"],
            presence_id=scene["presence_id"],
            admission_allowed=allowed,
        )
        if result.outcome == "accepted" and result.run is not None:
            await service._dispatch(result.run)
    service._timer_slot = proposal
