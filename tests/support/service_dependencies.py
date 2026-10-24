"""Construct real domain dependencies explicitly for isolated tool fixtures."""

from pathlib import Path

from yuki.admin.config_service import RuntimeConfigService
from yuki.client_api.identity import ClientIdentityService
from yuki.conversation.media_service import ConversationMediaService
from yuki.emoji.library import EmojiLibrary
from yuki.executor.service import LocalExecutors
from yuki.external_agents.service import ExternalAgentService
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.routing import PresenceRouter
from yuki.persistence.database import Database
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.task_repository import SandboxTaskRepository
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.media_resolver import MediaResolver
from yuki.social.service import SocialService
from yuki.workspace.inspect import WorkspaceInspector
from yuki.workspace.service import WorkspaceService
from yuki.workspace.store import WorkspaceStore


def workspace_service(store, *, database=None, registry=None, sandbox=None):
    database = database or Database(f"sqlite+aiosqlite:///{store.root / 'test-owner.sqlite3'}")
    registry = registry or GatewayConnectionRegistry(providers=builtin_provider_catalog())
    preprocessor = ImagePreprocessor()
    return WorkspaceService(
        store,
        sandbox=sandbox or SandboxClient(store.root / "manager.sock"),
        visual_inspector=WorkspaceInspector(store, preprocessor),
        conversation_media=ConversationMediaService(
            database,
            store.root / "media-cache",
            MediaResolver(),
            preprocessor,
            gateway_registry=registry,
        ),
    )


def social_service(database, router, writer, *, settings=None, runtime_config=None, workspace=None):
    if settings is None:
        from tests.conftest import make_settings

        settings = make_settings(database.url)
    workspace = workspace or workspace_service(
        WorkspaceStore(Path(database.engine.url.database).parent / "tool-environment"),
        database=database,
    )
    return SocialService(
        database,
        router,
        writer,
        runtime_config=runtime_config or RuntimeConfigService(settings=settings, database=database),
        workspace_service=workspace,
        emoji_library=EmojiLibrary(workspace),
    )


def tool_dependencies(database, settings, *, runtime_config=None):
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    router = PresenceRouter(database, registry)
    tasks = SandboxTaskRepository(database)
    sandbox = SandboxClient(settings.sandbox_socket, tasks=tasks)
    store = WorkspaceStore(Path(database.engine.url.database).parent / "tool-environment")
    workspace = workspace_service(store, database=database, registry=registry, sandbox=sandbox)
    social = social_service(
        database,
        router,
        ScopedEventLedgerUnitOfWork(database),
        settings=settings,
        runtime_config=runtime_config,
        workspace=workspace,
    )
    return {
        "social_service": social,
        "workspace_service": workspace,
        "sandbox_client": sandbox,
        "external_agent_service": ExternalAgentService(database, tasks),
        "local_executors": LocalExecutors(ClientIdentityService(database, router), store),
    }
