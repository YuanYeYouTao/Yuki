"""Effect-gate behavior for generated replies versus direct plugin notifications."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.plugin_host.notification_delivery import (
    NotificationDeliveryReceipt,
    PluginNotificationOutboxWorker,
)
from qq_ai_bot.plugin_host.notification_repository import (
    TURN_ERROR_SUPERSEDED_COVERED,
    BackgroundTurnFenceError,
    OutboxRecord,
)
from qq_ai_bot.services.effect_gate import ConversationEffectGate


def _item(*, part_type: str) -> OutboxRecord:
    return OutboxRecord(
        id=1,
        notification_id="notification",
        source_event_id=9,
        plugin_id="fixture",
        target_type="private",
        target_id="1001",
        bot_user_id="8000",
        part_type=part_type,
        text="hello",
        media_handle_id=None,
        attempts=1,
        canonical_target_person_id="person-id",
        canonical_target_space_id=None,
        canonical_presence_id="presence-id",
        canonical_conversation_id="conversation-id",
    )


class _Repository:
    def __init__(self) -> None:
        self.first_agent_check = asyncio.Event()
        self.stale = False
        self.finishes: list[tuple[str, str | None]] = []
        self.retries: list[str] = []

    async def require_outbox_ready(self, _item: OutboxRecord) -> None:
        return None

    async def granted_canonical_creator(self, **_kwargs: object) -> str:
        return "creator"

    async def require_agent_reply_ready(self, _item: OutboxRecord) -> str:
        self.first_agent_check.set()
        if self.stale:
            raise BackgroundTurnFenceError(TURN_ERROR_SUPERSEDED_COVERED)
        return "private:8000:1001"

    async def finish_outbox(
        self,
        _item_id: int,
        *,
        attempt: int,
        status: str,
        platform_message_id: str | None = None,
        error_category: str | None = None,
    ) -> bool:
        assert attempt == 1
        self.finishes.append((status, error_category))
        return True

    async def retry_outbox(
        self,
        _item_id: int,
        *,
        attempt: int,
        error_category: str,
    ) -> bool:
        assert attempt == 1
        self.retries.append(error_category)
        return True


class _Transport:
    def __init__(self, receipt: NotificationDeliveryReceipt | None) -> None:
        self.receipt = receipt
        self.calls = 0

    async def send_text(self, **_kwargs: object) -> Any:
        self.calls += 1
        return self.receipt

    async def send_media(self, **_kwargs: object) -> Any:
        raise AssertionError("media path was not expected")


class _Ledger:
    def __init__(self) -> None:
        self.append_calls = 0

    async def get_event(self, _event_id: int) -> object:
        return SimpleNamespace(content="external summary")

    async def append(self, **_kwargs: object) -> tuple[object, bool]:
        self.append_calls += 1
        return (
            SimpleNamespace(
                author_kind=AuthorKind.YUKI.value,
                author_presence_id="presence-id",
                ingress_presence_id="presence-id",
                canonical_conversation_id="conversation-id",
            ),
            True,
        )


@pytest.mark.asyncio
async def test_reset_winning_shared_gate_suppresses_agent_reply() -> None:
    gate = ConversationEffectGate()
    repository = _Repository()
    transport = _Transport(
        NotificationDeliveryReceipt(
            message_id="1",
            sender_account_id="8000",
            external_target_id="1001",
            route_kind="person",
            presence_id="presence-id",
        )
    )
    worker = PluginNotificationOutboxWorker(
        repository=repository,  # type: ignore[arg-type]
        artifacts=SimpleNamespace(),  # type: ignore[arg-type]
        ledger=_Ledger(),  # type: ignore[arg-type]
        transport=transport,
        effect_gate=gate,
        effect_gate_timeout_seconds=1,
    )
    async with gate.hold("private:8000:1001", timeout_seconds=1):
        delivery = asyncio.create_task(worker._deliver(_item(part_type="agent_reply")))
        await asyncio.wait_for(repository.first_agent_check.wait(), timeout=1)
        repository.stale = True
    await delivery
    assert transport.calls == 0
    assert repository.finishes == []


@pytest.mark.asyncio
async def test_direct_notification_ignores_conversation_gate_and_requires_receipt() -> None:
    gate = ConversationEffectGate()
    repository = _Repository()
    ledger = _Ledger()
    transport = _Transport(
        NotificationDeliveryReceipt(
            message_id="2",
            sender_account_id="8000",
            external_target_id="1001",
            route_kind="person",
            presence_id="presence-id",
        )
    )
    worker = PluginNotificationOutboxWorker(
        repository=repository,  # type: ignore[arg-type]
        artifacts=SimpleNamespace(),  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        transport=transport,
        effect_gate=gate,
    )
    async with gate.hold("private:8000:1001", timeout_seconds=1):
        await asyncio.wait_for(worker._deliver(_item(part_type="text")), timeout=1)
    assert transport.calls == 1
    assert ledger.append_calls == 1
    assert repository.finishes == [("sent", None)]

    repository = _Repository()
    ledger = _Ledger()
    worker = PluginNotificationOutboxWorker(
        repository=repository,  # type: ignore[arg-type]
        artifacts=SimpleNamespace(),  # type: ignore[arg-type]
        ledger=ledger,  # type: ignore[arg-type]
        transport=_Transport(None),
        effect_gate=gate,
    )
    await worker._deliver(_item(part_type="text"))
    assert ledger.append_calls == 0
    assert repository.finishes == [("uncertain", "delivery_receipt_invalid")]
