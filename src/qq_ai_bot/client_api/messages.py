"""Personal requests enter through App Gateway, never by writing an API ledger."""

import json
from typing import Any

from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.identity import ClientIdentityService, ClientPrincipal
from qq_ai_bot.client_api.requests import ClientRequests, personal_command
from qq_ai_bot.gateway.providers.app.ingress import AppGateway, AppMessageRejected
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.identity.errors import CanonicalIdentityError


class ClientMessages:
    def __init__(self, identity: ClientIdentityService, gateway: AppGateway) -> None:
        self.identity, self.gateway = identity, gateway
        self.requests = ClientRequests(identity)

    async def _previous(
        self,
        principal: ClientPrincipal,
        request_id: str,
        text: str,
        segments: tuple[dict[str, Any], ...],
    ) -> dict[str, Any] | None:
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_device(session, principal.device_id)
            if await personal_command(session, principal.person_id, request_id) is not None:
                raise ClientError("request_conflict")
            event = await self.requests.message(session, principal, request_id)
            if event is None:
                return None
            payload_segments = [
                part for part in json.loads(event.segments_json) if part["type"] != "yuki_context"
            ]
            if event.content != text or payload_segments != list(segments):
                raise ClientError("request_conflict")
            return {
                "event_id": event.id,
                "conversation_id": event.canonical_conversation_id,
                "replay": True,
            }

    async def submit(
        self,
        principal: ClientPrincipal,
        request_id: str,
        text: str,
        attachments: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        text = text.strip()
        if attachments:
            raise ClientError("not_found")
        if not text:
            raise ClientError("validation_error")
        segments = ({"type": "text", "data": {"text": text}},)
        previous = await self._previous(principal, request_id, text, segments)
        if previous is not None:
            return previous
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_device(session, principal.device_id)
            binding = await session.get(IdentityBindingModel, principal.binding_id)
            assert binding is not None
        try:
            appended = await self.gateway.receive(
                binding, principal.conversation_id, request_id, text, segments
            )
        except AppMessageRejected as exc:
            raise ClientError(
                "unauthenticated"
                if exc.reason in {"private_not_allowed", "canonical_owner_disabled"}
                else "unavailable"
            ) from exc
        except CanonicalIdentityError as exc:
            if exc.category != "receipt_conflict":
                raise
            previous = await self._previous(principal, request_id, text, segments)
            if previous is None:
                raise ClientError("request_conflict") from exc
            return previous
        return {
            "event_id": appended.event.id,
            "conversation_id": appended.event.canonical_conversation_id,
            "replay": not appended.created,
        }
