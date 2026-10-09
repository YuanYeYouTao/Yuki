"""Configuration declarations that affect only future records."""

from __future__ import annotations

from qq_ai_bot.admin.config_spec_helpers import (
    _G,
    _GGU,
    _field,
    _spec,
)
from qq_ai_bot.admin.models import ConfigApplyMode, ConfigSpec


def future_config_specs() -> tuple[ConfigSpec, ...]:
    return (
        _spec(
            "web.source_retention_days",
            "联网来源保留天数",
            "之后运行的清理任务使用的新保留天数。",
            aliases=("来源保留时间",),
            value_type="integer",
            minimum=1,
            maximum=365,
            scopes=_G,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="WEB_SOURCE_RETENTION_DAYS",
            getter=_field("web_source_retention_days"),
            settings_fields=("web_source_retention_days",),
            category="web",
        ),
        _spec(
            "web.source_max_runs_per_conversation",
            "每会话联网来源批次上限",
            "之后新保存来源时允许保留的搜索批次数。",
            aliases=("来源批次上限",),
            value_type="integer",
            minimum=1,
            maximum=100,
            scopes=_GGU,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="WEB_SOURCE_MAX_RUNS_PER_CONVERSATION",
            getter=_field("web_source_max_runs_per_conversation"),
            settings_fields=("web_source_max_runs_per_conversation",),
            category="web",
        ),
        _spec(
            "vision.analysis_retention_days",
            "视觉分析缓存保留天数",
            "之后执行的清理任务使用的新视觉分析缓存保留天数。",
            aliases=("视觉缓存保留时间",),
            value_type="integer",
            minimum=1,
            maximum=365,
            scopes=_G,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="VISION_ANALYSIS_RETENTION_DAYS",
            getter=_field("vision_analysis_retention_days"),
            settings_fields=("vision_analysis_retention_days",),
            category="vision",
        ),
    )
