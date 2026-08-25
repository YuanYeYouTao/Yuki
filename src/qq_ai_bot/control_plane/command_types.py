"""Strict control-command payloads. No I/O and no catalog objects.

C11 writes only canonical identity/route rows. No command is legacy-equivalent,
so v1 always fails closed as pending_cutover without fabricating people/groups.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, final

from qq_ai_bot.control_plane.commands import ControlResult
from qq_ai_bot.control_plane.json_types import JsonValue
from qq_ai_bot.control_plane.problems import Problem, ProblemCode
from qq_ai_bot.control_plane.query_types import RouteKind, RouteReferenceState
from qq_ai_bot.control_plane.tokens import require_opaque_token
from qq_ai_bot.domain.control import YukiControlTarget
from qq_ai_bot.domain.identity import (
    IdentityBindingId,
    PersonId,
    PresenceId,
    SpaceBindingId,
    SpaceId,
)

YUKI_TARGET_TOKEN: Final[str] = "yuki"
_UNSAFE_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "display_name",
        "external_account_id",
        "external_space_id",
        "group_id",
        "nickname",
        "password",
        "path",
        "payload",
        "provider",
        "secret",
        "token",
        "user_id",
    }
)
_PERSON_STATE_KEYS: Final[frozenset[str]] = frozenset({"enabled", "revision"})
_SPACE_STATE_KEYS: Final[frozenset[str]] = frozenset({"enabled", "revision"})
_BINDING_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {"binding_id", "person_id", "platform", "revision", "status"}
)
_SPACE_BINDING_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {"binding_id", "platform", "revision", "space_id", "status"}
)
_PRESENCE_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {"enabled", "ingest_eligible", "platform", "presence_id", "revision"}
)
_ROUTE_STATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "binding_id",
        "kind",
        "owner_id",
        "paused",
        "presence_id",
        "reference_state",
        "revision",
        "route_generation",
    }
)
_BINDING_AUDIT_AFTER_KEYS: Final[frozenset[str]] = _BINDING_STATE_KEYS | frozenset(
    {"owner_revision"}
)
_SPACE_BINDING_AUDIT_AFTER_KEYS: Final[frozenset[str]] = _SPACE_BINDING_STATE_KEYS | frozenset(
    {"owner_revision"}
)
_PRESENCE_UPDATE_BEFORE_KEYS: Final[frozenset[str]] = frozenset(
    {"enabled", "ingest_eligible", "revision"}
)
_ROUTE_UPDATE_BEFORE_KEYS: Final[frozenset[str]] = frozenset(
    {"paused", "revision", "route_generation"}
)
_FAILURE_AFTER_KEYS: Final[frozenset[str]] = frozenset({"problem"})
_MANAGEMENT_STATE_KEYS: Final[frozenset[str]] = frozenset({"resource", "revision", "status"})
_MANAGEMENT_OPERATIONS: Final[frozenset[str]] = frozenset(
    {
        "control.config.set",
        "control.config.unset",
        "control.config.rollback",
        "control.memory.mutate",
        "control.memory.rebuild",
        "control.memory.dream",
        "control.memory.maintenance",
        "control.automation.mutate",
        "control.plugin.mutate",
        "control.mcp.mutate",
        "control.emoji.mutate",
        "control.speech.mutate",
        "control.operation.cancel",
        "control.operation.retry",
    }
)
_REBUILD_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "planned",
        "extracting",
        "extraction_paused",
        "review",
        "committing",
        "commit_paused",
        "completed",
        "cancelled",
        "failed",
    }
)
_REBUILD_START_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "extracting",
        "extraction_paused",
        "review",
        "committing",
        "commit_paused",
        "completed",
    }
)
_DREAM_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "planned",
        "running",
        "partial_failed",
        "completed",
        "cancelled",
        "rolling_back",
        "rolled_back",
    }
)
_DREAM_START_STATUSES: Final[frozenset[str]] = frozenset({"running", "partial_failed", "completed"})
_EMOJI_STATUSES: Final[frozenset[str]] = frozenset(
    {"candidate", "recognized", "adopted", "rejected", "banned", "missing"}
)
_LONG_OPERATION_KINDS: Final[frozenset[str]] = frozenset({"rebuild", "dream"})

_MAX_DISPLAY_NAME = 128
_MAX_EXTERNAL_ID = 255
_MAX_PLATFORM = 32


@final
class ControlCommandError(Exception):
    """Controlled command failure. Callers must not treat this as success."""

    def __init__(self, problem: Problem) -> None:
        if type(problem) is not Problem:
            raise TypeError("problem must be Problem")
        self.problem = problem
        super().__init__(problem.code.value)


@final
class CommandOperation(StrEnum):
    PERSON_ENABLE = "identity.person.enable"
    PERSON_DISABLE = "identity.person.disable"
    BINDING_ATTACH = "identity.binding.attach"
    SPACE_ENABLE = "identity.space.enable"
    SPACE_DISABLE = "identity.space.disable"
    SPACE_BINDING_ATTACH = "identity.space.binding.attach"
    PRESENCE_REGISTER = "identity.presence.register"
    PRESENCE_START = "identity.presence.start"
    PRESENCE_STOP = "identity.presence.stop"
    PRESENCE_SET_INGEST = "identity.presence.set_ingest"
    ROUTE_SET = "route.set"
    ROUTE_PAUSE = "route.pause"
    ROUTE_RESUME = "route.resume"
    CONFIG_SET = "control.config.set"
    CONFIG_UNSET = "control.config.unset"
    CONFIG_ROLLBACK = "control.config.rollback"
    MEMORY_MUTATE = "control.memory.mutate"
    MEMORY_REBUILD = "control.memory.rebuild"
    MEMORY_DREAM = "control.memory.dream"
    MEMORY_MAINTENANCE = "control.memory.maintenance"
    AUTOMATION_MUTATE = "control.automation.mutate"
    PLUGIN_MUTATE = "control.plugin.mutate"
    MCP_MUTATE = "control.mcp.mutate"
    EMOJI_MUTATE = "control.emoji.mutate"
    SPEECH_MUTATE = "control.speech.mutate"
    OPERATION_CANCEL = "control.operation.cancel"
    OPERATION_RETRY = "control.operation.retry"


CACHEABLE_COMMAND_FAILURES: Final[frozenset[ProblemCode]] = frozenset(
    {
        ProblemCode.VALIDATION_ERROR,
        ProblemCode.VERSION_CONFLICT,
        ProblemCode.NOT_FOUND,
        ProblemCode.PRECONDITION_FAILED,
        ProblemCode.BINDING_AMBIGUOUS,
        ProblemCode.ROUTE_AMBIGUOUS,
        ProblemCode.POPULATED_MERGE_FORBIDDEN,
        ProblemCode.PENDING_CUTOVER,
        ProblemCode.SECRET_NOT_READABLE,
    }
)


def _invalid() -> ControlCommandError:
    return ControlCommandError(Problem(ProblemCode.VALIDATION_ERROR))


def _require_object(payload: object) -> Mapping[str, JsonValue]:
    if not isinstance(payload, Mapping):
        raise _invalid()
    return payload


def _reject_unknown(payload: Mapping[str, JsonValue], allowed: frozenset[str]) -> None:
    if any(type(key) is not str for key in payload):
        raise _invalid()
    if set(payload) - allowed:
        raise _invalid()


def require_empty_payload(payload: object) -> None:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset())


def normalize_platform(value: object) -> str:
    try:
        token = require_opaque_token(value, name="platform", max_length=_MAX_PLATFORM)
    except (TypeError, ValueError) as exc:
        raise _invalid() from exc
    return token.casefold()


def normalize_external_id(value: object) -> str:
    if type(value) is not str:
        raise _invalid()
    token = value.strip()
    if not token or token != value or len(token) > _MAX_EXTERNAL_ID:
        raise _invalid()
    return token


def normalize_display_name(value: object) -> str:
    if type(value) is not str:
        raise _invalid()
    token = value.strip()
    if len(token) > _MAX_DISPLAY_NAME:
        raise _invalid()
    if any(unicodedata.category(ch) == "Cc" for ch in token):
        raise _invalid()
    return token


def _require_uuid(
    value: object, parser: type[IdentityBindingId] | type[PresenceId] | type[SpaceBindingId]
) -> str:
    if type(value) is not str:
        raise _invalid()
    try:
        return parser.parse(value).text
    except (TypeError, ValueError) as exc:
        raise _invalid() from exc


def _require_bool(value: object) -> bool:
    if type(value) is not bool:
        raise _invalid()
    return value


def _require_kind(value: object) -> RouteKind:
    if type(value) is not str:
        raise _invalid()
    try:
        return RouteKind(value)
    except ValueError as exc:
        raise _invalid() from exc


@final
@dataclass(frozen=True, slots=True)
class AttachBindingPayload:
    platform: str
    external_account_id: str
    display_name: str

    def material(self) -> dict[str, JsonValue]:
        return {
            "display_name": self.display_name,
            "external_account_id": self.external_account_id,
            "platform": self.platform,
        }


@final
@dataclass(frozen=True, slots=True)
class AttachSpaceBindingPayload:
    platform: str
    external_space_id: str
    display_name: str

    def material(self) -> dict[str, JsonValue]:
        return {
            "display_name": self.display_name,
            "external_space_id": self.external_space_id,
            "platform": self.platform,
        }


@final
@dataclass(frozen=True, slots=True)
class RegisterPresencePayload:
    platform: str
    external_account_id: str

    def material(self) -> dict[str, JsonValue]:
        return {
            "external_account_id": self.external_account_id,
            "platform": self.platform,
        }


@final
@dataclass(frozen=True, slots=True)
class SetIngestPayload:
    ingest_eligible: bool

    def material(self) -> dict[str, JsonValue]:
        return {"ingest_eligible": self.ingest_eligible}


@final
@dataclass(frozen=True, slots=True)
class SetRoutePayload:
    kind: RouteKind
    identity_binding_id: str | None
    space_binding_id: str | None
    presence_id: str | None
    ingest_presence_id: str | None
    paused: bool

    def material(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {"kind": self.kind.value, "paused": self.paused}
        if self.kind is RouteKind.PERSON_ACTIVE:
            payload["identity_binding_id"] = self.identity_binding_id or ""
            payload["presence_id"] = self.presence_id or ""
        elif self.kind is RouteKind.SPACE_BINDING_INGEST:
            payload["ingest_presence_id"] = self.ingest_presence_id or ""
        else:
            payload["space_binding_id"] = self.space_binding_id or ""
            payload["presence_id"] = self.presence_id or ""
        return payload


@final
@dataclass(frozen=True, slots=True)
class RouteActionPayload:
    kind: RouteKind

    def material(self) -> dict[str, JsonValue]:
        return {"kind": self.kind.value}


def parse_attach_binding(payload: object) -> AttachBindingPayload:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset({"platform", "external_account_id", "display_name"}))
    if "platform" not in mapping or "external_account_id" not in mapping:
        raise _invalid()
    display = (
        "" if "display_name" not in mapping else normalize_display_name(mapping["display_name"])
    )
    return AttachBindingPayload(
        platform=normalize_platform(mapping["platform"]),
        external_account_id=normalize_external_id(mapping["external_account_id"]),
        display_name=display,
    )


def parse_attach_space_binding(payload: object) -> AttachSpaceBindingPayload:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset({"platform", "external_space_id", "display_name"}))
    if "platform" not in mapping or "external_space_id" not in mapping:
        raise _invalid()
    display = (
        "" if "display_name" not in mapping else normalize_display_name(mapping["display_name"])
    )
    return AttachSpaceBindingPayload(
        platform=normalize_platform(mapping["platform"]),
        external_space_id=normalize_external_id(mapping["external_space_id"]),
        display_name=display,
    )


def parse_register_presence(payload: object) -> RegisterPresencePayload:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset({"platform", "external_account_id"}))
    if "platform" not in mapping or "external_account_id" not in mapping:
        raise _invalid()
    return RegisterPresencePayload(
        platform=normalize_platform(mapping["platform"]),
        external_account_id=normalize_external_id(mapping["external_account_id"]),
    )


def parse_set_ingest(payload: object) -> SetIngestPayload:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset({"ingest_eligible"}))
    if "ingest_eligible" not in mapping:
        raise _invalid()
    return SetIngestPayload(ingest_eligible=_require_bool(mapping["ingest_eligible"]))


def parse_set_route(payload: object) -> SetRoutePayload:
    mapping = _require_object(payload)
    if "kind" not in mapping:
        raise _invalid()
    kind = _require_kind(mapping["kind"])
    paused = False if "paused" not in mapping else _require_bool(mapping["paused"])
    if kind is RouteKind.PERSON_ACTIVE:
        _reject_unknown(
            mapping, frozenset({"kind", "identity_binding_id", "presence_id", "paused"})
        )
        if "identity_binding_id" not in mapping or "presence_id" not in mapping:
            raise _invalid()
        return SetRoutePayload(
            kind=kind,
            identity_binding_id=_require_uuid(mapping["identity_binding_id"], IdentityBindingId),
            space_binding_id=None,
            presence_id=_require_uuid(mapping["presence_id"], PresenceId),
            ingest_presence_id=None,
            paused=paused,
        )
    if kind is RouteKind.SPACE_BINDING_INGEST:
        _reject_unknown(mapping, frozenset({"kind", "ingest_presence_id", "paused"}))
        if "ingest_presence_id" not in mapping:
            raise _invalid()
        return SetRoutePayload(
            kind=kind,
            identity_binding_id=None,
            space_binding_id=None,
            presence_id=None,
            ingest_presence_id=_require_uuid(mapping["ingest_presence_id"], PresenceId),
            paused=paused,
        )
    _reject_unknown(mapping, frozenset({"kind", "space_binding_id", "presence_id", "paused"}))
    if "space_binding_id" not in mapping or "presence_id" not in mapping:
        raise _invalid()
    return SetRoutePayload(
        kind=kind,
        identity_binding_id=None,
        space_binding_id=_require_uuid(mapping["space_binding_id"], SpaceBindingId),
        presence_id=_require_uuid(mapping["presence_id"], PresenceId),
        ingest_presence_id=None,
        paused=paused,
    )


def parse_route_action(payload: object) -> RouteActionPayload:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset({"kind"}))
    if "kind" not in mapping:
        raise _invalid()
    return RouteActionPayload(kind=_require_kind(mapping["kind"]))


@final
@dataclass(frozen=True, slots=True)
class ConfigWritePayload:
    key: str
    scope_type: str
    scope_id: str
    value: JsonValue | None

    def material(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "key": self.key,
            "scope_id": self.scope_id,
            "scope_type": self.scope_type,
        }
        if self.value is not None:
            encoded = json.dumps(
                _jsonable(self.value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            payload["value_digest"] = hashlib.sha256(encoded.encode()).hexdigest()
        return payload


@final
@dataclass(frozen=True, slots=True)
class ConfigRollbackPayload:
    change_id: int

    def material(self) -> dict[str, JsonValue]:
        return {"change_id": self.change_id}


@final
@dataclass(frozen=True, slots=True)
class ManagementActionPayload:
    action: str
    resource_id: str
    spec: dict[str, JsonValue] | None = None

    def material(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            "action": self.action,
            "resource_id": self.resource_id,
        }
        if self.spec is not None:
            payload["spec"] = dict(self.spec)
        return payload


def _require_scope_type(value: object) -> str:
    if type(value) is not str:
        raise _invalid()
    token = value.strip().casefold()
    if token not in {"global", "group", "user"}:
        raise _invalid()
    return token


def parse_config_write(payload: object, *, require_value: bool) -> ConfigWritePayload:
    mapping = _require_object(payload)
    allowed = frozenset({"key", "scope_type", "scope_id", "value"})
    _reject_unknown(mapping, allowed)
    if "key" not in mapping or "scope_type" not in mapping:
        raise _invalid()
    try:
        key = require_opaque_token(mapping["key"], name="key", max_length=128)
    except (TypeError, ValueError) as exc:
        raise _invalid() from exc
    scope_type = _require_scope_type(mapping["scope_type"])
    scope_id = "" if "scope_id" not in mapping else mapping["scope_id"]
    if type(scope_id) is not str:
        raise _invalid()
    if scope_type == "global":
        if scope_id:
            raise _invalid()
    elif not scope_id.strip() or scope_id != scope_id.strip():
        raise _invalid()
    if require_value and "value" not in mapping:
        raise _invalid()
    if not require_value and "value" in mapping:
        raise _invalid()
    return ConfigWritePayload(
        key=key,
        scope_type=scope_type,
        scope_id=scope_id,
        value=None if not require_value else mapping["value"],
    )


def parse_config_rollback(payload: object) -> ConfigRollbackPayload:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset({"change_id"}))
    if "change_id" not in mapping:
        raise _invalid()
    change_id = mapping["change_id"]
    if type(change_id) is bool or type(change_id) is not int or change_id < 1:
        raise _invalid()
    return ConfigRollbackPayload(change_id=change_id)


def parse_management_action(payload: object) -> ManagementActionPayload:
    mapping = _require_object(payload)
    _reject_unknown(mapping, frozenset({"action", "resource_id", "spec"}))
    if "action" not in mapping:
        raise _invalid()
    try:
        action = require_opaque_token(mapping["action"], name="action", max_length=32)
    except (TypeError, ValueError) as exc:
        raise _invalid() from exc
    resource_id = "yuki" if "resource_id" not in mapping else mapping["resource_id"]
    try:
        token = require_opaque_token(resource_id, name="resource_id", max_length=128)
    except (TypeError, ValueError) as exc:
        raise _invalid() from exc
    raw_spec = mapping.get("spec")
    spec: dict[str, JsonValue] | None = None
    if raw_spec is not None:
        if not isinstance(raw_spec, Mapping) or any(type(key) is not str for key in raw_spec):
            raise _invalid()
        spec = {str(key): _as_json_value(item) for key, item in raw_spec.items()}
    return ManagementActionPayload(action=action, resource_id=token, spec=spec)


def _as_json_value(value: object) -> JsonValue:
    if value is None or type(value) is bool or type(value) is int or type(value) is float:
        return value
    if type(value) is str:
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise _invalid()
        return {str(key): _as_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return tuple(_as_json_value(item) for item in value)
    raise _invalid()


def _jsonable(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_jsonable(item) for item in value]
    return value


def bind_command_hash(
    *,
    operation: str,
    target_id: str,
    expected_revision: int,
    payload: Mapping[str, JsonValue],
) -> str:
    if type(operation) is not str or not operation:
        raise TypeError("operation must be a nonempty str")
    if type(target_id) is not str or type(expected_revision) is not int:
        raise TypeError("hash fields have invalid types")
    if type(expected_revision) is bool or expected_revision < 0:
        raise ValueError("expected_revision is out of range")
    material = {
        "expected_revision": expected_revision,
        "operation": operation,
        "payload": _jsonable(dict(payload)),
        "target_id": target_id,
    }
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _mismatch() -> ControlCommandError:
    return ControlCommandError(Problem(ProblemCode.STATE_MISMATCH))


def json_key_is_unsafe(key: object) -> bool:
    if type(key) is not str:
        return True
    lowered = key.casefold()
    if lowered in _UNSAFE_STATE_KEYS:
        return True
    return "secret" in lowered or "password" in lowered or "api_key" in lowered


def _is_container(value: object) -> bool:
    return isinstance(value, Mapping) or type(value) in (list, tuple)


def _reject_unsafe_keys_deep(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if json_key_is_unsafe(key):
                raise _mismatch()
            _reject_unsafe_keys_deep(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _reject_unsafe_keys_deep(item)


def require_flat_safe_object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise _mismatch()
    _reject_unsafe_keys_deep(value)
    if any(type(key) is not str for key in value):
        raise _mismatch()
    if any(_is_container(item) for item in value.values()):
        raise _mismatch()
    return {key: item for key, item in value.items() if type(key) is str}


def require_command_target(target: object, expected: type[object]) -> None:
    if expected is YukiControlTarget:
        if target is not YukiControlTarget.PERMANENT_YUKI:
            raise _invalid()
        return
    if type(target) is not expected:
        raise _invalid()


def _require_state_uuid(
    value: object,
    parser: (
        type[PersonId]
        | type[SpaceId]
        | type[IdentityBindingId]
        | type[PresenceId]
        | type[SpaceBindingId]
    ),
) -> str:
    if type(value) is not str:
        raise _mismatch()
    try:
        return parser.parse(value).text
    except (TypeError, ValueError) as exc:
        raise _mismatch() from exc


def _require_state_bool(value: object) -> bool:
    if type(value) is not bool:
        raise _mismatch()
    return value


def _require_state_revision(value: object) -> int:
    if type(value) is bool or type(value) is not int or value < 1:
        raise _mismatch()
    return value


def _require_state_platform(value: object) -> str:
    try:
        return require_opaque_token(value, name="platform", max_length=_MAX_PLATFORM).casefold()
    except (TypeError, ValueError) as exc:
        raise _mismatch() from exc


def _require_exact_keys(payload: Mapping[str, JsonValue], allowed: frozenset[str]) -> None:
    if set(payload) != allowed:
        raise _mismatch()


def _require_enabled_state(payload: Mapping[str, JsonValue], revision: int) -> dict[str, JsonValue]:
    _require_exact_keys(payload, _PERSON_STATE_KEYS)
    if _require_state_revision(payload["revision"]) != revision:
        raise _mismatch()
    return {"enabled": _require_state_bool(payload["enabled"]), "revision": revision}


def _require_owner_before(payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    _require_exact_keys(payload, _PERSON_STATE_KEYS)
    return {
        "enabled": _require_state_bool(payload["enabled"]),
        "revision": _require_state_revision(payload["revision"]),
    }


def _require_presence_update_before(payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    _require_exact_keys(payload, _PRESENCE_UPDATE_BEFORE_KEYS)
    return {
        "enabled": _require_state_bool(payload["enabled"]),
        "ingest_eligible": _require_state_bool(payload["ingest_eligible"]),
        "revision": _require_state_revision(payload["revision"]),
    }


def _require_route_update_before(payload: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    _require_exact_keys(payload, _ROUTE_UPDATE_BEFORE_KEYS)
    return {
        "paused": _require_state_bool(payload["paused"]),
        "revision": _require_state_revision(payload["revision"]),
        "route_generation": _require_state_revision(payload["route_generation"]),
    }


def _route_operation(operation: str) -> tuple[str, RouteKind] | None:
    for base in (
        CommandOperation.ROUTE_SET,
        CommandOperation.ROUTE_PAUSE,
        CommandOperation.ROUTE_RESUME,
    ):
        prefix = f"{base.value}."
        if operation.startswith(prefix):
            token = operation[len(prefix) :]
            try:
                return base.value, RouteKind(token)
            except ValueError:
                return None
    return None


def _material_platform(material: Mapping[str, JsonValue]) -> str:
    if "platform" not in material:
        raise _mismatch()
    return _require_state_platform(material["platform"])


def _material_bool(material: Mapping[str, JsonValue], key: str) -> bool:
    if key not in material:
        raise _mismatch()
    return _require_state_bool(material[key])


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
    if type(resource_id) is not str or type(audit_id) is not str:
        raise _mismatch()
    if type(semantic_target_id) is not str:
        raise _mismatch()
    expected = _require_state_revision(revision)
    projected = project_success_effective(
        raw_state,
        operation=operation,
        resource_id=resource_id,
        revision=expected,
        semantic_target_id=semantic_target_id,
        material=material,
    )
    try:
        return ControlResult(
            success=True,
            resource_id=resource_id,
            revision=expected,
            audit_id=audit_id,
            effective_state=projected,
        )
    except (TypeError, ValueError) as exc:
        raise _mismatch() from exc


def project_success_effective(
    raw_state: object,
    *,
    operation: str,
    resource_id: str,
    revision: int,
    semantic_target_id: str,
    material: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    state = require_flat_safe_object(raw_state)
    projected = _project_effective_shape(
        operation, state, resource_id=resource_id, revision=revision
    )
    _require_operation_semantics(
        operation,
        projected,
        resource_id=resource_id,
        revision=revision,
        semantic_target_id=semantic_target_id,
        material=material,
    )
    return projected


def _project_effective_shape(
    operation: str,
    state: Mapping[str, JsonValue],
    *,
    resource_id: str,
    revision: int,
) -> dict[str, JsonValue]:
    if operation in {
        CommandOperation.PERSON_ENABLE.value,
        CommandOperation.PERSON_DISABLE.value,
    }:
        if _require_state_uuid(resource_id, PersonId) != resource_id:
            raise _mismatch()
        return _require_enabled_state(state, revision)
    if operation in {
        CommandOperation.SPACE_ENABLE.value,
        CommandOperation.SPACE_DISABLE.value,
    }:
        if _require_state_uuid(resource_id, SpaceId) != resource_id:
            raise _mismatch()
        return _require_enabled_state(state, revision)
    if operation == CommandOperation.BINDING_ATTACH.value:
        return _project_binding_effective(
            state, resource_id=resource_id, revision=revision, space=False
        )
    if operation == CommandOperation.SPACE_BINDING_ATTACH.value:
        return _project_binding_effective(
            state, resource_id=resource_id, revision=revision, space=True
        )
    if operation in {
        CommandOperation.PRESENCE_REGISTER.value,
        CommandOperation.PRESENCE_START.value,
        CommandOperation.PRESENCE_STOP.value,
        CommandOperation.PRESENCE_SET_INGEST.value,
    }:
        return _project_presence_effective(state, resource_id=resource_id, revision=revision)
    routed = _route_operation(operation)
    if routed is not None:
        return _project_route_effective(
            routed[1], state, resource_id=resource_id, revision=revision
        )
    if operation in _MANAGEMENT_OPERATIONS:
        return _project_management_effective(state, resource_id=resource_id, revision=revision)
    raise _mismatch()


def _project_management_effective(
    state: Mapping[str, JsonValue],
    *,
    resource_id: str,
    revision: int,
) -> dict[str, JsonValue]:
    _require_exact_keys(state, _MANAGEMENT_STATE_KEYS)
    if type(state["resource"]) is not str or state["resource"] != resource_id:
        raise _mismatch()
    if _require_state_revision(state["revision"]) != revision:
        raise _mismatch()
    try:
        status = require_opaque_token(state["status"], name="status", max_length=32)
    except (TypeError, ValueError) as exc:
        raise _mismatch() from exc
    return {"resource": resource_id, "revision": revision, "status": status}


def _project_binding_effective(
    state: Mapping[str, JsonValue],
    *,
    resource_id: str,
    revision: int,
    space: bool,
) -> dict[str, JsonValue]:
    keys = _SPACE_BINDING_STATE_KEYS if space else _BINDING_STATE_KEYS
    _require_exact_keys(state, keys)
    parser = SpaceBindingId if space else IdentityBindingId
    binding_id = _require_state_uuid(state["binding_id"], parser)
    if binding_id != resource_id:
        raise _mismatch()
    if _require_state_revision(state["revision"]) != revision:
        raise _mismatch()
    status = state["status"]
    if status != "active":
        raise _mismatch()
    owner_key = "space_id" if space else "person_id"
    owner_parser = SpaceId if space else PersonId
    return {
        "binding_id": binding_id,
        owner_key: _require_state_uuid(state[owner_key], owner_parser),
        "platform": _require_state_platform(state["platform"]),
        "status": status,
        "revision": revision,
    }


def _project_presence_effective(
    state: Mapping[str, JsonValue],
    *,
    resource_id: str,
    revision: int,
) -> dict[str, JsonValue]:
    _require_exact_keys(state, _PRESENCE_STATE_KEYS)
    presence_id = _require_state_uuid(state["presence_id"], PresenceId)
    if presence_id != resource_id:
        raise _mismatch()
    if _require_state_revision(state["revision"]) != revision:
        raise _mismatch()
    return {
        "presence_id": presence_id,
        "platform": _require_state_platform(state["platform"]),
        "enabled": _require_state_bool(state["enabled"]),
        "ingest_eligible": _require_state_bool(state["ingest_eligible"]),
        "revision": revision,
    }


def _project_route_effective(
    kind: RouteKind,
    state: Mapping[str, JsonValue],
    *,
    resource_id: str,
    revision: int,
) -> dict[str, JsonValue]:
    _require_exact_keys(state, _ROUTE_STATE_KEYS)
    if type(state["kind"]) is not str or state["kind"] != kind.value:
        raise _mismatch()
    try:
        reference = (
            RouteReferenceState(state["reference_state"])
            if type(state["reference_state"]) is str
            else None
        )
    except ValueError as exc:
        raise _mismatch() from exc
    if reference is None:
        raise _mismatch()
    if _require_state_revision(state["revision"]) != revision:
        raise _mismatch()
    generation = _require_state_revision(state["route_generation"])
    presence_id = _require_state_uuid(state["presence_id"], PresenceId)
    if kind is RouteKind.PERSON_ACTIVE:
        if _require_state_uuid(state["owner_id"], PersonId) != resource_id:
            raise _mismatch()
        binding_id = _require_state_uuid(state["binding_id"], IdentityBindingId)
    elif kind is RouteKind.SPACE_BINDING_INGEST:
        if _require_state_uuid(state["owner_id"], SpaceBindingId) != resource_id:
            raise _mismatch()
        binding_id = _require_state_uuid(state["binding_id"], SpaceBindingId)
    else:
        if _require_state_uuid(state["owner_id"], SpaceId) != resource_id:
            raise _mismatch()
        binding_id = _require_state_uuid(state["binding_id"], SpaceBindingId)
    return {
        "kind": kind.value,
        "owner_id": resource_id,
        "binding_id": binding_id,
        "presence_id": presence_id,
        "paused": _require_state_bool(state["paused"]),
        "revision": revision,
        "route_generation": generation,
        "reference_state": reference.value,
    }


def expected_operation_kind(operation: str, material: Mapping[str, JsonValue]) -> str | None:
    if operation == CommandOperation.MEMORY_REBUILD.value:
        return "rebuild"
    if operation == CommandOperation.MEMORY_DREAM.value:
        return "dream"
    if operation in {
        CommandOperation.OPERATION_CANCEL.value,
        CommandOperation.OPERATION_RETRY.value,
    }:
        resource = material.get("resource_id")
        if type(resource) is not str:
            return None
        kind, separator, rest = resource.partition(":")
        if separator == ":" and kind in _LONG_OPERATION_KINDS and rest:
            return kind
    return None


def require_receipt_operation_pair(
    *,
    operation: str,
    material: Mapping[str, JsonValue],
    resource_id: str,
    kind: object,
    ref: object,
) -> tuple[str, str] | tuple[None, None]:
    expected = expected_operation_kind(operation, material)
    if expected is None:
        if kind is not None or ref is not None:
            raise _mismatch()
        return None, None
    if type(kind) is not str or type(ref) is not str:
        raise _mismatch()
    try:
        token = require_opaque_token(kind, name="operation_kind", max_length=64)
        stored = require_opaque_token(ref, name="operation_ref", max_length=128)
    except (TypeError, ValueError) as exc:
        raise _mismatch() from exc
    if token != expected or stored != f"{expected}:{resource_id}":
        raise _mismatch()
    return token, stored


def _material_action(material: Mapping[str, JsonValue]) -> str:
    action = material.get("action")
    if type(action) is not str:
        raise _mismatch()
    return action


def _material_resource(material: Mapping[str, JsonValue]) -> str:
    resource = material.get("resource_id")
    if type(resource) is not str:
        raise _mismatch()
    return resource


def _require_generated_id(resource_id: str) -> None:
    if type(resource_id) is not str or not resource_id or resource_id == "yuki":
        raise _mismatch()
    if resource_id.isdigit():
        if int(resource_id) < 1:
            raise _mismatch()
        return
    if len(resource_id) < 32:
        raise _mismatch()


def _require_management_semantics(
    operation: str,
    state: Mapping[str, JsonValue],
    *,
    resource_id: str,
    semantic_target_id: str,
    material: Mapping[str, JsonValue],
) -> None:
    status = state["status"]
    if type(status) is not str:
        raise _mismatch()
    if operation == CommandOperation.CONFIG_SET.value:
        if (
            status != "applied"
            or resource_id != semantic_target_id
            or resource_id != material.get("key")
        ):
            raise _mismatch()
        return
    if operation == CommandOperation.CONFIG_UNSET.value:
        if (
            status != "removed"
            or resource_id != semantic_target_id
            or resource_id != material.get("key")
        ):
            raise _mismatch()
        return
    if operation == CommandOperation.CONFIG_ROLLBACK.value:
        if (
            status != "rolled_back"
            or resource_id != semantic_target_id
            or resource_id != str(material.get("change_id"))
        ):
            raise _mismatch()
        return
    action = _material_action(material)
    if operation == CommandOperation.MEMORY_MUTATE.value:
        if action not in {"confirm", "quarantine"} or status != action:
            raise _mismatch()
        if resource_id != semantic_target_id or resource_id != _material_resource(material):
            raise _mismatch()
        return
    if operation == CommandOperation.MEMORY_REBUILD.value:
        if action not in {"plan", "start", "cancel"} or status not in _REBUILD_STATUSES:
            raise _mismatch()
        if action == "plan" and status != "planned":
            raise _mismatch()
        if action == "start" and status not in _REBUILD_START_STATUSES:
            raise _mismatch()
        if action == "cancel" and status != "cancelled":
            raise _mismatch()
        if action == "plan" or (action == "start" and _material_resource(material) == "index"):
            _require_generated_id(resource_id)
        elif resource_id != _material_resource(material):
            raise _mismatch()
        return
    if operation == CommandOperation.MEMORY_DREAM.value:
        if action not in {"plan", "start", "cancel"} or status not in _DREAM_STATUSES:
            raise _mismatch()
        if action == "plan" and status != "planned":
            raise _mismatch()
        if action == "start" and status not in _DREAM_START_STATUSES:
            raise _mismatch()
        if action == "cancel" and status != "cancelled":
            raise _mismatch()
        if action == "plan" or (action == "start" and _material_resource(material) == "index"):
            _require_generated_id(resource_id)
        elif resource_id != _material_resource(material):
            raise _mismatch()
        return
    if operation == CommandOperation.MEMORY_MAINTENANCE.value:
        if action not in {"plan", "start", "run"} or status != "completed":
            raise _mismatch()
        if resource_id != _material_resource(material):
            raise _mismatch()
        return
    if operation == CommandOperation.AUTOMATION_MUTATE.value:
        if action == "create":
            if status != "active" or "spec" not in material:
                raise _mismatch()
            _require_generated_id(resource_id)
            return
        if resource_id != _material_resource(material):
            raise _mismatch()
        if action == "update":
            if status not in {"active", "paused"} or "spec" not in material:
                raise _mismatch()
            return
        expected = {
            "pause": "paused",
            "resume": "active",
            "cancel": "cancelled",
            "run_now": "active",
        }.get(action)
        if expected is None or status != expected:
            raise _mismatch()
        return
    if operation == CommandOperation.PLUGIN_MUTATE.value:
        if action == "retry":
            if status not in {"pending", "failed"}:
                raise _mismatch()
            _require_generated_id(resource_id)
            return
        if resource_id != _material_resource(material):
            raise _mismatch()
        expected = {
            "approve": "approved",
            "enable": "approved",
            "disable": "disabled",
            "doctor": None,
        }.get(action)
        if action == "doctor":
            if status not in {"healthy", "unhealthy"}:
                raise _mismatch()
            return
        if expected is None or status != expected:
            raise _mismatch()
        return
    if operation == CommandOperation.MCP_MUTATE.value:
        if action not in {"enable", "disable", "refresh", "reconnect"}:
            raise _mismatch()
        if resource_id != _material_resource(material):
            raise _mismatch()
        if action == "enable" and status != "enabled":
            raise _mismatch()
        if action == "disable" and status != "disabled":
            raise _mismatch()
        if action in {"refresh", "reconnect"} and status not in {"enabled", "disabled"}:
            raise _mismatch()
        return
    if operation == CommandOperation.EMOJI_MUTATE.value:
        if action not in {"pin", "unpin", "reject", "ban"} or status not in _EMOJI_STATUSES:
            raise _mismatch()
        if resource_id != _material_resource(material):
            raise _mismatch()
        if action in {"pin", "unpin"} and status not in {
            "candidate",
            "recognized",
            "adopted",
        }:
            raise _mismatch()
        if action == "reject" and status != "rejected":
            raise _mismatch()
        if action == "ban" and status != "banned":
            raise _mismatch()
        return
    if operation == CommandOperation.SPEECH_MUTATE.value:
        if action not in {"enable", "disable"}:
            raise _mismatch()
        if resource_id != _material_resource(material):
            raise _mismatch()
        if action == "enable" and status != "enabled":
            raise _mismatch()
        if action == "disable" and status != "disabled":
            raise _mismatch()
        return
    if operation == CommandOperation.OPERATION_CANCEL.value:
        if status != "cancelled":
            raise _mismatch()
        kind, separator, rest = _material_resource(material).partition(":")
        if (
            separator != ":"
            or kind not in {"rebuild", "dream", "automation"}
            or rest != resource_id
        ):
            raise _mismatch()
        return
    if operation == CommandOperation.OPERATION_RETRY.value:
        kind, separator, rest = _material_resource(material).partition(":")
        if separator != ":" or rest != resource_id:
            raise _mismatch()
        allowed = {
            "rebuild": _REBUILD_START_STATUSES,
            "dream": _DREAM_START_STATUSES,
            "plugin-outbox": frozenset({"pending", "failed"}),
            "automation": frozenset({"active"}),
        }.get(kind)
        if allowed is None or status not in allowed:
            raise _mismatch()
        return
    raise _mismatch()


def _require_operation_semantics(
    operation: str,
    state: Mapping[str, JsonValue],
    *,
    resource_id: str,
    revision: int,
    semantic_target_id: str,
    material: Mapping[str, JsonValue],
) -> None:
    if operation == CommandOperation.PERSON_ENABLE.value:
        if state["enabled"] is not True or resource_id != semantic_target_id:
            raise _mismatch()
        _require_state_uuid(semantic_target_id, PersonId)
        return
    if operation == CommandOperation.PERSON_DISABLE.value:
        if state["enabled"] is not False or resource_id != semantic_target_id:
            raise _mismatch()
        _require_state_uuid(semantic_target_id, PersonId)
        return
    if operation == CommandOperation.SPACE_ENABLE.value:
        if state["enabled"] is not True or resource_id != semantic_target_id:
            raise _mismatch()
        _require_state_uuid(semantic_target_id, SpaceId)
        return
    if operation == CommandOperation.SPACE_DISABLE.value:
        if state["enabled"] is not False or resource_id != semantic_target_id:
            raise _mismatch()
        _require_state_uuid(semantic_target_id, SpaceId)
        return
    if operation == CommandOperation.BINDING_ATTACH.value:
        if (
            state["status"] != "active"
            or revision != 1
            or state["revision"] != 1
            or state["person_id"] != semantic_target_id
            or state["platform"] != _material_platform(material)
            or state["binding_id"] != resource_id
        ):
            raise _mismatch()
        _require_state_uuid(semantic_target_id, PersonId)
        return
    if operation == CommandOperation.SPACE_BINDING_ATTACH.value:
        if (
            state["status"] != "active"
            or revision != 1
            or state["revision"] != 1
            or state["space_id"] != semantic_target_id
            or state["platform"] != _material_platform(material)
            or state["binding_id"] != resource_id
        ):
            raise _mismatch()
        _require_state_uuid(semantic_target_id, SpaceId)
        return
    if operation == CommandOperation.PRESENCE_REGISTER.value:
        if (
            semantic_target_id != YUKI_TARGET_TOKEN
            or revision != 1
            or state["revision"] != 1
            or state["enabled"] is not True
            or state["ingest_eligible"] is not True
            or state["platform"] != _material_platform(material)
            or state["presence_id"] != resource_id
        ):
            raise _mismatch()
        return
    if operation == CommandOperation.PRESENCE_START.value:
        if state["enabled"] is not True or resource_id != semantic_target_id:
            raise _mismatch()
        _require_state_uuid(semantic_target_id, PresenceId)
        return
    if operation == CommandOperation.PRESENCE_STOP.value:
        if state["enabled"] is not False or resource_id != semantic_target_id:
            raise _mismatch()
        _require_state_uuid(semantic_target_id, PresenceId)
        return
    if operation == CommandOperation.PRESENCE_SET_INGEST.value:
        if (
            state["ingest_eligible"] is not _material_bool(material, "ingest_eligible")
            or resource_id != semantic_target_id
        ):
            raise _mismatch()
        _require_state_uuid(semantic_target_id, PresenceId)
        return
    if operation in _MANAGEMENT_OPERATIONS:
        _require_management_semantics(
            operation,
            state,
            resource_id=resource_id,
            semantic_target_id=semantic_target_id,
            material=material,
        )
        return
    routed = _route_operation(operation)
    if routed is None:
        raise _mismatch()
    base, kind = routed
    if state["kind"] != kind.value or resource_id != semantic_target_id:
        raise _mismatch()
    if state["owner_id"] != semantic_target_id:
        raise _mismatch()
    if "kind" not in material or material["kind"] != kind.value:
        raise _mismatch()
    if base == CommandOperation.ROUTE_PAUSE.value:
        if state["paused"] is not True:
            raise _mismatch()
        return
    if base == CommandOperation.ROUTE_RESUME.value:
        if state["paused"] is not False:
            raise _mismatch()
        return
    if state["paused"] is not _material_bool(material, "paused"):
        raise _mismatch()
    if kind is RouteKind.PERSON_ACTIVE:
        if state["binding_id"] != material.get("identity_binding_id"):
            raise _mismatch()
        if state["presence_id"] != material.get("presence_id"):
            raise _mismatch()
        return
    if kind is RouteKind.SPACE_BINDING_INGEST:
        if state["binding_id"] != semantic_target_id:
            raise _mismatch()
        if state["presence_id"] != material.get("ingest_presence_id"):
            raise _mismatch()
        return
    if state["binding_id"] != material.get("space_binding_id"):
        raise _mismatch()
    if state["presence_id"] != material.get("presence_id"):
        raise _mismatch()


def validate_success_audit_before(raw: object, *, operation: str) -> dict[str, JsonValue]:
    payload = require_flat_safe_object(raw)
    if operation in {
        CommandOperation.PERSON_ENABLE.value,
        CommandOperation.PERSON_DISABLE.value,
        CommandOperation.SPACE_ENABLE.value,
        CommandOperation.SPACE_DISABLE.value,
        CommandOperation.BINDING_ATTACH.value,
        CommandOperation.SPACE_BINDING_ATTACH.value,
    }:
        return _require_owner_before(payload)
    if operation == CommandOperation.PRESENCE_REGISTER.value:
        _require_exact_keys(payload, frozenset())
        return {}
    if operation in {
        CommandOperation.PRESENCE_START.value,
        CommandOperation.PRESENCE_STOP.value,
        CommandOperation.PRESENCE_SET_INGEST.value,
    }:
        return _require_presence_update_before(payload)
    routed = _route_operation(operation)
    if routed is not None:
        if routed[0] == CommandOperation.ROUTE_SET.value and payload == {}:
            return {}
        return _require_route_update_before(payload)
    if operation in _MANAGEMENT_OPERATIONS:
        if payload == {}:
            return {}
        _require_exact_keys(payload, frozenset({"revision"}))
        return {"revision": _require_state_revision(payload["revision"])}
    raise _mismatch()


def validate_success_audit_after(
    raw: object,
    *,
    operation: str,
    resource_id: str,
    revision: int,
    semantic_target_id: str,
    material: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    payload = require_flat_safe_object(raw)
    if operation == CommandOperation.BINDING_ATTACH.value:
        _require_exact_keys(payload, _BINDING_AUDIT_AFTER_KEYS)
        _require_state_revision(payload["owner_revision"])
        effective = {key: payload[key] for key in _BINDING_STATE_KEYS}
    elif operation == CommandOperation.SPACE_BINDING_ATTACH.value:
        _require_exact_keys(payload, _SPACE_BINDING_AUDIT_AFTER_KEYS)
        _require_state_revision(payload["owner_revision"])
        effective = {key: payload[key] for key in _SPACE_BINDING_STATE_KEYS}
    else:
        effective = dict(payload)
    return project_success_effective(
        effective,
        operation=operation,
        resource_id=resource_id,
        revision=revision,
        semantic_target_id=semantic_target_id,
        material=material,
    )


def validate_failure_audit_before(raw: object, *, operation: str) -> dict[str, JsonValue]:
    payload = require_flat_safe_object(raw)
    if payload == {}:
        return {}
    if operation in {
        CommandOperation.PERSON_ENABLE.value,
        CommandOperation.PERSON_DISABLE.value,
        CommandOperation.BINDING_ATTACH.value,
        CommandOperation.SPACE_ENABLE.value,
        CommandOperation.SPACE_DISABLE.value,
        CommandOperation.SPACE_BINDING_ATTACH.value,
    }:
        return _require_owner_before(payload)
    if operation in {
        CommandOperation.PRESENCE_REGISTER.value,
        CommandOperation.PRESENCE_START.value,
        CommandOperation.PRESENCE_STOP.value,
        CommandOperation.PRESENCE_SET_INGEST.value,
    }:
        return _require_presence_update_before(payload)
    if _route_operation(operation) is not None:
        return _require_route_update_before(payload)
    if operation in _MANAGEMENT_OPERATIONS:
        return {}
    raise _mismatch()


def validate_failure_audit_after(raw: object, *, problem_code: str) -> dict[str, JsonValue]:
    payload = require_flat_safe_object(raw)
    _require_exact_keys(payload, _FAILURE_AFTER_KEYS)
    if type(payload["problem"]) is not str or payload["problem"] != problem_code:
        raise _mismatch()
    return {"problem": problem_code}


def require_cacheable_problem(code: object) -> ProblemCode:
    if type(code) is not str:
        raise _mismatch()
    try:
        parsed = ProblemCode(code)
    except ValueError as exc:
        raise _mismatch() from exc
    if parsed not in CACHEABLE_COMMAND_FAILURES:
        raise _mismatch()
    return parsed


def failure_audit_target_type(operation: str) -> str:
    if operation == CommandOperation.PRESENCE_REGISTER.value:
        return "yuki"
    if (
        operation.startswith("identity.person.")
        or operation == CommandOperation.BINDING_ATTACH.value
    ):
        return "person"
    if operation.startswith("identity.space."):
        return "space"
    if operation in {
        CommandOperation.PRESENCE_START.value,
        CommandOperation.PRESENCE_STOP.value,
        CommandOperation.PRESENCE_SET_INGEST.value,
    }:
        return "presence"
    if "person_active" in operation:
        return "person_active_route"
    if "space_binding_ingest" in operation:
        return "space_binding_ingest_route"
    if "space_active" in operation:
        return "space_active_route"
    if operation.startswith("route."):
        return "route"
    if operation.startswith("control.config."):
        return "config"
    if operation.startswith("control.memory."):
        return "memory"
    if operation.startswith("control.automation."):
        return "automation"
    if operation.startswith("control.plugin."):
        return "plugin"
    if operation.startswith("control.mcp."):
        return "mcp"
    if operation.startswith("control.emoji."):
        return "emoji"
    if operation.startswith("control.speech."):
        return "speech"
    return "command"


def success_audit_target_type(operation: str) -> str:
    if operation in {
        CommandOperation.PERSON_ENABLE.value,
        CommandOperation.PERSON_DISABLE.value,
    }:
        return "person"
    if operation == CommandOperation.BINDING_ATTACH.value:
        return "identity_binding"
    if operation in {
        CommandOperation.SPACE_ENABLE.value,
        CommandOperation.SPACE_DISABLE.value,
    }:
        return "space"
    if operation == CommandOperation.SPACE_BINDING_ATTACH.value:
        return "space_binding"
    if operation in {
        CommandOperation.PRESENCE_REGISTER.value,
        CommandOperation.PRESENCE_START.value,
        CommandOperation.PRESENCE_STOP.value,
        CommandOperation.PRESENCE_SET_INGEST.value,
    }:
        return "presence"
    if "person_active" in operation:
        return "person_active_route"
    if "space_binding_ingest" in operation:
        return "space_binding_ingest_route"
    if "space_active" in operation:
        return "space_active_route"
    if operation in _MANAGEMENT_OPERATIONS:
        return failure_audit_target_type(operation)
    raise _mismatch()
