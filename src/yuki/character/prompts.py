"""Original prompt files, Settings activation and durable control-command recovery."""

import asyncio
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.requests import personal_command
from yuki.config import Settings, prompt_document_revision
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.models import AdminOperationEventModel
from yuki.services.media_resolver import _media_io

PromptKey = Literal["system_prompt", "persona"]
PLACEHOLDER = "{{YUKI_PERSONA_CORE}}"
KEYS: tuple[PromptKey, ...] = ("system_prompt", "persona")


def _source(path: Path) -> tuple[bytes, float]:
    content = path.read_bytes()
    return content, path.stat().st_mtime


def _writable(path: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=path.parent):
            pass
    except OSError:
        return False
    return True


def _sync(path: Path) -> None:
    for target in (path, path.parent):
        descriptor = os.open(target, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _replace(path: Path, expected_revision: str, content: bytes) -> None:
    def check() -> None:
        if prompt_document_revision(path.read_bytes()) != expected_revision:
            raise ClientError("version_conflict")

    check()
    descriptor, temporary = tempfile.mkstemp(prefix=".yuki-prompt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        check()
        os.replace(temporary, path)
        _sync(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CharacterPrompts:
    def __init__(self, identity: ClientIdentityService, settings: Settings) -> None:
        self.identity, self.settings = identity, settings
        self.lock = asyncio.Lock()
        self.revision = 0  # Disposable SSE hint; source hashes remain authoritative.

    def path(self, key: PromptKey) -> Path | None:
        return (
            self.settings.system_prompt_file
            if key == "system_prompt"
            else self.settings.resolved_bot_persona_file
        )

    async def _actor(
        self,
        session: AsyncSession,
        principal: ClientPrincipal | None,
        operator_id: str | None,
    ) -> ClientPrincipal | None:
        if principal is not None:
            principal = await self.identity.require_principal(session, principal)
            if not principal.is_owner:
                raise ClientError("owner_required")
        else:
            assert operator_id is not None
        return principal

    async def _document(self, key: PromptKey) -> dict[str, Any]:
        reason: str | None
        path = self.path(key)
        if path is None:
            content, modified = self.settings.system_prompt_template.encode("utf-8"), None
            writable, reason = False, "system_prompt_from_environment"
        else:
            try:
                content, modified = await _media_io(_source, path)
            except OSError as exc:
                raise ClientError("unavailable", details={"key": key}) from exc
            writable = await _media_io(_writable, path)
            reason = None if writable else "directory_not_writable"
        try:
            decoded = content.decode("utf-8")
        except UnicodeError as exc:
            raise ClientError("validation_error", details={"key": key}) from exc
        revision = prompt_document_revision(content)
        async with self.identity.database.sessions() as reader:
            audit = await reader.scalar(
                select(AdminOperationEventModel)
                .join(
                    ControlCommandReceiptModel,
                    ControlCommandReceiptModel.audit_id == AdminOperationEventModel.id,
                )
                .where(
                    ControlCommandReceiptModel.operation_ref == f"character_prompt:{key}",
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
            "key": key,
            "file_name": path.name if path else "SYSTEM_PROMPT",
            "content": decoded,
            "revision": revision,
            "updated_at": datetime.fromtimestamp(modified, UTC).isoformat()
            if modified is not None
            else None,
            "updated_by": updated_by,
            "writable": writable,
            "reason": reason,
        }

    async def read(
        self,
        *,
        principal: ClientPrincipal | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.identity.database.sessions() as session:
            await self._actor(session, principal, operator_id)
        async with self.lock:
            return {
                "documents": [await self._document(key) for key in KEYS],
                "placeholder": PLACEHOLDER,
                "active": self.settings.active_prompt_revisions,
            }

    async def _reserve(
        self,
        request_id: str,
        key: str,
        bound: str,
        *,
        principal: ClientPrincipal | None,
        operator_id: str | None,
    ) -> tuple[int, dict[str, Any] | None, bool]:
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
            reference = f"character_prompt:{key}"
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
            audit.operation, audit.capability = "client.character_prompt", "client.character_prompt"
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
        key: PromptKey,
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
        if not content.strip():
            raise ClientError("validation_error")
        bound = fingerprint(
            "client.character_prompt",
            key,
            {"expected_revision": expected_revision, "content": content},
        )
        async with self.lock:
            row_id, original, created = await self._reserve(
                request_id, key, bound, principal=principal, operator_id=operator_id
            )
            if original is not None:
                return original
            document = await self._document(key)
            path = self.path(key)
            if path is None or not document["writable"]:
                details = {"reason": document["reason"], "written": False}
                await self._finish(row_id, details, "precondition_failed")
                raise ClientError("precondition_failed", details=details)
            revision = prompt_document_revision(data)
            if document["revision"] != expected_revision and (
                created or document["revision"] != revision
            ):
                details = {"current": document, "written": False}
                await self._finish(row_id, details, "version_conflict")
                raise ClientError("version_conflict", details=details)
            try:
                async with self.identity.database.sessions() as reader:
                    await self._actor(reader, principal, operator_id)
                if document["revision"] != revision:
                    await _media_io(_replace, path, expected_revision, data)
                    self.revision += 1
                else:
                    await _media_io(_sync, path)
            except ClientError as exc:
                details = {"current": await self._document(key), "written": False}
                await self._finish(row_id, details, exc.code)
                raise ClientError(exc.code, details=details) from exc
            except OSError as exc:
                # A rename may already have happened; only the original file hash
                # and receipt can settle this request on its next submission.
                raise ClientError("unknown") from exc
            return await self._activate(row_id, revision=revision, written=True, key=key)

    async def _activate(
        self,
        row_id: int,
        *,
        revision: str | None,
        written: bool,
        key: PromptKey | None = None,
    ) -> dict[str, Any]:
        try:
            rendered = await _media_io(self.settings.read_prompt_documents)
        except (ValueError, OSError) as exc:
            details = {
                "written": written,
                "active": self.settings.active_prompt_revisions,
                "reason": "prompt_documents_invalid",
            }
            await self._finish(row_id, details, "precondition_failed")
            raise ClientError("precondition_failed", details=details) from exc
        if key is not None and rendered.revisions[key] != revision:
            details = {"current": await self._document(key), "written": written}
            await self._finish(row_id, details, "version_conflict")
            raise ClientError("version_conflict", details=details)
        self.settings.apply_prompt_documents(rendered)
        self.revision += 1
        result: dict[str, Any] = {"applied_at": datetime.now(UTC).isoformat()}
        if revision is not None:
            result["revision"] = revision
        else:
            result["active"] = rendered.revisions
        result["warnings"] = (
            [] if rendered.persona_placeholder_present else ["persona_placeholder_missing"]
        )
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
                "reload",
                fingerprint("client.character_prompt.reload", "reload", {}),
                principal=principal,
                operator_id=operator_id,
            )
            return (
                original
                if original is not None
                else await self._activate(row_id, revision=None, written=False)
            )
