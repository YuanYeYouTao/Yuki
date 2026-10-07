"""Application module builders and immutable bundles."""

from qq_ai_bot.application.modules.admin import AdminBundle, AdminModule
from qq_ai_bot.application.modules.automation import AutomationBundle, AutomationModule
from qq_ai_bot.application.modules.control_plane import ControlPlaneBundle, ControlPlaneModule
from qq_ai_bot.application.modules.conversation import ConversationBundle, ConversationModule
from qq_ai_bot.application.modules.emoji import EmojiBundle, EmojiModule
from qq_ai_bot.application.modules.media import MediaBundle, MediaModule
from qq_ai_bot.application.modules.model_runtime import ModelRuntimeBundle, ModelRuntimeModule
from qq_ai_bot.application.modules.persistence import PersistenceBundle, PersistenceModule
from qq_ai_bot.application.modules.plugins import PluginBundle, PluginModule
from qq_ai_bot.application.modules.runtime_foundation import (
    RuntimeFoundationBundle,
    RuntimeFoundationModule,
)
from qq_ai_bot.application.modules.web import WebBundle, WebModule

__all__ = [
    "AdminBundle",
    "AdminModule",
    "AutomationBundle",
    "AutomationModule",
    "ControlPlaneBundle",
    "ControlPlaneModule",
    "ConversationBundle",
    "ConversationModule",
    "EmojiBundle",
    "EmojiModule",
    "MediaBundle",
    "MediaModule",
    "ModelRuntimeBundle",
    "ModelRuntimeModule",
    "PersistenceBundle",
    "PersistenceModule",
    "PluginBundle",
    "PluginModule",
    "RuntimeFoundationBundle",
    "RuntimeFoundationModule",
    "WebBundle",
    "WebModule",
]
