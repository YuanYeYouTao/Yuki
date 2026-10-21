"""Content-addressed personal media, registered by original command receipts."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.authority.json_types import JsonValue
from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.conversation.media_service import ConversationMediaService
from yuki.identity.person_identity import person_family_condition
from yuki.services.media_resolver import _media_io


async def owned_upload(session: AsyncSession, person_id: str, media_id: str) -> dict[str, Any]:
    import json

    row = await session.scalar(
        select(ControlCommandReceiptModel)
        .join(ClientDeviceModel, ClientDeviceModel.id == ControlCommandReceiptModel.principal_id)
        .where(
            person_family_condition(ClientDeviceModel.person_id, person_id),
            ControlCommandReceiptModel.operation_kind == "client.upload",
            ControlCommandReceiptModel.result_resource_id == media_id,
            ControlCommandReceiptModel.status == "succeeded",
        )
    )
    if row is None or row.effective_state_json is None:
        raise ClientError("not_found")
    result: dict[str, Any] = json.loads(row.effective_state_json)
    if result["expires_at"] <= datetime.now(UTC).timestamp():
        raise ClientError("not_found")
    return result


class ConversationUploads:
    def __init__(self, identity: ClientIdentityService, media: ConversationMediaService) -> None:
        self.identity, self.media = identity, media
        self.root = media.root / "uploads"

    def path(self, sha256: str) -> Path:
        return self.root / f"{sha256}.blob"

    async def upload(
        self,
        principal: ClientPrincipal,
        request_id: str,
        sha256: str,
        size: int,
        filename: str,
        kind: str,
        chunks: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        if (
            not filename
            or any(char in filename for char in "\\/:\x00\r\n")
            or kind not in {"image", "video", "file", "record"}
            or size <= 0
            or len(sha256) != 64
            or any(c not in "0123456789abcdef" for c in sha256)
        ):
            raise ClientError("validation_error")
        operation = "client.upload"
        bound = fingerprint(
            operation,
            principal.request_scope,
            {"sha256": sha256, "size": size, "filename": filename, "kind": kind},
        )
        async with self.identity.database.sessions() as session:
            principal = await self.identity.require_principal(session, principal)
            previous = await self.identity.replay(
                session, principal.device_id, request_id, bound, principal=principal
            )
            if previous is not None:
                return previous
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.root / f"{uuid4().hex}.part"
        digest = hashlib.sha256()
        length = 0
        self.media._staging_paths.add(temporary)
        try:
            output = await _media_io(temporary.open, "wb")
            try:
                async for chunk in chunks:
                    length += len(chunk)
                    if length > size:
                        raise ClientError("validation_error")
                    digest.update(chunk)
                    await _media_io(output.write, chunk)
            finally:
                await _media_io(output.close)
            if length != size or digest.hexdigest() != sha256:
                raise ClientError("validation_error")
            async with self.media._lock:
                destination = self.path(sha256)
                if not destination.exists():
                    await _media_io(os.replace, temporary, destination)
                result: dict[str, JsonValue] = {
                    "media_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"yuki:upload:{principal.request_scope}:{request_id}",
                        )
                    ),
                    "sha256": sha256,
                    "size": size,
                    "filename": filename,
                    "kind": kind,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=24)).timestamp(),
                }
                async with self.identity.database.immediate_session() as writer:
                    principal = await self.identity.require_principal(writer, principal)
                    previous = await self.identity.replay(
                        writer, principal.device_id, request_id, bound, principal=principal
                    )
                    if previous is not None:
                        return previous
                    await self.identity.receipts.record_personal_command(
                        writer,
                        principal_id=principal.device_id,
                        principal_kind="device",
                        person_id=principal.person_id,
                        request_id=request_id,
                        operation=operation,
                        operation_ref=principal.request_scope,
                        payload_hash=bound,
                        resource_id=str(result["media_id"]),
                        result=result,
                        request_person_id=principal.request_person_id,
                        request_conversation_id=principal.request_conversation_id,
                    )
            return {**result, "replay": False}
        finally:
            try:
                await _media_io(partial(temporary.unlink, missing_ok=True))
            finally:
                self.media._staging_paths.discard(temporary)
