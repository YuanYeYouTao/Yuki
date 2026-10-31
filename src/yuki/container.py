"""Application resource container and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from yuki import __version__
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import RuntimeConfigSnapshot
from yuki.admin.permission_catalog import PermissionCatalogService
from yuki.application.lifecycle import LifecycleRegistry
from yuki.application.modules import (
    AdminModule,
    AutomationModule,
    ConversationModule,
    MediaModule,
    ModelRuntimeModule,
    PersistenceModule,
    PluginModule,
    WebModule,
)
from yuki.config import Settings
from yuki.domain.messages import InboundMessage
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.registry import GatewayConnectionRegistry, configure_process_registry
from yuki.identity.bootstrap import bootstrap_settings_identity
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter, RouteMonitor
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.persistence.database import Database
from yuki.plugin_host.background_turns import PluginBackgroundTurnWorker
from yuki.plugin_host.config import BoundConfigFacade
from yuki.plugin_host.extension_registry import ExtensionKind
from yuki.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
    ToolRuntimeProjection,
)
from yuki.plugin_host.http_client import BoundHttpFacade
from yuki.plugin_host.manifest import PluginManifest
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_delivery import (
    PlatformNotificationTransport,
    PluginNotificationOutboxWorker,
)
from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki.plugin_host.secrets import BoundSecretsFacade
from yuki.plugin_host.session_facade import BoundAgentSessionFacade
from yuki.plugin_host.storage import BoundStorageFacade
from yuki.runtime.origin import TurnOrigin
from yuki.services.autonomous_groups import AutonomousGroupService
from yuki.services.command_service import CommandService
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.effect_gate import ConversationEffectGate
from yuki.services.group_recovery import NativeGroupRecovery
from yuki.services.native_media import NativeMediaPreparer
from yuki.services.plugin_events import publish_notification
from yuki.services.processing_status import ProcessingStatusService
from yuki.services.processor import MessageProcessor, ProcessResult
from yuki.services.turn_coordinator import ConversationTurnCoordinator
from yuki.services.vision_service import VisionService
from yuki.time.service import TimeContextService
from yuki.tool_results.artifacts import ToolArtifactRepository
from yuki.tool_results.recorder import ToolReceiptRepository
from yuki.vision.base import VisionProvider
from yuki.workspace.store import WorkspaceStore
from yuki_plugin_sdk.events import EventName
from yuki_plugin_sdk.permissions import PluginPermission

if TYPE_CHECKING:
    from yuki_participation.self_report import SelfDelta

    from yuki.conversation.ordinary_admission import OrdinaryParticipationBinding
    from yuki.domain.messages import ChatResponse
    from yuki.services.agent_tools import ToolRuntime


logger = logging.getLogger(__name__)


class ApplicationContainer:
    """Own all external resources for the NoneBot application lifespan."""

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        owns_database: bool = False,
        runtime_config: RuntimeConfigService | None = None,
        vision_provider: VisionProvider | None = None,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        if bool(settings.feishu_app_id) != bool(settings.feishu_app_secret.get_secret_value()):
            raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET must be configured together")
        self.settings = settings
        store = workspace_store or WorkspaceStore(settings.workspace_directory)
        self.started_at = time.monotonic()
        self.lifecycle = LifecycleRegistry()
        persistence = PersistenceModule(
            settings,
            lifecycle=self.lifecycle,
            database=database,
            owns_database=owns_database,
            runtime_config=runtime_config,
        ).build()
        self.persistence = persistence
        self.database = persistence.database
        self.runtime_config = persistence.runtime_config
        self.lifecycle.register("application_identity", start=self._initialize_identity)
        self.gateway_providers = builtin_provider_catalog(
            qq_transfer_root=settings.social_transfer_directory,
            qq_gateway_root=settings.social_gateway_transfer_directory,
        )
        self.gateway_registry = GatewayConnectionRegistry(providers=self.gateway_providers)
        configure_process_registry(self.gateway_registry)
        self.presence_router = PresenceRouter(self.database, self.gateway_registry)
        self.route_monitor = RouteMonitor(self.presence_router)
        self.tool_receipts = ToolReceiptRepository(
            self.database,
            reflection_excerpt_characters=settings.memory_self_reflection_tool_receipt_characters,
            reflection_retention_days=settings.memory_self_reflection_tool_receipt_retention_days,
        )
        self.tool_artifacts = ToolArtifactRepository(
            self.database,
            Path("data/tool_artifacts"),
            retention_seconds=settings.tooling_result_artifact_retention_seconds,
            max_media_bytes=settings.vision_max_prepared_bytes,
            max_media_frames=settings.vision_max_frames_per_turn,
        )
        self.permission_catalog = PermissionCatalogService(
            config_registry=self.runtime_config.registry,
        )
        self.conversation_scopes = persistence.conversation_scopes
        self.scoped_events = persistence.scoped_events
        self.groups = persistence.groups
        self.private_users = persistence.private_users
        self.people = persistence.people
        self.ledger = persistence.ledger
        self.memories = persistence.memories
        self.memory_context = persistence.memory_context
        self.memory_index = persistence.memory_index
        self.memory_jobs = persistence.memory_jobs
        self.memory_audit = persistence.memory_audit
        self.memory_metrics = persistence.memory_metrics
        self.web_sources = persistence.web_sources
        self.media_analyses = persistence.media_analyses
        self.time_context = TimeContextService(
            default_timezone=settings.default_timezone,
        )
        model_runtime = ModelRuntimeModule(
            settings.model_runtime,
            self.database,
            lifecycle=self.lifecycle,
            web_settings=settings.web,
        ).build()
        self.model_runtime = model_runtime
        self.model_profiles = model_runtime.profiles
        embedding_connection = self.model_profiles.embedding
        self.memory_embeddings = MemoryEmbeddingRuntime(
            settings=settings,
            database=self.database,
            facts=self.memories,
            retriever=self.memory_context.retriever,
            connection=embedding_connection,
            api_key=model_runtime.clients.api_key_for_alias(embedding_connection.api_key_env)
            if embedding_connection
            else "",
        )
        self.model_clients = model_runtime.clients
        self.model_invocations = model_runtime.invocations
        self.models = model_runtime.executor
        self.provider = model_runtime.chat_provider
        self.web_module = WebModule(
            settings.web,
            lifecycle=self.lifecycle,
            catalog=self.model_profiles,
            clients=self.model_clients,
            invocations=self.model_invocations,
        )
        self.web_bundle = self.web_module.build()
        self.web_provider = self.web_bundle.provider
        media = MediaModule(
            settings=settings.vision,
            analyses=self.media_analyses,
            lifecycle=self.lifecycle,
            provider=vision_provider,
        ).build()
        from yuki.application.modules.asr import build_asr

        self.asr = build_asr(settings, self.lifecycle)
        self.media = media
        self.vision_provider = media.provider
        self.media_resolver = media.resolver
        self.image_preprocessor = media.image_preprocessor
        self.vision: VisionService | None = media.vision
        self.concurrency = ConcurrencyManager()
        self.turn_coordinator = ConversationTurnCoordinator(
            interrupt_autonomous_on_new_message=(
                settings.conversation_interrupt_autonomous_on_new_message
            ),
        )
        self.conversation_effect_gate = ConversationEffectGate()
        self.processing_status = ProcessingStatusService(
            self.presence_router, self.gateway_registry
        )
        from yuki.client_api.identity import ClientIdentityService
        from yuki.conversation.media_service import ConversationMediaService
        from yuki.external_agents.service import ExternalAgentService
        from yuki.sandbox.client import SandboxClient
        from yuki.sandbox.completion_receiver import CompletionReceiver
        from yuki.sandbox.task_repository import SandboxTaskRepository
        from yuki.workspace.inspect import WorkspaceInspector
        from yuki.workspace.service import WorkspaceService
        from yuki.workspace.short_state import ShortState

        self.sandbox_tasks = SandboxTaskRepository(self.database)
        self.sandbox_client = SandboxClient(settings.sandbox_socket, tasks=self.sandbox_tasks)
        self.database.sandbox_client = self.sandbox_client
        self.external_agents = ExternalAgentService(self.database, self.sandbox_tasks)
        self.sandbox_client.local_devices = self.external_agents.local_devices
        self.database.external_agents = self.external_agents
        self.client_identity = ClientIdentityService(self.database, self.presence_router)
        self.sandbox_completions = CompletionReceiver(self.sandbox_client, self.sandbox_tasks)
        self.conversation_media = ConversationMediaService(
            self.database,
            settings.conversation_media_cache_directory,
            self.media_resolver,
            self.image_preprocessor,
            gateway_registry=self.gateway_registry,
            max_prepared_bytes=settings.vision_max_prepared_bytes,
        )
        self.workspace_service = WorkspaceService(
            store,
            sandbox=self.sandbox_client,
            conversation_media=self.conversation_media,
            visual_inspector=WorkspaceInspector(
                store,
                self.image_preprocessor,
                max_prepared_bytes=settings.vision_max_prepared_bytes,
            ),
        )
        from yuki.admin.models import VisionRuntimeConfig
        from yuki.emoji.library import EmojiLibrary
        from yuki.social.service import SocialService

        async def sticker_vision_runtime() -> VisionRuntimeConfig:
            return (await self.runtime_config.snapshot()).vision

        self.emoji_library = EmojiLibrary(
            workspace=self.workspace_service,
            preprocessor=self.image_preprocessor,
            provider=self.vision_provider,
            vision_runtime=sticker_vision_runtime,
        )
        self.social_service = SocialService(
            self.database,
            self.presence_router,
            persistence.scoped_events,
            runtime_config=self.runtime_config,
            workspace_service=self.workspace_service,
            emoji_library=self.emoji_library,
        )
        self.emoji_delivery = self.social_service.emoji_delivery
        self.conversation_module = ConversationModule(
            settings=settings,
            persistence=persistence,
            model_runtime=model_runtime,
            runtime_config=self.runtime_config,
            permission_catalog=self.permission_catalog,
            concurrency=self.concurrency,
            turns=self.turn_coordinator,
            effect_gate=self.conversation_effect_gate,
            web_provider=self.web_provider,
            memory_embeddings=self.memory_embeddings,
            tool_artifacts=self.tool_artifacts,
            tool_receipts=self.tool_receipts,
            processing_status=self.processing_status,
            social=self.social_service,
            workspace=self.workspace_service,
            sandbox=self.sandbox_client,
            external_agents=self.external_agents,
            short_state=ShortState(store),
            observe_main_response=self._observe_main_response,
            participation_runtime_context=self._participation_runtime_context,
        )
        conversation = self.conversation_module.build()
        self.conversation = conversation
        self.prompt_registry = conversation.prompt_registry
        self.admission_features = conversation.admission_features
        self.rate_limiter = conversation.rate_limiter
        self.agent_tools = conversation.agent_tools
        self.chat = conversation.chat
        self.runtime = self.chat.runtime
        self.main_agent_contract = self.chat.main_contract
        from yuki.runtime.subagent_repository import SubagentRepository
        from yuki.runtime.subagent_scheduler import SubagentScheduler
        from yuki.runtime.work_repository import WorkRepository
        from yuki.runtime.work_scheduler import WorkScheduler
        from yuki.sandbox.continuation_worker import SandboxContinuationWorker
        from yuki.services.main_agent_backend import MainAgentBackend
        from yuki.services.subagent_execution import (
            SubagentExecution,
            SubagentExecutionDependencies,
        )
        from yuki.services.work_resume import WorkResumeDependencies, WorkResumer

        works = WorkRepository(self.database)
        self.sandbox_continuations = SandboxContinuationWorker(works)

        async def resume_plugin(work: dict[str, object], source: dict[str, object]) -> None:
            from yuki.plugin_host.main_turn import resume_plugin_work

            if source.get("owner") == "plugin_background":
                await self.plugin_background_turns.resume_work(work, source)
            else:
                await resume_plugin_work(self._plugin_contexts.get, self.ledger, work, source)

        self.work_resumer = WorkResumer(
            works,
            WorkResumeDependencies(
                settings=self.settings,
                ledger=self.ledger,
                conversation_scopes=self.conversation_scopes,
                turn_coordinator=self.turn_coordinator,
                runtime_config=self.runtime_config,
                sandbox_tasks=self.sandbox_tasks,
                active_bindings=self.runtime.bindings,
                generate_wakeup=self.chat.generate_main_agent_wakeup,
                generate_self=self.chat.generate_self_initiative,
                resume_plugin=resume_plugin,
                generate_origin=self.chat.generate_origin_turn,
            ),
        )
        self.work_scheduler = WorkScheduler(
            works,
            self.work_resumer.resume,
            chat_admission_enabled=self.settings.runtime_work_enabled,
        )
        self.database.subagents_enabled = self.settings.subagents_enabled
        children = SubagentRepository(works)
        self._plugin_contexts: dict[str, HostPluginContext] = {}
        self.subagent_execution = SubagentExecution(
            works,
            children,
            SubagentExecutionDependencies(
                active_bindings=self.runtime.bindings,
                ledger=self.ledger,
                runtime_config=self.runtime_config,
                sandbox_tasks=self.sandbox_tasks,
                sandbox_client=self.sandbox_client,
                runner=self.runtime.runner,
                load_tools=self.main_agent_contract.definitions,
                open_memory=self.chat.open_memory_session,
                open_self_memory=self.chat.open_self_memory_session,
                static_messages=self.chat._prompt_composer.static_messages,
                lookup_plugin_host=self._plugin_contexts.get,
                backend_factory=lambda runtime, host: MainAgentBackend(
                    self.chat, runtime, plugin_host=host
                ),
                web_capabilities=self.chat.web_capabilities,
            ),
        )
        self.subagent_scheduler = SubagentScheduler(
            works,
            children,
            self.subagent_execution,
            admission_enabled=self.settings.subagents_enabled,
            max_concurrency=self.settings.subagent_concurrency,
        )
        self.runtime.register_worker("runtime_work", self.work_scheduler)
        self.runtime.register_worker("subagents", self.subagent_scheduler)
        self.memory_mutations = conversation.memory_mutations
        self.memory_worker = conversation.memory_worker
        self.memory_rebuild_service = conversation.memory_rebuild_service
        self.memory_rebuild_worker = conversation.memory_rebuild_worker
        self.memory_maintenance_worker = conversation.memory_maintenance_worker
        self.memory_self_reflection_worker = conversation.memory_self_reflection_worker
        self.memory_dream_worker = conversation.memory_dream_worker
        admin = AdminModule(
            settings=settings,
            database=self.database,
            runtime_config=self.runtime_config,
            memories=self.memories,
            memory_context=self.memory_context,
            memory_index=self.memory_index,
            memory_embeddings=self.memory_embeddings,
            memory_audit=self.memory_audit,
            memory_maintenance=self.memory_maintenance_worker,
            memory_mutations=self.memory_mutations,
            ledger=self.ledger,
            memory_self_reflection=self.memory_self_reflection_worker,
            memory_dream=self.memory_dream_worker,
        ).build()
        self.admin = admin
        self.admin_audit = admin.audit
        self.memory_admin = admin.memories
        self.preference_admin = admin.preferences
        self.config_admin = admin.config
        self.automation_module = AutomationModule(
            settings=settings,
            database=self.database,
            chat=self.chat,
            runtime_config=self.runtime_config,
            time_service=self.time_context,
            admin_audit=self.admin_audit,
            conversation_scopes=self.conversation_scopes,
            turns=self.turn_coordinator,
            work_repository=works,
        )
        automation = self.automation_module.build()
        self.automation_bundle = automation
        self.automation_repository = automation.repository
        self.automation = automation.service
        self.automation_tools = automation.tools
        self.chat.set_automation_tools(self.automation_tools)
        self.automation_worker = automation.worker
        self.plugin_notification_repository = PluginNotificationRepository(
            self.database,
            self.scoped_events,
        )
        self.plugin_media_artifacts = PluginMediaArtifactStore(self.database)
        self.plugin_notification_outbox = PluginNotificationOutboxWorker(
            repository=self.plugin_notification_repository,
            artifacts=self.plugin_media_artifacts,
            workspace_store=store,
            ledger=self.ledger,
            transport=PlatformNotificationTransport(
                database=self.database,
                router=self.presence_router,
            ),
            effect_gate=self.conversation_effect_gate,
            effect_gate_timeout_seconds=settings.conversation_effect_gate_timeout_seconds,
        )
        self.plugin_background_turns = PluginBackgroundTurnWorker(
            repository=self.plugin_notification_repository,
            ledger=self.ledger,
            runtime_config=self.runtime_config,
            chat=self.chat,
            turns=self.turn_coordinator,
            conversation_scopes=self.conversation_scopes,
        )
        self.plugin_module = PluginModule(
            settings=settings.plugins,
            yuki_version=__version__,
            database=self.database,
            models=self.models,
            concurrency=self.concurrency,
            runtime_config=self.runtime_config,
            prompt_registry=self.prompt_registry,
            context_factory=self._create_plugin_context,
            plugin_invocation_scope=self._plugin_invocation_scope,
            signal_invocation_scope=self._plugin_signal_scope,
            on_activated=self._activate_plugin_extensions,
            on_deactivated=self._deactivate_plugin_extensions,
        )
        plugins = self.plugin_module.build()
        self.plugins = plugins
        self.plugin_config_values = plugins.config_values
        self.plugin_state = plugins.state
        self.plugin_audit = plugins.audit
        self.plugin_session_repository = plugins.session_repository
        self.plugin_sessions = plugins.sessions
        self.plugin_http = plugins.http
        self.plugin_events = plugins.events
        self.plugin_extensions = plugins.extensions
        self.plugin_prompts = plugins.prompts
        self.plugin_manager = plugins.manager
        self.plugin_tools = plugins.tools
        self.plugin_direct_commands = plugins.direct_commands
        self.plugin_commands = plugins.commands
        self.plugin_admission_signals = plugins.admission_signals
        from yuki.admin.config_files import ConfigFileService

        self.config_files = ConfigFileService(
            settings,
            self.model_profiles,
            model_executor=self.models,
            web_module=self.web_module,
        )
        from yuki.application.modules.admin_api import build_admin_api
        from yuki.character.repository import CharacterRepository
        from yuki.character.soul import CharacterSoul

        self.character = CharacterRepository(self.client_identity, settings)
        self.soul = CharacterSoul(self.client_identity, settings)
        from yuki.character.avatar import AvatarStore, CharacterAvatars

        self.character_avatars = CharacterAvatars(
            self.character,
            AvatarStore(
                (settings.sqlite_path.parent if settings.sqlite_path is not None else Path("data"))
                / "character-assets"
                / "avatars"
            ),
        )
        self.admin_api = build_admin_api(self)
        from yuki.client_api.http import ClientApi
        from yuki.services.client_link import ClientLinkCommands

        self.client_links = ClientLinkCommands(
            self.client_identity, self.gateway_registry, self.settings.client_public_url
        )
        self.chat.set_plugin_tools(self.plugin_tools)
        from yuki.services.semantic_participation import SemanticParticipationService

        self.semantic_participation = SemanticParticipationService(
            database=self.database,
            settings=settings,
            ledger=self.ledger,
            runtime_config=self.runtime_config,
            validate_turn_snapshot=self.chat.validate_turn_snapshot,
            promote=self._promote_semantic_event,
            traces=self.models.traces,
        )
        self.config_files.set_semantic_runtime(self.semantic_participation)
        self.autonomous_groups = AutonomousGroupService(
            chat=self.chat,
            runtime_config=self.runtime_config,
            admission_features=self.admission_features,
            turn_coordinator=self.turn_coordinator,
            admission_signals=self.plugin_admission_signals,
            participation=self.semantic_participation,
        )
        self.command_service = CommandService(
            admin_operations=self.admin_api.commands,
            admin_queries=self.admin_api.queries,
            conversation_scopes=self.conversation_scopes,
            settings=settings,
            people=self.people,
            memories=self.memories,
            concurrency=self.concurrency,
            platform_connected=lambda scope: (
                self.gateway_registry.snapshot_presence(
                    presence_id=scope.presence_id or "",
                    platform=scope.platform,
                    external_account_id=scope.bot_user_id,
                ).health.value
                == "connected"
            ),
            model_executor=self.models,
            runtime_config=self.runtime_config,
            memory_admin=self.memory_admin,
            memory_rebuild=self.memory_rebuild_service,
            preference_admin=self.preference_admin,
            config_admin=self.config_admin,
            permission_catalog=self.permission_catalog,
            vision_service=self.vision,
            automation_service=self.automation,
            automation_repository=self.automation_repository,
            automation_worker=self.automation_worker,
            turn_coordinator=self.turn_coordinator,
            plugin_commands=self.plugin_commands,
            model_invocations=self.model_invocations,
        )
        self.canonical_ingress = CanonicalIngressResolver(
            self.database,
            self.gateway_registry,
            self.presence_router,
        )
        self.canonical_uow = CanonicalIngressUnitOfWork(
            self.database,
            self.presence_router,
        )
        self.processor = MessageProcessor(
            conversation_media=self.conversation_media,
            asr_service=self.asr,
            settings=settings,
            ledger=self.ledger,
            conversation_scopes=self.conversation_scopes,
            effect_gate=self.conversation_effect_gate,
            groups=self.groups,
            private_users=self.private_users,
            chat=self.chat,
            rate_limiter=self.rate_limiter,
            concurrency=self.concurrency,
            canonical_ingress=self.canonical_ingress,
            client_links=self.client_links,
            group_recovery=NativeGroupRecovery(
                settings, self.database, self.gateway_registry, self.presence_router
            ),
            canonical_uow=self.canonical_uow,
            people=self.people,
            memories=self.memories,
            memory_worker=self.memory_worker,
            autonomous_groups=self.autonomous_groups,
            runtime_config=self.runtime_config,
            automation_service=self.automation,
            automation_repository=self.automation_repository,
            automation_worker=self.automation_worker,
            command_service=self.command_service,
            direct_plugin_commands=self.plugin_direct_commands,
            admission_signals=self.plugin_admission_signals,
            turn_coordinator=self.turn_coordinator,
            event_publisher=self.plugin_events,
            emoji_library=self.emoji_library,
        )
        self.processor.set_participation(self.semantic_participation)
        from yuki.client_api.automations import ClientAutomations
        from yuki.client_api.capabilities import ClientCapabilities
        from yuki.client_api.computer import ClientComputer
        from yuki.client_api.files import ClientFiles
        from yuki.client_api.memory import ClientMemory
        from yuki.client_api.messages import ClientMessages
        from yuki.client_api.prompts import ClientPrompts
        from yuki.client_api.views import ClientViews
        from yuki.client_api.works import ClientWorks
        from yuki.conversation.uploads import ConversationUploads
        from yuki.gateway.providers.app.ingress import AppGateway

        self.app_gateway = AppGateway(
            self.database, self.gateway_registry, self.processor, self.social_service
        )
        self.acp_gateway = AppGateway(
            self.database,
            self.gateway_registry,
            self.processor,
            self.social_service,
            platform="acp",
        )
        client_gateways = {"app": self.app_gateway, "acp": self.acp_gateway}
        client_prompts = ClientPrompts(
            self.client_identity,
            client_gateways,
            self.runtime.bindings,
            concurrency=self.concurrency,
        )
        self.telegram_gateway = None
        if settings.telegram_bot_token.get_secret_value():
            from yuki.gateway.providers.telegram import TelegramClient
            from yuki.gateway.providers.telegram_ingress import TelegramGateway

            self.telegram_gateway = TelegramGateway(
                self.database,
                self.gateway_registry,
                self.presence_router,
                self.processor,
                self.scoped_events,
                TelegramClient(settings.telegram_bot_token.get_secret_value()),
                poll_interval=settings.telegram_poll_interval_seconds,
            )
        self.feishu_gateway = None
        if settings.feishu_app_id:
            from yuki.gateway.providers.feishu import FeishuClient
            from yuki.gateway.providers.feishu_ingress import FeishuGateway

            self.feishu_gateway = FeishuGateway(
                database=self.database,
                registry=self.gateway_registry,
                router=self.presence_router,
                processor=self.processor,
                writer=self.scoped_events,
                client=FeishuClient(
                    settings.feishu_app_id,
                    settings.feishu_app_secret.get_secret_value(),
                    domain=settings.feishu_domain,
                ),
            )
        self.gateway_health_sources = {
            platform: gateway.health
            for platform, gateway in (
                ("telegram", self.telegram_gateway),
                (
                    self.feishu_gateway.client.platform
                    if self.feishu_gateway is not None
                    else "feishu",
                    self.feishu_gateway,
                ),
            )
            if gateway is not None
        }
        self.client_computer = ClientComputer(self.client_identity, self.sandbox_client)
        client_works = ClientWorks(
            self.client_identity, client_gateways, computer=self.client_computer
        )
        client_views = ClientViews(
            self.client_identity,
            client_works,
            self.models.traces,
            character=self.character,
            soul=self.soul,
            computer=self.client_computer,
        )
        client_files = ClientFiles(
            ConversationUploads(self.client_identity, self.conversation_media),
            client_views,
            self.workspace_service.store,
        )
        self.client_api = ClientApi(
            self.client_identity,
            ClientMessages(
                self.client_identity, client_gateways, client_files, prompts=client_prompts
            ),
            client_works,
            client_views,
            client_files,
            client_prompts,
            self.character,
            self.soul,
            self.character_avatars,
            memory=ClientMemory(
                self.client_identity, self.memories, self.memory_mutations, self.app_gateway
            ),
            automations=ClientAutomations(
                self.client_identity, self.automation_repository, self.automation
            ),
            capabilities=ClientCapabilities(
                self.client_identity, self.plugin_manager, self.sandbox_client
            ),
            computer=self.client_computer,
        )
        self._cleanup_stop = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._register_lifecycle()

    async def _promote_semantic_event(
        self, event_id: int, binding: OrdinaryParticipationBinding
    ) -> ProcessResult:
        return await self.processor.promote_committed_event(
            event_id, binding, reason="semantic_invitation"
        )

    async def _observe_main_response(
        self, runtime: ToolRuntime, sequence: int, response: ChatResponse, delta: SelfDelta | None
    ) -> None:
        await self.semantic_participation.observe_main_response(runtime, sequence, response, delta)

    async def _participation_runtime_context(self, runtime: ToolRuntime) -> dict[str, Any] | None:
        return await self.semantic_participation.runtime_context(runtime)

    def _create_plugin_context(
        self,
        manifest: PluginManifest,
        permissions: frozenset[PluginPermission],
    ) -> HostPluginContext:
        schema_items = self.plugin_extensions.list(
            plugin_id=manifest.id,
            kind=ExtensionKind.CONFIG_SCHEMA,
        )
        config_schema = (
            cast(type[BaseModel], schema_items[0].registration) if schema_items else None
        )

        def config_factory(
            current_user_id: str | None,
            current_group_id: str | None,
        ) -> BoundConfigFacade:
            return BoundConfigFacade(
                repository=self.plugin_config_values,
                plugin_id=manifest.id,
                approved_permissions=permissions,
                schema=config_schema,
                current_user_id=current_user_id,
                current_group_id=current_group_id,
            )

        def session_factory(invocation: PluginInvocation) -> BoundAgentSessionFacade:
            return BoundAgentSessionFacade(
                service=self.plugin_sessions,
                plugin_id=manifest.id,
                actor_user_id=invocation.actor_person_id,
                current_group_id=invocation.space_id,
                approved_permissions=permissions,
                conversation_id=invocation.conversation_id,
            )

        secrets = BoundSecretsFacade(
            plugin_id=manifest.id,
            declared_names=manifest.secrets,
        )
        context = HostPluginContext(
            plugin_id=manifest.id,
            approved_permissions=permissions,
            scheduler_task_limit=min(
                manifest.limits.background_tasks,
                self.settings.plugin_background_task_limit,
            ),
            services=PluginFacadeServices(
                social=self.social_service,
                approval_revision=manifest.manifest_hash,
                ledger=self.ledger,
                people=self.people,
                groups=self.groups,
                memories=self.memories,
                memory_context=self.memory_context,
                memory_admin=self.memory_admin,
                runtime_config=self.runtime_config,
                agent_runner=self.runtime.runner,
                web_provider=self.web_provider,
                vision=self.vision,
                automation=self.automation,
                storage=BoundStorageFacade(
                    repository=self.plugin_state,
                    plugin_id=manifest.id,
                    approved_permissions=permissions,
                    storage_mb=manifest.limits.storage_mb,
                ),
                config_factory=config_factory,
                secrets=secrets,
                http=BoundHttpFacade(
                    client=self.plugin_http,
                    approved_permissions=permissions,
                    allowed_hosts=manifest.network.allowed_hosts,
                    secrets=secrets,
                    http_concurrency=manifest.limits.http_concurrency,
                ),
                agent_sessions_factory=session_factory,
                events=self.plugin_events,
                audit=self.plugin_audit,
                notifications=self.plugin_notification_repository,
                notification_wake=self._wake_plugin_notifications,
                media_artifacts=self.plugin_media_artifacts,
                native_media=NativeMediaPreparer(
                    self.image_preprocessor,
                    max_bytes=self.settings.vision_max_prepared_bytes,
                ),
                media_storage_mb=manifest.limits.storage_mb,
            ),
        )
        self._plugin_contexts[manifest.id] = context
        return context

    def _wake_plugin_notifications(self) -> None:
        self.plugin_notification_outbox.wake()
        self.plugin_background_turns.wake()

    def _plugin_invocation_scope(self, plugin_id: str, runtime: ToolRuntimeProjection) -> object:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.invocation_scope(
            plugin_id,
            runtime,
        )

    def _plugin_signal_scope(
        self,
        plugin_id: str,
        message: InboundMessage,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
    ) -> AbstractContextManager[object] | AbstractAsyncContextManager[object]:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.bind(
            PluginInvocation(
                plugin_id=plugin_id,
                origin=origin,
                actor_user_id=message.sender.user_id,
                bot_user_id=message.bot_user_id or "unknown-bot",
                inbound=message,
                runtime_config=runtime,
            )
        )

    def _activate_plugin_extensions(self, manifest: PluginManifest) -> None:
        self.plugin_prompts.activate(manifest.id)

    def _deactivate_plugin_extensions(self, plugin_id: str) -> None:
        self.plugin_prompts.deactivate(plugin_id)
        self._plugin_contexts.pop(plugin_id, None)

    @classmethod
    async def create(cls, settings: Settings) -> ApplicationContainer:
        """Load restart overrides before constructing long-lived clients and limits."""

        from yuki.identity.write_settings import configure_identity_write_settings

        database = Database(settings.database_url)
        configure_identity_write_settings(settings)
        runtime_config = RuntimeConfigService(
            settings=settings,
            database=database,
        )
        try:
            await runtime_config.initialize()
            # model_copy preserves cached domain projections computed during
            # Settings validation. Rebuild so long-lived modules see overrides,
            # and validate the combined configuration before creating clients.
            try:
                active_settings = Settings(
                    _env_file=None,
                    **{**settings.model_dump(), **await runtime_config.startup_settings_updates()},
                )
            except ValidationError:
                # Validation errors can retain credential-bearing input mappings.
                raise ValueError("activated runtime settings failed validation") from None
            from yuki.services.media_resolver import _media_io

            workspace_store = await _media_io(WorkspaceStore, active_settings.workspace_directory)
            return cls(
                active_settings,
                database=database,
                owns_database=True,
                runtime_config=runtime_config,
                workspace_store=workspace_store,
            )
        except BaseException:
            await database.close()
            raise

    def onebot_connected(self) -> bool:
        """App's always-local connection is not evidence of a live QQ gateway."""

        return self.gateway_registry.has_any_active(platform="qq")

    def _register_lifecycle(self) -> None:
        self.lifecycle.register(
            "control_recovery",
            start=self.admin_api.commands.executor.receipts.recover_interrupted_controls,
        )
        self.lifecycle.register(
            "social_recovery", start=self.social_service.receipts.recover_interrupted
        )
        self.lifecycle.register("external_agents", close=self.external_agents.close)
        self.lifecycle.register(
            "client_computer", start=self.client_computer.start, close=self.client_computer.close
        )
        self.lifecycle.register(
            "workspace", start=self.workspace_service.start, close=self.workspace_service.close
        )
        self.lifecycle.register(
            "conversation_media",
            start=self.conversation_media.start,
            close=self.conversation_media.close,
        )
        self.lifecycle.register("sticker_library", close=self.emoji_library.close)
        self.lifecycle.register(
            "memory_embeddings",
            start=self.memory_embeddings.start,
            close=self.memory_embeddings.close,
            health=self.memory_embeddings.health,
        )
        self.lifecycle.register("autonomous_groups", close=self.autonomous_groups.close)
        self.lifecycle.register(
            "plugin_notification_outbox",
            start=self.plugin_notification_outbox.start,
            close=self.plugin_notification_outbox.close,
        )
        self.plugin_module.register_lifecycle(self.plugins, self.lifecycle)
        self.lifecycle.register("main_agent_manifest", start=self._freeze_main_manifest)
        self.lifecycle.register(
            "emoji_delivery", start=self.emoji_delivery.start, close=self.emoji_delivery.close
        )
        self.lifecycle.register(
            "semantic_participation",
            start=self.semantic_participation.start,
            close=self.semantic_participation.close,
            health=self.semantic_participation.health,
        )
        self.lifecycle.register(
            "sandbox_completions",
            start=self.sandbox_completions.start,
            close=self.sandbox_completions.close,
            health=self.sandbox_completions.health,
        )
        self.lifecycle.register(
            "sandbox_continuations",
            start=self.sandbox_continuations.start,
            close=self.sandbox_continuations.close,
            health=self.sandbox_continuations.health,
        )
        self.lifecycle.register(
            "app_gateway", start=self.app_gateway.start, close=self.app_gateway.close
        )
        self.lifecycle.register(
            "acp_gateway", start=self.acp_gateway.start, close=self.acp_gateway.close
        )
        self.lifecycle.register("processing_status", close=self.processing_status.close)
        self.lifecycle.register(
            "yuki_runtime",
            start=self.runtime.start,
            close=self.runtime.close,
            health=self.runtime.health,
        )
        self.lifecycle.register(
            "plugin_background_turns",
            start=self.plugin_background_turns.start,
            close=self.plugin_background_turns.close,
            health=self.plugin_background_turns.health,
        )
        if self.telegram_gateway is not None:
            self.lifecycle.register(
                "telegram_gateway",
                start=self.telegram_gateway.start,
                close=self.telegram_gateway.close,
                health=self.telegram_gateway.health,
            )
        if self.feishu_gateway is not None:
            self.lifecycle.register(
                "feishu_gateway",
                start=self.feishu_gateway.start,
                close=self.feishu_gateway.close,
                health=self.feishu_gateway.health,
            )
        self.lifecycle.register("application_event", start=self._publish_started)
        self.lifecycle.register(
            "maintenance",
            start=self._start_cleanup,
            close=self._close_cleanup,
        )
        self.conversation_module.register_workers(self.conversation, self.lifecycle)
        self.automation_module.register_lifecycle(self.automation_bundle, self.lifecycle)

    async def _freeze_main_manifest(self) -> None:
        # Finish plugin registration before queued background turns can freeze a partial catalog.
        await self.main_agent_contract.definitions()

    async def _publish_started(self) -> None:
        await publish_notification(
            self.plugin_events,
            EventName.APPLICATION_STARTED,
            {"version": __version__},
        )

    async def _start_cleanup(self) -> None:
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="processed-event-cleanup",
        )

    async def _close_cleanup(self) -> None:
        self._cleanup_stop.set()
        if self._cleanup_task is not None:
            await self._cleanup_task

    async def _initialize_identity(self) -> None:
        await bootstrap_settings_identity(self.database, self.settings)
        character = await self.character.current()
        self.settings.apply_character_name(character.display_name, character.revision)

    async def start(self) -> None:
        """Start maintenance tasks after migrations have run."""

        await self.lifecycle.start()

    async def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                runtime = await self.runtime_config.snapshot()
                web_deleted = await self.web_sources.cleanup_expired(
                    retention_days=runtime.web.source_retention_days
                )
                if web_deleted:
                    logger.info("web_source_runs_cleaned count=%d", web_deleted)
                vision_deleted = await self.media_analyses.cleanup_expired()
                if vision_deleted:
                    logger.info("media_analyses_cleaned count=%d", vision_deleted)
                plugin_state_deleted = await self.plugin_state.cleanup_expired()
                await self.client_identity.cleanup_expired()
                media_artifacts_deleted = await self.plugin_media_artifacts.cleanup()
                if media_artifacts_deleted:
                    logger.info(
                        "plugin_media_artifacts_cleaned count=%d",
                        media_artifacts_deleted,
                    )
                if plugin_state_deleted:
                    logger.info("plugin_state_cleaned count=%d", plugin_state_deleted)
                artifact_deleted = await self.tool_artifacts.cleanup()
                if artifact_deleted:
                    logger.info("tool_artifacts_cleaned count=%d", artifact_deleted)
                plugin_sessions_expired = await self.plugin_session_repository.expire_due()
                if plugin_sessions_expired:
                    logger.info(
                        "plugin_sessions_expired count=%d",
                        plugin_sessions_expired,
                    )
                if self.models.traces is not None:
                    await self.models.traces.cleanup_expired()
            except (SQLAlchemyError, OSError, RuntimeError) as exc:
                logger.error("processed_event_cleanup_failed", exc_info=exc)
            try:
                await asyncio.wait_for(
                    self._cleanup_stop.wait(),
                    timeout=self.settings.processed_event_cleanup_seconds,
                )
            except TimeoutError:
                continue

    async def close(self) -> None:
        """Gracefully stop tasks and close provider/database pools."""

        await publish_notification(
            self.plugin_events,
            EventName.APPLICATION_STOPPING,
            {"version": __version__},
        )
        await self.lifecycle.close()


_container: ApplicationContainer | None = None


def set_container(container: ApplicationContainer) -> None:
    """Publish the initialized lifespan container to adapter handlers."""

    global _container
    _container = container


def get_container() -> ApplicationContainer:
    """Return the initialized container or fail clearly during invalid lifecycle use."""

    if _container is None:
        raise RuntimeError("application container is not initialized")
    return _container
