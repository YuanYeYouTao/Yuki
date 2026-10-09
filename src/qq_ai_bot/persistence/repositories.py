"""Stable repository imports grouped behind domain-specific implementations."""

from qq_ai_bot.persistence.event_repository import (
    AgentActionRepository,
    EventLedgerRepository,
)
from qq_ai_bot.persistence.media_repository import (
    EmojiDescriptionRepository,
    MediaAnalysisRepository,
)
from qq_ai_bot.persistence.people_repository import (
    GroupSettingsRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
)
from qq_ai_bot.persistence.repository_records import (
    EmojiDescriptionRecord,
    EventRecord,
    GroupSetting,
    MediaAnalysisRecord,
    PrivateUserSetting,
)
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository

__all__ = [
    "AgentActionRepository",
    "EmojiDescriptionRecord",
    "EmojiDescriptionRepository",
    "EventLedgerRepository",
    "EventRecord",
    "GroupSetting",
    "GroupSettingsRepository",
    "MediaAnalysisRecord",
    "MediaAnalysisRepository",
    "PeopleRepository",
    "PrivateUserSetting",
    "PrivateUserSettingsRepository",
    "WebSearchSourceRepository",
]
