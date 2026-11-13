"""Shared isolated database and service fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.support.model_executor import require_model_executor
from yuki.admin.config_service import RuntimeConfigService
from yuki.asr.service import ASRService
from yuki.config import Settings
from yuki.conversation.scope_repository import ConversationScopeRepository
from yuki.domain.messages import OutboundMessage, OutboundSendReceipt
from yuki.llm.base import LLMProvider
from yuki.llm.fake import FakeLLMProvider
from yuki.memory.repository import MemoryFactRepository, MemoryJobRepository
from yuki.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from yuki.memory.service import MemoryFactService
from yuki.memory.worker import MemoryWorker
from yuki.persistence.database import Database
from yuki.persistence.repositories import (
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
from yuki.services.prompt_composer import PromptComposer
from yuki.services.rate_limit import SlidingWindowRateLimiter
from yuki.services.turn_coordinator import ConversationTurnCoordinator
from yuki.services.vision_rate_limit import VisionRateLimiter
from yuki.services.vision_service import VisionService
from yuki.vision.base import VisionProvider
from yuki.web.base import WebSearchProvider


@pytest.fixture(scope="session", autouse=True)
def selected_test_soul():
    """Select public test data explicitly; production never falls back to a preset."""
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("SOUL_FILE", str(Path(__file__).resolve().parents[1] / "config/SOUL.md"))
        yield


@pytest.fixture
def tool_delivery_evidence(request):
    """Keep this scenario's actual evidence even when its assertions fail."""
    root = request.config.rootpath
    parent = (
        root
        / "test-results"
        / "tool-result-delivery"
        / hashlib.sha256(request.node.nodeid.encode()).hexdigest()
    )
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
    evidence = {}
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    try:
        yield directory, evidence
    finally:
        (directory / "evidence.json").write_text(
            json.dumps(
                {"nodeid": request.node.nodeid, "source_revision": revision, "evidence": evidence},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )


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


class _AbsentGroupRecovery:
    """Production always wires native group recovery; the harness has no recovery route."""

    def is_enable_request(self, message):
        return False

    async def enable(self, bot, message):
        return None

    async def hint(self, bot, message, reason):
        return None


_OPEN_ORDINARY_INPUTS: list[_ImmediateOrdinaryInput] = []


class _ImmediateOrdinaryInput:
    """Autonomous-group stand-in: no quiet window, ordinary input is promoted at once."""

    def __init__(self):
        self.tasks: set[asyncio.Task[int]] = set()
        # Closed with the test database, as the real service closes before it.
        _OPEN_ORDINARY_INPUTS.append(self)

    async def close(self) -> None:
        tasks = [task for task in self.tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def observe(self, message, *, ordinary_attempt=None):
        if ordinary_attempt is not None:
            task = asyncio.create_task(ordinary_attempt())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def observe_context(self, message, *, direct):
        return None

    def is_current_ordinary_input(self, message):
        return True


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
    settings = Settings.model_validate(values)
    settings.apply_character_name("Yuki", revision=1)
    return settings


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
    from tests.support.service_dependencies import tool_dependencies
    from yuki.workspace.short_state import ShortState

    dependencies = tool_dependencies(database, settings, runtime_config=runtime_config)
    agent_tools = AgentToolService(
        **dependencies,
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
        short_state=ShortState(dependencies["workspace_service"].store),
        observe_main_response=AsyncMock(),
        participation_runtime_context=AsyncMock(return_value=None),
        memory_context=agent_tools._memory_context,
        memory_partition_lookup=DatabaseMemoryPartitionLookup(database),
        prompt_composer=PromptComposer(settings),
        conversation_scopes=conversation_scopes,
        effect_gate=effect_gate,
        turn_coordinator=turn_coordinator,
        processing_status=SimpleNamespace(during=lambda **_: nullcontext()),
    )
    from tests.support.canonical_ingress import fixture_ingress
    from tests.support.client_identity import client_identity_service
    from yuki.admin.audit import AdminAuditService
    from yuki.admin.config_files import ConfigFileService
    from yuki.admin.control_resolution import ControlAccess
    from yuki.admin.operations import AdminOperations
    from yuki.admin.permission_catalog import PermissionCatalogService
    from yuki.admin.queries import AdminQueries
    from yuki.application.modules.automation import AutomationModule
    from yuki.gateway.platforms import PlatformGateways
    from yuki.gateway.providers import builtin_provider_catalog
    from yuki.gateway.registry import GatewayConnectionRegistry
    from yuki.memory.claim_processor import MemoryClaimProcessor
    from yuki.memory.event_extractor import MemoryEventExtractor
    from yuki.memory.mutation.service import MemoryMutationService
    from yuki.memory.rebuild.repository import MemoryRebuildRepository
    from yuki.memory.rebuild.service import MemoryRebuildService
    from yuki.model_runtime.repository import ModelInvocationRepository
    from yuki.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
    from yuki.plugin_host.command_adapter import PluginCommandAdapter
    from yuki.plugin_host.direct_command_router import DirectCommandRouter
    from yuki.plugin_host.discovery import PluginDiscovery
    from yuki.plugin_host.event_bus import PluginEventBus
    from yuki.plugin_host.extension_registry import ExtensionRegistry
    from yuki.plugin_host.loader import PluginLoader
    from yuki.plugin_host.manager import PluginManager
    from yuki.plugin_host.repository import PluginInstallationRepository
    from yuki.runtime.work_repository import WorkRepository
    from yuki.services.admin.config_admin import ConfigAdminService
    from yuki.services.admin.memory_admin import MemoryAdminService
    from yuki.services.admin.preference_admin import PreferenceAdminService
    from yuki.time.service import TimeContextService
    from yuki_plugin_sdk.testing import FakePluginContext

    audit = AdminAuditService(database)
    claim_processor = MemoryClaimProcessor(settings=settings, facts=memories)
    mutations = MemoryMutationService(
        settings=settings,
        facts=memories,
        processor=claim_processor,
        ledger=ledger,
    )
    memory_admin = MemoryAdminService(
        settings=settings,
        memories=memories,
        audit=audit,
        mutations=mutations,
        ledger=ledger,
        runtime_config=runtime_config,
    )
    extensions = ExtensionRegistry()
    plugin_events = PluginEventBus()
    plugins = PluginManager(
        enabled=False,
        discovery=PluginDiscovery(
            Path("__test_plugin_directory_not_present__"), yuki_version="0.0.0", enabled=False
        ),
        installations=PluginInstallationRepository(database),
        loader=PluginLoader(),
        extensions=extensions,
        event_bus=plugin_events,
        context_factory=lambda manifest, _permissions: FakePluginContext(manifest.id),
    )
    plugin_direct_commands = DirectCommandRouter(
        bindings=settings.plugin_direct_command_bindings, registry=extensions, manager=plugins
    )
    automation = AutomationModule(
        settings=settings,
        database=database,
        chat=chat,
        runtime_config=runtime_config,
        time_service=TimeContextService(default_timezone=settings.default_timezone),
        admin_audit=audit,
        conversation_scopes=conversation_scopes,
        turns=turn_coordinator,
        concurrency=concurrency,
        work_repository=WorkRepository(database),
    ).build()
    files = ConfigFileService(settings)
    operations = AdminOperations(
        database,
        plugins,
        files,
        identities=client_identity_service(database, fixture_ingress(database)._router),
        people=profiles,
        config=runtime_config,
        groups=groups,
        router=fixture_ingress(database)._router,
        platforms=PlatformGateways(settings.platforms_file, {}),
        providers=builtin_provider_catalog(),
        registry=GatewayConnectionRegistry(providers=builtin_provider_catalog()),
    )

    async def health():
        return []

    queries = AdminQueries(database, plugins, files, health)
    rebuild = MemoryRebuildService(
        settings=settings,
        repository=MemoryRebuildRepository(database),
        ledger=ledger,
        extractor=MemoryEventExtractor(
            models,
            concurrency,
            people=profiles,
            timezone=settings.default_timezone,
        ),
        processor=claim_processor,
    )
    command_service = command_service or CommandService(
        admin_operations=operations,
        admin_queries=queries,
        conversation_scopes=conversation_scopes,
        memory_rebuild=rebuild,
        settings=settings,
        people=profiles,
        memories=memories,
        concurrency=concurrency,
        platform_connected=lambda _platform: True,
        model_executor=models,
        runtime_config=runtime_config,
        memory_admin=memory_admin,
        preference_admin=PreferenceAdminService(memory_mutations=memory_admin),
        config_admin=ConfigAdminService(runtime_config),
        permission_catalog=PermissionCatalogService(config_registry=runtime_config.registry),
        vision_service=vision,
        automation_service=automation.service,
        automation_repository=automation.repository,
        automation_worker=automation.worker,
        turn_coordinator=turn_coordinator,
        plugin_commands=PluginCommandAdapter(
            manager=plugins,
            registry=extensions,
            control=ControlAccess(database),
            direct_commands=plugin_direct_commands,
        ),
        model_invocations=ModelInvocationRepository(database),
    )
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
        people=profiles,
        memory_worker=MemoryWorker(
            settings=settings,
            jobs=MemoryJobRepository(database),
            facts=memories,
            ledger=ledger,
            model_executor=models,
            concurrency=concurrency,
        ),
        autonomous_groups=_ImmediateOrdinaryInput(),
        runtime_config=runtime_config,
        asr_service=ASRService(
            settings=settings.asr,
            provider=None,
            resolver=MediaResolver(
                max_download_bytes=settings.asr_max_download_bytes,
                timeout_seconds=settings.asr_timeout_seconds,
            ),
        ),
        conversation_media=SimpleNamespace(
            prepare_message=lambda message, gateway, *, source_segments: message,
            submit=lambda event_id, gateway, *, message: None,
        ),
        command_service=command_service,
        direct_plugin_commands=direct_plugin_commands or plugin_direct_commands,
        turn_coordinator=turn_coordinator,
        admission_signals=PluginAdmissionSignalAdapter(extensions),
        event_publisher=plugin_events,
        emoji_library=SimpleNamespace(submit=lambda message, *, source_event_id: None),
        group_recovery=_AbsentGroupRecovery(),
        client_links=SimpleNamespace(is_request=lambda message: False),
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


async def _seed_database(db: Database) -> None:
    """The original fixed identity seed, applied to a real newly created schema."""
    from datetime import UTC, datetime

    from tests.support.canonical_identity import ensure_person, ensure_presence, ensure_space
    from yuki.identity.canonical_repository import create_presence

    identity_since = datetime(2026, 8, 24, tzinfo=UTC)
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
            person_id = await ensure_person(
                session, user_id, display_name=f"test-{user_id}", now=identity_since
            )
            if user_id == "9000":
                from yuki.identity.db_models import CanonicalPersonModel

                person = await session.get(CanonicalPersonModel, person_id)
                assert person is not None
                person.is_superuser = True
        for bot_user_id in ("7777", "8000", "8001", "9999", "99999"):
            await ensure_presence(session, bot_user_id, now=identity_since)
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


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _database_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from yuki.identity.write_settings import (
        IdentityWriteSettings,
        configure_identity_write_settings,
        reset_identity_write_settings,
    )

    path = tmp_path_factory.mktemp("database-template") / "template.db"
    db = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    reset_identity_write_settings()
    configure_identity_write_settings(IdentityWriteSettings())
    try:
        await db.create_schema()
        await _seed_database(db)
        async with db.engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await db.close()
        reset_identity_write_settings()
    path.chmod(0o444)
    return path


@pytest_asyncio.fixture
async def database(tmp_path: Path, _database_template: Path) -> Database:
    from yuki.identity.write_settings import (
        IdentityWriteSettings,
        configure_identity_write_settings,
        reset_identity_write_settings,
    )

    path = tmp_path / "test.db"
    await asyncio.to_thread(shutil.copyfile, _database_template, path)
    db = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    reset_identity_write_settings()
    configure_identity_write_settings(IdentityWriteSettings())
    try:
        yield db
    finally:
        while _OPEN_ORDINARY_INPUTS:
            await _OPEN_ORDINARY_INPUTS.pop().close()
        reset_identity_write_settings()
        await db.close()
