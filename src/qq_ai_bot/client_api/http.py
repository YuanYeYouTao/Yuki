"""Personal-device HTTP boundary; caller identity comes only from the token."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header
from pydantic import UUID4, BaseModel, ConfigDict, Field, SecretStr

from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.http_errors import install_identity_errors
from qq_ai_bot.client_api.identity import ClientIdentityService, ClientPrincipal


@dataclass(frozen=True, slots=True)
class ClientApi:
    identity: ClientIdentityService


class PersonalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4 = Field(default_factory=uuid4)


class PairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr
    device_label: str = Field(min_length=1, max_length=128)


def attach_client_api(app: FastAPI, resolve: Callable[[], ClientApi]) -> None:
    router = APIRouter(prefix="/client/v1")

    async def principal(authorization: Annotated[str | None, Header()] = None) -> ClientPrincipal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise ClientError("unauthenticated")
        return await resolve().identity.authenticate(token)

    Actor = Annotated[ClientPrincipal, Depends(principal)]

    @router.post("/pair")
    async def pair(body: PairBody, authorization: Annotated[str | None, Header()] = None) -> Any:
        if authorization is not None:
            raise ClientError("unauthenticated")
        return {
            "data": await resolve().identity.pair(body.code.get_secret_value(), body.device_label)
        }

    @router.get("/me")
    async def me(actor: Actor) -> Any:
        return {"data": await resolve().identity.me(actor)}

    @router.post("/link-codes")
    async def link_code(actor: Actor, body: PersonalRequest | None = None) -> Any:
        return {
            "data": await resolve().identity.issue_for_device(
                actor, str(body.request_id) if body else str(uuid4())
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
