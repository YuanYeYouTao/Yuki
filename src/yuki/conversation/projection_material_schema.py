"""Frozen 0108 projection invalidation SQL, shared by migration and metadata."""

PROJECTION_TRIGGERS_0108 = {
    "prompt_projection_reset": """
        CREATE TRIGGER prompt_projection_reset AFTER UPDATE OF generation, starts_after_event_id
        ON canonical_conversations
        WHEN OLD.generation IS NOT NEW.generation OR OLD.starts_after_event_id IS NOT
        NEW.starts_after_event_id
    BEGIN
        UPDATE canonical_conversations
        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN (NEW.id);
        UPDATE prompt_projections
        SET payload_json='[]', visible_material_json='{}', byte_size=4, revision=revision+1,
            invalidated_reason='reset'
        WHERE conversation_id IN (NEW.id);
    END""",
    "prompt_projection_event_delete": """
CREATE TRIGGER prompt_projection_event_delete AFTER DELETE ON chat_events

    BEGIN
        UPDATE canonical_conversations
        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN
        (OLD.canonical_conversation_id);
        UPDATE prompt_projections
        SET payload_json='[]', visible_material_json='{}', byte_size=4, revision=revision+1,
            invalidated_reason='deleted_event'
        WHERE conversation_id IN (OLD.canonical_conversation_id);
    END""",
    "prompt_projection_event_update": """
        CREATE TRIGGER prompt_projection_event_update AFTER UPDATE OF content, segments_json,
        visual_summary, external_payload_json, suppression_status, canonical_conversation_id ON
        chat_events
        WHEN OLD.content IS NOT NEW.content OR OLD.segments_json IS NOT NEW.segments_json OR
        OLD.visual_summary IS NOT NEW.visual_summary OR OLD.external_payload_json IS NOT
        NEW.external_payload_json OR OLD.suppression_status IS NOT NEW.suppression_status OR
        OLD.canonical_conversation_id IS NOT NEW.canonical_conversation_id
    BEGIN
        UPDATE canonical_conversations
        SET prompt_source_revision=prompt_source_revision+1 WHERE id IN
        (OLD.canonical_conversation_id, NEW.canonical_conversation_id);
        UPDATE prompt_projections
        SET payload_json='[]', visible_material_json='{}', byte_size=4, revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);
    END""",
    "prompt_projection_audio_update": """
CREATE TRIGGER prompt_projection_audio_update
    AFTER UPDATE OF audio_transcript ON chat_events
    WHEN OLD.audio_transcript IS NOT NEW.audio_transcript
    BEGIN
        UPDATE canonical_conversations
        SET prompt_source_revision=prompt_source_revision+1
        WHERE id = NEW.canonical_conversation_id;
        UPDATE prompt_projections
        SET payload_json='[]', visible_material_json='{}', byte_size=4, revision=revision+1,
        invalidated_reason='source_changed'
        WHERE conversation_id = NEW.canonical_conversation_id;
    END""",
    "prompt_projection_owner_update": """
CREATE TRIGGER prompt_projection_owner_update
    AFTER UPDATE OF kind, person_id, space_id ON canonical_conversations
    WHEN OLD.kind IS NOT NEW.kind OR OLD.person_id IS NOT NEW.person_id
        OR OLD.space_id IS NOT NEW.space_id
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id = NEW.id;
        UPDATE prompt_projections SET payload_json='[]', visible_material_json='{}',
        byte_size=4, revision=revision+1,
            invalidated_reason='read_scope_changed'
        WHERE conversation_id = NEW.id;
    END""",
    "prompt_projection_event_metadata_update": """
CREATE TRIGGER
        prompt_projection_event_metadata_update AFTER UPDATE OF id, bot_user_id,
        platform_message_id, scope_type, group_id, private_peer_user_id, sender_user_id,
        sender_nickname, sender_group_card, direction, event_kind, source_plugin_id,
        external_source, external_event_key, external_event_type, external_resume_wait,
        external_target_id, reply_to_message_id, reply_to_event_id, origin, automation_id,
        automation_run_id, occurred_at, observed_at, canonical_event_id, author_kind,
        author_person_id, author_presence_id, ingress_presence_id, utterance_fingerprint,
        ingress_provider, ingress_gateway_instance_id, caused_by_event_id ON chat_events
        WHEN OLD.id IS NOT NEW.id OR OLD.bot_user_id IS NOT NEW.bot_user_id OR
        OLD.platform_message_id IS NOT NEW.platform_message_id OR OLD.scope_type IS NOT
        NEW.scope_type OR OLD.group_id IS NOT NEW.group_id OR OLD.private_peer_user_id IS NOT
        NEW.private_peer_user_id OR OLD.sender_user_id IS NOT NEW.sender_user_id OR
        OLD.sender_nickname IS NOT NEW.sender_nickname OR OLD.sender_group_card IS NOT
        NEW.sender_group_card OR OLD.direction IS NOT NEW.direction OR OLD.event_kind IS NOT
        NEW.event_kind OR OLD.source_plugin_id IS NOT NEW.source_plugin_id OR
        OLD.external_source IS NOT NEW.external_source OR OLD.external_event_key IS NOT
        NEW.external_event_key OR OLD.external_event_type IS NOT NEW.external_event_type OR
        OLD.external_resume_wait IS NOT NEW.external_resume_wait OR OLD.external_target_id IS
        NOT NEW.external_target_id OR OLD.reply_to_message_id IS NOT NEW.reply_to_message_id OR
        OLD.reply_to_event_id IS NOT NEW.reply_to_event_id OR OLD.origin IS NOT NEW.origin OR
        OLD.automation_id IS NOT NEW.automation_id OR OLD.automation_run_id IS NOT
        NEW.automation_run_id OR OLD.occurred_at IS NOT NEW.occurred_at OR OLD.observed_at IS
        NOT NEW.observed_at OR OLD.canonical_event_id IS NOT NEW.canonical_event_id OR
        OLD.author_kind IS NOT NEW.author_kind OR OLD.author_person_id IS NOT
        NEW.author_person_id OR OLD.author_presence_id IS NOT NEW.author_presence_id OR
        OLD.ingress_presence_id IS NOT NEW.ingress_presence_id OR OLD.utterance_fingerprint IS
        NOT NEW.utterance_fingerprint OR OLD.ingress_provider IS NOT NEW.ingress_provider OR
        OLD.ingress_gateway_instance_id IS NOT NEW.ingress_gateway_instance_id OR
        OLD.caused_by_event_id IS NOT NEW.caused_by_event_id
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);
        UPDATE prompt_projections SET payload_json='[]', visible_material_json='{}',
        byte_size=4, revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);
    END""",
    "context_selection_source_changed": """
CREATE TRIGGER context_selection_source_changed
    AFTER UPDATE OF prompt_source_revision ON canonical_conversations
    WHEN OLD.prompt_source_revision IS NOT NEW.prompt_source_revision
    BEGIN
        DELETE FROM model_context_selections WHERE conversation_id=NEW.id
            AND observation_sources_json='[]';
    END""",
    "context_observation_reset": """
CREATE TRIGGER context_observation_reset
    AFTER UPDATE OF generation, starts_after_event_id ON canonical_conversations
    WHEN OLD.generation IS NOT NEW.generation
        OR OLD.starts_after_event_id IS NOT NEW.starts_after_event_id
    BEGIN
        DELETE FROM model_context_observations WHERE conversation_id=NEW.id;
        DELETE FROM model_context_selections WHERE conversation_id=NEW.id;
    END""",
    "context_observation_privacy": """
CREATE TRIGGER context_observation_privacy
    AFTER UPDATE OF privacy_generation ON execution_trace_state
    WHEN OLD.privacy_generation IS NOT NEW.privacy_generation
    BEGIN
        DELETE FROM model_context_observations WHERE privacy_generation !=
        NEW.privacy_generation;
    END""",
    "context_observation_privacy_initial": """
CREATE TRIGGER context_observation_privacy_initial
    AFTER INSERT ON execution_trace_state
    BEGIN
        DELETE FROM model_context_observations WHERE privacy_generation !=
        NEW.privacy_generation;
    END""",
    "context_observation_delete": """
CREATE TRIGGER context_observation_delete
    AFTER DELETE ON model_context_observations
    BEGIN
        DELETE FROM tool_artifact_refs WHERE owner_kind='observation'
            AND owner_id IN (WITH RECURSIVE dependent(id) AS (
    SELECT id FROM model_context_observations
    WHERE EXISTS (SELECT 1 FROM json_each(parent_sources_json)
        WHERE json_extract(value,'$[0]')=OLD.id)
    UNION
    SELECT child.id FROM model_context_observations AS child,
        json_each(child.parent_sources_json) AS parent
        JOIN dependent ON json_extract(parent.value,'$[0]')=dependent.id
) SELECT id FROM dependent);
        DELETE FROM model_context_selections WHERE EXISTS (
            SELECT 1 FROM json_each(observation_sources_json)
            WHERE json_extract(value,'$[0]') IN (WITH RECURSIVE dependent(id) AS (
    SELECT id FROM model_context_observations
    WHERE EXISTS (SELECT 1 FROM json_each(parent_sources_json)
        WHERE json_extract(value,'$[0]')=OLD.id)
    UNION
    SELECT child.id FROM model_context_observations AS child,
        json_each(child.parent_sources_json) AS parent
        JOIN dependent ON json_extract(parent.value,'$[0]')=dependent.id
) SELECT id FROM dependent)
        );
        DELETE FROM model_context_observations WHERE id IN (WITH RECURSIVE dependent(id) AS (
    SELECT id FROM model_context_observations
    WHERE EXISTS (SELECT 1 FROM json_each(parent_sources_json)
        WHERE json_extract(value,'$[0]')=OLD.id)
    UNION
    SELECT child.id FROM model_context_observations AS child,
        json_each(child.parent_sources_json) AS parent
        JOIN dependent ON json_extract(parent.value,'$[0]')=dependent.id
) SELECT id FROM dependent);
        DELETE FROM tool_artifact_refs WHERE owner_kind='observation' AND owner_id=OLD.id;
        DELETE FROM model_context_selections WHERE EXISTS (
            SELECT 1 FROM json_each(observation_sources_json)
            WHERE json_extract(value,'$[0]')=OLD.id
        );
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id=OLD.conversation_id;
        UPDATE prompt_projections SET payload_json='[]', visible_material_json='{}',
        byte_size=4,revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id=OLD.conversation_id;
    END""",
    "context_observation_update": """
CREATE TRIGGER
        context_observation_update AFTER UPDATE OF id, conversation_id, generation, actor_id,
        read_scope, source_key, source_event_id, version, payload_json, parent_sources_json,
        summary_view_key
        ON model_context_observations WHEN OLD.id IS NOT NEW.id OR OLD.conversation_id IS NOT
        NEW.conversation_id OR OLD.generation IS NOT NEW.generation OR OLD.actor_id IS NOT
        NEW.actor_id OR OLD.read_scope IS NOT NEW.read_scope OR OLD.source_key IS NOT
        NEW.source_key OR OLD.source_event_id IS NOT NEW.source_event_id OR OLD.version IS NOT
        NEW.version OR OLD.payload_json IS NOT NEW.payload_json OR OLD.parent_sources_json IS
        NOT NEW.parent_sources_json OR OLD.summary_view_key IS NOT NEW.summary_view_key
    BEGIN
        DELETE FROM tool_artifact_refs WHERE owner_kind='observation'
            AND owner_id IN (WITH RECURSIVE dependent(id) AS (
    SELECT id FROM model_context_observations
    WHERE EXISTS (SELECT 1 FROM json_each(parent_sources_json)
        WHERE json_extract(value,'$[0]')=OLD.id)
    UNION
    SELECT child.id FROM model_context_observations AS child,
        json_each(child.parent_sources_json) AS parent
        JOIN dependent ON json_extract(parent.value,'$[0]')=dependent.id
) SELECT id FROM dependent);
        DELETE FROM model_context_selections WHERE EXISTS (
            SELECT 1 FROM json_each(observation_sources_json)
            WHERE json_extract(value,'$[0]')=OLD.id
                OR json_extract(value,'$[0]') IN (WITH RECURSIVE dependent(id) AS (
    SELECT id FROM model_context_observations
    WHERE EXISTS (SELECT 1 FROM json_each(parent_sources_json)
        WHERE json_extract(value,'$[0]')=OLD.id)
    UNION
    SELECT child.id FROM model_context_observations AS child,
        json_each(child.parent_sources_json) AS parent
        JOIN dependent ON json_extract(parent.value,'$[0]')=dependent.id
) SELECT id FROM dependent)
        );
        DELETE FROM model_context_observations WHERE id IN (WITH RECURSIVE dependent(id) AS (
    SELECT id FROM model_context_observations
    WHERE EXISTS (SELECT 1 FROM json_each(parent_sources_json)
        WHERE json_extract(value,'$[0]')=OLD.id)
    UNION
    SELECT child.id FROM model_context_observations AS child,
        json_each(child.parent_sources_json) AS parent
        JOIN dependent ON json_extract(parent.value,'$[0]')=dependent.id
) SELECT id FROM dependent);
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
            WHERE id IN (OLD.conversation_id, NEW.conversation_id);
        UPDATE prompt_projections SET payload_json='[]', visible_material_json='{}',
        byte_size=4,revision=revision+1,
            invalidated_reason='source_changed'
            WHERE conversation_id IN (OLD.conversation_id, NEW.conversation_id);
    END""",
    "context_source_canonical_conversation_rollups_owner": """
CREATE TRIGGER context_source_canonical_conversation_rollups_owner
    AFTER UPDATE OF conversation_id, generation ON canonical_conversation_rollups
    WHEN OLD.conversation_id IS NOT NEW.conversation_id
        OR OLD.generation IS NOT NEW.generation
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id IN (OLD.conversation_id, NEW.conversation_id);
        UPDATE prompt_projections SET payload_json='[]', visible_material_json='{}',
        byte_size=4,revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id IN (OLD.conversation_id, NEW.conversation_id);
    END""",
    "context_source_canonical_conversation_rollup_emergency_overlays_owner": """
CREATE TRIGGER context_source_canonical_conversation_rollup_emergency_overlays_owner
        AFTER UPDATE OF conversation_id, generation ON
        canonical_conversation_rollup_emergency_overlays
    WHEN OLD.conversation_id IS NOT NEW.conversation_id
        OR OLD.generation IS NOT NEW.generation
    BEGIN
        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1
        WHERE id IN (OLD.conversation_id, NEW.conversation_id);
        UPDATE prompt_projections SET payload_json='[]', visible_material_json='{}',
        byte_size=4,revision=revision+1,
            invalidated_reason='source_changed'
        WHERE conversation_id IN (OLD.conversation_id, NEW.conversation_id);
    END""",
}
