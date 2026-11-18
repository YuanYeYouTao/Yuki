"""Response models used by the actual personal management routes."""

from typing import Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, JsonValue

from yuki.client_api.responses import Work, WorkWait


class MemoryFactView(BaseModel):
    fact_id: int
    kind: str
    key: str
    text: str
    confidence: float
    revision: str
    updated_at: str
    evidence_count: int
    scope: Literal["person", "self"]
    writable: bool


class MemoryPage(BaseModel):
    items: list[MemoryFactView]
    next_cursor: str | None


class MemoryEvidenceView(BaseModel):
    event_id: int
    conversation_id: str
    excerpt: str


class MemoryDetail(MemoryFactView):
    evidence: list[MemoryEvidenceView]


class MemoryCorrectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: str
    action: Literal["correct", "retract"]
    text: str | None = None


class MemoryCorrectionResult(BaseModel):
    event_id: int
    old_fact_id: int
    new_fact_id: int | None = None
    applied_operation: Literal["correct", "invalidate"]
    revision: str | None = None
    replay: bool = False


class AutomationView(BaseModel):
    automation_id: int
    title: str
    schedule_text: str
    next_run_at: str | None
    state: str
    revision: str
    conversation_id: str | None
    replay: bool | None = None


class AutomationPage(BaseModel):
    items: list[AutomationView]


class AutomationActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: str


class ProactiveRoute(BaseModel):
    binding_id: str | None
    platform: str | None
    paused: bool
    revision: int
    replay: bool | None = None


class ProactiveRouteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID4
    expected_revision: int = Field(strict=True, ge=0)
    binding_id: UUID4


class PluginCapability(BaseModel):
    id: str
    name: str
    description: str


class EnvironmentTool(BaseModel):
    name: str
    version: str


class EnvironmentCapabilities(BaseModel):
    tools: list[EnvironmentTool]
    desktop: bool


class CharacterCapabilities(BaseModel):
    plugins: list[PluginCapability]
    environment: EnvironmentCapabilities


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


class WorkPage(BaseModel):
    works: list[Work]
    next_before: str | None


class StepArtifact(BaseModel):
    artifact_id: str
    version: str


StepKind = Literal[
    "terminal",
    "computer",
    "browser",
    "message",
    "chat",
    "search",
    "web",
    "file",
    "agent_read",
    "agent_control",
    "memory",
    "schedule",
    "task",
    "wait",
    "environment",
    "device_legacy",
    "tool",
]


class StepDetail(BaseModel):
    """Structured public facts of one original call; never arguments or result text."""

    record_seconds: float
    exit_code: int | None = None
    output_lost: bool | None = None
    where: Literal["yuki", "device"] | None = None
    text_delivered: bool | None = None
    target_kind: Literal["group", "private", "channel"] | None = None
    # Present only when the viewer may read that agent operation now.
    operation_id: str | None = None
    # The subtask or delegation record a subtask tool created or targeted (revision 14).
    subtask_id: str | None = None
    relationship: Literal["delegate", "observe", "takeover"] | None = None


class PublicStep(BaseModel):
    step_id: str
    # Legacy display fields kept for older clients.
    category: str
    label: str
    record: str
    status: str
    created_at: str
    updated_at: str
    artifacts: list[StepArtifact]
    kind: StepKind
    tool: str
    action: str | None
    detail: StepDetail


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
