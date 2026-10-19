"""Public response schemas exported for independently built personal clients."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Envelope[T](BaseModel):
    data: T


class PublicProblemCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    OWNER_REQUIRED = "owner_required"
    NOT_FOUND = "not_found"
    REQUEST_CONFLICT = "request_conflict"
    VERSION_CONFLICT = "version_conflict"
    PRECONDITION_FAILED = "precondition_failed"
    VALIDATION_ERROR = "validation_error"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    CONTROL_HELD = "control_held"
    MEDIA_UNAVAILABLE = "media_unavailable"
    EXECUTOR_OFFLINE = "executor_offline"


class PublicProblem(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: PublicProblemCode | str


class ProblemEnvelope(BaseModel):
    problem: PublicProblem


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


class TimeDueCondition(BaseModel):
    kind: Literal["time_due"]
    due_at: str


class ConversationCondition(BaseModel):
    kind: Literal["conversation"]


class PluginEventCondition(BaseModel):
    kind: Literal["plugin_event"]
    plugin_id: str
    event_type: str


class OwnedRunCondition(BaseModel):
    kind: Literal["owned_run"]
    run_kind: Literal["terminal", "acp", "help_request", "wait_control", "other"]


WaitCondition = Annotated[
    TimeDueCondition | ConversationCondition | PluginEventCondition | OwnedRunCondition,
    Field(discriminator="kind"),
]


class WorkWait(BaseModel):
    mode: Literal["any", "all"]
    since: str
    deadline_at: str | None
    conditions: list[WaitCondition]


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
    trigger_event_id: int | None = None
    step_count: int | None = None
    steps_token: str | None = None
    reason: str | None = None
    help_requested: bool = False
    wait: WorkWait | None = None


class WorkPage(BaseModel):
    works: list[WorkView]
    next_before: str | None


class StepArtifact(BaseModel):
    artifact_id: str
    version: str


class PublicStep(BaseModel):
    step_id: str
    category: str
    label: str
    record: Literal["prepared", "accepted", "failed", "unknown"]
    status: Literal["recorded", "running", "succeeded", "failed", "unknown"]
    created_at: str
    updated_at: str
    artifacts: list[StepArtifact]


class ChildWork(BaseModel):
    work_id: str
    goal: str
    state: str
    revision: int


class WorkSteps(BaseModel):
    steps: list[PublicStep]
    next_after: str | None
    children: list[ChildWork]
    wait: WorkWait | None
    steps_token: str
    work_revision: int


class ArtifactReference(BaseModel):
    event_id: int
    part_index: int


class ArtifactItem(BaseModel):
    ref: ArtifactReference
    artifact_id: str
    version: str
    name: str
    size: int | None
    media_type: str
    conversation_id: str
    work_id: str | None
    published_path: str | None
    created_at: str


class ArtifactPage(BaseModel):
    items: list[ArtifactItem]
    next_cursor: str | None


class WorkInputResult(BaseModel):
    event_id: int
    input_id: int
    replay: bool


class WorkCancelResult(BaseModel):
    work_id: str
    state: str
    revision: int
    replay: bool


class UploadResult(BaseModel):
    media_id: str
    sha256: str
    size: int
    filename: str
    kind: str
    expires_at: float
    replay: bool


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
    attachment_index: int
    availability: Literal["available", "expired", "not_readable"]
    expires_at: str | None


class Transcript(BaseModel):
    state: Literal["available", "none"]
    text: str | None = None


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
    display_text: str | None = None
    work_ids: list[str] = Field(default_factory=list)
    transcript: Transcript | None = None


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
    steps_token: str | None = None


class CharacterHint(BaseModel):
    revision: int


class WorkStepsHint(BaseModel):
    work_id: str


class ArtifactsHint(BaseModel):
    pass


class CharacterEnvironment(BaseModel):
    state: Literal["running", "stopped", "unavailable"]
    display: Literal["running", "stopped", "starting", "unavailable"]
    generation: str | None
    control: dict[str, JsonValue] | None
    help_requested: bool | None


class CharacterMine(BaseModel):
    live_conversations: list[str]
    works: dict[str, int]


class CharacterStatus(BaseModel):
    observed_at: str
    environment: CharacterEnvironment
    mine: CharacterMine
    revision: int


class SyncHint(BaseModel):
    conversations: list[Entry]
    works: list[WorkHint]
    activity: list[Activity]
    character_revision: int
    computer_revision: int | None = None


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
