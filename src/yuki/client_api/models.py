"""Response contracts for actual character and single-SOUL endpoints."""

from typing import Annotated, Any, Literal, NotRequired

from pydantic import UUID4, BaseModel, ConfigDict, Field, JsonValue
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


class PublicProblem(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str


class ProblemEnvelope(BaseModel):
    problem: PublicProblem


class ComputerControlView(BaseModel):
    holder: Literal["none", "agent", "human"]
    lease_id: str | None = None
    device_id: str | None = None
    device_label: str | None = None
    device_online: bool | None = None
    work_id: str | None = None
    since: str | None = None


class ComputerHelp(BaseModel):
    wait_id: str
    kind: Literal["help", "wait_control"]
    work_id: str | None
    note: str | None
    since: str


class ComputerTab(BaseModel):
    target_id: str
    title: str
    url_host: str
    work_id: str | None
    work_state: str | None
    ownership: Literal["work", "unknown"]


class ComputerResources(BaseModel):
    memory_mib: int
    cpus: float


class ComputerView(BaseModel):
    environment: Literal["running", "stopped", "unavailable"]
    display: Literal["running", "stopped", "starting", "unavailable"]
    control: ComputerControlView | None
    help_requests: list[ComputerHelp]
    tabs: list[ComputerTab]
    tabs_available: bool
    resources: ComputerResources | None
    revision: int


class ComputerResult(BaseModel):
    control: ComputerControlView | None = None
    display: Literal["running", "stopped", "starting", "unavailable"] | None = None
    by: dict[str, JsonValue] | str | None = None
    note: str | None = None
    at: str | None = None
    replay: bool | None = None


class DisplayTicket(BaseModel):
    ticket: str
    expires_at: str
    ws_path: str


class ComputerFile(BaseModel):
    name: str
    kind: Literal["directory", "file", "link"]
    size: int
    mtime: float


class ComputerFiles(BaseModel):
    path: str
    items: list[ComputerFile]
    next_cursor: str | None


class ComputerHint(BaseModel):
    revision: int
