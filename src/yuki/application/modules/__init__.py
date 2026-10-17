"""Application module builders and immutable bundles."""

from yuki.application.modules.admin import AdminBundle, AdminModule
from yuki.application.modules.automation import AutomationBundle, AutomationModule
from yuki.application.modules.conversation import ConversationBundle, ConversationModule
from yuki.application.modules.emoji import EmojiBundle, EmojiModule
from yuki.application.modules.media import MediaBundle, MediaModule
from yuki.application.modules.model_runtime import ModelRuntimeBundle, ModelRuntimeModule
from yuki.application.modules.persistence import PersistenceBundle, PersistenceModule
from yuki.application.modules.plugins import PluginBundle, PluginModule
from yuki.application.modules.web import WebBundle, WebModule

__all__ = [
    "AdminBundle",
    "AdminModule",
    "AutomationBundle",
    "AutomationModule",
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
    "WebBundle",
    "WebModule",
]
