"""Personal requests enter through App Gateway, never by writing an API ledger."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuki.client_api.files import ClientFiles
    from yuki.client_api.prompts import ClientPrompts

from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.requests import ClientRequests, personal_command
from yuki.domain.messages import MessageAttachment
from yuki.gateway.providers.app.ingress import AppGateway, AppMessageRejected
from yuki.identity.db_models import IdentityBindingModel
from yuki.identity.errors import CanonicalIdentityError


class ClientMessages:
    def __init__(
        self,
        identity: ClientIdentityService,
        gateway: AppGateway | Mapping[str, AppGateway],
        files: ClientFiles | None = None,
        *,
        prompts: ClientPrompts | None = None,
    ) -> None:
        self.identity = identity
        self.gateways = (
            dict(gateway) if isinstance(gateway, Mapping) else {gateway.platform: gateway}
        )
        self.requests = ClientRequests(identity, prompts=prompts)
        self.files = files

    async def _previous(
        self,
        principal: ClientPrincipal,
        request_id: str,
        text: str,
        references: tuple[str, ...],
    ) -> dict[str, Any] | None:
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_principal(session, principal)
            command = await personal_command(
                session,
                principal.request_person_id,
                request_id,
                conversation_id=principal.request_conversation_id,
            )
            if command is not None and command.operation_kind != "client.message":
                raise ClientError("request_conflict")
            event = await self.requests.message(session, principal, request_id)
            if event is None:
                if command is not None:
                    raise ClientError("not_found")
                return None
            payload_segments = [
                part for part in json.loads(event.segments_json) if part["type"] != "yuki_context"
            ]
            original_references = tuple(
                part.get("data", {}).get("media_id") for part in payload_segments[1:]
            )
            rendered = text + "".join(
                f" [{part['type']}附件{index}：{part['data']['filename']}]"
                for index, part in enumerate(payload_segments[1:])
            )
            if (
                not payload_segments
                or payload_segments[0] != {"type": "text", "data": {"text": text}}
                or original_references != references
                or event.content != rendered
            ):
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
        if not text and not attachments:
            raise ClientError("validation_error")
        segments = ({"type": "text", "data": {"text": text}},)
        previous = await self._previous(principal, request_id, text, attachments)
        if previous is not None:
            return previous
        transient: tuple[MessageAttachment, ...] = ()
        rendered = text
        if attachments:
            if self.files is None:
                raise ClientError("unavailable")
            media_segments, transient = await self.files.attachments(principal, attachments)
            segments += media_segments
            rendered += "".join(
                f" [{part['type']}附件{index}：{part['data']['filename']}]"
                for index, part in enumerate(media_segments)
            )
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_principal(session, principal)
            binding = await session.get(IdentityBindingModel, principal.binding_id)
            assert binding is not None
        try:
            appended = await self.gateways[principal.platform].receive(
                binding,
                principal.conversation_id,
                request_id,
                rendered,
                segments,
                transient,
                principal=principal,
                thread_id=principal.thread_id,
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
            previous = await self._previous(principal, request_id, text, attachments)
            if previous is None:
                raise ClientError("request_conflict") from exc
            return previous
        if not appended.created:
            previous = await self._previous(principal, request_id, text, attachments)
            if previous is None:
                raise ClientError("request_conflict")
            return previous
        return {
            "event_id": appended.event.id,
            "conversation_id": appended.event.canonical_conversation_id,
            "replay": not appended.created,
        }
