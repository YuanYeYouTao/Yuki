"""Message models shared by adapters and business services."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from yuki.domain.conversations import ConversationScope, ScopeType

if TYPE_CHECKING:
    from yuki.services.media_resolver import PreparedAttachment


def sanitize_display_name(value: str) -> str:
    """Flatten untrusted platform identity text for one-line model metadata."""

    visible = "".join(
        " " if character.isspace() else character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )
    return " ".join(visible.split())[:128]


class AttachmentKind(StrEnum):
    """Attachment kinds intentionally not downloaded by the MVP."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    FORWARD = "forward"
    CARD = "card"
    UNKNOWN = "unknown"


class ReasoningEffort(StrEnum):
    """Provider-neutral reasoning effort values used by Responses APIs."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class NativeToolType(StrEnum):
    """Provider-executed tools supported by the compatibility layer."""

    WEB_SEARCH = "web_search"


class NativeToolStatus(StrEnum):
    """Normalized lifecycle state for a provider-executed tool."""

    IN_PROGRESS = "in_progress"
    SEARCHING = "searching"
    COMPLETED = "completed"
    FAILED = "failed"


class CitationOrigin(StrEnum):
    """Where a provider-neutral source URL was recovered."""

    ANNOTATION = "annotation"
    NATIVE_RESULT = "native_result"
    OPEN_PAGE_ACTION = "open_page_action"
    ANSWER_TEXT = "answer_text"


class ModelResponseStatus(StrEnum):
    """Successful provider response states returned to the Agent loop."""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class ProviderContinuation:
    """Opaque provider-owned state that may only live inside one Agent turn."""

    provider: str
    protocol: str
    payload: object = field(repr=False)
    profile_id: str = ""


@dataclass(frozen=True, slots=True)
class MessageAttachment:
    """Transient event media reference; payload fields are never persisted verbatim."""

    kind: AttachmentKind
    label: str
    segment_index: int = 0
    source: str = "current"
    file: str | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    summary: str | None = field(default=None, repr=False)
    sub_type: str | None = None
    file_size: int | None = None
    emoji_id: str | None = None
    emoji_package_id: str | None = None
    is_sticker: bool = False
    sticker_format: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    preview: MessageAttachment | None = field(default=None, repr=False)
    key: str | None = field(default=None, repr=False)
    filename: str | None = field(default=None, repr=False)
    local_source: tuple[Path, str] | None = field(default=None, repr=False)
    # Host-owned bytes for this admitted message only; never persisted or supplied by adapters.
    prepared_media: PreparedAttachment | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SenderIdentity:
    """Platform-neutral sender identity."""

    user_id: str
    nickname: str = ""
    group_card: str = ""
    is_bot: bool = False
    group_card_known: bool = False
    nickname_known: bool = False
    native_username: str = ""

    @property
    def display_name(self) -> str:
        """Return the event-provided display name without database fallback."""

        return self.group_card or self.nickname


@dataclass(frozen=True, slots=True)
class MessageMention:
    """One real platform mention, located in its own message's normalized text."""

    kind: str
    segment_index: int
    text_start: int
    text_end: int
    target_user_id: str | None = None
    display_name: str = ""
    person_id: str | None = None
    presence_id: str | None = None
    account_id: str | None = None
    assignment_id: str | None = None

    @property
    def text(self) -> str:
        name = sanitize_display_name(self.display_name)
        return "@" + ("全体成员" if self.kind == "all" else name or "未识别成员")


def render_message_mentions(
    text: str, mentions: tuple[MessageMention, ...]
) -> tuple[str, tuple[MessageMention, ...]]:
    """Replace only native mention spans, preserving literal @ text and repetition."""

    parts: list[str] = []
    rendered: list[MessageMention] = []
    cursor = length = 0
    for mention in mentions:
        prefix = text[cursor : mention.text_start]
        parts.extend((prefix, mention.text))
        length += len(prefix)
        rendered.append(replace(mention, text_start=length, text_end=length + len(mention.text)))
        length += len(mention.text)
        cursor = mention.text_end
    parts.append(text[cursor:])
    return "".join(parts), tuple(rendered)


