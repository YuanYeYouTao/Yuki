"""Complete synthetic service assembly; never starts gateways or calls a provider."""

from types import SimpleNamespace

from qq_ai_bot.config import Settings
from qq_ai_bot.container import ApplicationContainer
from qq_ai_bot.persistence.database import Database
from tests.support.model_profiles import write_fake_profiles


async def full_contract(root, *, code_enabled=True):
    settings = Settings.model_validate(
        {
            "database_url": f"sqlite+aiosqlite:///{root / 'contract.sqlite3'}",
            "code_mode_enabled": code_enabled,
            "llm_provider": "fake",
            "llm_model": "fake",
            "model_profiles_file": write_fake_profiles(root / "models.toml"),
            "workspace_directory": root / "workspace",
            "conversation_media_cache_directory": root / "media",
            "social_transfer_directory": root / "transfer",
            "plugin_directory": root / "plugins",
            "plugin_system_enabled": False,
            "sandbox_socket": root / "absent.sock",
            "web_mode": "native",
            "web_search_bridge_state_path": root / "search.sqlite3",
            "emoji_storage_root": root / "emoji",
        }
    )
    database = Database(settings.database_url)
    await database.create_schema()
    app = ApplicationContainer(settings, database=database)
    # Inert web dependency is sufficient for declaration; calling it fails.
    app.chat._tools._web_provider = SimpleNamespace()
    return app
