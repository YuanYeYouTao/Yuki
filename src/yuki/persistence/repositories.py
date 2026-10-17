"""Stable repository imports grouped behind domain-specific implementations."""

from yuki.persistence.event_repository import (
    EventLedgerRepository,
)
from yuki.persistence.media_repository import (
    EmojiDescriptionRepository,
    MediaAnalysisRepository,
)
from yuki.persistence.people_repository import (
    GroupSettingsRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
)
from yuki.persistence.repository_records import (
    EmojiDescriptionRecord,
    EventRecord,
    GroupSetting,
    MediaAnalysisRecord,
    PrivateUserSetting,
)
from yuki.persistence.web_repository import WebSearchSourceRepository

__all__ = [
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
