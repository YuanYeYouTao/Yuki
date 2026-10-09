"""Offline reconciliation of legacy frozen Work delivery plans.

Support window: only the v3.8.3 Main Agent (alembic heads 0059–0061) wrote a
frozen final ``delivery_plan`` into the Work journal; v3.8.4 (head 0072) wrapped
but never sent through it and the current runtime has no writer. A database
restored from a v3.8.3/v3.8.4 backup and upgraded to head may still hold such
plans. The online runtime never executes them: a restored ``delivery`` journal
pauses its Work with ``legacy_delivery_not_resumed``.

``qq-ai-bot-cli work import-legacy-deliveries`` runs this module on a stopped
copy. It never calls a gateway, never resends and never re-reserves budget:

* accepted shards keep their original receipts; their missing outbound ledger
  rows are appended from the stored text (idempotent, CAS on receipt bytes);
* shards that were never claimed are recorded as definite not-sent facts on the
  original ``final-N`` keys and the Work stays paused for an operator;
* a plan whose final intent is ``dispatching``/``unknown``, any prepared/unknown
  shard, or a partial plan without its reservation is left as is;
* a fully delivered plan completes its Work.

Only the frozen plan/hash shapes below are accepted; anything else fails closed.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from qq_ai_bot.domain.messages import AttachmentKind
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_repository import WorkConflict, bounded_json
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work

LEGACY_DELIVERY_PAUSE = "legacy_delivery_not_resumed"


def frozen_plan_hash(values: list[dict[str, Any]]) -> str:
    # asdict originally retained tuple media and StrEnum kinds. JSON changes
    # those two representations; restore them without removing/reordering fields.
    original = deepcopy(values)
    for value in original:
        value["media"] = tuple(
            {**media, "kind": AttachmentKind(media["kind"])} for media in value["media"]
        )
    return hashlib.sha256(repr(original).encode()).hexdigest()


def _valid_plan(values: Any) -> bool:
    return (
        isinstance(values, list)
        and bool(values)
        and all(
            isinstance(value, dict)
            and isinstance(value.get("text"), str)
            and isinstance(value.get("media"), list)
            and all(isinstance(media, dict) and "kind" in media for media in value["media"])
            for value in values
        )
    )


@dataclass
class ImportReport:
    plans: int = 0
    completed: int = 0
    paused: int = 0
    not_sent_recorded: int = 0
    ledger_repaired: int = 0
    unresolved: int = 0
    invalid: int = 0
    work_ids: list[str] = field(default_factory=list)

    def as_counts(self) -> dict[str, int]:
        return {key: value for key, value in vars(self).items() if isinstance(value, int)}


async def import_legacy_deliveries(
    database: Database, ledger: Any, *, dry_run: bool = False, page: int = 64
) -> ImportReport:
    """Page through journals by stable work ID; each write rechecks its own read."""
    report = ImportReport()
    cursor = ""
    while True:
        async with database.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(journal.c.work_id, journal.c.chain_id, journal.c.payload_json)
                        .join(work, work.c.id == journal.c.work_id)
                        .where(
                            journal.c.work_id > cursor,
                            journal.c.phase.in_(("delivery", "delivered")),
                            work.c.state.not_in(("completed", "failed", "cancelled")),
                        )
                        .order_by(journal.c.work_id)
                        .limit(page)
                    )
                )
                .mappings()
                .all()
            )
        if not rows:
            return report
        cursor = rows[-1]["work_id"]
        for row in rows:
            await _import_one(database, ledger, dict(row), report, dry_run=dry_run)


async def _import_one(
    database: Database,
    ledger: Any,
    row: dict[str, Any],
    report: ImportReport,
    *,
    dry_run: bool,
) -> None:
    from qq_ai_bot.runtime.protocol_store import ProtocolStore

    work_id = row["work_id"]
    try:
        payload = await ProtocolStore(database).hydrate(json.loads(row["payload_json"]))
        metadata = payload["metadata"]
        values = metadata["progress"]["delivery_plan"]
        origin = metadata.get(
            "delivery_origin",
            {
                "work_id": work_id,
                "chain_id": row["chain_id"],
                "sequence": metadata.get("sequence", 0),
            },
        )
        chain, sequence = origin["chain_id"], origin["sequence"]
    except (KeyError, TypeError, ValueError, OSError):
        report.invalid += 1
        return
    if (
        not _valid_plan(values)
        or origin.get("work_id") != work_id
        or not isinstance(chain, str)
        or type(sequence) is not int
    ):
        report.invalid += 1
        return
    report.plans += 1
    report.work_ids.append(work_id)
    prefix = f"{chain}:{sequence}:"
    keys = [f"{prefix}final-{index}" for index in range(1, len(values) + 1)]
    async with database.sessions() as session:
        receipts = {
            item["effect_key"]: dict(item)
            for item in (
                await session.execute(select(effects).where(effects.c.effect_key.in_(keys)))
            ).mappings()
        }
        intent = (
            (
                await session.execute(
                    select(deliveries).where(deliveries.c.id == f"{prefix}final-plan")
                )
            )
            .mappings()
            .first()
        )
        current = dict(
            (await session.execute(select(work).where(work.c.id == work_id))).mappings().one()
        )
    if intent is not None:
        try:
            reserved = json.loads(intent["payload_json"])
        except ValueError:
            reserved = None
        if (
            intent["work_id"] != work_id
            or intent["kind"] != "final"
            or reserved != {"plan_hash": frozen_plan_hash(values)}
            or intent["message_count"] != len(values)
        ):
            report.invalid += 1
            return
        if intent["state"] in {"dispatching", "unknown"}:
            # The gateway may have accepted part of it; never decide for it.
            report.unresolved += 1
            return
    accepted = [
        receipts[key]
        for key in keys
        if key in receipts
        and receipts[key]["work_id"] == work_id
        and receipts[key]["state"] == "accepted"
        and json.loads(receipts[key]["receipt_json"]).get("transport_accepted") is True
    ]
    if len(accepted) != len(receipts):
        # A prepared/unknown/failed shard may have reached the gateway.
        report.unresolved += 1
        return
    unsent = [key for key in keys if key not in receipts]
    if unsent and (
        (intent is None and receipts) or (intent is not None and intent["state"] == "accepted")
    ):
        # Partial dispatch without a reservation, or an "accepted" plan with
        # missing shards, cannot be re-accounted; leave it for an operator.
        report.unresolved += 1
        return
    if not dry_run:
        report.ledger_repaired += await _repair_ledger(database, ledger, current, accepted)
        async with database.immediate_session() as session:
            changed = await session.execute(
                update(work)
                .where(work.c.id == work_id, work.c.revision == current["revision"])
                .values(
                    state="suspended" if unsent else "completed",
                    reason=LEGACY_DELIVERY_PAUSE if unsent else "legacy_delivery_imported",
                    revision=work.c.revision + 1,
                    updated=time.time(),
                )
            )
            if not changed.rowcount:  # type: ignore[attr-defined]
                raise WorkConflict("legacy_delivery_import_changed")
            for key in unsent:
                # Never claimed, never dispatched: a definite not-sent fact.
                await session.execute(
                    insert(effects)
                    .values(
                        effect_key=key,
                        work_id=work_id,
                        kind="final",
                        state="failed",
                        receipt_json=bounded_json(
                            {
                                "error": "delivery_not_dispatched",
                                "executed": False,
                                "mutation_committed": False,
                            }
                        ),
                        created=time.time(),
                        updated=time.time(),
                    )
                    .on_conflict_do_nothing(index_elements=[effects.c.effect_key])
                )
            # The plan is no longer executable; later restores read a paired record.
            await session.execute(
                update(journal).where(journal.c.work_id == work_id).values(phase="paired")
            )
    report.not_sent_recorded += len(unsent)
    if unsent:
        report.paused += 1
    else:
        report.completed += 1


async def _repair_ledger(
    database: Database, ledger: Any, current: dict[str, Any], rows: list[dict[str, Any]]
) -> int:
    """Append missing outbound ledger rows from stored receipts; never call a gateway."""
    source = json.loads(current["source_json"])
    original = await ledger.get_event(source.get("trigger_event_id"))
    if original is None:
        return 0
    repaired = 0
    for row in rows:
        receipt = json.loads(row["receipt_json"])
        if (
            receipt.get("ledger_recorded")
            or not receipt.get("message_id")
            or not isinstance(receipt.get("text"), str)
        ):
            continue
        if (
            await ledger.find_by_platform_message(
                bot_user_id=original.bot_user_id, platform_message_id=receipt["message_id"]
            )
            is None
        ):
            await ledger.append(
                bot_user_id=original.bot_user_id,
                platform_message_id=receipt["message_id"],
                scope_type=original.scope_type,
                sender_user_id=original.bot_user_id,
                direction="outbound",
                content=receipt["text"],
                group_id=original.group_id,
                private_peer_user_id=None if original.group_id else original.sender_user_id,
                sender_is_bot=True,
                origin="system_task",
                caused_by_event_id=original.id,
            )
        async with database.immediate_session() as session:
            # CAS on the original receipt bytes; a concurrent writer wins.
            await session.execute(
                update(effects)
                .where(
                    effects.c.effect_key == row["effect_key"],
                    effects.c.receipt_json == row["receipt_json"],
                )
                .values(receipt_json=bounded_json({**receipt, "ledger_recorded": True}))
            )
        repaired += 1
    return repaired
