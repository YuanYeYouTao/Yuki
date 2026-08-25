"""Transport-independent domain models."""

from qq_ai_bot.domain.control import DecisionContext, DecisionPrincipal
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import (
    AuthorKind,
    BindingId,
    ConnectionGeneration,
    ConversationGeneration,
    ConversationId,
    IdentityBindingId,
    PersonId,
    PresenceId,
    PrincipalId,
    RequestId,
    RouteGeneration,
    SpaceBindingId,
    SpaceId,
)
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    InboundMessage,
    MessageAttachment,
    OutboundMessage,
    OutboundSendReceipt,
    ReasoningEffort,
    SenderIdentity,
)

__all__ = [
    "AuthorKind",
    "BindingId",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ConnectionGeneration",
    "ConversationGeneration",
    "ConversationId",
    "ConversationScope",
    "DecisionContext",
    "DecisionPrincipal",
    "IdentityBindingId",
    "InboundMessage",
    "MessageAttachment",
    "OutboundMessage",
    "OutboundSendReceipt",
    "PersonId",
    "PresenceId",
    "PrincipalId",
    "ReasoningEffort",
    "RequestId",
    "RouteGeneration",
    "ScopeType",
    "SenderIdentity",
    "SpaceBindingId",
    "SpaceId",
]
