"""Stable failure categories for conversation rollup."""


class ConversationRollupError(RuntimeError):
    """Base class for safe rollup failures."""


class ConversationCoverageError(ConversationRollupError):
    """A continuous, bounded prompt snapshot could not be produced."""


class RollupLeaseLostError(ConversationRollupError):
    """The owner/token/expiry lease fence rejected an operation."""


class RollupSourceChangedError(ConversationRollupError):
    """Candidate inputs changed while summary text was being generated."""


class ScopeGenerationSupersededError(ConversationRollupError):
    """A turn or worker references an obsolete scope generation."""


def model_failure_error_category(exc: BaseException) -> str:
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or name == "LLMTimeoutError":
        return "model_timeout"
    if name == "LLMEmptyResponseError":
        return "model_reasoning_only" if str(exc) == "rollup_reasoning_only" else "model_empty"
    if name == "LLMIncompleteResponseError":
        return "model_truncated"
    if name == "BackgroundModelPreempted":
        return "model_preempted"
    if isinstance(exc, ValueError):
        return (
            "model_summary_too_long" if str(exc) == "rollup_summary_too_long" else "model_quality"
        )
    return name
