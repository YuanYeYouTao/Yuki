"""Reviewed configuration for incoming voice, separate from TTS preferences."""

from qq_ai_bot.admin.config_spec_helpers import _configured, _field, _spec
from qq_ai_bot.admin.models import ConfigApplyMode, ConfigSpec


def asr_config_specs() -> tuple[ConfigSpec, ...]:
    return (
        *(
            _spec(
                "asr." + name,
                display,
                description,
                value_type=kind,
                mode=ConfigApplyMode.RESTART_REQUIRED,
                category="asr",
                getter=_field("asr_" + name),
                settings_fields=("asr_" + name,),
                env_alias="ASR_" + name.upper(),
            )
            for name, display, description, kind in (
                (
                    "enabled",
                    "语音识别开关",
                    "自动识别触发回复的语音消息；独立于发送语音开关。",
                    "boolean",
                ),
                ("base_url", "语音识别地址", "留空时与 API Key 一起复用现有千问连接。", "string"),
                ("model", "语音识别模型", "使用千问 ASR 模型，不使用聊天或 TTS 模型。", "string"),
                (
                    "timeout_seconds",
                    "语音识别超时",
                    "包含排队、下载、转码与识别的总时限。",
                    "number",
                ),
                ("max_download_bytes", "语音下载上限", "单个语音允许下载的最大字节数。", "integer"),
                (
                    "max_duration_seconds",
                    "语音时长上限",
                    "单个语音最大秒数，不超过 300。",
                    "integer",
                ),
                ("global_concurrency", "语音识别并发", "同时处理的语音消息数量。", "integer"),
                (
                    "queue_max_pending",
                    "语音队列容量",
                    "包含执行中的最大待处理消息数量。",
                    "integer",
                ),
            )
        ),
        _spec(
            "asr.api_key",
            "语音识别 API Key",
            "仅显示配置状态；留空复用千问连接。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            category="secret",
            getter=_configured("asr_api_key"),
            sensitive=True,
        ),
    )
