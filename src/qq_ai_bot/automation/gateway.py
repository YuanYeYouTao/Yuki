"""Proactive OneBot gateway bound to one exact connected bot account."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry, RegistryClosed
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.routing import PresenceRouter, ResolvedSend, RouteSendError
from qq_ai_bot.persistence.repositories import AgentActionRepository, EventLedgerRepository

logger = logging.getLogger(__name__)


class ProactiveGatewayError(RuntimeError):
    """Sanitized proactive transport failure with stable category."""

    def __init__(self, category: str, *, uncertain: bool = False) -> None:
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


class ProactiveGateway(Protocol):
    @property
    def connected(self) -> bool: ...

    async def send_private(self, user_id: str, text: str) -> object: ...

    async def send_group(self, group_id: str, text: str) -> object: ...

    async def send_emoji(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        content: bytes,
        mime_type: str,
        emoji_id: str,
        summary: str,
    ) -> object: ...

    async def send_voice(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        local_path: str,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> object: ...

    async def call_api(self, action: str, params: dict[str, object]) -> object: ...


class OneBotProactiveGateway:
    """Send without a MessageEvent and persist successful outgoing QQ messages."""

    def __init__(
        self,
        *,
        bot_user_id: str,
        creator_user_id: str,
        automation_id: int,
        automation_run_id: int,
        ledger: EventLedgerRepository,
        actions: AgentActionRepository,
        registry: GatewayConnectionRegistry | None = None,
        router: PresenceRouter | None = None,
        target_person_id: str | None = None,
        target_space_id: str | None = None,
    ) -> None:
        self._bot_user_id = bot_user_id
        self._creator_user_id = creator_user_id
        self._automation_id = automation_id
        self._automation_run_id = automation_run_id
        self._ledger = ledger
        self._actions = actions
        self._registry = registry
        self._router = router
        self._target_person_id = target_person_id
        self._target_space_id = target_space_id

    @property
    def connected(self) -> bool:
        if self._registry is None:
            return False
        if self._target_person_id or self._target_space_id:
            return self._registry.has_any_active()
        return self._registry.has_unique_account(IDENTITY_PLATFORM, self._bot_user_id)

    async def send_private(self, user_id: str, text: str) -> object:
        result, resolved = await self._invoke(
            "send_private_msg", {"user_id": user_id, "message": text}
        )
        await self._record_message(
            result,
            resolved=resolved,
            scope_type=ScopeType.PRIVATE,
            private_peer_user_id=user_id,
            group_id=None,
            text=text,
        )
        return result

    async def send_group(self, group_id: str, text: str) -> object:
        result, resolved = await self._invoke(
            "send_group_msg", {"group_id": group_id, "message": text}
        )
        await self._record_message(
            result,
            resolved=resolved,
            scope_type=ScopeType.GROUP,
            private_peer_user_id=None,
            group_id=group_id,
            text=text,
        )
        return result

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        started = time.perf_counter()
        try:
            result, _resolved = await self._invoke(action, params)
        except ProactiveGatewayError as exc:
            await self._actions.record(
                actor_user_id=self._creator_user_id,
                action=action,
                success=False,
                duration_seconds=time.perf_counter() - started,
                error_category=exc.category,
            )
            raise
        await self._actions.record(
            actor_user_id=self._creator_user_id,
            action=action,
            success=True,
            duration_seconds=time.perf_counter() - started,
        )
        return result

    async def send_emoji(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        content: bytes,
        mime_type: str,
        emoji_id: str,
        summary: str,
    ) -> object:
        if (user_id is None) == (group_id is None):
            raise ProactiveGatewayError("invalid_emoji_target")
        encoded = base64.b64encode(content).decode("ascii")
        message = [
            {
                "type": "image",
                "data": {"file": f"base64://{encoded}", "sub_type": 1},
            }
        ]
        action = "send_group_msg" if group_id is not None else "send_private_msg"
        target_key = "group_id" if group_id is not None else "user_id"
        target_value = group_id if group_id is not None else user_id
        result, resolved = await self._invoke(
            action, {target_key: str(target_value), "message": message}
        )
        await self._record_media_message(
            result,
            resolved=resolved,
            user_id=user_id,
            group_id=group_id,
            emoji_id=emoji_id,
            mime_type=mime_type,
            summary=summary,
        )
        return result

    async def send_voice(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        local_path: str,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> object:
        if (user_id is None) == (group_id is None):
            raise ProactiveGatewayError("invalid_speech_target")
        content = await asyncio.to_thread(Path(local_path).read_bytes)
        encoded = base64.b64encode(content).decode("ascii")
        message = [{"type": "record", "data": {"file": f"base64://{encoded}"}}]
        del content, encoded
        action = "send_group_msg" if group_id is not None else "send_private_msg"
        target_key = "group_id" if group_id is not None else "user_id"
        target_value = group_id if group_id is not None else user_id
        result, resolved = await self._invoke(
            action, {target_key: str(target_value), "message": message}
        )
        await self._record_voice_message(
            result,
            resolved=resolved,
            user_id=user_id,
            group_id=group_id,
            spoken_text=spoken_text,
            generation_id=generation_id,
            profile_id=profile_id,
            reference_key=reference_key,
            duration_milliseconds=duration_milliseconds,
        )
        return result

    async def _invoke(self, action: str, params: dict[str, object]) -> tuple[object, ResolvedSend]:
        resolved = await self._resolve_route(action=action)
        if resolved is None:
            raise ProactiveGatewayError("bot_unavailable")
        bound = await self._bound_onebot_params(action, params, resolved)
        bot = resolved.connection.bot
        call_api = getattr(bot, "call_api", None)
        if bot is None or not callable(call_api):
            raise ProactiveGatewayError("bot_unavailable")
        try:
            return await call_api(action, **bound), resolved
        except Exception as exc:
            # Once the API invocation started, a transport break cannot prove whether
            # QQ accepted a send. The executor therefore never retries send actions.
            logger.error(
                "automation_onebot_failed automation_id=%d run_id=%d action=%s category=%s",
                self._automation_id,
                self._automation_run_id,
                action,
                type(exc).__name__,
            )
            raise ProactiveGatewayError(
                "onebot_transport_uncertain",
                uncertain=action in {"send_private_msg", "send_group_msg"},
            ) from exc

    async def _resolve_bot(self, *, action: str) -> Any | None:
        resolved = await self._resolve_route(action=action)
        if resolved is None:
            return None
        return resolved.connection.bot

    async def _resolve_route(self, *, action: str) -> ResolvedSend | None:
        group_action = "group" in action
        if self._target_person_id and self._target_space_id:
            raise ProactiveGatewayError("state_mismatch")
        if self._target_person_id or self._target_space_id:
            if self._router is None:
                raise ProactiveGatewayError("none")
            if self._target_person_id:
                if group_action:
                    raise ProactiveGatewayError("capability")
                try:
                    return await self._router.resolve_send_for_person(self._target_person_id)
                except RouteSendError as exc:
                    raise ProactiveGatewayError(exc.category) from exc
            if not group_action:
                raise ProactiveGatewayError("capability")
            try:
                return await self._router.resolve_send_for_space(self._target_space_id or "")
            except RouteSendError as exc:
                raise ProactiveGatewayError(exc.category) from exc
        if self._router is not None and await self._router.uses_canonical_send():
            raise ProactiveGatewayError("none")
        if self._registry is None:
            return None
        from qq_ai_bot.gateway.registry import require_capability

        try:
            resolution = self._registry.resolve_account(IDENTITY_PLATFORM, self._bot_user_id)
            require_capability(resolution, "send_group" if group_action else "send_private")
        except RegistryClosed:
            return None
        return ResolvedSend(
            presence_id=resolution.snapshot.presence_id or "",
            binding_id="",
            platform=resolution.snapshot.platform,
            external_target_id="",
            route_generation=resolution.snapshot.generation,
            connection=resolution,
            kind="account",
            sender_account_id=resolution.snapshot.external_account_id,
        )

    def _ledger_ids(
        self,
        *,
        resolved: ResolvedSend,
        private_peer_user_id: str | None,
        group_id: str | None,
    ) -> tuple[str, str | None, str | None]:
        if resolved.kind == "account":
            return self._bot_user_id, private_peer_user_id, group_id
        sender = resolved.sender_account_id
        if resolved.kind == "person":
            return sender, resolved.external_target_id, None
        return sender, None, resolved.external_target_id

    async def _record_message(
        self,
        result: object,
        *,
        resolved: ResolvedSend,
        scope_type: ScopeType,
        private_peer_user_id: str | None,
        group_id: str | None,
        text: str,
    ) -> None:
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        sender, peer, space = self._ledger_ids(
            resolved=resolved,
            private_peer_user_id=private_peer_user_id,
            group_id=group_id,
        )
        await self._ledger.append(
            bot_user_id=sender,
            platform_message_id=str(message_id or f"automation-{uuid.uuid4()}")[:128],
            scope_type=scope_type,
            sender_user_id=sender,
            direction="outbound",
            content=text,
            segments=({"type": "text", "data": {"text": text}},),
            group_id=space,
            private_peer_user_id=peer,
            sender_is_bot=True,
            origin="scheduled_automation",
            automation_id=self._automation_id,
            automation_run_id=self._automation_run_id,
        )

    async def _record_media_message(
        self,
        result: object,
        *,
        resolved: ResolvedSend,
        user_id: str | None,
        group_id: str | None,
        emoji_id: str,
        mime_type: str,
        summary: str,
    ) -> None:
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        content = f"[表情：{summary}]"
        sender, peer, space = self._ledger_ids(
            resolved=resolved,
            private_peer_user_id=user_id,
            group_id=group_id,
        )
        await self._ledger.append(
            bot_user_id=sender,
            platform_message_id=str(message_id or f"automation-{uuid.uuid4()}")[:128],
            scope_type=ScopeType.GROUP if group_id is not None else ScopeType.PRIVATE,
            sender_user_id=sender,
            direction="outbound",
            content=content,
            segments=(
                {
                    "type": "image",
                    "data": {
                        "emoji_id": emoji_id,
                        "mime_type": mime_type,
                        "summary": summary,
                    },
                },
            ),
            group_id=space,
            private_peer_user_id=peer,
            sender_is_bot=True,
            origin="scheduled_automation",
            automation_id=self._automation_id,
            automation_run_id=self._automation_run_id,
        )

    async def _record_voice_message(
        self,
        result: object,
        *,
        resolved: ResolvedSend,
        user_id: str | None,
        group_id: str | None,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> None:
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        sender, peer, space = self._ledger_ids(
            resolved=resolved,
            private_peer_user_id=user_id,
            group_id=group_id,
        )
        await self._ledger.append(
            bot_user_id=sender,
            platform_message_id=str(message_id or f"automation-{uuid.uuid4()}")[:128],
            scope_type=ScopeType.GROUP if group_id is not None else ScopeType.PRIVATE,
            sender_user_id=sender,
            direction="outbound",
            content=spoken_text,
            segments=(
                {
                    "type": "record",
                    "data": {
                        "generation_id": generation_id,
                        "profile_id": profile_id,
                        "reference_key": reference_key,
                        "duration_milliseconds": duration_milliseconds,
                    },
                },
            ),
            group_id=space,
            private_peer_user_id=peer,
            sender_is_bot=True,
            origin="scheduled_automation",
            automation_id=self._automation_id,
            automation_run_id=self._automation_run_id,
        )

    async def _bound_onebot_params(
        self,
        action: str,
        params: dict[str, object],
        resolved: ResolvedSend,
    ) -> dict[str, object]:
        bound = dict(params)
        if resolved.kind == "account":
            return bound
        if resolved.kind == "person":
            if "group" in action:
                raise ProactiveGatewayError("capability")
            raw = bound.get("user_id")
            if raw is not None:
                await self._require_person_owner(str(raw))
            bound["user_id"] = resolved.external_target_id
            return bound
        if "group" not in action:
            raise ProactiveGatewayError("capability")
        raw = bound.get("group_id")
        if raw is not None:
            await self._require_space_owner(str(raw))
        bound["group_id"] = resolved.external_target_id
        return bound

    async def _require_person_owner(self, external_id: str) -> None:
        if self._router is None or not self._target_person_id:
            raise ProactiveGatewayError("target_mismatch")
        allow_unknown = await self._router.uses_canonical_send()
        if not await self._router.person_owns_external(
            self._target_person_id,
            external_id,
            allow_unknown=allow_unknown,
        ):
            raise ProactiveGatewayError("target_mismatch")

    async def _require_space_owner(self, external_id: str) -> None:
        if self._router is None or not self._target_space_id:
            raise ProactiveGatewayError("target_mismatch")
        allow_unknown = await self._router.uses_canonical_send()
        if not await self._router.space_owns_external(
            self._target_space_id,
            external_id,
            allow_unknown=allow_unknown,
        ):
            raise ProactiveGatewayError("target_mismatch")


class FakeOneBotProactiveGateway:
    """Network-free test gateway with deterministic sent-message capture."""

    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected
        self.private_messages: list[tuple[str, str]] = []
        self.group_messages: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.emojis: list[tuple[str, str, str]] = []
        self.voices: list[tuple[str, str, str]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def send_private(self, user_id: str, text: str) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        self.private_messages.append((user_id, text))
        return {"message_id": len(self.private_messages)}

    async def send_group(self, group_id: str, text: str) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        self.group_messages.append((group_id, text))
        return {"message_id": len(self.group_messages)}

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        self.calls.append((action, params))
        return {"ok": True}

    async def send_emoji(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        content: bytes,
        mime_type: str,
        emoji_id: str,
        summary: str,
    ) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        scope = "group" if group_id is not None else "private"
        target = group_id if group_id is not None else user_id
        self.emojis.append((scope, str(target), emoji_id))
        return {"message_id": len(self.emojis)}

    async def send_voice(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        local_path: str,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        scope = "group" if group_id is not None else "private"
        target = group_id if group_id is not None else user_id
        self.voices.append((scope, str(target), profile_id))
        return {"message_id": len(self.voices)}
