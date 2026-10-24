"""Response contracts for actual character and single-SOUL endpoints."""

from typing import Annotated, Any, NotRequired

from pydantic import UUID4, BaseModel, ConfigDict, Field
from typing_extensions import TypedDict


class Envelope[T](BaseModel):
    data: T


class Accent(BaseModel):
    light: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    dark: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class PublicCharacter(BaseModel):
    id: str
    display_name: str
    accent: Accent
    avatar_url: str | None


class MinimumClient(BaseModel):
    desktop: str | None
    android: str | None


class Meta(BaseModel):
    api_revision: int
    server_version: str
    character: PublicCharacter
    min_client: MinimumClient
    capabilities: list[str]


class Viewer(BaseModel):
    is_owner: bool


class Avatar(BaseModel):
    sha256: str
    sizes: dict[str, str]


class CurrentAvatar(Avatar):
    revision: int
    updated_at: str


class CharacterProfile(BaseModel):
    id: str
    slug: str
    display_name: str
    bio: str
    accent: Accent
    revision: int
    avatar_revision: int
    avatar: CurrentAvatar | None
    default_avatar: Avatar | None
    viewer: Viewer
    replay: bool | None = None


class CharacterUpdateBody(TypedDict):
    request_id: UUID4
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    display_name: NotRequired[str]
    slug: NotRequired[str]
    bio: NotRequired[str]
    accent: NotRequired[Accent]


class AvatarResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: int = Field(strict=True, ge=0)


def character_changes(body: CharacterUpdateBody) -> dict[str, Any]:
    changes = {
        key: value for key, value in body.items() if key not in {"request_id", "expected_revision"}
    }
    if "accent" in body:
        changes["accent"] = body["accent"].model_dump()
    return changes


class SoulDocument(BaseModel):
    file_name: str
    content: str
    revision: str
    active_revision: str
    updated_at: str | None
    updated_by: str
    writable: bool
    reason: str | None


class SoulSaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: str
    content: str


class SoulReloadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4


class SoulResult(BaseModel):
    revision: str
    active_revision: str
    applied_at: str
    replay: bool | None = None
