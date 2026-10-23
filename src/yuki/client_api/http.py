"""Personal-device HTTP boundary; caller identity comes only from the token."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import UUID4, BaseModel, ConfigDict, Field, SecretStr

from yuki.character.repository import CharacterRepository
from yuki.character.soul import CharacterSoul
from yuki.client_api.agent_target import AgentTarget
from yuki.client_api.errors import ClientError
from yuki.client_api.files import ClientFiles
from yuki.client_api.http_errors import install_identity_errors
from yuki.client_api.identity import ClientIdentityService, ClientPrincipal
from yuki.client_api.messages import ClientMessages
from yuki.client_api.models import (
    CharacterProfile,
    CharacterUpdateBody,
    Envelope,
    Meta,
    SoulDocument,
    SoulReloadBody,
    SoulResult,
    SoulSaveBody,
    character_changes,
)
from yuki.client_api.prompts import ClientPrompts
from yuki.client_api.responses import (
    Activity,
    Events,
    Home,
    InputReceipt,
    LinkCodeReceipt,
    Me,
    MessageReceipt,
    PairReceipt,
    RequestReceipt,
    UploadReceipt,
    Work,
    WorkCancelReceipt,
)
from yuki.client_api.sessions import ClientSessions
from yuki.client_api.version import CLIENT_API_REVISION
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.identity.credentials import CredentialPurpose


@dataclass(frozen=True, slots=True)
class ClientApi:
    identity: ClientIdentityService
    messages: ClientMessages | None = None
    works: ClientWorks | None = None
    views: ClientViews | None = None
    files: ClientFiles | None = None
    prompts: ClientPrompts | None = None
    character: CharacterRepository | None = None
    soul: CharacterSoul | None = None


class PersonalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4 = Field(default_factory=uuid4)


class PairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr
    device_label: str = Field(min_length=1, max_length=128)


class LinkCodeBody(PersonalRequest):
    purpose: CredentialPurpose = "chat"


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    text: str
    attachments: tuple[str, ...] = ()
    agent_target: AgentTarget | None = None


class WorkInputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    text: str
    agent_target: AgentTarget | None = None


class WorkCancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: int = Field(strict=True, ge=1)


def attach_client_api(app: FastAPI, resolve: Callable[[], ClientApi]) -> None:
    app.extra.setdefault("x-yuki-api-revision", CLIENT_API_REVISION)
    router = APIRouter(prefix="/client/v1")

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

    @router.get("/meta", response_model=Envelope[Meta])
    async def meta() -> Any:
        return {"data": await character().meta()}

    @router.get(
        "/character", response_model=Envelope[CharacterProfile], response_model_exclude_unset=True
    )
    async def character_profile(actor: Actor) -> Any:
        return {"data": await character().get(actor)}

    @router.patch(
        "/character", response_model=Envelope[CharacterProfile], response_model_exclude_unset=True
    )
    async def update_character(body: CharacterUpdateBody, actor: Actor) -> Any:
        return {
            "data": await character().update(
                str(body["request_id"]),
                body["expected_revision"],
                character_changes(body),
                principal=actor,
            )
        }

    @router.get("/character/soul", response_model=Envelope[SoulDocument])
    async def read_soul(actor: Actor) -> Any:
        return {"data": await soul().read(principal=actor)}

    @router.put(
        "/character/soul", response_model=Envelope[SoulResult], response_model_exclude_unset=True
    )
    async def save_soul(body: SoulSaveBody, actor: Actor) -> Any:
        return {
            "data": await soul().save(
                str(body.request_id), body.expected_revision, body.content, principal=actor
            )
        }

    @router.post(
        "/character/soul/reload",
        response_model=Envelope[SoulResult],
        response_model_exclude_unset=True,
    )
    async def reload_soul(body: SoulReloadBody, actor: Actor) -> Any:
        return {"data": await soul().reload(str(body.request_id), principal=actor)}

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
        async with resolve().identity.database.read_session() as session:
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

    @router.post(
        "/uploads", response_model=Envelope[UploadReceipt], response_model_exclude_unset=True
    )
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

    @router.get("/artifacts/{identity}")
    async def artifact(identity: UUID, actor: Actor, version: str) -> Response:
        metadata, content = await files().artifact(actor, str(identity), version)
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "ETag": f'"{metadata["sha256"]}"',
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(metadata['name'])}",
            },
        )

    @router.get("/home", response_model=Envelope[Home], response_model_exclude_unset=True)
    async def home(actor: Actor) -> Any:
        return {"data": await views().home(actor)}

    @router.get("/conversations/{identity}/events/{event_id}/attachments/{attachment_index}")
    async def attachment(
        identity: UUID4, event_id: int, attachment_index: int, actor: Actor
    ) -> Response:
        metadata, content = await files().attachment(
            actor, str(identity), event_id, attachment_index
        )
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "ETag": f'"{metadata["sha256"]}"',
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(metadata['name'])}",
            },
        )

    @router.get(
        "/conversations/{identity}/events",
        response_model=Envelope[Events],
        response_model_exclude_unset=True,
    )
    async def events(
        identity: UUID4,
        actor: Actor,
        after: int = Query(0, ge=0),
        before: int | None = Query(None, ge=1),
        limit: int = Query(50, ge=1),
    ) -> Any:
        return {
            "data": await views().events(
                actor, str(identity), after=after, before=before, limit=limit
            )
        }

    @router.get("/activity", response_model=Envelope[Activity], response_model_exclude_unset=True)
    async def activity(actor: Actor, conversation_id: UUID4) -> Any:
        return {"data": await views().activity(actor, str(conversation_id))}

    @router.get("/stream")
    async def stream(actor: Actor) -> StreamingResponse:
        return StreamingResponse(
            views().stream(actor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get(
        "/works/{identity}", response_model=Envelope[Work], response_model_exclude_unset=True
    )
    async def work(identity: UUID4, actor: Actor) -> Any:
        return {"data": await works().get(actor, str(identity))}

    @router.post(
        "/works/{identity}/inputs",
        response_model=Envelope[InputReceipt],
        response_model_exclude_unset=True,
    )
    async def work_input(identity: UUID4, body: WorkInputBody, actor: Actor) -> Any:
        return {
            "data": await works().submit(
                actor, str(identity), str(body.request_id), body.text, body.agent_target
            )
        }

    @router.post(
        "/works/{identity}/cancel",
        response_model=Envelope[WorkCancelReceipt],
        response_model_exclude_unset=True,
    )
    async def work_cancel(identity: UUID4, body: WorkCancelBody, actor: Actor) -> Any:
        return {
            "data": await works().cancel(
                actor, str(identity), str(body.request_id), body.expected_revision
            )
        }

    @router.post(
        "/messages", response_model=Envelope[MessageReceipt], response_model_exclude_unset=True
    )
    async def messages(body: MessageBody, actor: Actor) -> Any:
        service = resolve().messages
        if service is None:
            raise ClientError("unavailable")
        return {
            "data": await service.submit(
                actor, str(body.request_id), body.text, body.attachments, body.agent_target
            )
        }

    @router.get(
        "/requests/{request_id}",
        response_model=Envelope[RequestReceipt],
        response_model_exclude_unset=True,
    )
    async def original_request(request_id: UUID4, actor: Actor) -> Any:
        from yuki.client_api.requests import ClientRequests

        return {
            "data": await ClientRequests(resolve().identity, prompts=resolve().prompts).get(
                actor, str(request_id)
            )
        }

    @router.post("/pair", response_model=Envelope[PairReceipt], response_model_exclude_unset=True)
    async def pair(body: PairBody, authorization: Annotated[str | None, Header()] = None) -> Any:
        if authorization is not None:
            raise ClientError("unauthenticated")
        return {
            "data": await resolve().identity.pair(body.code.get_secret_value(), body.device_label)
        }

    @router.get("/me", response_model=Envelope[Me], response_model_exclude_unset=True)
    async def me(actor: Actor) -> Any:
        return {"data": await resolve().identity.me(actor)}

    @router.post(
        "/link-codes", response_model=Envelope[LinkCodeReceipt], response_model_exclude_unset=True
    )
    async def link_code(actor: Actor, body: LinkCodeBody | None = None) -> Any:
        return {
            "data": await resolve().identity.issue_for_device(
                actor,
                str(body.request_id) if body else str(uuid4()),
                purpose=body.purpose if body else "chat",
            )
        }

    @router.delete("/devices/{identity}")
    async def revoke(identity: UUID4, actor: Actor, body: PersonalRequest | None = None) -> Any:
        return {
            "data": await resolve().identity.revoke(
                actor, str(identity), str(body.request_id) if body else str(uuid4())
            )
        }

    install_identity_errors(app)
    app.include_router(router)
