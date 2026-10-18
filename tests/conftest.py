"""Shared isolated database and service fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import count
from pathlib import Path

import pytest
import pytest_asyncio

from tests.support.model_executor import require_model_executor
from yuki.admin.config_service import RuntimeConfigService
from yuki.config import Settings
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.messages import OutboundMessage, OutboundSendReceipt
from yuki.llm.base import LLMProvider
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from yuki.memory.service import MemoryFactService
from yuki.persistence.database import Database
from yuki.persistence.repositories import (
    EmojiDescriptionRepository,
    EventLedgerRepository,
    GroupSettingsRepository,
    MediaAnalysisRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
)
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.services.agent_tools import AgentToolService
from yuki.services.chat import ChatService
from yuki.services.command_service import CommandService
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.effect_gate import ConversationEffectGate
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver
from yuki.services.processor import DirectPluginCommandResolver, MessageProcessor
from yuki.services.rate_limit import SlidingWindowRateLimiter
from yuki.services.turn_coordinator import ConversationTurnCoordinator
from yuki.services.vision_rate_limit import VisionRateLimiter
from yuki.services.vision_service import VisionService
from yuki.vision.base import VisionProvider
from yuki.web.base import WebSearchProvider


class MemorySender:
    """Record outbound messages and optionally fail every send."""

    _message_ids = count(900001)

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboundMessage] = []
        self.calls = 0
        self.fail = fail

    async def send(self, message: OutboundMessage) -> OutboundSendReceipt:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic send failure")
        self.messages.append(message)
        return OutboundSendReceipt(
            platform_message_id=str(next(self._message_ids)),
            transport="test",
        )


@dataclass(slots=True)
class Harness:
    settings: Settings
    database: Database
    ledger: EventLedgerRepository
    scoped_events: ScopedEventLedgerUnitOfWork
    conversation_scopes: ConversationScopeRepository
    groups: GroupSettingsRepository
    private_users: PrivateUserSettingsRepository
    profiles: PeopleRepository
    provider: LLMProvider
    concurrency: ConcurrencyManager
    processor: MessageProcessor
    vision: VisionService | None


def make_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "superusers_csv": "9000",
        "enabled_groups_csv": "2001,2002",
        "ignored_bot_users_csv": "7777",
        "llm_provider": "fake",
        "llm_model": "fake-model",
        "model_profiles_file": Path("__test_model_profiles_not_present__.toml"),
        "global_llm_concurrency": 4,
        "per_user_requests_per_minute": 20,
        "per_group_requests_per_minute": 50,
        "daily_chat_message_delay_min_seconds": 0,
        "daily_chat_message_delay_max_seconds": 0,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def build_harness(
    database: Database,
    settings: Settings,
    provider: LLMProvider | None = None,
    *,
    web_provider: WebSearchProvider | None = None,
    vision_provider: VisionProvider | None = None,
    command_service: CommandService | None = None,
    direct_plugin_commands: DirectPluginCommandResolver | None = None,
) -> Harness:
    from yuki.identity.write_settings import configure_identity_write_settings

    configure_identity_write_settings(settings)
    groups = GroupSettingsRepository(database)
    private_users = PrivateUserSettingsRepository(
        database,
    )
    profiles = PeopleRepository(
        database,
    )
    scoped_events = ScopedEventLedgerUnitOfWork(database)
    ledger = EventLedgerRepository(database)
    ledger.set_scoped_writer(scoped_events)
    conversation_scopes = ConversationScopeRepository(database)
    memories = MemoryFactService(MemoryFactRepository(database))
    vision = (
        VisionService(
            provider=vision_provider,
            resolver=MediaResolver(
                max_download_bytes=settings.vision_max_download_bytes,
                timeout_seconds=settings.vision_media_download_timeout_seconds,
            ),
            preprocessor=ImagePreprocessor(
                max_dimension=settings.vision_max_dimension,
                max_pixels=settings.vision_max_pixels,
                max_prepared_bytes=settings.vision_max_prepared_bytes,
                gif_max_frames=8,
            ),
            analyses=MediaAnalysisRepository(database),
            rate_limiter=VisionRateLimiter(),
            emoji_descriptions=EmojiDescriptionRepository(database),
            max_prepared_bytes=settings.vision_max_prepared_bytes,
            global_concurrency=settings.vision_global_concurrency,
            queue_max_pending=settings.vision_queue_max_pending,
            queue_timeout_seconds=settings.vision_queue_timeout_seconds,
        )
        if vision_provider is not None
        else None
    )
    llm = provider or FakeLLMProvider()
    models = require_model_executor(None, provider=llm, model=settings.llm_model or "fake")
    concurrency = ConcurrencyManager()
    runtime_config = RuntimeConfigService(settings=settings, database=database)
    turn_coordinator = ConversationTurnCoordinator(
        interrupt_autonomous_on_new_message=(
            settings.conversation_interrupt_autonomous_on_new_message
        ),
    )
    effect_gate = ConversationEffectGate()
    agent_tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=memories,
        web_provider=web_provider,
        runtime_config=runtime_config,
    )
    chat = ChatService(
        settings=settings,
        model_executor=models,
        concurrency=concurrency,
        ledger=ledger,
        people=profiles,
        memories=memories,
        tools=agent_tools,
        runtime_config=runtime_config,
        memory_partition_lookup=DatabaseMemoryPartitionLookup(database),
        conversation_scopes=conversation_scopes,
        effect_gate=effect_gate,
        turn_coordinator=turn_coordinator,
    )
    from tests.support.canonical_ingress import fixture_ingress

    ingress = fixture_ingress(database)
    processor = MessageProcessor(
        canonical_ingress=ingress,
        canonical_uow=ingress.uow,
        settings=settings,
        ledger=ledger,
        conversation_scopes=conversation_scopes,
        effect_gate=effect_gate,
        groups=groups,
        private_users=private_users,
        chat=chat,
        rate_limiter=SlidingWindowRateLimiter(
            per_user=settings.per_user_requests_per_minute,
            per_group=settings.per_group_requests_per_minute,
        ),
        concurrency=concurrency,
        onebot_connected=lambda: True,
        people=profiles,
        memories=memories,
        runtime_config=runtime_config,
        vision_service=vision,
        command_service=command_service,
        direct_plugin_commands=direct_plugin_commands,
        turn_coordinator=turn_coordinator,
    )
    return Harness(
        settings,
        database,
        ledger,
        scoped_events,
        conversation_scopes,
        groups,
        private_users,
        profiles,
        llm,
        concurrency,
        processor,
        vision,
    )


@pytest.fixture(autouse=True)
def _identity_write_settings_isolation() -> Iterator[None]:
    from yuki.identity.write_settings import (
        IdentityWriteSettings,
        configure_identity_write_settings,
        reset_identity_write_settings,
    )

    reset_identity_write_settings()
    configure_identity_write_settings(IdentityWriteSettings())
    try:
        yield
    finally:
        reset_identity_write_settings()


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    from yuki.identity.write_settings import (
        IdentityWriteSettings,
        configure_identity_write_settings,
        reset_identity_write_settings,
    )

    path = (tmp_path / "test.db").as_posix()
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.create_schema()
    from yuki.identity.canonical_repository import (
        create_presence,
        ensure_person,
        ensure_presence,
        ensure_space,
    )

    async with db.immediate_session() as session:
        for user_id in (
            "1001",
            "1002",
            "1003",
            "1004",
            "2002",
            "2003",
            "9000",
            "10001",
            "10002",
            "10010001",
            "12345678",
            "90000",
            "123456789",
            "3003",
            "1808058482",
            "1808058483",
        ):
            await ensure_person(session, user_id, display_name=f"test-{user_id}")
        for bot_user_id in ("7777", "8000", "8001", "9999", "99999"):
            await ensure_presence(session, bot_user_id)
        from datetime import UTC, datetime

        await create_presence(
            session, external_account_id="yuki-app", platform="app", now=datetime.now(UTC)
        )
        await create_presence(
            session, external_account_id="yuki-acp", platform="acp", now=datetime.now(UTC)
        )
        for group_id in (
            "2001",
            "2002",
            "2999",
            "3001",
            "3002",
            "20010001",
            "20001",
            "29999999",
            "1049765710",
        ):
            await ensure_space(
                session,
                group_id,
                name=f"test-{group_id}",
                require_mention=True,
            )
    reset_identity_write_settings()
    configure_identity_write_settings(IdentityWriteSettings())
    try:
        yield db
    finally:
        reset_identity_write_settings()
        await db.close()
