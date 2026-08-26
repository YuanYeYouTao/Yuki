"""Stopped/offline recount of prompt-visible uncovered character watermarks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from qq_ai_bot.config import Settings
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.rollup.errors import ConversationRollupError
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.origins import parse_rollup_llm_origins
from qq_ai_bot.conversation.rollup.repository import (
    calculate_canonical_uncovered,
    recount_canonical_uncovered,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.schema_guard import CanonicalSchemaError, require_canonical_schema

_REQUIRED_WATERMARK_COLUMNS = frozenset(
    {
        "uncovered_event_count",
        "uncovered_character_count",
        "covered_through_event_id",
        "starts_after_event_id",
        "last_event_id",
        "generation",
    }
)

_ERROR_INCOMPATIBLE_SCHEMA = "incompatible schema"
_ERROR_INCOMPATIBLE_RUNTIME = "incompatible runtime"
_ERROR_INVARIANT_VIOLATION = "invariant violation"
_ERROR_DOMAIN_FAILURE = "domain failure"


class UncoveredRecountError(RuntimeError):
    """Recount refused because the replica is not a stopped compatible database."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, slots=True)
class UncoveredRecountReport:
    conversation_count: int
    uncovered_event_count: int
    uncovered_character_count: int

    def as_counts(self) -> dict[str, int]:
        """Return counts only. Never include content or identifiers."""

        return {
            "conversation_count": self.conversation_count,
            "uncovered_event_count": self.uncovered_event_count,
            "uncovered_character_count": self.uncovered_character_count,
        }


@dataclass(frozen=True, slots=True)
class UncoveredCheckReport:
    conversation_count: int
    mismatch_count: int
    uncovered_event_count: int
    uncovered_character_count: int

    def as_counts(self) -> dict[str, int]:
        """Return only aggregate counters; never include content or identifiers."""

        return {
            "conversation_count": self.conversation_count,
            "mismatch_count": self.mismatch_count,
            "uncovered_event_count": self.uncovered_event_count,
            "uncovered_character_count": self.uncovered_character_count,
        }


def rollup_policy_from_settings(settings: Settings) -> RollupPolicyConfig:
    """Build the production rollup policy used by recount and live append."""

    return RollupPolicyConfig(
        raw_tail_events=settings.conversation_rollup_raw_tail_events,
        raw_tail_characters=settings.conversation_rollup_raw_tail_characters,
        trigger_events=settings.conversation_rollup_trigger_events,
        trigger_characters=settings.conversation_rollup_trigger_characters,
        stop_events=settings.conversation_rollup_stop_events,
        stop_characters=settings.conversation_rollup_stop_characters,
        batch_max_events=settings.conversation_rollup_batch_max_events,
        batch_max_characters=settings.conversation_rollup_batch_max_characters,
        summary_max_characters=settings.conversation_rollup_summary_max_characters,
        bot_display_name=settings.bot_display_name,
        timezone=settings.default_timezone,
        llm_origins=parse_rollup_llm_origins(settings.conversation_rollup_llm_origins),
    )


async def recount_all_canonical_uncovered(
    session: AsyncSession,
    config: RollupPolicyConfig,
) -> UncoveredRecountReport:
    """Rewrite uncovered character counters with the additive durable message ruler."""

    conversations = tuple(
        (
            await session.scalars(
                select(CanonicalConversationModel).order_by(CanonicalConversationModel.id.asc())
            )
        ).all()
    )
    total_events = 0
    total_characters = 0
    for conversation in conversations:
        event_count, character_count = await recount_canonical_uncovered(
            session, conversation, config
        )
        total_events += event_count
        total_characters += character_count
    return UncoveredRecountReport(
        conversation_count=len(conversations),
        uncovered_event_count=total_events,
        uncovered_character_count=total_characters,
    )


async def check_all_canonical_uncovered(
    session: AsyncSession,
    config: RollupPolicyConfig,
) -> UncoveredCheckReport:
    """Compare stored counters with current rulers without writing any row."""

    conversations = tuple(
        (
            await session.scalars(
                select(CanonicalConversationModel).order_by(CanonicalConversationModel.id.asc())
            )
        ).all()
    )
    mismatches = 0
    total_events = 0
    total_characters = 0
    for conversation in conversations:
        event_count, character_count = await calculate_canonical_uncovered(
            session,
            conversation,
            config,
        )
        total_events += event_count
        total_characters += character_count
        mismatches += int(
            conversation.uncovered_event_count != event_count
            or conversation.uncovered_character_count != character_count
        )
    return UncoveredCheckReport(
        conversation_count=len(conversations),
        mismatch_count=mismatches,
        uncovered_event_count=total_events,
        uncovered_character_count=total_characters,
    )


async def run_stopped_offline_uncovered_recount(
    database_url: str,
    config: RollupPolicyConfig,
) -> UncoveredRecountReport:
    """Fail closed unless the replica is the canonical schema and accepts exclusive writes."""

    try:
        await require_canonical_schema(database_url)
    except CanonicalSchemaError as exc:
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_SCHEMA) from exc
    database = Database(database_url)
    try:
        async with database.immediate_session() as session:
            await _require_uncovered_watermark_columns(session)
            return await recount_all_canonical_uncovered(session, config)
    except UncoveredRecountError:
        raise
    except ConversationRollupError as exc:
        raise UncoveredRecountError(_ERROR_INVARIANT_VIOLATION) from exc
    except SQLAlchemyError as exc:
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_RUNTIME) from exc
    except (ValueError, TypeError) as exc:
        raise UncoveredRecountError(_ERROR_DOMAIN_FAILURE) from exc
    finally:
        await database.close()


async def run_offline_uncovered_check(
    database_url: str,
    config: RollupPolicyConfig,
) -> UncoveredCheckReport:
    """Read-only coverage drift check for a canonical stopped-database rehearsal."""

    read_only_url = _read_only_sqlite_url(database_url)
    try:
        await require_canonical_schema(read_only_url)
    except CanonicalSchemaError as exc:
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_SCHEMA) from exc
    engine = create_async_engine(read_only_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with sessions() as session:
            await _require_uncovered_watermark_columns(session)
            return await check_all_canonical_uncovered(session, config)
    except UncoveredRecountError:
        raise
    except ConversationRollupError as exc:
        raise UncoveredRecountError(_ERROR_INVARIANT_VIOLATION) from exc
    except SQLAlchemyError as exc:
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_RUNTIME) from exc
    except (ValueError, TypeError) as exc:
        raise UncoveredRecountError(_ERROR_DOMAIN_FAILURE) from exc
    finally:
        await engine.dispose()


def _read_only_sqlite_url(database_url: str) -> str:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_SCHEMA)
    raw_path = database_url.removeprefix(prefix)
    if raw_path == ":memory:":
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_SCHEMA)
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_SCHEMA)
    return f"{prefix}file:{path.as_posix()}?mode=ro&uri=true"


async def _require_uncovered_watermark_columns(session: AsyncSession) -> None:
    rows = await session.execute(text('PRAGMA table_info("canonical_conversations")'))
    columns = {str(row[1]) for row in rows}
    if not _REQUIRED_WATERMARK_COLUMNS.issubset(columns):
        raise UncoveredRecountError(_ERROR_INCOMPATIBLE_SCHEMA)
