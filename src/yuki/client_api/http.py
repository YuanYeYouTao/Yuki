"""Personal-device HTTP boundary; caller identity comes only from the token."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import UUID4, BaseModel, ConfigDict, Field, SecretStr
from starlette.background import BackgroundTask

from yuki.character.avatar import CharacterAvatars
from yuki.client_api.automations import ClientAutomations
from yuki.client_api.capabilities import ClientCapabilities
from yuki.client_api.computer import ClientComputer
from yuki.client_api.errors import ClientError
from yuki.client_api.files import ClientFiles
from yuki.client_api.http_errors import install_identity_errors
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.memory import ClientMemory
from yuki.client_api.messages import ClientMessages
from yuki.client_api.models import (
    Accent,
    Activity,
    ArtifactPage,
    ArtifactsHint,
    AutomationPage,
    AutomationView,
    CharacterCapabilities,
    CharacterHint,
    CharacterProfile,
    CharacterStatus,
    ComputerFiles,
    ComputerHint,
    ComputerResult,
    ComputerView,
    DeviceRevocation,
    DisplayTicket,
    Entry,
    Envelope,
    EventsPage,
    Home,
    LinkCode,
    Me,
    MemoryCorrectionResult,
    MemoryDetail,
    MemoryPage,
    MessageResult,
    Meta,
    OriginalRequest,
    PairResult,
    ProactiveRoute,
    ProblemEnvelope,
    SyncHint,
    UploadResult,
    WorkCancelResult,
    WorkHint,
    WorkInputResult,
    WorkPage,
    WorkSteps,
    WorkStepsHint,
    WorkView,
)
from yuki.client_api.prompts import ClientPrompts
from yuki.client_api.sessions import ClientSessions
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks


@dataclass(frozen=True, slots=True)
class ClientApi:
    identity: ClientIdentityService
    messages: ClientMessages | None = None
    works: ClientWorks | None = None
    views: ClientViews | None = None
    files: ClientFiles | None = None
    prompts: ClientPrompts | None = None
    computer: ClientComputer | None = None
    memory: ClientMemory | None = None
    automations: ClientAutomations | None = None
    capabilities: ClientCapabilities | None = None
    avatars: CharacterAvatars | None = None


class PersonalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4 = Field(default_factory=uuid4)


class MemoryCorrectionBody(PersonalRequest):
    request_id: UUID4 = Field(...)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["retract", "correct"]
    text: str | None = None


class AutomationActionBody(PersonalRequest):
    request_id: UUID4 = Field(...)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class AvatarResetBody(PersonalRequest):
    request_id: UUID4 = Field(...)
    expected_revision: int = Field(strict=True, ge=0)


class PairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr
    device_label: str = Field(min_length=1, max_length=128)


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    text: str
    attachments: tuple[str, ...] = ()


class WorkInputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    text: str


class WorkCancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: int = Field(strict=True, ge=1)


class CharacterBody(PersonalRequest):
    expected_revision: int = Field(strict=True, ge=1)
    display_name: str | None = Field(default=None, min_length=1)
    bio: str | None = None
    accent: Accent | None = None


class ProactiveRouteBody(PersonalRequest):
    expected_revision: int = Field(strict=True, ge=0)
    binding_id: UUID4


class ComputerRequest(PersonalRequest):
    request_id: UUID4 = Field(...)


class ComputerControlBody(ComputerRequest):
    action: Literal["take", "return", "transfer"]
    lease_id: UUID4 | None = None
    note: str | None = None


class ComputerSleepBody(ComputerRequest):
    confirm_active_work_tabs: bool = Field(default=False, strict=True)


def attach_client_api(app: FastAPI, resolve: Callable[[], ClientApi]) -> None:
    router = APIRouter(
        prefix="/client/v1",
        responses={
            status: {"model": ProblemEnvelope} for status in (401, 403, 404, 409, 422, 500, 503)
        },
    )

    async def principal(
        authorization: Annotated[str | None, Header()] = None,
        x_yuki_entry: Annotated[str | None, Header()] = None,
        x_yuki_conversation: Annotated[str | None, Header()] = None,
    ) -> ClientPrincipal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise ClientError("unauthenticated")
        return await resolve().identity.authenticate(
            token, binding_id=x_yuki_entry, conversation_id=x_yuki_conversation
        )

    Actor = Annotated[ClientPrincipal, Depends(principal)]

    async def base_principal(
        authorization: Annotated[str | None, Header()] = None,
        x_yuki_entry: Annotated[str | None, Header()] = None,
    ) -> ClientPrincipal:
        return await principal(authorization, x_yuki_entry, None)

    BaseActor = Annotated[ClientPrincipal, Depends(base_principal)]

    from yuki.character.repository import CharacterRepository
    from yuki.client_api.routes import ClientProactiveRoute

    @router.get("/meta", response_model=Envelope[Meta])
    async def meta() -> Any:
        return {"data": await CharacterRepository(resolve().identity).meta()}

    @router.get(
        "/character", response_model=Envelope[CharacterProfile], response_model_exclude_unset=True
    )
    async def character(actor: Actor) -> Any:
        return {"data": await CharacterRepository(resolve().identity).get(actor)}

    @router.get("/character/status", response_model=Envelope[CharacterStatus])
    async def character_status(actor: Actor) -> Any:
        return {"data": await views().character_status(actor)}

    @router.patch(
        "/character", response_model=Envelope[CharacterProfile], response_model_exclude_unset=True
    )
    async def update_character(body: CharacterBody, actor: Actor) -> Any:
        changes = body.model_dump(
            mode="json", exclude={"request_id", "expected_revision"}, exclude_unset=True
        )
        if any(value is None for value in changes.values()):
            raise ClientError("validation_error")
        return {
            "data": await CharacterRepository(resolve().identity).update(
                str(body.request_id),
                body.expected_revision,
                changes,
                principal=actor,
            )
        }

    @router.get(
        "/me/proactive-route",
        response_model=Envelope[ProactiveRoute],
        response_model_exclude_unset=True,
    )
    async def proactive_route(actor: Actor) -> Any:
        return {"data": await ClientProactiveRoute(resolve().identity).get(actor)}

    @router.put(
        "/me/proactive-route",
        response_model=Envelope[ProactiveRoute],
        response_model_exclude_unset=True,
    )
    async def set_proactive_route(body: ProactiveRouteBody, actor: Actor) -> Any:
        return {
            "data": await ClientProactiveRoute(resolve().identity).set(
                actor,
                str(body.request_id),
                body.expected_revision,
                str(body.binding_id),
            )
        }

    @router.post("/acp/sessions")
    async def new_session(body: PersonalRequest, actor: BaseActor) -> Any:
        return {
            "data": await ClientSessions(resolve().identity).create(actor, str(body.request_id))
        }

    @router.get("/acp/sessions/{session_id}")
    async def load_session(session_id: UUID4, actor: Actor) -> Any:
        return {"data": await ClientSessions(resolve().identity).load(actor, str(session_id))}

    @router.post("/acp/sessions/{session_id}/requests/{request_id}/cancel")
    async def cancel_prompt(session_id: UUID4, request_id: UUID4, actor: Actor) -> Any:
        service = resolve().prompts
        if service is None:
            raise ClientError("unavailable")
        async with resolve().identity.database.sessions() as session:
            actor = await resolve().identity.require_device(
                session,
                actor.device_id,
                binding_id=actor.binding_id if actor.platform == "acp" else None,
                conversation_id=str(session_id),
            )
        return {"data": await service.cancel(actor, str(request_id))}

    def works() -> ClientWorks:
        service = resolve().works
        if service is None:
            raise ClientError("unavailable")
        return service

    def views() -> ClientViews:
        return resolve().views or ClientViews(resolve().identity, works())

    def files() -> ClientFiles:
        service = resolve().files
        if service is None:
            raise ClientError("unavailable")
        return service

    @router.post("/uploads", response_model=Envelope[UploadResult])
    async def upload(
        request: Request,
        actor: Actor,
        request_id: UUID4,
        sha256: str,
        size: int,
        filename: str,
        kind: str = "file",
    ) -> Any:
        return {
            "data": await files().uploads.upload(
                actor, str(request_id), sha256, size, filename, kind, request.stream()
            )
        }

    @router.get("/artifacts", response_model=Envelope[ArtifactPage])
    async def artifact_page(
        actor: Actor,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=50),
        kind: Literal["all", "image", "document", "code"] = "all",
    ) -> Any:
        return {"data": await files().list(actor, cursor, limit, kind)}

    @router.get("/artifacts/{identity}")
    async def artifact(identity: UUID, actor: Actor, version: str) -> Response:
        metadata, content = await files().artifact(actor, str(identity), version)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "ETag": f'"{metadata["sha256"]}"',
                "X-Yuki-Sha256": metadata["sha256"],
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(metadata['name'])}",
            },
        )

    @router.get("/home", response_model=Envelope[Home])
    async def home(actor: Actor, works: Literal["active"] | None = None) -> Any:
        return {"data": await views().home(actor, active=works == "active")}

    @router.get(
        "/conversations/{identity}/events",
        response_model=Envelope[EventsPage],
        response_model_exclude_unset=True,
    )
    async def events(
        identity: UUID4,
        actor: Actor,
        after: int = Query(0, ge=0),
        before: int | None = Query(None, ge=1),
        limit: int = Query(50, ge=1, le=50),
    ) -> Any:
        return {
            "data": await views().events(
                actor, str(identity), after=after, before=before, limit=limit
            )
        }

    @router.get("/activity", response_model=Envelope[Activity])
    async def activity(actor: Actor, conversation_id: UUID4) -> Any:
        return {"data": await views().activity(actor, str(conversation_id))}

    @router.get(
        "/conversations/{identity}/events/{event_id}/attachments/{index}",
        response_class=FileResponse,
        responses={
            200: {
                "content": {"application/octet-stream": {}},
                "headers": {"X-Yuki-Sha256": {"schema": {"type": "string"}}},
            }
        },
    )
    async def attachment_content(
        identity: UUID4, event_id: int, index: int, actor: Actor
    ) -> FileResponse:
        item, path = await files().attachment_content(actor, str(identity), event_id, index)
        assert item.content_sha256 is not None
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=item.display_name or f"attachment-{index}",
            headers={"X-Yuki-Sha256": item.content_sha256},
        )

    @router.get("/stream")
    async def stream(actor: Actor, works: Literal["active"] | None = None) -> StreamingResponse:
        return StreamingResponse(
            views().stream(actor, active=works == "active"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/works", response_model=Envelope[WorkPage])
    async def works_page(
        actor: Actor,
        group: Literal["active", "ended"] = "active",
        origin: Literal["all", "this_entry", "other"] = "all",
        before: str | None = None,
        limit: int = Query(default=50, ge=1, le=50),
    ) -> Any:
        return {"data": await works().list(actor, group, origin, before, limit)}

    @router.get("/works/{identity}/steps", response_model=Envelope[WorkSteps])
    async def work_steps(
        identity: UUID4,
        actor: Actor,
        after: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> Any:
        return {"data": await works().steps(actor, str(identity), after, limit)}

    @router.get("/works/{identity}", response_model=Envelope[WorkView])
    async def work(identity: UUID4, actor: Actor) -> Any:
        return {"data": await works().get(actor, str(identity))}

    @router.post("/works/{identity}/inputs", response_model=Envelope[WorkInputResult])
    async def work_input(identity: UUID4, body: WorkInputBody, actor: Actor) -> Any:
        return {"data": await works().submit(actor, str(identity), str(body.request_id), body.text)}

    @router.post("/works/{identity}/cancel", response_model=Envelope[WorkCancelResult])
    async def work_cancel(identity: UUID4, body: WorkCancelBody, actor: Actor) -> Any:
        return {
            "data": await works().cancel(
                actor, str(identity), str(body.request_id), body.expected_revision
            )
        }

    @router.post("/messages", response_model=Envelope[MessageResult])
    async def messages(body: MessageBody, actor: Actor) -> Any:
        service = resolve().messages
        if service is None:
            raise ClientError("unavailable")
        return {
            "data": await service.submit(actor, str(body.request_id), body.text, body.attachments)
        }

    @router.get(
        "/requests/{request_id}",
        response_model=Envelope[OriginalRequest],
        response_model_exclude_unset=True,
    )
    async def original_request(request_id: UUID4, actor: Actor) -> Any:
        from yuki.client_api.requests import ClientRequests

        return {
            "data": await ClientRequests(resolve().identity, prompts=resolve().prompts).get(
                actor, str(request_id)
            )
        }

    @router.post("/pair", response_model=Envelope[PairResult])
    async def pair(body: PairBody, authorization: Annotated[str | None, Header()] = None) -> Any:
        if authorization is not None:
            raise ClientError("unauthenticated")
        return {
            "data": await resolve().identity.pair(body.code.get_secret_value(), body.device_label)
        }

    @router.get("/me", response_model=Envelope[Me])
    async def me(actor: Actor) -> Any:
        return {"data": await resolve().identity.me(actor)}

    @router.post(
        "/link-codes", response_model=Envelope[LinkCode], response_model_exclude_unset=True
    )
    async def link_code(actor: Actor, body: PersonalRequest | None = None) -> Any:
        return {
            "data": await resolve().identity.issue_for_device(
                actor, str(body.request_id) if body else str(uuid4())
            )
        }

    @router.delete("/devices/{identity}", response_model=Envelope[DeviceRevocation])
    async def revoke(identity: UUID4, actor: Actor, body: PersonalRequest | None = None) -> Any:
        return {
            "data": await resolve().identity.revoke(
                actor, str(identity), str(body.request_id) if body else str(uuid4())
            )
        }

    def avatars() -> CharacterAvatars:
        service = resolve().avatars
        if service is None:
            raise ClientError("unavailable")
        return service

    @router.put(
        "/character/avatar",
        response_model=Envelope[CharacterProfile],
        response_model_exclude_unset=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
            }
        },
    )
    async def set_avatar(
        request: Request,
        actor: Actor,
        request_id: UUID4,
        sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
        size: Annotated[int, Query(ge=0)],
        expected_revision: Annotated[int, Query(ge=0)],
    ) -> Any:
        return {
            "data": await avatars().change(
                str(request_id),
                expected_revision,
                action="set",
                sha256=sha256,
                size=size,
                chunks=request.stream(),
                principal=actor,
            )
        }

    @router.delete(
        "/character/avatar",
        response_model=Envelope[CharacterProfile],
        response_model_exclude_unset=True,
    )
    async def reset_avatar(body: AvatarResetBody, actor: Actor) -> Any:
        return {
            "data": await avatars().change(
                str(body.request_id), body.expected_revision, action="reset", principal=actor
            )
        }

    @router.get(
        "/character/avatar/{sha256}/{size}.png",
        response_class=FileResponse,
        responses={
            200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}
        },
    )
    async def avatar_file(
        sha256: str,
        size: int,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Any:
        actor = await principal(authorization) if authorization is not None else None
        path = await avatars().file(sha256, size, actor)
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get(
        "/automations", response_model=Envelope[AutomationPage], response_model_exclude_unset=True
    )
    async def automations(actor: Actor, scope: Literal["mine", "self"] = "mine") -> Any:
        service = resolve().automations
        if service is None:
            raise ClientError("unavailable")
        return {"data": await service.list(actor, scope)}

    @router.post(
        "/automations/{automation_id}/actions/{action}",
        response_model=Envelope[AutomationView],
        response_model_exclude_unset=True,
    )
    async def automation_action(
        automation_id: int,
        action: Literal["pause", "resume", "delete"],
        body: AutomationActionBody,
        actor: Actor,
    ) -> Any:
        service = resolve().automations
        if service is None:
            raise ClientError("unavailable")
        return {
            "data": await service.action(
                actor, automation_id, str(body.request_id), body.expected_revision, action
            )
        }

    @router.get("/character/capabilities", response_model=Envelope[CharacterCapabilities])
    async def capabilities(actor: Actor) -> Any:
        service = resolve().capabilities
        if service is None:
            raise ClientError("unavailable")
        return {"data": await service.get(actor)}

    def memory() -> ClientMemory:
        service = resolve().memory
        if service is None:
            raise ClientError("unavailable")
        return service

    @router.get("/memory/facts", response_model=Envelope[MemoryPage])
    async def memory_facts(
        actor: Actor,
        about: Literal["me", "self"] = "me",
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> Any:
        return {"data": await memory().list(actor, about=about, before=cursor, limit=limit)}

    @router.get("/memory/facts/{fact_id}", response_model=Envelope[MemoryDetail])
    async def memory_fact(fact_id: int, actor: Actor) -> Any:
        return {"data": await memory().get(actor, fact_id)}

    @router.post(
        "/memory/facts/{fact_id}/corrections",
        response_model=Envelope[MemoryCorrectionResult],
        response_model_exclude_unset=True,
    )
    async def memory_correction(fact_id: int, body: MemoryCorrectionBody, actor: Actor) -> Any:
        return {
            "data": await memory().correct(
                actor,
                fact_id,
                str(body.request_id),
                body.expected_revision,
                body.action,
                body.text,
            )
        }

    def computer() -> ClientComputer:
        service = resolve().computer
        if service is None:
            raise ClientError("unavailable")
        return service

    @router.get(
        "/computer", response_model=Envelope[ComputerView], response_model_exclude_unset=True
    )
    async def get_computer(actor: Actor) -> Any:
        return {"data": await computer().get(actor)}

    @router.post(
        "/computer/control",
        response_model=Envelope[ComputerResult],
        response_model_exclude_unset=True,
    )
    async def control_computer(body: ComputerControlBody, actor: Actor) -> Any:
        payload = {"lease_id": str(body.lease_id) if body.lease_id else None, "note": body.note}
        return {"data": await computer().command(actor, str(body.request_id), body.action, payload)}

    @router.post(
        "/computer/wake", response_model=Envelope[ComputerResult], response_model_exclude_unset=True
    )
    async def wake_computer(body: ComputerRequest, actor: Actor) -> Any:
        return {"data": await computer().command(actor, str(body.request_id), "wake", {})}

    @router.post(
        "/computer/sleep",
        response_model=Envelope[ComputerResult],
        response_model_exclude_unset=True,
    )
    async def sleep_computer(body: ComputerSleepBody, actor: Actor) -> Any:
        return {
            "data": await computer().command(
                actor,
                str(body.request_id),
                "sleep",
                {"confirm_active_work_tabs": body.confirm_active_work_tabs},
            )
        }

    @router.post("/computer/display-tickets", response_model=Envelope[DisplayTicket])
    async def display_ticket(actor: Actor, request: Request) -> Any:
        return {"data": await computer().ticket(actor, request.scope.get("root_path", ""))}

    @router.websocket("/computer/display")
    async def display(socket: WebSocket, ticket: str = "") -> None:
        if resolve().computer is None:
            await socket.close(code=1012)
            return
        await computer().display(socket, ticket)

    @router.get("/computer/files", response_model=Envelope[ComputerFiles])
    async def computer_files(
        actor: Actor,
        path: str = "",
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> Any:
        from yuki.client_api.work_projection import cursor as encode_cursor
        from yuki.client_api.work_projection import decode_cursor

        after = None
        if cursor:
            value = decode_cursor(cursor)
            if (
                not isinstance(value, list)
                or len(value) != 2
                or value[0] != path
                or not isinstance(value[1], str)
            ):
                raise ClientError("validation_error")
            after = value[1]
        result = await computer().files(actor, path, after, limit)
        result["next_cursor"] = (
            encode_cursor([path, result.pop("next_after")]) if result.get("next_after") else None
        )
        result.pop("next_after", None)
        return {"data": result}

    @router.get(
        "/computer/files/content",
        responses={
            200: {
                "content": {"application/octet-stream": {}},
                "headers": {
                    "X-Yuki-Sha256": {"schema": {"type": "string"}},
                    "Content-Length": {"schema": {"type": "integer"}},
                },
            }
        },
    )
    async def computer_file_content(actor: Actor, path: str) -> StreamingResponse:
        metadata, body, close = await computer().content(actor, path)
        return StreamingResponse(
            body,
            background=BackgroundTask(close),
            media_type="application/octet-stream",
            headers={
                "X-Yuki-Sha256": metadata["sha256"],
                "Content-Length": str(metadata["size"]),
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(metadata["name"], safe=""),
                "Cache-Control": "no-store",
            },
        )

    install_identity_errors(app)
    app.include_router(router)
    original_openapi = app.openapi

    def openapi_with_sse() -> dict[str, Any]:
        schema = original_openapi()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in (
            SyncHint,
            Entry,
            WorkHint,
            Activity,
            CharacterHint,
            ComputerHint,
            WorkStepsHint,
            ArtifactsHint,
        ):
            document = model.model_json_schema(ref_template="#/components/schemas/{model}")
            components.update(document.pop("$defs", {}))
            components[model.__name__] = document
        schema["x-yuki-sse-kinds"] = {
            "sync": "#/components/schemas/SyncHint",
            "conversation": "#/components/schemas/Entry",
            "work": "#/components/schemas/WorkHint",
            "activity": "#/components/schemas/Activity",
            "character": "#/components/schemas/CharacterHint",
            "computer": "#/components/schemas/ComputerHint",
            "work_steps": "#/components/schemas/WorkStepsHint",
            "artifacts": "#/components/schemas/ArtifactsHint",
        }
        schema["x-yuki-websockets"] = {
            "/client/v1/computer/display": {
                "subprotocol": "binary",
                "ticket": "single-use-device-bound-60s",
                "protocol": "RFB 3.8",
            }
        }
        return schema

    app.openapi = openapi_with_sse  # type: ignore[method-assign]
