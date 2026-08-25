"""OneBot v11 normalization and sending adapter."""

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.adapters.onebot.provider_adapter import (
    NapCatOneBotAdapter,
    SnowLumaOneBotAdapter,
    provider_id_for_bot,
)
from qq_ai_bot.adapters.onebot.sender import OneBotSender

__all__ = [
    "NapCatOneBotAdapter",
    "OneBotSender",
    "SnowLumaOneBotAdapter",
    "normalize_event",
    "provider_id_for_bot",
]
