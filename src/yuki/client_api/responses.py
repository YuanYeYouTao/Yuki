"""Typed public projections; retained extension fields never disappear on serialization."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class PublicProjection(BaseModel):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class Device(PublicProjection):
    device_id: str
    purpose: Literal["chat", "connector"]
    label: str
    created_at: datetime
    revoked_at: datetime | None


class Binding(PublicProjection):
    binding_id: str
    platform: str
    display_name: str
    status: str
    source_person_id: str


class Me(PublicProjection):
    person_id: str
    source_person_id: str
    request_person_id: str
    request_conversation_id: str | None
    request_scope: str
    platform: str
    presence_id: str
    thread_id: str | None
    binding_id: str
    display_name: str
    conversation_id: str
    bindings: list[Binding]
    devices: list[Device]


class Conversation(PublicProjection):
    conversation_id: str
    binding_id: str
    platform: str
    display_name: str
    status: str
    generation: int
    latest_event_id: int
    revision: int


class WorkSource(PublicProjection):
    conversation_id: str
    kind: str
    binding_id: str | None
    platform: str | None
    display_name: str
    space_id: str | None


class WorkBudget(PublicProjection):
    root_id: str
    models: int
    model_limit: int | None
    tools: int
    tool_limit: int | None


class Artifact(PublicProjection):
    artifact_id: str
    version: str | None = None
    filename: str | None = None
    name: str | None = None
    size: int | None = None
    mime_type: str | None = None


class WorkResult(PublicProjection):
    event_id: int
    conversation_id: str
    text: str
    artifacts: list[Artifact]


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


class Work(PublicProjection):
    work_id: str
    conversation_id: str
    generation: int
    state: str
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
    reason: str | None = None
    step_count: int | None = None
    steps_token: str | None = None
    wait: WorkWait | None = None
    help_requested: bool | None = None


class Home(PublicProjection):
    main: Conversation
    entries: list[Conversation]
    works: list[Work]
    snapshot_at: str


class Attachment(PublicProjection):
    attachment_index: int
    segment_index: int
    kind: str
    media_id: str | None = None
    artifact_id: str | None = None
    version: str | None = None
    filename: str | None = None
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None


class Transcript(PublicProjection):
    segment_index: int
    text: str
    source: str


class Event(PublicProjection):
    sender_display_name: str | None = None
    id: int
    event_id: int
    conversation_id: str
    event_kind: str
    origin: str
    direction: str
    text: str
    content: str
    binding_id: str
    platform: str
    author_kind: str
    author_person_id: str | None
    attachments: list[Attachment]
    asr: list[Transcript]
    observed_at: str
    occurred_at: str | None
    created_at: str
    work_ids: list[str] = Field(default_factory=list)


class Events(PublicProjection):
    generation: int
    revision: int
    items: list[Event]
    has_more: bool
    next_after: int
    next_before: int | None
    latest_event_id: int


class ActivitySource(PublicProjection):
    origin: str
    started_at: str
    kind: str


class Activity(PublicProjection):
    conversation_id: str
    evidence: str
    sources: list[ActivitySource]
    step_kind: str | None
    revision: int


class MessageReceipt(PublicProjection):
    event_id: int
    conversation_id: str
    replay: bool


class InputReceipt(PublicProjection):
    event_id: int
    input_id: int
    replay: bool


class UploadReceipt(PublicProjection):
    media_id: str
    sha256: str
    size: int
    filename: str
    kind: str
    expires_at: float
    replay: bool | None = None


class PairReceipt(PublicProjection):
    device_id: str
    token: str


class LinkCodeReceipt(PublicProjection):
    code_id: str
    code: str | None
    expires_at: str
    purpose: Literal["chat", "connector"]
    request_id: str | None = None
    replay: bool | None = None


class TurnState(PublicProjection):
    state: Literal["running", "ended", "unknown"]
    outcome: str | None
    error_category: str | None = None


class LinkedWork(PublicProjection):
    work_id: str
    state: str
    revision: int
    reason: str | None
    active: bool


class RequestReceipt(PublicProjection):
    found: bool
    operation_kind: str | None = None
    target: str | None = None
    payload_hash: str | None = None
    result: dict[str, JsonValue] | None = None
    event_id: int | None = None
    conversation_id: str | None = None
    problem_code: str | None = None
    problem: dict[str, JsonValue] | None = None
    state: str | None = None
    status: str | None = None
    turn: TurnState | None = None
    work: LinkedWork | None = None
    result_event_ids: list[int] | None = None


class NativeSession(PublicProjection):
    id: str
    title: str | None = None
    label: str | None = None
    status: str | None = None
    cwd: str | None = None


class NativeSessions(PublicProjection):
    instance_id: str
    sessions: list[NativeSession] | None = None
    next_cursor: str | None = None
    error: str | None = None
    executed: bool | None = None


class NativeEvent(PublicProjection):
    cursor: int
    kind: str
    permission_id: str | None = None
    payload: dict[str, JsonValue] | None = None


class NativeOperation(PublicProjection):
    operation_id: str
    work_id: str | None = None
    action: str | None = None
    instance_id: str
    session_id: str | None = None
    execution_id: str | None = None
    run_id: str | None = None
    status: str | None = None
    pending: bool | None = None
    admitted: bool | None = None
    uncertain: bool | None = None
    events: list[NativeEvent] | None = None
    next_cursor: int | None = None


class NativeOperations(PublicProjection):
    items: list[NativeOperation]
    next_cursor: str | None


class NativeDevice(PublicProjection):
    id: str
    label: str
    online: bool


class NativeInstance(PublicProjection):
    id: str
    local_id: str
    device_id: str
    online: bool
    label: str | None = None
    driver: str | None = None
    capabilities: list[str] | None = None
    status: str | None = None
    error_type: str | None = None


class NativeDirectory(PublicProjection):
    devices: list[NativeDevice]
    instances: list[NativeInstance]


class WorkCancelReceipt(PublicProjection):
    work_id: str
    state: str
    revision: int
    replay: bool
