"""Failure-isolated collection of bounded plugin admission signals."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from yuki.admin.models import RuntimeConfigSnapshot
from yuki.conversation.scope import plugin_conversation_key
from yuki.domain.messages import InboundMessage, mention_snapshots, snapshot_mention_segments
from yuki.plugin_host.canonical_projection import projection_from_inbound
from yuki.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from yuki.runtime.origin import TurnOrigin
from yuki_plugin_sdk.models import (
    AdmissionSignal as SdkAdmissionSignal,
)
from yuki_plugin_sdk.models import (
    AdmissionSignalContext,
    CurrentMessage,
)
from yuki_plugin_sdk.models import TurnOrigin as SdkTurnOrigin
from yuki_plugin_sdk.registrar import AdmissionSignalProvider, AdmissionSignalRegistration

InvocationScope = Callable[
    [str, InboundMessage, TurnOrigin, RuntimeConfigSnapshot],
    AbstractContextManager[object] | AbstractAsyncContextManager[object],
]

logger = logging.getLogger(__name__)


class PluginAdmissionSignalAdapter:
    """Collect plugin admission hints and project them as SDK AdmissionSignal.

    Admission signals only influence autonomous-group participation scoring.
    They must not change tool exposure, memory contracts, or authority.
    """

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        timeout_seconds: float = 3.0,
        invocation_scope: InvocationScope | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("admission signal timeout must be positive")
        self._registry = registry
        self._timeout = timeout_seconds
        self._invocation_scope = invocation_scope

    def configure_timeout(self, timeout_seconds: float) -> None:
        """Apply the HOT hook timeout to subsequent signal collections."""

        if timeout_seconds <= 0:
            raise ValueError("admission signal timeout must be positive")
        self._timeout = timeout_seconds

    async def collect(
        self,
        *,
        message: InboundMessage,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[SdkAdmissionSignal, ...]:
        identity = projection_from_inbound(message)
        signal_context = AdmissionSignalContext(
            conversation_key=plugin_conversation_key(message, message.scope()),
            origin=SdkTurnOrigin(origin.value),
            current=CurrentMessage(
                platform=message.platform,
                author_kind=message.author_kind,
                author_account_id=message.author_account_id,
                mentions=mention_snapshots(
                    snapshot_mention_segments(message.segments, message.mentions)
                ),
                native_message_id=message.native_message_id,
                sender_user_id=message.sender.user_id,
                scope_type=message.scope_type.value,
                group_id=message.group_id,
                text=message.text,
                received_at=message.received_at,
                **identity.sdk_fields(),
            ),
            **identity.sdk_fields(),
        )
        tasks = [
            self._collect_one(
                item.plugin_id,
                cast(AdmissionSignalRegistration, item.registration),
                message=message,
                origin=origin,
                runtime=runtime,
                signal_context=signal_context,
            )
            for item in self._registry.list(kind=ExtensionKind.ADMISSION_SIGNAL)
        ]
        if not tasks:
            return ()
        values = await asyncio.gather(*tasks)
        return tuple(value for value in values if value is not None)

    async def _collect_one(
        self,
        plugin_id: str,
        registration: AdmissionSignalRegistration,
        *,
        message: InboundMessage,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
        signal_context: AdmissionSignalContext,
    ) -> SdkAdmissionSignal | None:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._scope(plugin_id, message, origin, runtime):
                    signal = await _invoke_provider(registration.provider, signal_context)
        except Exception:
            logger.debug("plugin admission signal failed: plugin_id=%s", plugin_id, exc_info=True)
            return None
        if signal is None:
            return None
        expires = signal.expires_at
        if expires is not None:
            normalized = expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires
            if normalized <= datetime.now(UTC):
                return None
        return signal.model_copy(update={"source_plugin_id": plugin_id})

    @asynccontextmanager
    async def _scope(
        self,
        plugin_id: str,
        message: InboundMessage,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
    ) -> AsyncIterator[None]:
        if self._invocation_scope is None:
            yield
            return
        scope = self._invocation_scope(plugin_id, message, origin, runtime)
        if isinstance(scope, AbstractAsyncContextManager):
            async with scope:
                yield
            return
        with scope:
            yield


async def _invoke_provider(
    provider: AdmissionSignalProvider,
    context: AdmissionSignalContext,
) -> SdkAdmissionSignal | None:
    try:
        accepts_context = bool(inspect.signature(provider).parameters)
    except (TypeError, ValueError):
        accepts_context = True
    if accepts_context:
        contextual = cast(
            Callable[[AdmissionSignalContext], Awaitable[SdkAdmissionSignal | None]],
            provider,
        )
        return await contextual(context)
    parameterless = cast(Callable[[], Awaitable[SdkAdmissionSignal | None]], provider)
    return await parameterless()


__all__ = ["PluginAdmissionSignalAdapter"]
