"""Live management command payloads and original receipt projections.

The old UI method table is retired. Receipt validation checks its stored facts,
identity and correlation; domain services decide the operation's actual result.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from yuki.admin.authority.commands import ControlResult
from yuki.admin.authority.json_types import JsonValue, freeze_json_object
from yuki.admin.authority.problems import Problem, ProblemCode
from yuki.admin.authority.tokens import require_opaque_token
from yuki.domain.identity import IdentityBindingId, PersonId


class ControlCommandError(Exception):
    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        super().__init__(problem.code.value)


class CommandOperation(StrEnum):
    PERSON_CREATE = "identity.person.create"
    PERSON_DISABLE = "identity.person.disable"
    PERSON_ENABLE = "identity.person.enable"
    PERSON_SUPERUSER = "identity.person.superuser"
    BINDING_ENABLE = "identity.binding.enable"
    PRESENCE_STATE = "identity.presence.state"
    BINDING_ATTACH = "identity.binding.attach"
    BINDING_DISABLE = "identity.binding.disable"
    PLUGIN_MUTATE = "control.plugin.mutate"
    PLUGIN_CONFIGURE = "control.plugin.configure"
    CONFIG_FILE_SAVE = "control.config.file.save"
    CONFIG_MUTATE = "control.config.mutate"
    GROUP_MUTATE = "control.group.mutate"
    EMOJI_MUTATE = "control.emoji.mutate"
    WORK_MUTATE = "control.work.mutate"


CACHEABLE_COMMAND_FAILURES: Final[frozenset[ProblemCode]] = frozenset(
    {
        ProblemCode.VALIDATION_ERROR,
        ProblemCode.VERSION_CONFLICT,
        ProblemCode.NOT_FOUND,
        ProblemCode.PRECONDITION_FAILED,
        ProblemCode.BINDING_AMBIGUOUS,
        ProblemCode.ROUTE_AMBIGUOUS,
        ProblemCode.POPULATED_MERGE_FORBIDDEN,
        ProblemCode.SECRET_NOT_READABLE,
        ProblemCode.OPERATION_UNAVAILABLE,
    }
)


def _error(code: ProblemCode = ProblemCode.STATE_MISMATCH) -> ControlCommandError:
    return ControlCommandError(Problem(code))


@dataclass(frozen=True, slots=True)
class AttachBindingPayload:
    platform: str
    external_account_id: str
    display_name: str


def parse_attach_binding(payload: object) -> AttachBindingPayload:
    try:
        data = freeze_json_object(payload)
        if set(data) - {"platform", "external_account_id", "display_name"}:
            raise ValueError("unknown binding fields")
        platform = require_opaque_token(data.get("platform"), name="platform", max_length=32)
        account = data.get("external_account_id")
        display = data.get("display_name", "")
        if (
            type(account) is not str
            or not account
            or account != account.strip()
            or len(account) > 255
            or type(display) is not str
        ):
            raise ValueError("invalid binding")
        display = display.strip()
        if len(display) > 128 or any(unicodedata.category(ch) == "Cc" for ch in display):
            raise ValueError("invalid display name")
        return AttachBindingPayload(platform.casefold(), account, display)
    except (TypeError, ValueError):
        raise _error(ProblemCode.VALIDATION_ERROR) from None


def _jsonable(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def bind_command_hash(
    *, operation: str, target_id: str, expected_revision: int, payload: Mapping[str, JsonValue]
) -> str:
    material = {
        "expected_revision": expected_revision,
        "operation": operation,
        "payload": _jsonable(dict(payload)),
        "target_id": target_id,
    }
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _object(value: object, keys: set[str]) -> dict[str, JsonValue]:
    try:
        data = dict(freeze_json_object(value))
        if set(data) != keys:
            raise ValueError("invalid receipt fields")
        return data
    except (TypeError, ValueError):
        raise _error() from None


def _revision(value: JsonValue) -> int:
    if type(value) is not int or value < 1:
        raise _error()
    return value


def _person(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise _error()
    try:
        return PersonId.parse(value).text
    except (TypeError, ValueError):
        raise _error() from None


def _binding(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise _error()
    try:
        return IdentityBindingId.parse(value).text
    except (TypeError, ValueError):
        raise _error() from None


def project_replayed_result(
    *,
    operation: str,
    resource_id: str,
    revision: int,
    audit_id: str,
    raw_state: object,
    semantic_target_id: str,
    material: Mapping[str, JsonValue],
) -> ControlResult:
    if operation == CommandOperation.PERSON_DISABLE:
        state = _object(raw_state, {"enabled", "revision"})
        if type(state["enabled"]) is not bool or _person(resource_id) != semantic_target_id:
            raise _error()
    elif (
        operation == CommandOperation.PLUGIN_MUTATE
        and material.get("action") == "doctor"
        and isinstance(raw_state, Mapping)
        and "diagnostics" in raw_state
    ):
        state = _object(raw_state, {"resource", "revision", "status", "diagnostics"})
        if state["resource"] != resource_id or resource_id != semantic_target_id:
            raise _error()
    elif operation in {CommandOperation.CONFIG_MUTATE, CommandOperation.EMOJI_MUTATE}:
        state = _object(raw_state, {"resource", "revision", "status", "change"})
        if state["resource"] != resource_id or resource_id != semantic_target_id:
            raise _error()
    elif operation == CommandOperation.BINDING_ATTACH:
        state = _object(raw_state, {"binding_id", "person_id", "platform", "status", "revision"})
        _person(state["person_id"])
        if _binding(state["binding_id"]) != resource_id or state["platform"] != material.get(
            "platform"
        ):
            raise _error()
    else:
        try:
            CommandOperation(operation)
        except ValueError:
            raise _error() from None
        state = _object(raw_state, {"resource", "revision", "status"})
        if state["resource"] != resource_id:
            raise _error()
        if operation == CommandOperation.PERSON_CREATE:
            _person(resource_id)
        elif resource_id != semantic_target_id:
            raise _error()
        if operation in {CommandOperation.BINDING_DISABLE, CommandOperation.BINDING_ENABLE}:
            _binding(resource_id)
    if _revision(state["revision"]) != revision:
        raise _error()
    if "status" in state:
        try:
            require_opaque_token(state["status"], name="status", max_length=32)
        except (TypeError, ValueError):
            raise _error() from None
    return ControlResult(
        success=True,
        resource_id=resource_id,
        revision=revision,
        audit_id=audit_id,
        effective_state=state,
    )


def validate_success_audit_before(raw: object, *, operation: str) -> dict[str, JsonValue]:
    if raw == {}:
        return {}
    if operation in {CommandOperation.PERSON_DISABLE, CommandOperation.BINDING_ATTACH}:
        data = _object(raw, {"enabled", "revision"})
        if type(data["enabled"]) is not bool:
            raise _error()
    else:
        data = _object(raw, {"revision"})
    _revision(data["revision"])
    return data


def validate_success_audit_after(
    raw: object,
    *,
    operation: str,
    resource_id: str,
    revision: int,
    semantic_target_id: str,
    material: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    try:
        state = dict(freeze_json_object(raw))
    except (TypeError, ValueError):
        raise _error() from None
    if operation == CommandOperation.BINDING_ATTACH:
        _revision(state.pop("owner_revision", None))
    return dict(
        project_replayed_result(
            operation=operation,
            resource_id=resource_id,
            revision=revision,
            audit_id="0",
            raw_state=state,
            semantic_target_id=semantic_target_id,
            material=material,
        ).effective_state
    )


def validate_failure_audit_before(raw: object, *, operation: str) -> dict[str, JsonValue]:
    return validate_success_audit_before(raw, operation=operation)


def validate_failure_audit_after(raw: object, *, problem_code: str) -> dict[str, JsonValue]:
    data = _object(raw, {"problem"})
    if data["problem"] != problem_code:
        raise _error()
    return data


def require_cacheable_problem(code: object) -> ProblemCode:
    if not isinstance(code, str):
        raise _error()
    try:
        parsed = ProblemCode(code)
    except (ValueError, TypeError):
        raise _error() from None
    if parsed not in CACHEABLE_COMMAND_FAILURES:
        raise _error()
    return parsed


def require_receipt_operation_pair(
    *,
    operation: str,
    material: Mapping[str, JsonValue],
    resource_id: str,
    kind: object,
    ref: object,
) -> tuple[None, None]:
    # Current synchronous commands have no subordinate operation. Pending external
    # effects keep their original control operation until they are settled.
    if kind is not None or ref is not None:
        raise _error()
    return None, None


def failure_audit_target_type(operation: str) -> str:
    targets = {
        CommandOperation.PERSON_CREATE: "person",
        CommandOperation.PERSON_DISABLE: "person",
        CommandOperation.PERSON_ENABLE: "person",
        CommandOperation.PERSON_SUPERUSER: "person",
        CommandOperation.BINDING_ENABLE: "identity_binding",
        CommandOperation.PRESENCE_STATE: "presence",
        CommandOperation.BINDING_ATTACH: "person",
        CommandOperation.BINDING_DISABLE: "identity_binding",
        CommandOperation.PLUGIN_MUTATE: "plugin",
        CommandOperation.PLUGIN_CONFIGURE: "plugin",
        CommandOperation.CONFIG_FILE_SAVE: "config",
        CommandOperation.CONFIG_MUTATE: "config",
        CommandOperation.GROUP_MUTATE: "group",
        CommandOperation.EMOJI_MUTATE: "emoji",
        CommandOperation.WORK_MUTATE: "work",
    }
    try:
        return targets[CommandOperation(operation)]
    except (ValueError, KeyError):
        raise _error() from None


def success_audit_target_type(operation: str) -> str:
    return (
        "identity_binding"
        if operation == CommandOperation.BINDING_ATTACH
        else failure_audit_target_type(operation)
    )
