"""One original SOUL file, atomic publication and original control receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.requests import personal_command
from yuki.config import Settings
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.models import AdminOperationEventModel
from yuki.services.media_resolver import _media_io


def document_revision(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _source(path: Path) -> tuple[bytes, float]:
    return path.read_bytes(), path.stat().st_mtime


def _writable(path: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=path.parent):
            pass
    except OSError:
        return False
    return True


def _sync(path: Path) -> None:
    for target in (path,) if os.name == "nt" else (path, path.parent):
        descriptor = os.open(target, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _replace(path: Path, expected_revision: str, content: bytes) -> None:
    def check() -> None:
        if document_revision(path.read_bytes()) != expected_revision:
            raise ClientError("version_conflict")

    check()
    descriptor, temporary = tempfile.mkstemp(prefix=".soul-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name != "nt":
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        check()
        os.replace(temporary, path)
        _sync(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CharacterSoul:
    def __init__(self, identity: ClientIdentityService, settings: Settings) -> None:
        self.identity, self.settings = identity, settings
        self.lock = asyncio.Lock()
        self.revision = 0  # Disposable change hint; source/active hashes are facts.

    async def _actor(
        self, session: Any, principal: ClientPrincipal | None, operator_id: str | None
    ) -> ClientPrincipal | None:
        if principal is not None:
            return await self.identity.require_principal(session, principal)
        assert operator_id is not None
        return None

    async def _document(self) -> dict[str, Any]:
        path = self.settings.soul_file
        try:
            content, modified = await _media_io(_source, path)
            decoded = content.decode("utf-8")
        except UnicodeError as exc:
            raise ClientError("validation_error") from exc
        except OSError as exc:
            raise ClientError("unavailable") from exc
        writable = await _media_io(_writable, path)
        revision = document_revision(content)
        async with self.identity.database.read_session() as reader:
            audit = await reader.scalar(
                select(AdminOperationEventModel)
                .join(
                    ControlCommandReceiptModel,
                    ControlCommandReceiptModel.audit_id == AdminOperationEventModel.id,
                )
                .where(
                    ControlCommandReceiptModel.operation_ref == "character_soul",
                    ControlCommandReceiptModel.status == "succeeded",
                    func.json_extract(ControlCommandReceiptModel.effective_state_json, "$.revision")
                    == revision,
                )
                .order_by(
                    ControlCommandReceiptModel.updated_at.desc(),
                    ControlCommandReceiptModel.id.desc(),
                )
                .limit(1)
            )
            updated_by = json.loads(audit.before_json)["updated_by"] if audit else "external"
        return {
            "file_name": path.name,
            "content": decoded,
            "revision": revision,
            "active_revision": self.settings.active_soul_revision,
            "updated_at": datetime.fromtimestamp(modified, UTC).isoformat(),
            "updated_by": updated_by,
            "writable": writable,
            "reason": None if writable else "directory_not_writable",
        }

    async def read(
        self, *, principal: ClientPrincipal | None = None, operator_id: str | None = None
    ) -> dict[str, Any]:
        async with self.identity.database.read_session() as session:
            await self._actor(session, principal, operator_id)
        async with self.lock:
            return await self._document()

    async def _reserve(
        self,
        request_id: str,
        bound: str,
        *,
        principal: ClientPrincipal | None,
        operator_id: str | None,
        reload: bool = False,
    ) -> tuple[int, dict[str, Any] | None, bool]:
        reference = "character_soul:reload" if reload else "character_soul"
        async with self.identity.database.immediate_session() as session:
            current = await self._actor(session, principal, operator_id)
            issuer = current.device_id if current else operator_id
            assert issuer is not None
            old = (
                await personal_command(
                    session,
                    current.request_person_id,
                    request_id,
                    conversation_id=current.request_conversation_id,
                )
                if current
                else await session.scalar(
                    select(ControlCommandReceiptModel).where(
                        ControlCommandReceiptModel.principal_id == issuer,
                        ControlCommandReceiptModel.request_id == request_id,
                    )
                )
            )
            if old is not None:
                if (
                    old.payload_hash != bound
                    or old.operation_kind != "control"
                    or old.operation_ref != reference
                ):
                    raise ClientError("request_conflict")
                if old.status in {"succeeded", "failed"}:
                    return (
                        old.id,
                        await self.identity.replay(
                            session, issuer, request_id, bound, principal=current
                        ),
                        False,
                    )
                return old.id, None, False
            await self.identity.replay(session, issuer, request_id, bound, principal=current)
            row = await self.identity.receipts.record_personal_command(
                session,
                principal_id=issuer,
                principal_kind="device" if current else "control",
                person_id=current.person_id if current else issuer,
                request_id=request_id,
                operation="control",
                operation_ref=reference,
                payload_hash=bound,
                resource_id=reference,
                result={},
                request_person_id=current.request_person_id if current else None,
                request_conversation_id=current.request_conversation_id if current else None,
            )
            row.status, row.effective_state_json, row.result_resource_id = "running", None, None
            audit = await session.get(AdminOperationEventModel, row.audit_id)
            assert audit is not None
            device = await session.get(ClientDeviceModel, current.device_id) if current else None
            audit.operation, audit.capability = "client.character_soul", "client.character_soul"
            audit.before_json = json.dumps(
                {"updated_by": f"device:{device.label}" if device else "operator"}
            )
            audit.after_json, audit.success, audit.error_category = (
                '{"status":"running"}',
                False,
                "in_progress",
            )
            return row.id, None, True

    async def _finish(
        self, row_id: int, result: dict[str, Any], problem: str | None = None
    ) -> None:
        async with self.identity.database.immediate_session() as session:
            saved = await session.get(ControlCommandReceiptModel, row_id)
            if saved is None:
                raise ClientError("not_found")
            previous = await session.get(AdminOperationEventModel, saved.audit_id)
            assert previous is not None
            now = datetime.now(UTC)
            audit = AdminOperationEventModel(
                **{
                    field: getattr(previous, field)
                    for field in (
                        "actor_user_id",
                        "actor_principal_kind",
                        "actor_principal_id",
                        "control_request_id",
                        "trigger_message_id",
                        "conversation_key",
                        "capability",
                        "operation",
                        "target_type",
                        "target_id",
                    )
                },
                before_json=previous.before_json,
                after_json=json.dumps(result),
                success=problem is None,
                error_category=problem,
                duration_seconds=0,
                created_at=now,
            )
            session.add(audit)
            await session.flush()
            saved.audit_id, saved.updated_at = audit.id, now
            saved.status, saved.problem_code = (
                ("failed", problem) if problem else ("succeeded", None)
            )
            saved.result_resource_id = saved.operation_ref if problem is None else None
            saved.effective_state_json = json.dumps(result) if problem is None else None

    async def save(
        self,
        request_id: str,
        expected_revision: str,
        content: str,
        *,
        principal: ClientPrincipal | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            data = content.encode("utf-8")
        except UnicodeError as exc:
            raise ClientError("validation_error") from exc
        bound = fingerprint(
            "client.character_soul",
            "soul",
            {"expected_revision": expected_revision, "content": content},
        )
        async with self.lock:
            row_id, original, created = await self._reserve(
                request_id, bound, principal=principal, operator_id=operator_id
            )
            if original is not None:
                return original
            try:
                original_bytes, _modified = await _media_io(_source, self.settings.soul_file)
            except OSError as exc:
                raise ClientError("unknown") from exc
            current_revision = document_revision(original_bytes)
            revision = document_revision(data)
            if current_revision != expected_revision and (created or current_revision != revision):
                details = {"current_revision": current_revision, "written": False}
                await self._finish(row_id, details, "version_conflict")
                raise ClientError("version_conflict", details=details)
            try:
                async with self.identity.database.read_session() as reader:
                    await self._actor(reader, principal, operator_id)
                if current_revision != revision:
                    await _media_io(_replace, self.settings.soul_file, expected_revision, data)
                    self.revision += 1
                else:
                    await _media_io(_sync, self.settings.soul_file)
            except ClientError as exc:
                actual, _mtime = await _media_io(_source, self.settings.soul_file)
                details = {"current_revision": document_revision(actual), "written": False}
                await self._finish(row_id, details, exc.code)
                raise ClientError(exc.code, details=details) from exc
            except OSError as exc:
                raise ClientError("unknown") from exc
            return await self._activate(row_id, revision=revision, written=True)

    async def _activate(
        self, row_id: int, *, revision: str | None, written: bool
    ) -> dict[str, Any]:
        try:
            document = await _media_io(self.settings.read_soul_document)
        except (ValueError, OSError) as exc:
            details = {
                "written": written,
                "active_revision": self.settings.active_soul_revision,
                "reason": "soul_document_invalid",
            }
            await self._finish(row_id, details, "precondition_failed")
            raise ClientError("precondition_failed", details=details) from exc
        if revision is not None and document.revision != revision:
            details = {"current_revision": document.revision, "written": written}
            await self._finish(row_id, details, "version_conflict")
            raise ClientError("version_conflict", details=details)
        self.settings.apply_soul_document(document)
        self.revision += 1
        result = {
            "revision": document.revision,
            "active_revision": self.settings.active_soul_revision,
            "applied_at": datetime.now(UTC).isoformat(),
        }
        await self._finish(row_id, result)
        return result

    async def reload(
        self,
        request_id: str,
        *,
        principal: ClientPrincipal | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            row_id, original, _created = await self._reserve(
                request_id,
                fingerprint("client.character_soul.reload", "soul", {}),
                principal=principal,
                operator_id=operator_id,
                reload=True,
            )
            return (
                original
                if original is not None
                else await self._activate(row_id, revision=None, written=False)
            )
