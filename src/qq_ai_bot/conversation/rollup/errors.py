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
        return (
            "model_reasoning_only"
            if (
                str(exc) == "rollup_reasoning_only"
                or getattr(exc, "diagnostics", {}).get("reasoning_only")
            )
            else "model_empty"
        )
    if name == "LLMIncompleteResponseError":
        return "model_truncated"
    if name == "BackgroundModelPreempted":
        return "model_preempted"
    if isinstance(exc, ValueError):
        reasons = {
            "rollup_summary_too_long": "model_summary_too_long",
            "rollup_carry_exceeds_input_capacity": "model_input_capacity",
            "rollup_source_exceeds_input_capacity": "model_input_capacity",
            "rollup_empty_source": "model_empty_source",
            "rollup_summary_unexpected_tool_calls": "model_unexpected_tool_calls",
            "rollup_summary_unsupplied_reference": "model_unsupplied_reference",
        }
        return reasons.get(str(exc), "model_invalid_candidate")
    return name
