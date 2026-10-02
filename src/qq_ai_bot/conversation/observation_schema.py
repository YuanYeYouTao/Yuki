"""0089 source cleanup definitions; pure derived rollups do not invalidate views."""

from qq_ai_bot.conversation.projection_revision_schema import PROJECTION_TRIGGERS_CURRENT as _OLD

# Migration 0089 replaces the old rollup triggers, not event/privacy guards.
RETIRED_ROLLUP_TRIGGERS = tuple(name for name in _OLD if "canonical_conversation_rollup" in name)

CONTEXT_SOURCE_TRIGGERS = {
    "context_selection_source_changed": """CREATE TRIGGER context_selection_source_changed
    AFTER UPDATE OF prompt_source_revision ON canonical_conversations
    WHEN OLD.prompt_source_revision IS NOT NEW.prompt_source_revision
    BEGIN
        DELETE FROM model_context_selections WHERE conversation_id=NEW.id
            AND observation_sources_json='[]';
    END""",
    "context_observation_reset": """CREATE TRIGGER context_observation_reset
    AFTER UPDATE OF generation, starts_after_event_id ON canonical_conversations
    WHEN OLD.generation IS NOT NEW.generation
        OR OLD.starts_after_event_id IS NOT NEW.starts_after_event_id
    BEGIN
        DELETE FROM model_context_observations WHERE conversation_id=NEW.id;
        DELETE FROM model_context_selections WHERE conversation_id=NEW.id;
    END""",
    "context_observation_privacy": """CREATE TRIGGER context_observation_privacy
    AFTER UPDATE OF privacy_generation ON execution_trace_state
    WHEN OLD.privacy_generation IS NOT NEW.privacy_generation
    BEGIN
        DELETE FROM model_context_observations WHERE privacy_generation != NEW.privacy_generation;
    END""",
    "context_observation_privacy_initial": """CREATE TRIGGER context_observation_privacy_initial
    AFTER INSERT ON execution_trace_state
    BEGIN
        DELETE FROM model_context_observations WHERE privacy_generation != NEW.privacy_generation;
    END""",
    "context_observation_delete": """CREATE TRIGGER context_observation_delete
    AFTER DELETE ON model_context_observations
    BEGIN
        DELETE FROM tool_artifact_refs WHERE owner_kind='observation' AND owner_id=OLD.id;
        DELETE FROM model_context_selections WHERE EXISTS (
            SELECT 1 FROM json_each(observation_sources_json)
            WHERE json_extract(value,'$[0]')=OLD.id
        );
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id=OLD.conversation_id;
        UPDATE prompt_projections SET payload_json='[]',byte_size=2,revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id=OLD.conversation_id;
    END""",
}

# Derived clues have their own owners, but real deletion of a referenced source
# retires its descendants too. Normal Work retention keeps these sources.
_descendants = """WITH RECURSIVE dependent(id) AS (
    SELECT id FROM model_context_observations
    WHERE EXISTS (SELECT 1 FROM json_each(parent_sources_json)
        WHERE json_extract(value,'$[0]')=OLD.id)
    UNION
    SELECT child.id FROM model_context_observations AS child,
        json_each(child.parent_sources_json) AS parent
        JOIN dependent ON json_extract(parent.value,'$[0]')=dependent.id
) SELECT id FROM dependent"""
CONTEXT_SOURCE_TRIGGERS["context_observation_delete"] = CONTEXT_SOURCE_TRIGGERS[
    "context_observation_delete"
].replace(
    "BEGIN",
    f"""BEGIN
        DELETE FROM tool_artifact_refs WHERE owner_kind='observation'
            AND owner_id IN ({_descendants});
        DELETE FROM model_context_selections WHERE EXISTS (
            SELECT 1 FROM json_each(observation_sources_json)
            WHERE json_extract(value,'$[0]') IN ({_descendants})
        );
        DELETE FROM model_context_observations WHERE id IN ({_descendants});""",
    1,
)

_observed_columns = (
    "id",
    "conversation_id",
    "generation",
    "actor_id",
    "read_scope",
    "source_key",
    "source_event_id",
    "version",
    "payload_json",
    "parent_sources_json",
    "summary_view_key",
)
_observed_changed = " OR ".join(f"OLD.{key} IS NOT NEW.{key}" for key in _observed_columns)
CONTEXT_SOURCE_TRIGGERS["context_observation_update"] = f"""CREATE TRIGGER
    context_observation_update AFTER UPDATE OF {", ".join(_observed_columns)}
    ON model_context_observations WHEN {_observed_changed}
    BEGIN
        DELETE FROM tool_artifact_refs WHERE owner_kind='observation'
            AND owner_id IN ({_descendants});
        DELETE FROM model_context_selections WHERE EXISTS (
            SELECT 1 FROM json_each(observation_sources_json)
            WHERE json_extract(value,'$[0]')=OLD.id
                OR json_extract(value,'$[0]') IN ({_descendants})
        );
        DELETE FROM model_context_observations WHERE id IN ({_descendants});
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
            WHERE id IN (OLD.conversation_id, NEW.conversation_id);
        UPDATE prompt_projections SET payload_json='[]',byte_size=2,revision=revision+1,
            invalidated_reason='source_changed'
            WHERE conversation_id IN (OLD.conversation_id, NEW.conversation_id);
    END"""

# Ownership changes are real source invalidation, even for derived summaries.
for _table in (
    "canonical_conversation_rollups",
    "canonical_conversation_rollup_emergency_overlays",
):
    _name = f"context_source_{_table}_owner"
    CONTEXT_SOURCE_TRIGGERS[_name] = f"""CREATE TRIGGER {_name}
    AFTER UPDATE OF conversation_id, generation ON {_table}
    WHEN OLD.conversation_id IS NOT NEW.conversation_id
        OR OLD.generation IS NOT NEW.generation
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id IN (OLD.conversation_id, NEW.conversation_id);
        UPDATE prompt_projections SET payload_json='[]',byte_size=2,revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id IN (OLD.conversation_id, NEW.conversation_id);
    END"""

PROJECTION_TRIGGERS_0089 = {
    **{name: statement for name, statement in _OLD.items() if name not in RETIRED_ROLLUP_TRIGGERS},
    **CONTEXT_SOURCE_TRIGGERS,
}