def snapshot_mention_segments(
    segments: tuple[dict[str, object], ...], mentions: tuple[MessageMention, ...]
) -> tuple[dict[str, object], ...]:
    """Freeze displayed targets at their original positions, retaining native provenance."""

    result = list(segments)
    for mention in mentions:
        original = result[mention.segment_index]
        data = original.get("data", {})
        source = (
            data.get("source", original)
            if original.get("type") == "mention" and isinstance(data, dict)
            else original
        )
        result[mention.segment_index] = {
            "type": "mention",
            "data": {
                "kind": mention.kind,
                "display_name": mention.display_name,
                "person_id": mention.person_id,
                "presence_id": mention.presence_id,
                "account_id": mention.account_id,
                "assignment_id": mention.assignment_id,
                "text_start": mention.text_start,
                "text_end": mention.text_end,
                "source": source,
            },
        }
    return tuple(result)


MENTION_SNAPSHOT_FIELDS = (
    "kind",
    "display_name",
    "account_id",
    "assignment_id",
    "person_id",
    "presence_id",
    "text_start",
    "text_end",
)


def mention_snapshots(segments: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """The common event-time identity/span facts, without transport source payloads."""
    return tuple(
        {key: data.get(key) for key in MENTION_SNAPSHOT_FIELDS}
        for segment in segments
        if segment.get("type") == "mention" and isinstance(data := segment.get("data"), dict)
    )


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Normalized inbound message consumed by policies and chat services."""

    message_id: str
    event_type: str
    scope_type: ScopeType
    sender: SenderIdentity
    text: str
    bot_user_id: str = ""
    platform: str = "qq"
    raw_text: str = ""
    group_id: str | None = None
    mentions_bot: bool = False
    is_self_message: bool = False
    reply_text: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    mentioned_person_ids: tuple[str, ...] = ()
    mentions: tuple[MessageMention, ...] = ()
    reply_mentions: tuple[MessageMention, ...] = ()
    attachments: tuple[MessageAttachment, ...] = ()
    segments: tuple[dict[str, object], ...] = ()
    reply_attachments: tuple[MessageAttachment, ...] = ()
    reply_segments: tuple[dict[str, object], ...] = ()
    reply_to_message_id: str | None = None
    reply_sender_user_id: str | None = None
    reply_to_event_id: int | None = None
    canonical_reply_to_yuki: bool | None = None
    canonical_reply_author_kind: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    occurred_at: datetime | None = None
    legacy_conversation_key: str | None = None
    person_id: str | None = None
    space_id: str | None = None
    conversation_id: str | None = None
    presence_id: str | None = None
    thread_id: str | None = None
    # Host-stamped after ledger admission; never supplied by a transport adapter.
    source_event_id: int | None = None
    author_kind: str | None = None
    author_account_id: str | None = None
    source_execution_id: str | None = None
    yuki_account_ids: frozenset[str] = frozenset()
    group_name: str = ""

    @property
    def source_key(self) -> str:
        """Internal work provenance, independent of transport message credentials."""
        if self.source_event_id is not None:
            return f"event:{self.source_event_id}"
        if self.source_execution_id:
            return f"execution:{self.source_execution_id}"
        raise ValueError("missing_internal_source_anchor")

    def replies_to_yuki(self, *, yuki_account_ids: frozenset[str] = frozenset()) -> bool:
        """Reply-to-Yuki: canonical verdict wins; None allows Presence-id fallback."""

        if self.canonical_reply_to_yuki is True:
            return True
        if self.canonical_reply_to_yuki is False:
            return False
        if not self.reply_sender_user_id:
            return False
        if self.reply_sender_user_id == self.bot_user_id:
            return True
        return self.reply_sender_user_id in yuki_account_ids

    def scope(self, *, bot_user_id: str | None = None) -> ConversationScope:
        """Build the bot-aware conversation scope for this message."""

        bot = (bot_user_id if bot_user_id is not None else self.bot_user_id).strip()
        if not bot:
            raise ValueError("conversation scope requires bot_user_id")
        if self.scope_type is ScopeType.PRIVATE:
            return ConversationScope.private(
                bot,
                self.sender.user_id,
                platform=self.platform,
                presence_id=self.presence_id,
                conversation_id=self.conversation_id,
                thread_id=self.thread_id,
            )
        if self.group_id is None:
            raise ValueError("group message is missing group_id")
        return ConversationScope.group(
            bot,
            self.group_id,
            platform=self.platform,
            presence_id=self.presence_id,
            conversation_id=self.conversation_id,
            thread_id=self.thread_id,
        )


@dataclass(frozen=True, slots=True)
class OutboundMedia:
    """Ephemeral outbound media bytes with ledger-safe descriptive metadata."""

    kind: AttachmentKind
    content: bytes = field(default=b"", repr=False)
    mime_type: str = "application/octet-stream"
    summary: str = ""
    emoji_id: str | None = None
    animated: bool = False


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Transport-independent text and optional ephemeral media."""

    text: str = ""
    reply_to_message_id: str | None = None
    media: tuple[OutboundMedia, ...] = ()


@dataclass(frozen=True, slots=True)
class OutboundSendReceipt:
    """Proof that one transport accepted an outbound message."""

    platform_message_id: str
    transport: str = "onebot"

    def __post_init__(self) -> None:
        message_id = self.platform_message_id.strip()
        transport = self.transport.strip()
        if not message_id:
            raise ValueError("platform_message_id must not be empty")
        if not transport:
            raise ValueError("transport must not be empty")
        object.__setattr__(self, "platform_message_id", message_id)
        object.__setattr__(self, "transport", transport)


@dataclass(frozen=True, slots=True)
class LocalDeliveryReceipt:
    """An original Social operation already committed this local ledger event."""

    event_id: int

    @property
    def platform_message_id(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class ChatImage:
    """Ephemeral, locally validated image; never a remote URL or persisted history."""

    data_url: str = field(repr=False)
    source: str = "current"
    video_timestamp_seconds: float | None = None
    # Host-only source dependencies retained in private checkpoints. Adapters
    # encode pixels, never these authorization identities.
    conversation_id: str | None = None
    generation: int | None = None
    source_event_id: int | None = None
    attachment_index: int | None = None
    content_hash: str | None = None
    expires_at: str | None = None
    artifact_id: str | None = None
    workspace_path: str | None = None
    version: str | None = None
    tool_handle: str | None = None
    plugin_id: str | None = None
    plugin_media_handle: str | None = None
    plugin_tool_name: str | None = None
    plugin_approval_revision: str | None = None

    def __post_init__(self) -> None:
        if not self.data_url.startswith(("data:image/jpeg;base64,", "data:image/png;base64,")):
            raise ValueError("chat image must be a prepared inline image")
        if self.source not in {"current", "reply", "history", "workspace", "tool"}:
            raise ValueError("invalid image source")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A message sent to a chat completion API, including tool-call turns."""

    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    images: tuple[ChatImage, ...] = field(default=(), repr=False)
    response_item: ProviderContinuation | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolFunction:
    """A provider-neutral function tool call."""

    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A provider-neutral tool call."""

    id: str
    function: ToolFunction
    type: str = "function"


@dataclass(frozen=True, slots=True)
class ChatTool:
    """A JSON-schema function tool exposed to the model."""

    name: str
    description: str
    parameters: dict[str, object]
    namespace: str = ""
    aliases: tuple[str, ...] = ()
    use_when: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    schema_version: str = "1"
    # Host-only policy: read-only operations may still observe changing state.
    result_cacheable: bool = True


@dataclass(frozen=True, slots=True)
class NativeToolDefinition:
    """One provider-executed tool authorized for this request."""

    type: NativeToolType


@dataclass(frozen=True, slots=True)
class NativeToolEvent:
    """Sanitized event emitted by a provider-executed tool."""

    tool_type: NativeToolType
    call_id: str
    status: NativeToolStatus
    action_type: str = ""
    query: str = field(default="", repr=False)
    url: str = field(default="", repr=False)
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class ResponseCitation:
    """Provider-returned or deterministically recovered public citation."""

    url: str = field(repr=False)
    title: str = ""
    origin: CitationOrigin = CitationOrigin.ANNOTATION
    call_id: str | None = None
    snippet: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class FunctionCallOutput:
    """Result of a local Function Tool for a Responses continuation."""

    call_id: str
    output: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Provider-independent chat request."""

    messages: tuple[ChatMessage, ...]
    model: str = ""
    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: ReasoningEffort | None = None
    tools: tuple[ChatTool, ...] = ()
    tool_choice: str | None = None
    response_format: dict[str, object] | None = None
    structured_output: bool = False
    native_tools: tuple[NativeToolDefinition, ...] = ()
    continuation: ProviderContinuation | None = None
    continuation_items: tuple[ChatMessage | FunctionCallOutput, ...] = ()
    request_chain_id: str = ""

    # Trusted conversation/execution identity, transported outside model content.
    provider_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """Validated provider response."""

    content: str
    latency_seconds: float
    provider_request_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_creation_5m_input_tokens: int | None = None
    cache_creation_1h_input_tokens: int | None = None
    status: ModelResponseStatus = ModelResponseStatus.COMPLETED
    native_tool_events: tuple[NativeToolEvent, ...] = ()
    citations: tuple[ResponseCitation, ...] = ()
    continuation: ProviderContinuation | None = None
    reasoning_tokens: int | None = None
    incomplete_reason: str | None = None
