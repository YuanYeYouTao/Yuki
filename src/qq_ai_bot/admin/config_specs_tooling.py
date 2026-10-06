"""Unified Tool Kernel runtime configuration declarations."""

from __future__ import annotations

from qq_ai_bot.admin.config_spec_helpers import _G, _field, _spec
from qq_ai_bot.admin.models import ConfigSpec


def tooling_config_specs() -> tuple[ConfigSpec, ...]:
    return (
        _spec(
            "tooling.max_parallel_calls",
            "工具并发数",
            "同一 Agent 工具批次允许并发执行的 parallel_safe 工具数。",
            env_alias="TOOLING_MAX_PARALLEL_CALLS",
            getter=_field("tooling_max_parallel_calls"),
            settings_fields=("tooling_max_parallel_calls",),
            category="tooling",
            value_type="integer",
            minimum=1,
            scopes=_G,
        ),
        *(
            _spec(
                f"tooling.{name}",
                display,
                description,
                env_alias=f"TOOLING_{name.upper()}",
                getter=_field(f"tooling_{name}"),
                settings_fields=(f"tooling_{name}",),
                category="tooling",
                value_type="integer",
                minimum=1,
                scopes=_G,
            )
            for name, display, description in (
                ("result_token_budget", "工具结果 Token 预算", "为空时不额外限制统一工具结果。"),
                ("result_item_limit", "工具结果条目预算", "为空时不额外限制结构化结果条目。"),
            )
        ),
        _spec(
            "tooling.result_artifact_enabled",
            "超长结果 Artifact",
            "是否把超出模型预算的完整工具结果写入短期 Artifact。",
            value_type="boolean",
            scopes=_G,
            env_alias="TOOLING_RESULT_ARTIFACT_ENABLED",
            getter=_field("tooling_result_artifact_enabled"),
            settings_fields=("tooling_result_artifact_enabled",),
            category="tooling",
        ),
        _spec(
            "tooling.result_artifact_retention_seconds",
            "Artifact 保留时间",
            "完整工具结果 Artifact 的保留秒数。",
            env_alias="TOOLING_RESULT_ARTIFACT_RETENTION_SECONDS",
            getter=_field("tooling_result_artifact_retention_seconds"),
            settings_fields=("tooling_result_artifact_retention_seconds",),
            category="tooling",
            value_type="integer",
            minimum=1,
            scopes=_G,
        ),
    )
