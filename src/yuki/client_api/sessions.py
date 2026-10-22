"""ACP entry sessions reuse canonical Conversations and original command receipts."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from yuki.admin.authority.json_types import JsonValue
from yuki.client_api.db_models import ACP_ACCOUNT_ID, ACP_PLATFORM
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import find_presence
from yuki.identity.db_models import IdentityBindingModel


class ClientSessions:
    def __init__(self, identity: ClientIdentityService) -> None:
        self.identity = identity

    async def create(self, principal: ClientPrincipal, request_id: str) -> dict[str, Any]:
        async with self.identity.database.immediate_session() as session:
            principal = await self.identity.require_device(
                session, principal.device_id, binding_id=principal.binding_id
            )
            operation = "client.acp_session_new"
            bound = fingerprint(operation, principal.binding_id, {})
            previous = await self.identity.replay(
                session, principal.device_id, request_id, bound, principal=principal
            )
            if previous is not None:
                await self.identity.require_device(
                    session, principal.device_id, conversation_id=previous["conversation_id"]
                )
                return previous
            presence = await find_presence(session, ACP_ACCOUNT_ID, platform=ACP_PLATFORM)
            if presence is None:
                raise ClientError("unavailable")
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.person_id == principal.request_person_id,
                    IdentityBindingModel.platform == ACP_PLATFORM,
                    IdentityBindingModel.status != "ended",
                )
            )
            if binding is None:
                binding = await self.identity._binding(
                    session,
                    person_id=principal.request_person_id,
                    platform=ACP_PLATFORM,
                    account=str(uuid4()),
                    display_name="",
                    now=datetime.now(UTC),
                )
            if binding.status != "active":
                raise ClientError("precondition_failed")
            thread_id = str(uuid4())
            scope = ConversationScope.private(
                ACP_ACCOUNT_ID,
                binding.external_account_id,
                platform=ACP_PLATFORM,
                presence_id=presence.id,
                thread_id=thread_id,
            )
            entry = await ensure_canonical_conversation(
                session,
                kind="private",
                primary_scope_key=scope.key,
                identity_binding_id=binding.id,
                person_id=binding.person_id,
                presence_id=presence.id,
                thread_id=thread_id,
            )
            result: dict[str, JsonValue] = {
                "session_id": entry.conversation_id,
                "conversation_id": entry.conversation_id,
                "binding_id": binding.id,
                "platform": ACP_PLATFORM,
                "generation": entry.generation,
            }
            await self.identity.receipts.record_personal_command(
                session,
                principal_id=principal.device_id,
                principal_kind="device",
                person_id=principal.person_id,
                request_person_id=principal.request_person_id,
                request_id=request_id,
                operation=operation,
                operation_ref=principal.binding_id,
                payload_hash=bound,
                resource_id=entry.conversation_id,
                result=result,
            )
            return {**result, "replay": False}

    async def load(self, principal: ClientPrincipal, conversation_id: str) -> dict[str, Any]:
        async with self.identity.database.read_session() as session:
            current = await self.identity.require_device(
                session,
                principal.device_id,
                binding_id=principal.binding_id if principal.platform == ACP_PLATFORM else None,
                conversation_id=conversation_id,
            )
            conversation = await session.get(CanonicalConversationModel, current.conversation_id)
            assert conversation is not None
            return {
                "session_id": conversation.id,
                "conversation_id": conversation.id,
                "binding_id": current.binding_id,
                "platform": current.platform,
                "generation": conversation.generation,
            }
