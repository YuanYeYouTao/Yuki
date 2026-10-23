"""Host collection preserves validated SDK values and stamps their real source."""

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import make_settings

from yuki.admin.config_service import RuntimeConfigService
from yuki.automation.models import TurnOrigin
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.plugin_host.admission_adapter import PluginAdmissionSignalAdapter
from yuki.plugin_host.extension_registry import ExtensionRegistry
from yuki_plugin_sdk.models import AdmissionSignal
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.registrar import AdmissionSignalRegistration


@pytest.mark.parametrize(("score", "expired"), [(-10, False), (10, False), (10, True)])
async def test_signal_retains_full_sdk_summary_and_real_source(database, score, expired):
    summary = "完整插件摘要" * 82 + "末尾标记保留完整"
    signal = AdmissionSignal(
        source_plugin_id="untrusted.claim",
        score_delta=score,
        summary=summary,
        reason_code="test.signal",
        confidence=0.8,
        expires_at=datetime.now(UTC) + timedelta(days=-1 if expired else 1),
    )

    async def provider(context):
        assert context.current.text == "群消息"
        return signal

    registry = ExtensionRegistry()
    registry.registrar(
        "com.example.signal", (PluginPermission.ADMISSION_SIGNAL_REGISTER,)
    ).register_admission_signal(AdmissionSignalRegistration(name="signal", provider=provider))
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url), database=database
    ).snapshot()
    results = await PluginAdmissionSignalAdapter(registry).collect(
        message=InboundMessage(
            event_type="message",
            message_id="1",
            bot_user_id="8000",
            scope_type=ScopeType.GROUP,
            group_id="3001",
            sender=SenderIdentity(user_id="1001"),
            text="群消息",
            received_at=datetime.now(UTC),
        ),
        origin=TurnOrigin.AUTONOMOUS_GROUP,
        runtime=runtime,
    )
    if expired:
        assert results == ()
    else:
        (result,) = results
        assert result.source_plugin_id == "com.example.signal"
        assert result.summary == summary
        assert result.score_delta == score and result.confidence == signal.confidence
