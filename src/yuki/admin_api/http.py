"""Finite Bearer routes; caller-supplied principals never enter domain commands."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, ConfigDict, Field, SecretStr

from yuki.admin.authority.command_types import ControlCommandError
from yuki.admin.authority.commands import ControlCommand, ControlResult
from yuki.admin.authority.json_types import thaw_json_value
from yuki.admin.authority.operators import ControlOperatorAccess
from yuki.admin.authority.principal import ControlPrincipal, PrincipalSource
from yuki.admin.authority.problems import ControlQueryError, ProblemCode
from yuki.admin.config_files import ConfigFileError
from yuki.admin.operations import AdminOperations
from yuki.admin.operations.base import authorize, fail
from yuki.admin.queries import AdminQueries
from yuki.character.repository import CharacterRepository
from yuki.character.soul import CharacterSoul
from yuki.client_api.errors import ClientError
from yuki.client_api.http_errors import install_identity_errors
from yuki.client_api.identity import ClientIdentityService
from yuki.client_api.models import (
    CharacterProfile,
    CharacterUpdateBody,
    Envelope,
    SoulDocument,
    SoulReloadBody,
    SoulResult,
    SoulSaveBody,
    character_changes,
)
from yuki.domain.identity import RequestId
from yuki.plugin_host.configuration_service import PluginConfigurationError
from yuki.plugin_host.manager import PluginManagementRejected
from yuki_plugin_sdk.observation import PluginObservationRequest


@dataclass(frozen=True, slots=True)
class AdminApi:
    access: ControlOperatorAccess
    commands: AdminOperations
    identities: ClientIdentityService
    queries: AdminQueries
    character: CharacterRepository | None = None
    soul: CharacterSoul | None = None


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: int = Field(strict=True, ge=0)

    def command(self, payload: object) -> ControlCommand:
        return ControlCommand(
            request_id=RequestId.parse(str(self.request_id)),
            expected_revision=self.expected_revision,
            payload=payload,
        )


class PersonBody(CommandBody):
    display_name: str = Field(default="", max_length=128)


class BindingBody(CommandBody):
    platform: str
    external_account_id: str
    display_name: str = ""


class PresenceBody(CommandBody):
    enabled: bool | None = None
    ingest_eligible: bool | None = None


class PresenceRegistrationBody(BindingBody):
    expected_assignment_id: UUID4 | None = None


class PresenceTransferBody(CommandBody):
    to_person: UUID4


class PluginBody(CommandBody):
    permissions: list[str] = Field(default_factory=list)


class ConfigBody(CommandBody):
    scope_type: Literal["global", "user", "group"] = "global"
    owner_id: UUID4 | None = None
    values: dict[str, Any]


class ModelsBody(CommandBody):
    document: dict[str, Any]
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)


class SecretBody(CommandBody):
    name: str
    value: SecretStr


class IdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4


class TransferBody(IdentityRequest):
    to_person: UUID4


class RenameBody(IdentityRequest):
    display_name: str = Field(max_length=128)


def result_view(result: ControlResult) -> dict[str, Any]:
    return {
        "success": result.success,
        "resource_id": result.resource_id,
        "revision": result.revision,
        "audit_id": result.audit_id,
        "effective_state": thaw_json_value(result.effective_state),
        "operation": asdict(result.operation) if result.operation is not None else None,
    }


def attach_admin_api(app: FastAPI, resolve: Callable[[], AdminApi]) -> None:
    router = APIRouter(prefix="/admin/v1")

    async def principal(authorization: Annotated[str | None, Header()] = None) -> ControlPrincipal:
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.casefold() != "bearer" or not credential:
            raise fail(ProblemCode.UNAUTHENTICATED)
        return await resolve().access.authenticate(credential, source=PrincipalSource.CLI)

    Actor = Annotated[ControlPrincipal, Depends(principal)]

    def character() -> CharacterRepository:
        service = resolve().character
        if service is None:
            raise ClientError("unavailable")
        return service

    def soul() -> CharacterSoul:
        service = resolve().soul
        if service is None:
            raise ClientError("unavailable")
        return service

    @router.get(
        "/character", response_model=Envelope[CharacterProfile], response_model_exclude_unset=True
    )
    async def character_profile(actor: Actor) -> Any:
        authorize(actor, "control.character.read")
        return {"data": await character().get()}

    @router.patch(
        "/character", response_model=Envelope[CharacterProfile], response_model_exclude_unset=True
    )
    async def update_character(body: CharacterUpdateBody, actor: Actor) -> Any:
        authorize(actor, "control.character.mutate")
        return {
            "data": await character().update(
                str(body["request_id"]),
                body["expected_revision"],
                character_changes(body),
                operator_id=actor.principal_id.text,
            )
        }

    @router.get("/character/owners")
    async def character_owners(actor: Actor) -> Any:
        authorize(actor, "control.character.read")
        return {"data": {"items": await character().owners()}}

    @router.post("/character/owners/{person_id}/{action}")
    async def character_owner(
        person_id: UUID4, action: Literal["grant", "revoke"], body: IdentityRequest, actor: Actor
    ) -> Any:
        authorize(actor, "control.character.mutate")
        return {
            "data": await character().set_owner(
                actor.principal_id.text,
                str(person_id),
                str(body.request_id),
                granted=action == "grant",
            )
        }

    @router.get("/character/soul", response_model=Envelope[SoulDocument])
    async def read_soul(actor: Actor) -> Any:
        authorize(actor, "control.character.read")
        return {"data": await soul().read(operator_id=actor.principal_id.text)}

    @router.put(
        "/character/soul", response_model=Envelope[SoulResult], response_model_exclude_unset=True
    )
    async def save_soul(body: SoulSaveBody, actor: Actor) -> Any:
        authorize(actor, "control.character.mutate")
        return {
            "data": await soul().save(
                str(body.request_id),
                body.expected_revision,
                body.content,
                operator_id=actor.principal_id.text,
            )
        }

    @router.post(
        "/character/soul/reload",
        response_model=Envelope[SoulResult],
        response_model_exclude_unset=True,
    )
    async def reload_soul(body: SoulReloadBody, actor: Actor) -> Any:
        authorize(actor, "control.character.mutate")
        return {
            "data": await soul().reload(str(body.request_id), operator_id=actor.principal_id.text)
        }

    @router.get("/health")
    async def health(actor: Actor) -> Any:
        authorize(actor, "control.health.read")
        return {"data": await resolve().queries.health()}

    @router.get("/operations/{identity}")
    async def operation(identity: str, actor: Actor) -> Any:
        return {"data": await resolve().queries.operation(actor, identity)}

    @router.get("/traces/{kind}/{identity}")
    async def traces(
        kind: Literal["turn", "work", "event"],
        identity: str,
        actor: Actor,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=250),
        content: bool = False,
    ) -> Any:
        return {
            "data": await resolve().queries.traces(actor, kind, identity, after, limit, content)
        }

    @router.get("/persons")
    async def persons(
        actor: Actor, after: str = "", limit: int = Query(default=100, ge=1, le=250)
    ) -> Any:
        return {"data": await resolve().queries.persons(actor, after, limit)}

    @router.get("/bindings")
    async def bindings(
        actor: Actor,
        after: str = "",
        limit: int = Query(default=100, ge=1, le=250),
        person_id: UUID4 | None = None,
        platform: str | None = None,
        external_account_id: str | None = None,
    ) -> Any:
        return {
            "data": await resolve().queries.bindings(
                actor,
                after,
                limit,
                person_id=str(person_id) if person_id else None,
                platform=platform,
                external_account_id=external_account_id,
            )
        }

    @router.get("/presences")
    async def presences(
        actor: Actor, after: str = "", limit: int = Query(default=100, ge=1, le=250)
    ) -> Any:
        return {"data": await resolve().queries.presences(actor, after, limit)}

    @router.get("/presences/{identity}")
    async def presence(identity: UUID4, actor: Actor) -> Any:
        return {"data": await resolve().queries.presence(actor, str(identity))}

    @router.post("/presences")
    async def register_presence(body: PresenceRegistrationBody, actor: Actor) -> Any:
        payload = body.model_dump(mode="json", exclude={"request_id", "expected_revision"})
        return {
            "data": result_view(
                await resolve().commands.identity.register_presence(actor, body.command(payload))
            )
        }

    @router.post("/presences/{identity}/unregister")
    async def unregister_presence(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.identity.unbind_assignment(
                    actor, str(identity), body.command({}), kind="yuki"
                )
            )
        }

    @router.post("/presences/{identity}/transfer")
    async def transfer_presence(identity: UUID4, body: PresenceTransferBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.identity.transfer_presence(
                    actor,
                    str(identity),
                    str(body.to_person),
                    body.command({"to_person": str(body.to_person)}),
                )
            )
        }

    @router.post("/presences/{identity}")
    async def presence_state(identity: UUID4, body: PresenceBody, actor: Actor) -> Any:
        if body.enabled is None and body.ingest_eligible is None:
            raise fail(ProblemCode.VALIDATION_ERROR)
        payload = {"enabled": body.enabled, "ingest_eligible": body.ingest_eligible}
        return {
            "data": result_view(
                await resolve().commands.identity.presence_state(
                    actor, str(identity), body.command(payload), **payload
                )
            )
        }

    @router.get("/works")
    async def works(
        actor: Actor,
        after: str = "",
        limit: int = Query(default=100, ge=1, le=250),
        state: str | None = None,
    ) -> Any:
        return {"data": await resolve().queries.works(actor, after, limit, state=state)}

    @router.get("/works/{identity}")
    async def work(identity: UUID4, actor: Actor) -> Any:
        return {"data": await resolve().queries.work(actor, str(identity))}

    @router.post("/persons/{identity}/enable")
    async def enable_person(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.identity.person_state(
                    actor, str(identity), body.command({"enabled": True}), enabled=True
                )
            )
        }

    @router.post("/persons/{identity}/superuser/{action}")
    async def superuser(
        identity: UUID4, action: Literal["grant", "revoke"], body: CommandBody, actor: Actor
    ) -> Any:
        granted = action == "grant"
        return {
            "data": result_view(
                await resolve().commands.identity.person_state(
                    actor,
                    str(identity),
                    body.command({"is_superuser": granted}),
                    is_superuser=granted,
                )
            )
        }

    @router.post("/bindings/{identity}/enable")
    async def enable_binding(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.identity.binding_state(
                    actor, str(identity), body.command({"enabled": True}), enabled=True
                )
            )
        }

    @router.post("/persons")
    async def create_person(body: PersonBody, actor: Actor) -> Any:
        payload = {"display_name": body.display_name} if body.display_name else {}
        return {
            "data": result_view(
                await resolve().commands.identity.create_person(actor, body.command(payload))
            )
        }

    @router.get("/persons/{identity}")
    async def person(identity: UUID4, actor: Actor) -> Any:
        return {"data": await resolve().queries.person(actor, str(identity))}

    @router.post("/persons/{identity}/merge")
    async def merge_person(identity: UUID4, body: TransferBody, actor: Actor) -> Any:
        return {
            "data": await resolve().commands.identity.change_person(
                actor,
                str(identity),
                str(body.request_id),
                to_person=str(body.to_person),
            )
        }

    @router.post("/persons/{identity}/rename")
    async def rename_person(identity: UUID4, body: RenameBody, actor: Actor) -> Any:
        return {
            "data": await resolve().commands.identity.change_person(
                actor,
                str(identity),
                str(body.request_id),
                display_name=body.display_name,
            )
        }

    @router.post("/persons/{identity}/disable")
    async def disable_person(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.identity.disable_person(
                    actor, str(identity), body.command({})
                )
            )
        }

    @router.post("/persons/{identity}/bindings")
    async def attach_binding(identity: UUID4, body: BindingBody, actor: Actor) -> Any:
        payload = body.model_dump(exclude={"request_id", "expected_revision"})
        return {
            "data": result_view(
                await resolve().commands.identity.attach_binding(
                    actor, str(identity), body.command(payload)
                )
            )
        }

    @router.get("/bindings/{identity}")
    async def binding(identity: UUID4, actor: Actor) -> Any:
        return {"data": await resolve().queries.binding(actor, str(identity))}

    @router.post("/persons/{identity}/client-pair")
    async def client_pair(identity: UUID4, body: IdentityRequest, actor: Actor) -> Any:
        authorize(actor, "identity.client.pair")
        return {
            "data": await resolve().identities.issue_for_operator(
                str(identity), actor.principal_id.text, str(body.request_id)
            )
        }

    @router.post("/persons/{identity}/forget")
    async def forget_person(identity: UUID4, body: IdentityRequest, actor: Actor) -> Any:
        return {
            "data": await resolve().commands.identity.forget_person(
                actor, str(identity), str(body.request_id)
            )
        }

    @router.post("/devices/{identity}/revoke")
    async def client_revoke(identity: UUID4, body: IdentityRequest, actor: Actor) -> Any:
        authorize(actor, "identity.client.revoke")
        return {
            "data": await resolve().identities.revoke_for_operator(
                str(identity), actor.principal_id.text, str(body.request_id)
            )
        }

    @router.post("/bindings/{identity}/transfer")
    async def transfer(identity: UUID4, body: TransferBody, actor: Actor) -> Any:
        return {
            "data": await resolve().commands.identity.transfer_binding(
                actor, str(identity), str(body.to_person), str(body.request_id)
            )
        }

    @router.post("/bindings/{identity}/disable")
    async def disable_binding(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.identity.disable_binding(
                    actor, str(identity), body.command({})
                )
            )
        }

    @router.post("/bindings/{identity}/unbind")
    async def unbind_binding(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.identity.unbind_assignment(
                    actor, str(identity), body.command({}), kind="person"
                )
            )
        }

    @router.get("/plugins")
    async def plugins(actor: Actor) -> Any:
        return {"data": await resolve().queries.plugins_list(actor)}

    @router.get("/plugins/{identity}")
    async def plugin(identity: str, actor: Actor) -> Any:
        return {"data": await resolve().queries.plugin(actor, identity)}

    @router.post("/plugins/{identity}/actions/{action}")
    async def plugin_action(
        identity: str,
        action: Literal["discover", "approve", "enable", "disable", "doctor"],
        body: PluginBody,
        actor: Actor,
    ) -> Any:
        target = "yuki" if action == "discover" else identity
        payload: dict[str, Any] = {"resource_id": target, "action": action}
        if action == "approve":
            payload["spec"] = {"permissions": body.permissions}
        return {
            "data": result_view(
                await resolve().commands.plugin.plugin_action(
                    actor, target, action, body.command(payload), body.permissions
                )
            )
        }

    @router.get("/plugins/{identity}/configuration")
    async def plugin_configuration(
        identity: str,
        actor: Actor,
        scope_type: Literal["global", "user", "group"] = "global",
        owner_id: UUID4 | None = None,
    ) -> Any:
        return {
            "data": await resolve().queries.plugin_config(
                actor, identity, scope_type, str(owner_id) if owner_id is not None else None
            )
        }

    @router.post("/plugins/{identity}/configuration")
    async def configure_plugin(identity: str, body: ConfigBody, actor: Actor) -> Any:
        spec = body.model_dump(mode="json", exclude={"request_id", "expected_revision"})
        command = body.command({"resource_id": identity, "action": "save", "spec": spec})
        return {
            "data": result_view(
                await resolve().commands.plugin.configure_plugin(actor, identity, command, spec)
            )
        }

    @router.get("/plugins/{identity}/observe")
    async def observe(
        identity: str,
        actor: Actor,
        cursor: str | None = None,
        limit: int = Query(default=10, ge=1, le=20),
    ) -> Any:
        authorize(actor, "control.plugin.config.content.read")
        return {
            "data": await resolve().commands.plugin.plugins.observe(
                identity, PluginObservationRequest(cursor=cursor, limit=limit)
            )
        }

    @router.post("/plugin-outbox/{identity}/retry")
    async def retry_outbox(identity: int, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.plugin.retry_outbox(
                    actor, identity, body.command({"resource_id": str(identity), "action": "retry"})
                )
            )
        }

    @router.get("/models")
    async def models(actor: Actor) -> Any:
        authorize(actor, "control.config.file.content.read")
        return {"data": await resolve().commands.model.files.read()}

    @router.post("/models/apply")
    async def apply_models(body: ModelsBody, actor: Actor) -> Any:
        spec = {
            "document": body.document,
            "api_keys": {key: value.get_secret_value() for key, value in body.api_keys.items()},
        }
        return {
            "data": result_view(
                await resolve().commands.model.apply_models(
                    actor,
                    body.command({"resource_id": "model_profiles", "action": "save", "spec": spec}),
                    spec,
                )
            )
        }

    @router.post("/models/secrets")
    async def secret(body: SecretBody, actor: Actor) -> Any:
        authorize(actor, "control.config.file.mutate")
        spec = {"api_keys": {body.name: body.value.get_secret_value()}}
        material = {"resource_id": "model_profiles", "action": "save", "spec": spec}
        return {
            "data": result_view(
                await resolve().commands.model.apply_models(actor, body.command(material), spec)
            )
        }

    @router.post("/works/{identity}/{action}")
    async def work_action(
        identity: UUID4, action: Literal["cancel", "resume"], body: CommandBody, actor: Actor
    ) -> Any:
        return {
            "data": result_view(
                await resolve().commands.work.work_action(
                    actor,
                    str(identity),
                    action,
                    body.command({"resource_id": str(identity), "action": action}),
                )
            )
        }

    async def domain_error(_request: Any, exc: Any) -> JSONResponse:
        code = exc.problem.code
        status = {
            ProblemCode.UNAUTHENTICATED: 401,
            ProblemCode.CAPABILITY_DENIED: 403,
            ProblemCode.NOT_FOUND: 404,
            ProblemCode.VALIDATION_ERROR: 422,
            ProblemCode.OPERATION_UNAVAILABLE: 503,
        }.get(code, 409)
        return JSONResponse({"problem": {"code": code.value}}, status_code=status)

    async def plugin_error(_request: Any, exc: Any) -> JSONResponse:
        code = (
            exc.category
            if isinstance(exc, (PluginConfigurationError, ConfigFileError))
            else "precondition_failed"
        )
        status = {"not_found": 404, "validation_error": 422, "operation_unavailable": 503}.get(
            code, 409
        )
        return JSONResponse({"problem": {"code": code}}, status_code=status)

    app.add_exception_handler(ControlCommandError, domain_error)
    app.add_exception_handler(ControlQueryError, domain_error)
    install_identity_errors(app)
    app.add_exception_handler(PluginConfigurationError, plugin_error)
    app.add_exception_handler(ConfigFileError, plugin_error)
    app.add_exception_handler(PluginManagementRejected, plugin_error)
    app.include_router(router)
