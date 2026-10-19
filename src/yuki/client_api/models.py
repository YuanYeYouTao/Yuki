"""Public response schemas exported for independently built personal clients."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


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
    desktop: str
    android: str | None


class Meta(BaseModel):
    api_revision: int
    server_version: str
    character: PublicCharacter
    min_client: MinimumClient


class Avatar(BaseModel):
    sha256: str
    sizes: dict[str, str]


class CurrentAvatar(Avatar):
    revision: int
    updated_at: str


class Viewer(BaseModel):
    is_owner: bool


class CharacterProfile(BaseModel):
    id: str
    slug: str
    display_name: str
    bio: str
    accent: Accent
    revision: int
    avatar: CurrentAvatar | None
    default_avatar: Avatar | None
    viewer: Viewer
    replay: bool | None = None


class Binding(BaseModel):
    binding_id: str
    platform: str
    display_name: str
    status: str
    source_person_id: str


class Device(BaseModel):
    device_id: str
    label: str
    created_at: datetime
    revoked_at: datetime | None


class Me(BaseModel):
    person_id: str
    source_person_id: str
    request_person_id: str
    request_conversation_id: str | None
    request_scope: str
    platform: str
    presence_id: str | None
    thread_id: str | None
    binding_id: str
    display_name: str
    conversation_id: str
    bindings: list[Binding]
    devices: list[Device]


class Entry(BaseModel):
    conversation_id: str
    binding_id: str
    platform: str
    display_name: str
    status: str
    generation: int
    latest_event_id: int | None
    revision: int


class WorkSource(BaseModel):
    conversation_id: str
    kind: str
    binding_id: str | None
    platform: str | None
    display_name: str
    space_id: str | None


class WorkResult(BaseModel):
    event_id: int
    conversation_id: str
    text: str
    artifacts: list[dict[str, JsonValue]]


class WorkBudget(BaseModel):
    root_id: str
    models: int
    model_limit: int | None
    tools: int
    tool_limit: int | None


class WorkView(BaseModel):
    model_config = ConfigDict(extra="allow")
    work_id: str
    conversation_id: str
    generation: int
    state: Literal[
        "queued",
        "running",
        "waiting_external",
        "waiting_user",
        "suspended",
        "completed",
        "failed",
        "cancelled",
    ]
    revision: int
    goal: str
    source: WorkSource
    result_delivery: WorkSource
    waiting_kind: str | None
    waiting_question: WorkResult | None
    child_count: int
    budget: WorkBudget
    results: list[WorkResult]
    updated_at: float


class Home(BaseModel):
    main: Entry
    entries: list[Entry]
    works: list[WorkView]
    snapshot_at: str


class ProactiveRoute(BaseModel):
    binding_id: str | None
    platform: str | None
    paused: bool
    revision: int
    replay: bool | None = None


class PairResult(BaseModel):
    device_id: str
    token: str


class Attachment(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: str
    media_id: str | None = None
    artifact_id: str | None = None
    version: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    size: int | None = None


class PublicEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    event_id: int
    conversation_id: str
    binding_id: str
    platform: str
    direction: str
    author_kind: str
    text: str
    origin: str
    created_at: str
    attachments: list[Attachment]


class EventsPage(BaseModel):
    items: list[PublicEvent]
    has_more: bool
    next_after: int
    next_before: int | None
    latest_event_id: int | None
    generation: int
    revision: int


class MessageResult(BaseModel):
    event_id: int
    conversation_id: str
    replay: bool


class TurnResult(BaseModel):
    state: str
    outcome: JsonValue = None


class RequestWork(BaseModel):
    model_config = ConfigDict(extra="allow")
    work_id: str
    state: str
    revision: int
    reason: str | None
    active: bool


class OriginalRequest(BaseModel):
    found: bool
    operation_kind: str | None = None
    target: str | None = None
    status: str | None = None
    result: JsonValue = None
    problem_code: str | None = None
    payload_hash: str | None = None
    problem: dict[str, JsonValue] | None = None
    event_id: int | None = None
    conversation_id: str | None = None
    result_event_ids: list[int] | None = None
    turn: TurnResult | None = None
    work: RequestWork | None = None


class ActivitySource(BaseModel):
    origin: str
    started_at: str
    kind: str


class Activity(BaseModel):
    conversation_id: str
    evidence: Literal["executing", "insufficient"]
    sources: list[ActivitySource]
    step_kind: str | None
    revision: int


class WorkHint(BaseModel):
    work_id: str
    revision: int


class SyncHint(BaseModel):
    conversations: list[Entry]
    works: list[WorkHint]
    activity: list[Activity]


class DeviceRevocation(BaseModel):
    device_id: str
    revoked: bool
    replay: bool


class LinkCode(BaseModel):
    code_id: str
    expires_at: str
    code: str | None
    request_id: str | None = None
    replay: bool
