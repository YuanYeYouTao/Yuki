"""Original personal receipts across Bot reservation, Manager effect and settlement."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

from yuki.admin.authority.json_types import JsonValue
from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.errors import ClientError
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal, fingerprint
from yuki.client_api.requests import personal_command
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.models import AdminOperationEventModel
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.computer_keys import manager_key as manager_key
from yuki.sandbox.computer_keys import release_key as release_key


def public_control(control: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in control.items()
        if key in {"holder", "lease_id", "device_id", "device_label", "work_id", "since"}
    }


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    if isinstance(result.get("control"), dict):
        result["control"] = public_control(result["control"])
    return result


class ComputerCommands:
    def __init__(self, identity: ClientIdentityService, sandbox: SandboxClient) -> None:
        self.identity, self.sandbox = identity, sandbox

    async def execute(
        self,
        principal: ClientPrincipal,
        request_id: str,
        action: str,
        payload: dict[str, Any],
        *,
        before_effect: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if action not in {"take", "return", "transfer", "wake", "sleep"}:
            raise ClientError("validation_error")
        allowed = (
            {"lease_id", "note"}
            if action in {"take", "return", "transfer"}
            else {"confirm_active_work_tabs"}
            if action == "sleep"
            else set()
        )
        if payload.keys() - allowed:
            raise ClientError("validation_error")
        if action in {"take", "return", "transfer"}:
            payload = {"lease_id": payload.get("lease_id"), "note": payload.get("note")}
            if (action == "take" and payload["lease_id"] is not None) or (
                action in {"return", "transfer"} and payload["lease_id"] is None
            ):
                raise ClientError("validation_error")
        elif action == "sleep":
            payload = {"confirm_active_work_tabs": payload.get("confirm_active_work_tabs", False)}
        operation = "client.computer." + (action if action in {"wake", "sleep"} else "control")
        body = {"action": action, **payload}
        bound = fingerprint(operation, principal.request_person_id, body)
        key = manager_key(principal.request_scope, request_id)
        row_id: int
        async with self.identity.database.immediate_session() as writer:
            current = await self.identity.require_superuser(writer, principal)
            previous = await personal_command(
                writer,
                current.request_person_id,
                request_id,
                conversation_id=current.request_conversation_id,
            )
            if previous is not None:
                if (
                    previous.payload_hash != bound
                    or previous.operation_kind != "control"
                    or previous.operation_ref != key
                ):
                    raise ClientError("request_conflict")
                if previous.status in {"succeeded", "failed"}:
                    replayed = await self.identity.replay(
                        writer, current.device_id, request_id, bound, principal=current
                    )
                    assert replayed is not None
                    return replayed
                audit = await writer.get(AdminOperationEventModel, previous.audit_id)
                assert audit is not None
                original = json.loads(audit.before_json)
                row_id = previous.id
            else:
                # Also reject an original message using this namespace and ID.
                await self.identity.replay(
                    writer, current.device_id, request_id, bound, principal=current
                )
                device = await writer.get(ClientDeviceModel, current.device_id)
                assert device is not None
                original = {
                    "action": action,
                    "payload": payload,
                    "actor": {
                        "person_id": current.person_id,
                        "device_id": current.device_id,
                        "device_label": device.label,
                    },
                }
                row = await self.identity.receipts.record_personal_command(
                    writer,
                    principal_id=current.device_id,
                    principal_kind="device",
                    person_id=current.person_id,
                    request_id=request_id,
                    operation="control",
                    operation_ref=key,
                    payload_hash=bound,
                    resource_id=key,
                    result=cast(dict[str, JsonValue], {}),
                    request_person_id=current.request_person_id,
                    request_conversation_id=current.request_conversation_id,
                )
                row.status, row.effective_state_json, row.result_resource_id = "running", None, None
                audit = await writer.get(AdminOperationEventModel, row.audit_id)
                assert audit is not None
                audit.operation, audit.capability = operation, operation
                audit.before_json, audit.after_json = json.dumps(original), '{"status":"running"}'
                audit.success, audit.error_category = False, "in_progress"
                row_id = row.id
        # No Bot write transaction spans either Manager call. The original actor
        # is reused after response loss, even when another device retries it.
        result = await self.sandbox.execute("control_request_get", {"key": key}, request_id=key)
        if result.get("error") == "unknown_request":
            method = "display_" + action if action in {"wake", "sleep"} else "control_set"
            arguments = (
                original["payload"]
                if action in {"wake", "sleep"}
                else {"action": action, "actor": original["actor"], **original["payload"]}
            )
            try:
                async with self.identity.database.read_session() as reader:
                    original_actor = await self.identity.require_device(
                        reader, original["actor"]["device_id"]
                    )
                    await self.identity.require_superuser(reader, original_actor)
                if before_effect:
                    await before_effect()
            except ClientError as exc:
                result = {"error": exc.code, **exc.details}
            else:
                result = await self.sandbox.execute(method, arguments, request_id=key)
        uncertain = result.get("error") in {
            "sandbox_unavailable",
            "invalid_request",
            "invalid_response",
        }
        problem = (
            "request_conflict"
            if result.get("error") == "idempotency_conflict"
            else result.get("error")
            if result.get("error")
            in {
                "control_held",
                "precondition_failed",
                "unauthenticated",
                "forbidden",
                "owner_required",  # Keep the original failure from retired operation receipts.
                "not_found",
            }
            else "unavailable"
            if result.get("error")
            else None
        )
        visible = public_result(result)
        async with self.identity.database.immediate_session() as writer:
            saved = await writer.get(ControlCommandReceiptModel, row_id)
            if saved is None:
                raise ClientError("not_found")
            if saved.status in {"succeeded", "failed"}:
                replayed = await self.identity.replay(
                    writer, principal.device_id, request_id, bound, principal=principal
                )
                assert replayed is not None
                return replayed
            old_audit = await writer.get(AdminOperationEventModel, saved.audit_id)
            assert old_audit is not None
            now = datetime.now(UTC)
            code = "effect_unknown" if uncertain else problem
            after = (
                {"status": "unknown"}
                if uncertain
                else {key: value for key, value in visible.items() if key != "error"}
            )
            audit = AdminOperationEventModel(
                **{
                    field: getattr(old_audit, field)
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
                before_json=old_audit.before_json,
                after_json=json.dumps(after),
                success=code is None,
                error_category=code,
                duration_seconds=0,
                created_at=now,
            )
            writer.add(audit)
            await writer.flush()
            saved.audit_id, saved.updated_at = audit.id, now
            saved.status = "unknown" if uncertain else "failed" if problem else "succeeded"
            saved.problem_code = code
            saved.result_resource_id = key if code is None else None
            saved.effective_state_json = json.dumps(visible) if code is None else None
        if uncertain:
            raise ClientError("unknown")
        if problem:
            raise ClientError(problem, details=after)
        return visible
