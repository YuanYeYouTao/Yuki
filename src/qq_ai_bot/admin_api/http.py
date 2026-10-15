"""Finite Bearer routes; caller-supplied principals never enter domain commands."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import UUID4, BaseModel, ConfigDict, Field, SecretStr

from qq_ai_bot.admin.authority.command_types import ControlCommandError
from qq_ai_bot.admin.authority.commands import ControlCommand, ControlResult
from qq_ai_bot.admin.authority.operators import ControlOperatorAccess
from qq_ai_bot.admin.authority.principal import ControlPrincipal, PrincipalSource
from qq_ai_bot.admin.authority.problems import ControlQueryError, ProblemCode
from qq_ai_bot.admin.config_files import ConfigFileError
from qq_ai_bot.admin_api.commands import AdminCommands, authorize, fail
from qq_ai_bot.admin_api.queries import AdminQueries
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.plugin_host.configuration_service import PluginConfigurationError
from qq_ai_bot.plugin_host.manager import PluginManagementRejected
from yuki_plugin_sdk.observation import PluginObservationRequest


@dataclass(frozen=True, slots=True)
class AdminApi:
    access: ControlOperatorAccess
    commands: AdminCommands
    queries: AdminQueries


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


class BindingBody(CommandBody):
    platform: str
    external_account_id: str
    display_name: str = ""


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


def result_view(result: ControlResult) -> dict[str, Any]:
    return {
        "success": result.success,
        "resource_id": result.resource_id,
        "revision": result.revision,
        "audit_id": result.audit_id,
        "effective_state": dict(result.effective_state),
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

    @router.post("/persons")
    async def create_person(body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(await resolve().commands.create_person(actor, body.command({})))
        }

    @router.get("/persons/{identity}")
    async def person(identity: UUID4, actor: Actor) -> Any:
        return {"data": await resolve().queries.person(actor, str(identity))}

    @router.post("/persons/{identity}/disable")
    async def disable_person(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.disable_person(actor, str(identity), body.command({}))
            )
        }

    @router.post("/persons/{identity}/bindings")
    async def attach_binding(identity: UUID4, body: BindingBody, actor: Actor) -> Any:
        payload = body.model_dump(exclude={"request_id", "expected_revision"})
        return {
            "data": result_view(
                await resolve().commands.attach_binding(actor, str(identity), body.command(payload))
            )
        }

    @router.get("/bindings/{identity}")
    async def binding(identity: UUID4, actor: Actor) -> Any:
        return {"data": await resolve().queries.binding(actor, str(identity))}

    @router.post("/bindings/{identity}/disable")
    async def disable_binding(identity: UUID4, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.disable_binding(actor, str(identity), body.command({}))
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
                await resolve().commands.plugin_action(
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
                await resolve().commands.configure_plugin(actor, identity, command, spec)
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
            "data": await resolve().commands.plugins.observe(
                identity, PluginObservationRequest(cursor=cursor, limit=limit)
            )
        }

    @router.post("/plugin-outbox/{identity}/retry")
    async def retry_outbox(identity: int, body: CommandBody, actor: Actor) -> Any:
        return {
            "data": result_view(
                await resolve().commands.retry_outbox(
                    actor, identity, body.command({"resource_id": str(identity), "action": "retry"})
                )
            )
        }

    @router.get("/models")
    async def models(actor: Actor) -> Any:
        authorize(actor, "control.config.file.content.read")
        return {"data": await resolve().commands.files.read()}

    @router.post("/models/apply")
    async def apply_models(body: ModelsBody, actor: Actor) -> Any:
        spec = {
            "document": body.document,
            "api_keys": {key: value.get_secret_value() for key, value in body.api_keys.items()},
        }
        return {
            "data": result_view(
                await resolve().commands.apply_models(
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
                await resolve().commands.apply_models(actor, body.command(material), spec)
            )
        }

    @router.post("/works/{identity}/{action}")
    async def work_action(
        identity: UUID4, action: Literal["cancel", "resume"], body: CommandBody, actor: Actor
    ) -> Any:
        return {
            "data": result_view(
                await resolve().commands.work_action(
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

    async def validation_error(_request: Any, _exc: Any) -> JSONResponse:
        # Validation responses never echo credential or secret-bearing input.
        if not _request.url.path.startswith("/admin/v1/"):
            from fastapi.exception_handlers import request_validation_exception_handler

            return await request_validation_exception_handler(_request, _exc)
        return JSONResponse({"problem": {"code": "validation_error"}}, status_code=422)

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
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(PluginConfigurationError, plugin_error)
    app.add_exception_handler(ConfigFileError, plugin_error)
    app.add_exception_handler(PluginManagementRejected, plugin_error)
    app.include_router(router)
