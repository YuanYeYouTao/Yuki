"""Replace dual account owners once; preserve original identities and execution evidence.

All DDL and data rules are frozen here. This irreversible upgrade does not
import current ORM models or execute retired tools. The caller stops dispatch
and owns the offline transaction (migrations/env.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

_DDL = {
    "chat_events": {
        "table": "CREATE TABLE chat_events (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tbot_user_id VARCHAR(64) NOT NULL, \n"
        "\tplatform_message_id VARCHAR(128) NOT NULL, \n"
        "\tscope_type VARCHAR(16) NOT NULL, \n"
        "\tgroup_id VARCHAR(64), \n"
        "\tprivate_peer_user_id VARCHAR(64), \n"
        "\tsender_user_id VARCHAR(64) NOT NULL, \n"
        "\tsender_nickname VARCHAR(128) DEFAULT '' NOT NULL, \n"
        "\tsender_group_card VARCHAR(128) DEFAULT '' NOT NULL, \n"
        "\tdirection VARCHAR(16) NOT NULL, \n"
        "\tevent_kind VARCHAR(32) DEFAULT 'message' NOT NULL, \n"
        "\tsource_plugin_id VARCHAR(128), \n"
        "\texternal_source VARCHAR(64), \n"
        "\texternal_event_key VARCHAR(255), \n"
        "\texternal_event_type VARCHAR(128), \n"
        "\texternal_resume_wait BOOLEAN DEFAULT '0' NOT NULL, \n"
        "\texternal_payload_json TEXT, \n"
        "\texternal_target_id VARCHAR(64), \n"
        "\tcontent TEXT NOT NULL, \n"
        "\tvisual_summary TEXT NOT NULL, \n"
        "\taudio_transcript TEXT DEFAULT '' NOT NULL, \n"
        "\tsegments_json TEXT NOT NULL, \n"
        "\treply_to_message_id VARCHAR(128), \n"
        "\treply_to_event_id INTEGER, \n"
        "\torigin VARCHAR(32) NOT NULL, \n"
        "\tautomation_id INTEGER, \n"
        "\tautomation_run_id INTEGER, \n"
        "\toccurred_at DATETIME NOT NULL, \n"
        "\tobserved_at DATETIME NOT NULL, \n"
        "\tcanonical_event_id VARCHAR(36) NOT NULL, \n"
        "\tcanonical_conversation_id VARCHAR(36), \n"
        "\tauthor_account_id VARCHAR(36), \n"
        "\tauthor_kind VARCHAR(16) NOT NULL, \n"
        "\tauthor_person_id VARCHAR(36), \n"
        "\tauthor_presence_id VARCHAR(36), \n"
        "\tingress_presence_id VARCHAR(36), \n"
        "\tutterance_fingerprint VARCHAR(64), \n"
        "\tsuppression_status VARCHAR(16) NOT NULL, \n"
        "\tingress_provider VARCHAR(32), \n"
        "\tingress_gateway_instance_id VARCHAR(128), \n"
        "\tcaused_by_event_id INTEGER, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT ck_chat_events_kind_payload CHECK ((event_kind = 'message' AND "
        "source_plugin_id IS NULL AND external_source IS NULL AND external_event_key "
        "IS NULL AND external_event_type IS NULL AND external_payload_json IS NULL "
        "AND external_target_id IS NULL) OR (event_kind = 'external_event' AND "
        "source_plugin_id IS NOT NULL AND external_source IS NOT NULL AND "
        "external_event_key IS NOT NULL AND external_event_type IS NOT NULL AND "
        "external_payload_json IS NOT NULL AND external_target_id IS NOT NULL AND "
        "origin = 'plugin_background' AND direction = 'external')), \n"
        "\tCONSTRAINT ck_chat_events_author_kind CHECK (author_kind IN ('person', "
        "'yuki', 'external_bot', 'system', 'account')), \n"
        "\tCONSTRAINT ck_chat_events_canonical_event_id CHECK "
        "(length(canonical_event_id) = 36 AND canonical_event_id = "
        "lower(canonical_event_id) AND canonical_event_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_chat_events_author CHECK ((author_kind = 'account' AND "
        "author_account_id IS NOT NULL AND author_person_id IS NULL AND "
        "author_presence_id IS NULL) OR (author_kind = 'person' AND author_person_id "
        "IS NOT NULL AND author_presence_id IS NULL AND author_account_id IS NULL) OR "
        "(author_kind = 'yuki' AND author_person_id IS NULL AND author_presence_id IS "
        "NOT NULL AND author_account_id IS NULL) OR (author_kind IN ('external_bot', "
        "'system') AND author_person_id IS NULL AND author_presence_id IS NULL AND "
        "author_account_id IS NULL)), \n"
        "\tCONSTRAINT ck_chat_events_conversation CHECK (canonical_conversation_id IS "
        "NOT NULL OR (author_kind = 'account' AND scope_type = 'private' AND "
        "direction = 'inbound')), \n"
        "\tCONSTRAINT ck_chat_events_suppression_status CHECK (suppression_status IN "
        "('keeper', 'duplicate')), \n"
        "\tCONSTRAINT ck_chat_events_duplicate_fingerprint CHECK (suppression_status "
        "!= 'duplicate' OR utterance_fingerprint IS NOT NULL), \n"
        "\tFOREIGN KEY(reply_to_event_id) REFERENCES chat_events (id) ON DELETE "
        "RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(automation_id) REFERENCES automations (id) ON DELETE SET "
        "NULL, \n"
        "\tFOREIGN KEY(automation_run_id) REFERENCES automation_runs (id) ON DELETE "
        "SET NULL, \n"
        "\tFOREIGN KEY(canonical_conversation_id) REFERENCES canonical_conversations "
        "(id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(author_account_id) REFERENCES accounts (id) ON DELETE "
        "RESTRICT, \n"
        "\tFOREIGN KEY(author_person_id) REFERENCES persons (id) ON DELETE RESTRICT "
        "ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(author_presence_id) REFERENCES account_assignments (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(ingress_presence_id) REFERENCES account_assignments (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(caused_by_event_id) REFERENCES chat_events (id) ON DELETE "
        "RESTRICT ON UPDATE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_chat_events_automation ON chat_events (automation_id, "
            "automation_run_id)",
            "CREATE INDEX ix_chat_events_bot_scope_group_id ON chat_events "
            "(bot_user_id, scope_type, group_id, id)",
            "CREATE INDEX ix_chat_events_bot_scope_private_id ON chat_events "
            "(bot_user_id, scope_type, private_peer_user_id, id)",
            "CREATE INDEX ix_chat_events_canonical_conversation_id ON chat_events "
            "(canonical_conversation_id)",
            "CREATE INDEX ix_chat_events_canonical_event_id ON chat_events (canonical_event_id)",
            "CREATE INDEX ix_chat_events_caused_by_event_id ON chat_events (caused_by_event_id)",
            "CREATE INDEX ix_chat_events_conversation_sender_time ON chat_events "
            "(canonical_conversation_id, sender_user_id, occurred_at, id) WHERE "
            "suppression_status = 'keeper'",
            "CREATE INDEX ix_chat_events_group_time ON chat_events (group_id, occurred_at)",
            "CREATE INDEX ix_chat_events_private_peer_time ON chat_events "
            "(private_peer_user_id, occurred_at)",
            "CREATE INDEX ix_chat_events_reply_to_event_id ON chat_events (reply_to_event_id)",
            "CREATE INDEX ix_chat_events_scope_time ON chat_events (scope_type, occurred_at)",
            "CREATE INDEX ix_chat_events_sender_time ON chat_events (sender_user_id, occurred_at)",
            "CREATE INDEX ix_chat_events_social_source_author ON chat_events "
            "(author_person_id, id) WHERE direction = 'inbound' AND author_kind = "
            "'person'",
            "CREATE UNIQUE INDEX uq_chat_events_canonical_event_keeper ON chat_events "
            "(canonical_event_id) WHERE suppression_status = 'keeper'",
            "CREATE UNIQUE INDEX uq_chat_events_external_event_target ON chat_events "
            "(source_plugin_id, external_event_key, canonical_conversation_id, "
            "scope_type, external_target_id) WHERE event_kind = 'external_event'",
        ],
    },
    "memory_rebuild_runs": {
        "table": "CREATE TABLE memory_rebuild_runs (\n"
        "\toriginal_selection_json TEXT, \n"
        "\toriginal_selection_hash VARCHAR(64), \n"
        "\tid INTEGER NOT NULL, \n"
        "\tpublic_id VARCHAR(36) NOT NULL, \n"
        "\tstatus VARCHAR(24) NOT NULL, \n"
        "\tselection_json TEXT NOT NULL, \n"
        "\tselection_hash VARCHAR(64) NOT NULL, \n"
        "\tsnapshot_max_event_id INTEGER NOT NULL, \n"
        "\tsnapshot_created_at DATETIME NOT NULL, \n"
        "\tscan_checkpoint_occurred_at DATETIME, \n"
        "\tscan_checkpoint_event_id INTEGER, \n"
        "\tcommit_checkpoint_event_id INTEGER, \n"
        "\tcommit_checkpoint_claim_index INTEGER, \n"
        "\tcreated_by_user_id VARCHAR(64), \n"
        "\textraction_fingerprint VARCHAR(64) NOT NULL, \n"
        "\tplan_statistics_json TEXT NOT NULL, \n"
        "\textraction_requests INTEGER NOT NULL, \n"
        "\tconsolidation_requests INTEGER NOT NULL, \n"
        "\tinput_tokens INTEGER NOT NULL, \n"
        "\toutput_tokens INTEGER NOT NULL, \n"
        "\tlatency_milliseconds INTEGER NOT NULL, \n"
        "\terror_category VARCHAR(64), \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tstarted_at DATETIME, \n"
        "\treview_ready_at DATETIME, \n"
        "\tcommit_started_at DATETIME, \n"
        "\tcompleted_at DATETIME, \n"
        "\tcancelled_at DATETIME, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT uq_memory_rebuild_runs_public_id UNIQUE (public_id), \n"
        "\tCONSTRAINT ck_memory_rebuild_snapshot CHECK (snapshot_max_event_id "
        ">= 0), \n"
        "\tCONSTRAINT ck_memory_rebuild_usage_nonnegative CHECK "
        "(extraction_requests >= 0 AND consolidation_requests >= 0 AND "
        "input_tokens >= 0 AND output_tokens >= 0 AND latency_milliseconds >= "
        "0), \n"
        "\tCONSTRAINT ck_memory_rebuild_run_status CHECK (status IN "
        "('planned','extracting','extraction_paused','review','committing','commit_paused','completed','cancelled','failed'))\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_memory_rebuild_runs_created_by ON "
            "memory_rebuild_runs (created_by_user_id)",
            "CREATE INDEX ix_memory_rebuild_runs_status_created ON "
            "memory_rebuild_runs (status, created_at)",
        ],
    },
    "automations": {
        "table": "CREATE TABLE automations (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tcreator_kind VARCHAR(8) DEFAULT 'person' NOT NULL, \n"
        "\tcreator_user_id VARCHAR(64) NOT NULL, \n"
        "\tbot_user_id VARCHAR(64) NOT NULL, \n"
        "\tname VARCHAR(128) NOT NULL, \n"
        "\tstatus VARCHAR(16) NOT NULL, \n"
        "\ttimezone VARCHAR(64) NOT NULL, \n"
        "\tschedule_json TEXT NOT NULL, \n"
        "\tscript_json TEXT NOT NULL, \n"
        "\tscript_hash VARCHAR(64) NOT NULL, \n"
        "\trequired_capabilities_json TEXT NOT NULL, \n"
        "\tauthority_snapshot_json TEXT NOT NULL, \n"
        "\tcreated_from_message_id VARCHAR(128) NOT NULL, \n"
        "\tcreation_source_key VARCHAR(255), \n"
        "\tnext_run_at DATETIME, \n"
        "\tlast_run_at DATETIME, \n"
        "\trun_count INTEGER NOT NULL, \n"
        "\tmax_runs INTEGER, \n"
        "\tconsecutive_failures INTEGER NOT NULL, \n"
        "\tclaimed_by VARCHAR(64), \n"
        "\tclaimed_until DATETIME, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tcanonical_creator_person_id VARCHAR(36), \n"
        "\tcanonical_target_person_id VARCHAR(36), \n"
        "\tcanonical_target_space_id VARCHAR(36), \n"
        "\tcanonical_presence_id VARCHAR(36), \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT ck_automations_status CHECK (status IN ('active', 'paused', "
        "'completed', 'cancelled', 'failed', 'blocked')), \n"
        "\tCONSTRAINT ck_automations_run_count CHECK (run_count >= 0), \n"
        "\tCONSTRAINT ck_automations_consecutive_failures CHECK (consecutive_failures "
        ">= 0), \n"
        "\tCONSTRAINT ck_automations_canonical_owner CHECK (((creator_kind = 'person' "
        "AND (canonical_creator_person_id IS NOT NULL OR status IN ('completed', "
        "'cancelled', 'failed', 'blocked'))) OR (creator_kind = 'self' AND "
        "canonical_creator_person_id IS NULL AND creator_user_id = '' AND "
        "created_from_message_id = '' AND canonical_target_person_id IS NULL)) AND "
        "((status IN ('active', 'paused') AND ((canonical_target_person_id IS NOT "
        "NULL AND canonical_target_space_id IS NULL) OR (canonical_target_person_id "
        "IS NULL AND canonical_target_space_id IS NOT NULL))) OR (status IN "
        "('completed', 'cancelled', 'failed', 'blocked') AND NOT "
        "(canonical_target_person_id IS NOT NULL AND canonical_target_space_id IS NOT "
        "NULL)))), \n"
        "\tFOREIGN KEY(canonical_creator_person_id) REFERENCES persons (id) ON DELETE "
        "RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons (id) ON DELETE "
        "RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces (id) ON DELETE "
        "RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_presence_id) REFERENCES account_assignments (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_automations_canonical_creator_person_id ON automations "
            "(canonical_creator_person_id)",
            "CREATE INDEX ix_automations_canonical_presence_id ON automations "
            "(canonical_presence_id)",
            "CREATE INDEX ix_automations_canonical_target_person_id ON automations "
            "(canonical_target_person_id)",
            "CREATE INDEX ix_automations_canonical_target_space_id ON automations "
            "(canonical_target_space_id)",
            "CREATE INDEX ix_automations_claim ON automations (claimed_until, claimed_by)",
            "CREATE INDEX ix_automations_creation_source_key ON automations (creation_source_key)",
            "CREATE INDEX ix_automations_creator_updated ON automations "
            "(creator_user_id, updated_at)",
            "CREATE INDEX ix_automations_status_next ON automations (status, next_run_at)",
            "CREATE UNIQUE INDEX uq_automation_creation_call ON automations "
            "(canonical_creator_person_id, creation_source_key) WHERE "
            "creation_source_key LIKE 'call:%'",
            "CREATE UNIQUE INDEX uq_automation_self_creation_call ON automations "
            "(creation_source_key) WHERE creator_kind = 'self' AND creation_source_key "
            "IS NOT NULL",
        ],
    },
    "automation_versions": {
        "table": "CREATE TABLE automation_versions (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tautomation_id INTEGER NOT NULL, \n"
        "\tversion INTEGER NOT NULL, \n"
        "\tscript_json TEXT NOT NULL, \n"
        "\tscript_hash VARCHAR(64) NOT NULL, \n"
        "\tauthority_snapshot_json TEXT, \n"
        "\tcanonical_target_person_id VARCHAR(36), \n"
        "\tcanonical_target_space_id VARCHAR(36), \n"
        "\tcanonical_presence_id VARCHAR(36), \n"
        "\tupdated_by VARCHAR(64) NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT uq_automation_versions_number UNIQUE (automation_id, "
        "version), \n"
        "\tFOREIGN KEY(automation_id) REFERENCES automations (id) ON DELETE "
        "CASCADE, \n"
        "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_presence_id) REFERENCES account_assignments "
        "(id) ON DELETE RESTRICT ON UPDATE RESTRICT\n"
        ")",
        "indexes": [],
    },
    "autonomy_initiative_runs": {
        "table": "CREATE TABLE autonomy_initiative_runs (\n"
        "\tid VARCHAR(36) NOT NULL, \n"
        "\tproposal_id TEXT NOT NULL, \n"
        "\tconversation_id VARCHAR(36) NOT NULL, \n"
        "\tgeneration INTEGER NOT NULL, \n"
        "\towner VARCHAR(16) NOT NULL, \n"
        "\tcontroller_epoch INTEGER NOT NULL, \n"
        "\tspace_id VARCHAR(36) NOT NULL, \n"
        "\tpresence_id VARCHAR(36) NOT NULL, \n"
        "\ttarget_person_id VARCHAR(36), \n"
        "\tpayload_hash VARCHAR(64) NOT NULL, \n"
        "\tsources_json TEXT NOT NULL, \n"
        "\tsupport_refs_json TEXT NOT NULL, \n"
        "\tstate VARCHAR(16) NOT NULL, \n"
        "\tfeedback_sequence INTEGER NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\ttrigger_kind VARCHAR(16) DEFAULT 'source' NOT NULL, \n"
        "\tthread_key TEXT, \n"
        "\tPRIMARY KEY (id), \n"
        "\tFOREIGN KEY(conversation_id, generation) REFERENCES "
        "autonomy_bindings (conversation_id, generation) ON DELETE "
        "RESTRICT, \n"
        "\tCONSTRAINT uq_autonomy_proposal_run UNIQUE (conversation_id, "
        "generation, owner, controller_epoch, proposal_id), \n"
        "\tCHECK (generation >= 1 AND controller_epoch >= 0 AND "
        "feedback_sequence >= 0), \n"
        "\tCHECK (owner IN ('legacy', 'semantic')), \n"
        "\tCHECK (state IN ('accepted', 'running', 'completed', "
        "'no_reply', 'interrupted', 'failed')), \n"
        "\tFOREIGN KEY(space_id) REFERENCES spaces (id) ON DELETE "
        "RESTRICT, \n"
        "\tFOREIGN KEY(presence_id) REFERENCES account_assignments (id) "
        "ON DELETE RESTRICT, \n"
        "\tFOREIGN KEY(target_person_id) REFERENCES persons (id) ON "
        "DELETE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_autonomy_runs_active ON "
            "autonomy_initiative_runs (conversation_id, generation, "
            "state)"
        ],
    },
    "canonical_conversations": {
        "table": "CREATE TABLE canonical_conversations (\n"
        "\tid VARCHAR(36) NOT NULL, \n"
        "\tkind VARCHAR(16) NOT NULL, \n"
        "\tperson_id VARCHAR(36), \n"
        "\tidentity_binding_id VARCHAR(36), \n"
        "\tspace_id VARCHAR(36), \n"
        "\tentry_presence_id VARCHAR(36), \n"
        "\tspace_binding_id VARCHAR(36), \n"
        "\tthread_id VARCHAR(128), \n"
        "\tprimary_alias_id VARCHAR(36) NOT NULL, \n"
        "\tprimary_marker INTEGER DEFAULT 1 NOT NULL, \n"
        "\tgeneration INTEGER NOT NULL, \n"
        "\tprompt_source_revision INTEGER DEFAULT 0 NOT NULL, \n"
        "\tstarts_after_event_id INTEGER NOT NULL, \n"
        "\tlast_event_id INTEGER NOT NULL, \n"
        "\tlast_generation_change_event_id INTEGER NOT NULL, \n"
        "\trevision INTEGER NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT fk_canonical_conversations_person FOREIGN "
        "KEY(person_id) REFERENCES persons (id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_canonical_conversations_binding FOREIGN "
        "KEY(identity_binding_id) REFERENCES account_assignments (id) ON "
        "DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_canonical_conversations_space FOREIGN "
        "KEY(space_id) REFERENCES spaces (id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_conversations_entry_presence FOREIGN "
        "KEY(entry_presence_id) REFERENCES account_assignments (id) ON "
        "DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_conversations_space_binding FOREIGN "
        "KEY(space_binding_id) REFERENCES space_bindings (id) ON DELETE "
        "RESTRICT, \n"
        "\tCONSTRAINT fk_canonical_conversations_primary_alias FOREIGN "
        "KEY(id, primary_alias_id, primary_marker) REFERENCES "
        "conversation_legacy_aliases (conversation_id, id, is_primary) ON "
        "DELETE NO ACTION ON UPDATE NO ACTION DEFERRABLE INITIALLY "
        "DEFERRED, \n"
        "\tCONSTRAINT ck_canonical_conversations_id CHECK (length(id) = "
        "36 AND id = lower(id) AND id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_canonical_conversations_kind CHECK (kind IN "
        "('private', 'space')), \n"
        "\tCONSTRAINT ck_canonical_conversations_owner_xor CHECK ((kind = "
        "'private' AND person_id IS NOT NULL AND space_id IS NULL AND "
        "identity_binding_id IS NOT NULL) OR (kind = 'space' AND space_id "
        "IS NOT NULL AND person_id IS NULL AND identity_binding_id IS "
        "NULL)), \n"
        "\tCONSTRAINT ck_canonical_conversations_person_id CHECK "
        "(person_id IS NULL OR (length(person_id) = 36 AND person_id = "
        "lower(person_id) AND person_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]')), \n"
        "\tCONSTRAINT ck_canonical_conversations_space_id CHECK (space_id "
        "IS NULL OR (length(space_id) = 36 AND space_id = lower(space_id) "
        "AND space_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]')), \n"
        "\tCONSTRAINT ck_canonical_conversations_primary_alias_id CHECK "
        "(length(primary_alias_id) = 36 AND primary_alias_id = "
        "lower(primary_alias_id) AND primary_alias_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_canonical_conversations_primary_marker CHECK "
        "(primary_marker = 1), \n"
        "\tCONSTRAINT ck_canonical_conversations_generation CHECK "
        "(generation >= 1), \n"
        "\tCONSTRAINT ck_canonical_conversations_revision CHECK (revision "
        ">= 1), \n"
        "\tCONSTRAINT ck_canonical_conversations_watermarks CHECK "
        "(starts_after_event_id >= 0 AND last_event_id >= 0 AND "
        "last_generation_change_event_id >= 0 AND starts_after_event_id "
        "<= last_event_id AND last_generation_change_event_id <= "
        "last_event_id)\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_conversations_person ON "
            "canonical_conversations (person_id) WHERE person_id IS NOT "
            "NULL",
            "CREATE UNIQUE INDEX uq_canonical_conversations_binding ON "
            "canonical_conversations (identity_binding_id, "
            "entry_presence_id, coalesce(thread_id, '')) WHERE kind = "
            "'private'",
            "CREATE UNIQUE INDEX uq_canonical_conversations_legacy_space "
            "ON canonical_conversations (space_id) WHERE kind = 'space' "
            "AND space_binding_id IS NULL",
            "CREATE UNIQUE INDEX uq_canonical_conversations_space ON "
            "canonical_conversations (space_binding_id, "
            "coalesce(thread_id, '')) WHERE kind = 'space' AND "
            "space_binding_id IS NOT NULL",
        ],
    },
    "person_active_routes": {
        "table": "CREATE TABLE person_active_routes (\n"
        "\tperson_id VARCHAR(36) NOT NULL, \n"
        "\tidentity_binding_id VARCHAR(36) NOT NULL, \n"
        "\tpresence_id VARCHAR(36) NOT NULL, \n"
        "\troute_generation INTEGER NOT NULL, \n"
        "\tpaused BOOLEAN NOT NULL, \n"
        "\trevision INTEGER NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (person_id), \n"
        "\tCONSTRAINT fk_person_active_routes_person FOREIGN KEY(person_id) "
        "REFERENCES persons (id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_person_active_routes_binding FOREIGN "
        "KEY(identity_binding_id) REFERENCES account_assignments (id) ON "
        "DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_person_active_routes_presence FOREIGN "
        "KEY(presence_id) REFERENCES account_assignments (id) ON DELETE "
        "RESTRICT, \n"
        "\tCONSTRAINT ck_person_active_routes_person_id CHECK "
        "(length(person_id) = 36 AND person_id = lower(person_id) AND "
        "person_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_person_active_routes_binding_id CHECK "
        "(length(identity_binding_id) = 36 AND identity_binding_id = "
        "lower(identity_binding_id) AND identity_binding_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_person_active_routes_presence_id CHECK "
        "(length(presence_id) = 36 AND presence_id = lower(presence_id) AND "
        "presence_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_person_active_routes_generation CHECK "
        "(route_generation >= 1), \n"
        "\tCONSTRAINT ck_person_active_routes_paused CHECK (paused IN (0, "
        "1)), \n"
        "\tCONSTRAINT ck_person_active_routes_revision CHECK (revision >= "
        "1)\n"
        ")",
        "indexes": [],
    },
    "space_binding_ingest_routes": {
        "table": "CREATE TABLE space_binding_ingest_routes (\n"
        "\tspace_binding_id VARCHAR(36) NOT NULL, \n"
        "\tingest_presence_id VARCHAR(36) NOT NULL, \n"
        "\troute_generation INTEGER NOT NULL, \n"
        "\tpaused BOOLEAN NOT NULL, \n"
        "\trevision INTEGER NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (space_binding_id), \n"
        "\tCONSTRAINT fk_space_binding_ingest_routes_binding FOREIGN "
        "KEY(space_binding_id) REFERENCES space_bindings (id) ON "
        "DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_space_binding_ingest_routes_presence FOREIGN "
        "KEY(ingest_presence_id) REFERENCES account_assignments (id) "
        "ON DELETE RESTRICT, \n"
        "\tCONSTRAINT ck_space_binding_ingest_routes_binding_id CHECK "
        "(length(space_binding_id) = 36 AND space_binding_id = "
        "lower(space_binding_id) AND space_binding_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_space_binding_ingest_routes_presence_id "
        "CHECK (length(ingest_presence_id) = 36 AND "
        "ingest_presence_id = lower(ingest_presence_id) AND "
        "ingest_presence_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_space_binding_ingest_routes_generation CHECK "
        "(route_generation >= 1), \n"
        "\tCONSTRAINT ck_space_binding_ingest_routes_paused CHECK "
        "(paused IN (0, 1)), \n"
        "\tCONSTRAINT ck_space_binding_ingest_routes_revision CHECK "
        "(revision >= 1)\n"
        ")",
        "indexes": [],
    },
    "space_active_routes": {
        "table": "CREATE TABLE space_active_routes (\n"
        "\tspace_id VARCHAR(36) NOT NULL, \n"
        "\tspace_binding_id VARCHAR(36) NOT NULL, \n"
        "\tpresence_id VARCHAR(36) NOT NULL, \n"
        "\troute_generation INTEGER NOT NULL, \n"
        "\tpaused BOOLEAN NOT NULL, \n"
        "\trevision INTEGER NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (space_id), \n"
        "\tCONSTRAINT fk_space_active_routes_space FOREIGN KEY(space_id) "
        "REFERENCES spaces (id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT fk_space_active_routes_binding FOREIGN "
        "KEY(space_binding_id) REFERENCES space_bindings (id) ON DELETE "
        "RESTRICT, \n"
        "\tCONSTRAINT fk_space_active_routes_presence FOREIGN "
        "KEY(presence_id) REFERENCES account_assignments (id) ON DELETE "
        "RESTRICT, \n"
        "\tCONSTRAINT ck_space_active_routes_space_id CHECK (length(space_id) "
        "= 36 AND space_id = lower(space_id) AND space_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_space_active_routes_binding_id CHECK "
        "(length(space_binding_id) = 36 AND space_binding_id = "
        "lower(space_binding_id) AND space_binding_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_space_active_routes_presence_id CHECK "
        "(length(presence_id) = 36 AND presence_id = lower(presence_id) AND "
        "presence_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_space_active_routes_generation CHECK "
        "(route_generation >= 1), \n"
        "\tCONSTRAINT ck_space_active_routes_paused CHECK (paused IN (0, "
        "1)), \n"
        "\tCONSTRAINT ck_space_active_routes_revision CHECK (revision >= 1)\n"
        ")",
        "indexes": [],
    },
    "canonical_event_receipts": {
        "table": "CREATE TABLE canonical_event_receipts (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tingress_account_id VARCHAR(36) NOT NULL, \n"
        "\ttransport_namespace VARCHAR NOT NULL, \n"
        "\tingress_presence_id VARCHAR(36) NOT NULL, \n"
        "\tevent_type VARCHAR(64) NOT NULL, \n"
        "\tplatform_message_id VARCHAR(128) NOT NULL, \n"
        "\tcanonical_event_id VARCHAR(36) NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tobserved_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT uq_canonical_event_receipts_transport UNIQUE "
        "(ingress_account_id, transport_namespace, event_type, "
        "platform_message_id), \n"
        "\tCONSTRAINT fk_canonical_event_receipts_ingress_presence "
        "FOREIGN KEY(ingress_presence_id) REFERENCES account_assignments "
        "(id) ON DELETE RESTRICT, \n"
        "\tCONSTRAINT ck_canonical_event_receipts_ingress_presence_id "
        "CHECK (length(ingress_presence_id) = 36 AND ingress_presence_id "
        "= lower(ingress_presence_id) AND ingress_presence_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_canonical_event_receipts_event_type CHECK "
        "(length(event_type) > 0 AND length(event_type) <= 64 AND "
        "event_type = trim(event_type)), \n"
        "\tCONSTRAINT ck_canonical_event_receipts_platform_message_id "
        "CHECK (length(platform_message_id) > 0 AND "
        "length(platform_message_id) <= 128 AND platform_message_id = "
        "trim(platform_message_id)), \n"
        "\tCONSTRAINT ck_canonical_event_receipts_canonical_event_id "
        "CHECK (length(canonical_event_id) = 36 AND canonical_event_id = "
        "lower(canonical_event_id) AND canonical_event_id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tFOREIGN KEY(ingress_account_id) REFERENCES accounts (id) ON "
        "DELETE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_canonical_event_receipts_canonical_event_id "
            "ON canonical_event_receipts (canonical_event_id)"
        ],
    },
    "accounts": {
        "table": "CREATE TABLE accounts (\n"
        "\tid VARCHAR(36) NOT NULL, \n"
        "\tplatform VARCHAR(32) NOT NULL, \n"
        "\texternal_account_id VARCHAR(255) NOT NULL, \n"
        "\tdisplay_name VARCHAR(128) DEFAULT '' NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT uq_accounts_native_key UNIQUE (platform, external_account_id), \n"
        "\tCONSTRAINT ck_accounts_id CHECK (length(id) = 36 AND id = lower(id) AND id "
        "GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_accounts_platform CHECK (length(platform) > 0 AND "
        "length(platform) <= 32 AND platform = lower(platform) AND platform = "
        "trim(platform)), \n"
        "\tCONSTRAINT ck_accounts_native_id CHECK (length(external_account_id) > 0 AND "
        "length(external_account_id) <= 255 AND external_account_id = "
        "trim(external_account_id))\n"
        ")",
        "indexes": [],
    },
    "account_assignments": {
        "table": "CREATE TABLE account_assignments (\n"
        "\tid VARCHAR(36) NOT NULL, \n"
        "\taccount_id VARCHAR(36) NOT NULL, \n"
        "\tkind VARCHAR(16) NOT NULL, \n"
        "\tperson_id VARCHAR(36), \n"
        "\tstatus VARCHAR(16) DEFAULT 'active' NOT NULL, \n"
        "\tstarted_at DATETIME, \n"
        "\tended_at DATETIME, \n"
        "\tfirst_seen_at DATETIME NOT NULL, \n"
        "\tlast_seen_at DATETIME NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\trevision INTEGER NOT NULL, \n"
        "\tingest_eligible BOOLEAN DEFAULT '1' NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT ck_account_assignments_id CHECK (length(id) = 36 AND id "
        "= lower(id) AND id GLOB "
        "'[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'), \n"
        "\tCONSTRAINT ck_account_assignments_kind CHECK (kind IN ('person', "
        "'yuki')), \n"
        "\tCONSTRAINT ck_account_assignments_owner CHECK ((kind='person' AND "
        "person_id IS NOT NULL) OR (kind='yuki' AND person_id IS NULL)), \n"
        "\tCONSTRAINT ck_account_assignments_status CHECK (status IN "
        "('active', 'disabled', 'ended')), \n"
        "\tCONSTRAINT ck_account_assignments_revision CHECK (revision >= "
        "1), \n"
        "\tFOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE "
        "RESTRICT, \n"
        "\tFOREIGN KEY(person_id) REFERENCES persons (id) ON DELETE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_account_assignments_history ON "
            "account_assignments (account_id, started_at, id)",
            "CREATE INDEX ix_account_assignments_person ON account_assignments (person_id, id)",
            "CREATE UNIQUE INDEX uq_account_assignments_current ON "
            "account_assignments (account_id) WHERE status <> 'ended'",
        ],
    },
    "account_space_profiles": {
        "table": "CREATE TABLE account_space_profiles (\n"
        "\taccount_id VARCHAR(36) NOT NULL, \n"
        "\tspace_binding_id VARCHAR(36) NOT NULL, \n"
        "\tdisplay_name VARCHAR(128) DEFAULT '' NOT NULL, \n"
        "\tfirst_seen_at DATETIME NOT NULL, \n"
        "\tlast_seen_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (account_id, space_binding_id), \n"
        "\tFOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE "
        "CASCADE, \n"
        "\tFOREIGN KEY(space_binding_id) REFERENCES space_bindings (id) ON "
        "DELETE CASCADE\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_account_space_profiles_space ON "
            "account_space_profiles (space_binding_id, account_id)"
        ],
    },
    "account_name_observations": {
        "table": "CREATE TABLE account_name_observations (\n"
        "\tid INTEGER NOT NULL, \n"
        "\taccount_id VARCHAR(36) NOT NULL, \n"
        "\tassignment_id VARCHAR(36), \n"
        "\tspace_binding_id VARCHAR(36), \n"
        "\tname VARCHAR(128) NOT NULL, \n"
        "\tkind VARCHAR(32) NOT NULL, \n"
        "\tfirst_seen_at DATETIME NOT NULL, \n"
        "\tlast_seen_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (id), \n"
        "\tFOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE "
        "CASCADE, \n"
        "\tFOREIGN KEY(assignment_id) REFERENCES account_assignments "
        "(id) ON DELETE CASCADE, \n"
        "\tFOREIGN KEY(space_binding_id) REFERENCES space_bindings (id) "
        "ON DELETE CASCADE\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_account_names_scope_account ON "
            "account_name_observations (space_binding_id, account_id, "
            "assignment_id)",
            "CREATE UNIQUE INDEX uq_account_names_observation ON "
            "account_name_observations (account_id, "
            "coalesce(assignment_id, ''), coalesce(space_binding_id, "
            "''), name)",
        ],
    },
    "plugin_background_target_grants": {
        "table": "CREATE TABLE plugin_background_target_grants (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tplugin_id VARCHAR(128) NOT NULL, \n"
        "\ttarget_type VARCHAR(16) NOT NULL, \n"
        "\ttarget_id VARCHAR(64) NOT NULL, \n"
        "\tbot_user_id VARCHAR(64) NOT NULL, \n"
        "\tenabled BOOLEAN NOT NULL, \n"
        "\tcreated_by_user_id VARCHAR(64) NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tcanonical_target_person_id VARCHAR(36), \n"
        "\tcanonical_target_space_id VARCHAR(36), \n"
        "\tcanonical_created_by_person_id VARCHAR(36), \n"
        "\tcanonical_presence_id VARCHAR(36), \n"
        "\tcanonical_conversation_id VARCHAR(36), \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT ck_plugin_background_target_type CHECK "
        "(target_type IN ('group', 'private')), \n"
        "\tCONSTRAINT ck_plugin_background_target_owner CHECK "
        "(NOT (canonical_target_person_id IS NOT NULL AND "
        "canonical_target_space_id IS NOT NULL) AND "
        "(canonical_target_person_id IS NULL OR target_type = "
        "'private') AND (canonical_target_space_id IS NULL OR "
        "target_type = 'group') AND (enabled = 0 OR "
        "(canonical_created_by_person_id IS NOT NULL AND "
        "canonical_presence_id IS NOT NULL AND ((target_type = "
        "'private' AND canonical_target_person_id IS NOT NULL AND "
        "canonical_target_space_id IS NULL) OR (target_type = "
        "'group' AND canonical_target_person_id IS NULL AND "
        "canonical_target_space_id IS NOT NULL))))), \n"
        "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations "
        "(plugin_id) ON DELETE CASCADE, \n"
        "\tFOREIGN KEY(canonical_target_person_id) REFERENCES "
        "persons (id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_target_space_id) REFERENCES "
        "spaces (id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_created_by_person_id) REFERENCES "
        "persons (id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_presence_id) REFERENCES "
        "account_assignments (id) ON DELETE RESTRICT ON UPDATE "
        "RESTRICT, \n"
        "\tFOREIGN KEY(canonical_conversation_id) REFERENCES "
        "canonical_conversations (id) ON DELETE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_plugin_background_target_enabled ON "
            "plugin_background_target_grants (plugin_id, enabled)",
            "CREATE INDEX "
            "ix_plugin_background_target_grants_canonical_created_by_person_id "
            "ON plugin_background_target_grants "
            "(canonical_created_by_person_id)",
            "CREATE INDEX "
            "ix_plugin_background_target_grants_canonical_presence_id "
            "ON plugin_background_target_grants "
            "(canonical_presence_id)",
            "CREATE INDEX "
            "ix_plugin_background_target_grants_canonical_target_person_id "
            "ON plugin_background_target_grants "
            "(canonical_target_person_id)",
            "CREATE INDEX "
            "ix_plugin_background_target_grants_canonical_target_space_id "
            "ON plugin_background_target_grants "
            "(canonical_target_space_id)",
            "CREATE UNIQUE INDEX "
            "uq_plugin_background_target_conversation ON "
            "plugin_background_target_grants (plugin_id, "
            "canonical_conversation_id) WHERE "
            "canonical_conversation_id IS NOT NULL",
            "CREATE UNIQUE INDEX "
            "uq_plugin_background_target_person ON "
            "plugin_background_target_grants (plugin_id, "
            "canonical_target_person_id) WHERE target_type = "
            "'private' AND canonical_conversation_id IS NULL",
            "CREATE UNIQUE INDEX uq_plugin_background_target_space "
            "ON plugin_background_target_grants (plugin_id, "
            "canonical_target_space_id) WHERE target_type = "
            "'group' AND canonical_conversation_id IS NULL",
        ],
    },
    "plugin_notification_outbox": {
        "table": "CREATE TABLE plugin_notification_outbox (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tsource_person_id VARCHAR(36), \n"
        "\tprepared_json TEXT, \n"
        "\tnotification_id VARCHAR(64) NOT NULL, \n"
        "\tpart_key VARCHAR(255) NOT NULL, \n"
        "\tsource_event_id INTEGER NOT NULL, \n"
        "\tplugin_id VARCHAR(128) NOT NULL, \n"
        "\ttarget_type VARCHAR(16) NOT NULL, \n"
        "\ttarget_id VARCHAR(64) NOT NULL, \n"
        "\tbot_user_id VARCHAR(64) NOT NULL, \n"
        "\tpart_type VARCHAR(16) NOT NULL, \n"
        "\ttext TEXT NOT NULL, \n"
        "\tmedia_handle_id VARCHAR(128), \n"
        "\tstatus VARCHAR(16) NOT NULL, \n"
        "\tattempts INTEGER NOT NULL, \n"
        "\tmax_attempts INTEGER NOT NULL, \n"
        "\tnext_attempt_at DATETIME NOT NULL, \n"
        "\tlease_until DATETIME, \n"
        "\tplatform_message_id VARCHAR(128), \n"
        "\tlast_error_category VARCHAR(64), \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tsent_at DATETIME, \n"
        "\tcanonical_target_person_id VARCHAR(36), \n"
        "\tcanonical_target_space_id VARCHAR(36), \n"
        "\tcanonical_conversation_id VARCHAR(36), \n"
        "\tcanonical_presence_id VARCHAR(36), \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT uq_plugin_notification_outbox_part UNIQUE "
        "(notification_id, part_key), \n"
        "\tCONSTRAINT ck_plugin_outbox_target_type CHECK (target_type "
        "IN ('group', 'private')), \n"
        "\tCONSTRAINT ck_plugin_outbox_target_owner CHECK (NOT "
        "(canonical_target_person_id IS NOT NULL AND "
        "canonical_target_space_id IS NOT NULL) AND "
        "(canonical_target_person_id IS NULL OR target_type = "
        "'private') AND (canonical_target_space_id IS NULL OR "
        "target_type = 'group') AND (status NOT IN ('pending', "
        "'processing') OR (canonical_conversation_id IS NOT NULL AND "
        "canonical_presence_id IS NOT NULL AND ((target_type = "
        "'private' AND canonical_target_person_id IS NOT NULL AND "
        "canonical_target_space_id IS NULL) OR (target_type = 'group' "
        "AND canonical_target_person_id IS NULL AND "
        "canonical_target_space_id IS NOT NULL))))), \n"
        "\tCONSTRAINT ck_plugin_notification_outbox_part_type CHECK "
        "(part_type IN ('text', 'media', 'agent_reply')), \n"
        "\tCONSTRAINT ck_plugin_notification_outbox_status CHECK "
        "(status IN ('pending', 'processing', 'sent', 'failed', "
        "'uncertain', 'cancelled')), \n"
        "\tCONSTRAINT ck_plugin_outbox_attempts CHECK (attempts >= 0 "
        "AND max_attempts >= 1), \n"
        "\tFOREIGN KEY(source_person_id) REFERENCES persons (id) ON "
        "DELETE RESTRICT, \n"
        "\tFOREIGN KEY(source_event_id) REFERENCES chat_events (id) ON "
        "DELETE CASCADE, \n"
        "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations "
        "(plugin_id) ON DELETE CASCADE, \n"
        "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons "
        "(id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces "
        "(id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_conversation_id) REFERENCES "
        "canonical_conversations (id) ON DELETE RESTRICT ON UPDATE "
        "RESTRICT, \n"
        "\tFOREIGN KEY(canonical_presence_id) REFERENCES "
        "account_assignments (id) ON DELETE RESTRICT ON UPDATE "
        "RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX "
            "ix_plugin_notification_outbox_canonical_conversation_id ON "
            "plugin_notification_outbox (canonical_conversation_id)",
            "CREATE INDEX "
            "ix_plugin_notification_outbox_canonical_presence_id ON "
            "plugin_notification_outbox (canonical_presence_id)",
            "CREATE INDEX "
            "ix_plugin_notification_outbox_canonical_target_person_id "
            "ON plugin_notification_outbox (canonical_target_person_id)",
            "CREATE INDEX "
            "ix_plugin_notification_outbox_canonical_target_space_id ON "
            "plugin_notification_outbox (canonical_target_space_id)",
            "CREATE INDEX ix_plugin_notification_outbox_due ON "
            "plugin_notification_outbox (status, next_attempt_at)",
            "CREATE INDEX ix_plugin_notification_outbox_plugin ON "
            "plugin_notification_outbox (plugin_id, status)",
            "CREATE INDEX ix_plugin_notification_outbox_source ON "
            "plugin_notification_outbox (source_event_id)",
        ],
    },
    "plugin_background_turn_jobs": {
        "table": "CREATE TABLE plugin_background_turn_jobs (\n"
        "\tid INTEGER NOT NULL, \n"
        "\tsource_person_id VARCHAR(36), \n"
        "\tsource_event_id INTEGER NOT NULL, \n"
        "\tplugin_id VARCHAR(128) NOT NULL, \n"
        "\ttarget_type VARCHAR(16) NOT NULL, \n"
        "\ttarget_id VARCHAR(64) NOT NULL, \n"
        "\tbot_user_id VARCHAR(64) NOT NULL, \n"
        "\tagent_intent VARCHAR(1000) NOT NULL, \n"
        "\tstatus VARCHAR(16) NOT NULL, \n"
        "\tattempts INTEGER NOT NULL, \n"
        "\tmax_attempts INTEGER NOT NULL, \n"
        "\tnext_attempt_at DATETIME NOT NULL, \n"
        "\tlease_until DATETIME, \n"
        "\tgenerated_text TEXT NOT NULL, \n"
        "\ttool_calls_used INTEGER NOT NULL, \n"
        "\tmodel_requests INTEGER NOT NULL, \n"
        "\tlast_error_category VARCHAR(64), \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tcompleted_at DATETIME, \n"
        "\tcanonical_target_person_id VARCHAR(36), \n"
        "\tcanonical_target_space_id VARCHAR(36), \n"
        "\tcanonical_conversation_id VARCHAR(36), \n"
        "\tcanonical_presence_id VARCHAR(36), \n"
        "\twork_id VARCHAR(36), \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT uq_plugin_background_turn_source UNIQUE "
        "(source_event_id), \n"
        "\tCONSTRAINT ck_plugin_turn_target_type CHECK (target_type "
        "IN ('group', 'private')), \n"
        "\tCONSTRAINT ck_plugin_turn_target_owner CHECK (NOT "
        "(canonical_target_person_id IS NOT NULL AND "
        "canonical_target_space_id IS NOT NULL) AND "
        "(canonical_target_person_id IS NULL OR target_type = "
        "'private') AND (canonical_target_space_id IS NULL OR "
        "target_type = 'group') AND (status NOT IN ('pending', "
        "'processing') OR (canonical_conversation_id IS NOT NULL AND "
        "canonical_presence_id IS NOT NULL AND ((target_type = "
        "'private' AND canonical_target_person_id IS NOT NULL AND "
        "canonical_target_space_id IS NULL) OR (target_type = 'group' "
        "AND canonical_target_person_id IS NULL AND "
        "canonical_target_space_id IS NOT NULL))))), \n"
        "\tCONSTRAINT ck_plugin_background_turn_status CHECK (status "
        "IN ('pending', 'processing', 'completed', 'failed', "
        "'cancelled')), \n"
        "\tCONSTRAINT ck_plugin_turn_attempts CHECK (attempts >= 0 "
        "AND max_attempts >= 1), \n"
        "\tFOREIGN KEY(source_person_id) REFERENCES persons (id) ON "
        "DELETE RESTRICT, \n"
        "\tFOREIGN KEY(source_event_id) REFERENCES chat_events (id) "
        "ON DELETE CASCADE, \n"
        "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations "
        "(plugin_id) ON DELETE CASCADE, \n"
        "\tFOREIGN KEY(canonical_target_person_id) REFERENCES persons "
        "(id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_target_space_id) REFERENCES spaces "
        "(id) ON DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_conversation_id) REFERENCES "
        "canonical_conversations (id) ON DELETE RESTRICT ON UPDATE "
        "RESTRICT, \n"
        "\tFOREIGN KEY(canonical_presence_id) REFERENCES "
        "account_assignments (id) ON DELETE RESTRICT ON UPDATE "
        "RESTRICT, \n"
        "\tFOREIGN KEY(work_id) REFERENCES runtime_work (id) ON "
        "DELETE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_plugin_background_turn_due ON "
            "plugin_background_turn_jobs (status, next_attempt_at)",
            "CREATE INDEX "
            "ix_plugin_background_turn_jobs_canonical_conversation_id "
            "ON plugin_background_turn_jobs "
            "(canonical_conversation_id)",
            "CREATE INDEX "
            "ix_plugin_background_turn_jobs_canonical_presence_id ON "
            "plugin_background_turn_jobs (canonical_presence_id)",
            "CREATE INDEX "
            "ix_plugin_background_turn_jobs_canonical_target_person_id "
            "ON plugin_background_turn_jobs "
            "(canonical_target_person_id)",
            "CREATE INDEX "
            "ix_plugin_background_turn_jobs_canonical_target_space_id "
            "ON plugin_background_turn_jobs "
            "(canonical_target_space_id)",
            "CREATE INDEX ix_plugin_background_turn_plugin ON "
            "plugin_background_turn_jobs (plugin_id, status)",
            "CREATE UNIQUE INDEX ux_plugin_background_turn_work ON "
            "plugin_background_turn_jobs (work_id)",
        ],
    },
    "runtime_work_journal": {
        "table": "CREATE TABLE runtime_work_journal (\n"
        "\twork_id VARCHAR(36) NOT NULL, \n"
        "\tchain_id VARCHAR(36) NOT NULL, \n"
        "\tcontract VARCHAR(64) NOT NULL, \n"
        "\tsource_revision INTEGER NOT NULL, \n"
        "\tphase VARCHAR(24) NOT NULL, \n"
        "\tpayload_json TEXT NOT NULL, \n"
        "\tarchived_journal_json TEXT, \n"
        "\tupdated FLOAT NOT NULL, \n"
        "\tPRIMARY KEY (work_id), \n"
        "\tFOREIGN KEY(work_id) REFERENCES runtime_work (id) ON DELETE "
        "RESTRICT\n"
        ")",
        "indexes": [],
    },
    "social_operation_receipts": {
        "table": "CREATE TABLE social_operation_receipts (\n"
        "\tid VARCHAR(36) NOT NULL, \n"
        "\tsource_turn_id VARCHAR(128) NOT NULL, \n"
        "\ttool_call_id VARCHAR(128) NOT NULL, \n"
        "\tsource_conversation_id VARCHAR(36) NOT NULL, \n"
        "\taction VARCHAR(32) NOT NULL, \n"
        "\tpayload_hash VARCHAR(64) NOT NULL, \n"
        "\ttarget_kind VARCHAR(16) NOT NULL, \n"
        "\ttarget_id VARCHAR(36) NOT NULL, \n"
        "\tpresence_id VARCHAR(36), \n"
        "\tstatus VARCHAR(16) NOT NULL, \n"
        "\tplatform_reference VARCHAR(255), \n"
        "\terror_category VARCHAR(64), \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tevent_id INTEGER, \n"
        "\tplanned_parts INTEGER, \n"
        "\tprepared_json TEXT, \n"
        "\tPRIMARY KEY (id), \n"
        "\tCONSTRAINT uq_social_operation_call UNIQUE (source_turn_id, "
        "tool_call_id), \n"
        "\tCONSTRAINT ck_social_operation_status CHECK (status IN "
        "('prepared','executing','succeeded','failed','uncertain')), \n"
        "\tCONSTRAINT ck_social_target_kind CHECK (target_kind IN "
        "('person','space')), \n"
        "\tFOREIGN KEY(presence_id) REFERENCES account_assignments (id) "
        "ON DELETE RESTRICT, \n"
        "\tFOREIGN KEY(event_id) REFERENCES chat_events (id) ON DELETE "
        "SET NULL ON UPDATE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_social_operation_event_id ON social_operation_receipts (event_id)",
            "CREATE INDEX ix_social_operation_scope_updated ON "
            "social_operation_receipts (source_conversation_id, "
            "updated_at)",
            "CREATE INDEX ix_social_operation_target_time ON "
            "social_operation_receipts (target_id, created_at)",
        ],
    },
    "runtime_config_overrides": {
        "table": "CREATE TABLE runtime_config_overrides (\n"
        "\tid INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, \n"
        "\tconfig_key VARCHAR(128) NOT NULL, \n"
        "\tscope_type VARCHAR(16) NOT NULL, \n"
        "\tvalue_json TEXT NOT NULL, \n"
        "\tvalue_type VARCHAR(16) NOT NULL, \n"
        "\tapply_mode VARCHAR(32) NOT NULL, \n"
        "\tversion INTEGER NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tupdated_by VARCHAR(64) NOT NULL, \n"
        "\tcanonical_person_id VARCHAR(36), \n"
        "\tcanonical_space_id VARCHAR(36), \n"
        "\tCONSTRAINT ck_runtime_config_overrides_scope_type CHECK "
        "(scope_type IN ('global', 'group', 'user')), \n"
        "\tCONSTRAINT ck_runtime_config_overrides_scope_owner CHECK "
        "((scope_type = 'global' AND canonical_person_id IS NULL AND "
        "canonical_space_id IS NULL) OR (scope_type = 'user' AND "
        "canonical_person_id IS NOT NULL AND canonical_space_id IS NULL) "
        "OR (scope_type = 'group' AND canonical_person_id IS NULL AND "
        "canonical_space_id IS NOT NULL)), \n"
        "\tCONSTRAINT ck_runtime_config_overrides_value_type CHECK "
        "(value_type IN ('string', 'integer', 'number', 'boolean', "
        "'enum')), \n"
        "\tCONSTRAINT ck_runtime_config_overrides_apply_mode CHECK "
        "(apply_mode IN ('hot', 'future_only', 'restart_required')), \n"
        "\tCONSTRAINT ck_runtime_config_overrides_version CHECK (version "
        ">= 1), \n"
        "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_runtime_config_overrides_canonical_person_id "
            "ON runtime_config_overrides (canonical_person_id)",
            "CREATE INDEX ix_runtime_config_overrides_canonical_space_id "
            "ON runtime_config_overrides (canonical_space_id)",
            "CREATE INDEX ix_runtime_config_overrides_scope_key ON "
            "runtime_config_overrides (scope_type, config_key)",
            "CREATE UNIQUE INDEX uq_runtime_config_overrides_global_key "
            "ON runtime_config_overrides (config_key) WHERE scope_type = "
            "'global'",
            "CREATE UNIQUE INDEX uq_runtime_config_overrides_person_key "
            "ON runtime_config_overrides (config_key, "
            "canonical_person_id) WHERE scope_type = 'user'",
            "CREATE UNIQUE INDEX uq_runtime_config_overrides_space_key ON "
            "runtime_config_overrides (config_key, canonical_space_id) "
            "WHERE scope_type = 'group'",
        ],
    },
    "plugin_config_values": {
        "table": "CREATE TABLE plugin_config_values (\n"
        "\tid INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, \n"
        "\tplugin_id VARCHAR(128) NOT NULL, \n"
        "\tscope_type VARCHAR(16) NOT NULL, \n"
        '\t"key" VARCHAR(128) NOT NULL, \n'
        "\tvalue_json TEXT NOT NULL, \n"
        "\tversion INTEGER NOT NULL, \n"
        "\tupdated_at DATETIME NOT NULL, \n"
        "\tcanonical_person_id VARCHAR(36), \n"
        "\tcanonical_space_id VARCHAR(36), \n"
        "\tCONSTRAINT ck_plugin_config_values_scope_type CHECK (scope_type "
        "IN ('global', 'group', 'user')), \n"
        "\tCONSTRAINT ck_plugin_config_values_scope_owner CHECK ((scope_type "
        "= 'global' AND canonical_person_id IS NULL AND canonical_space_id "
        "IS NULL) OR (scope_type = 'user' AND canonical_person_id IS NOT "
        "NULL AND canonical_space_id IS NULL) OR (scope_type = 'group' AND "
        "canonical_person_id IS NULL AND canonical_space_id IS NOT NULL)), \n"
        "\tCONSTRAINT ck_plugin_config_values_version CHECK (version >= "
        "1), \n"
        "\tFOREIGN KEY(plugin_id) REFERENCES plugin_installations "
        "(plugin_id) ON DELETE CASCADE, \n"
        "\tFOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON "
        "DELETE RESTRICT ON UPDATE RESTRICT, \n"
        "\tFOREIGN KEY(canonical_space_id) REFERENCES spaces (id) ON DELETE "
        "RESTRICT ON UPDATE RESTRICT\n"
        ")",
        "indexes": [
            "CREATE INDEX ix_plugin_config_values_canonical_person_id ON "
            "plugin_config_values (canonical_person_id)",
            "CREATE INDEX ix_plugin_config_values_canonical_space_id ON "
            "plugin_config_values (canonical_space_id)",
            "CREATE INDEX ix_plugin_config_values_plugin_scope ON "
            "plugin_config_values (plugin_id, scope_type)",
            "CREATE UNIQUE INDEX uq_plugin_config_values_global_key ON "
            'plugin_config_values (plugin_id, "key") WHERE scope_type = '
            "'global'",
            "CREATE UNIQUE INDEX uq_plugin_config_values_person_key ON "
            'plugin_config_values (plugin_id, canonical_person_id, "key") '
            "WHERE scope_type = 'user'",
            "CREATE UNIQUE INDEX uq_plugin_config_values_space_key ON "
            'plugin_config_values (plugin_id, canonical_space_id, "key") '
            "WHERE scope_type = 'group'",
        ],
    },
    "memberships": {
        "table": """CREATE TABLE memberships (
            first_seen_at DATETIME NOT NULL,
            last_seen_at DATETIME NOT NULL,
            canonical_person_id VARCHAR(36) NOT NULL,
            canonical_space_id VARCHAR(36) NOT NULL,
            PRIMARY KEY (canonical_person_id, canonical_space_id),
            FOREIGN KEY(canonical_person_id) REFERENCES persons (id)
                ON DELETE RESTRICT ON UPDATE RESTRICT,
            FOREIGN KEY(canonical_space_id) REFERENCES spaces (id)
                ON DELETE RESTRICT ON UPDATE RESTRICT
        )""",
        "indexes": [
            "CREATE INDEX ix_memberships_canonical_person_id ON memberships (canonical_person_id)",
            "CREATE INDEX ix_memberships_canonical_space_id ON memberships (canonical_space_id)",
        ],
    },
}

_TRIGGERS = (
    "CREATE TRIGGER trg_canonical_conversations_primary_alias_immutable\n"
    "BEFORE UPDATE OF primary_alias_id, primary_marker ON canonical_conversations\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'canonical conversation primary alias pointer is immutable')\n"
    "    WHERE NEW.primary_alias_id IS NOT OLD.primary_alias_id\n"
    "       OR NEW.primary_marker IS NOT OLD.primary_marker;\n"
    "END",
    "CREATE TRIGGER trg_conversation_legacy_aliases_primary_update\n"
    "BEFORE UPDATE ON conversation_legacy_aliases\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'pinned primary alias cannot be changed')\n"
    "    WHERE EXISTS (\n"
    "        SELECT 1 FROM canonical_conversations\n"
    "        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id\n"
    "    )\n"
    "    AND (\n"
    "        NEW.id IS NOT OLD.id\n"
    "        OR NEW.conversation_id IS NOT OLD.conversation_id\n"
    "        OR NEW.is_primary IS NOT OLD.is_primary\n"
    "        OR NEW.scope_key IS NOT OLD.scope_key\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_conversation_legacy_aliases_primary_delete\n"
    "BEFORE DELETE ON conversation_legacy_aliases\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT, 'pinned primary alias cannot be deleted')\n"
    "    WHERE EXISTS (\n"
    "        SELECT 1 FROM canonical_conversations\n"
    "        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id\n"
    "    );\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_consistency_insert\n"
    "BEFORE INSERT ON person_active_routes\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')\n"
    " WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments binding\n"
    "  JOIN accounts ba ON ba.id=binding.account_id\n"
    "  JOIN account_assignments presence ON presence.id=NEW.presence_id AND presence.kind='yuki'\n"
    "  JOIN accounts pa ON pa.id=presence.account_id\n"
    "  JOIN persons owner ON owner.id=binding.person_id\n"
    "  JOIN persons target ON target.id=NEW.person_id\n"
    "  WHERE binding.id=NEW.identity_binding_id AND binding.kind='person'\n"
    "   AND COALESCE(owner.merged_into_person_id,owner.id)\n"
    "       =COALESCE(target.merged_into_person_id,target.id)\n"
    "   AND ba.platform=pa.platform);\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_consistency_update\n"
    "BEFORE UPDATE ON person_active_routes\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')\n"
    " WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments binding\n"
    "  JOIN accounts ba ON ba.id=binding.account_id\n"
    "  JOIN account_assignments presence ON presence.id=NEW.presence_id AND presence.kind='yuki'\n"
    "  JOIN accounts pa ON pa.id=presence.account_id\n"
    "  JOIN persons owner ON owner.id=binding.person_id\n"
    "  JOIN persons target ON target.id=NEW.person_id\n"
    "  WHERE binding.id=NEW.identity_binding_id AND binding.kind='person'\n"
    "   AND COALESCE(owner.merged_into_person_id,owner.id)\n"
    "       =COALESCE(target.merged_into_person_id,target.id)\n"
    "   AND ba.platform=pa.platform);\n"
    "END",
    "CREATE TRIGGER trg_space_binding_ingest_routes_consistency_insert\n"
    "BEFORE INSERT ON space_binding_ingest_routes\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')\n"
    " WHERE NOT EXISTS (SELECT 1 FROM space_bindings binding\n"
    "  JOIN account_assignments presence ON presence.id=NEW.ingest_presence_id AND presence.kind='yuki'\n"
    "  JOIN accounts account ON account.id=presence.account_id\n"
    "  WHERE binding.id=NEW.space_binding_id AND binding.platform=account.platform);\n"
    "END",
    "CREATE TRIGGER trg_space_binding_ingest_routes_consistency_update\n"
    "BEFORE UPDATE ON space_binding_ingest_routes\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')\n"
    " WHERE NOT EXISTS (SELECT 1 FROM space_bindings binding\n"
    "  JOIN account_assignments presence ON presence.id=NEW.ingest_presence_id AND presence.kind='yuki'\n"
    "  JOIN accounts account ON account.id=presence.account_id\n"
    "  WHERE binding.id=NEW.space_binding_id AND binding.platform=account.platform);\n"
    "END",
    "CREATE TRIGGER trg_space_active_routes_consistency_insert\n"
    "BEFORE INSERT ON space_active_routes\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')\n"
    " WHERE NOT EXISTS (SELECT 1 FROM space_bindings binding\n"
    "  JOIN account_assignments presence ON presence.id=NEW.presence_id AND presence.kind='yuki'\n"
    "  JOIN accounts account ON account.id=presence.account_id\n"
    "  WHERE binding.id=NEW.space_binding_id AND binding.space_id=NEW.space_id\n"
    "   AND binding.platform=account.platform);\n"
    "END",
    "CREATE TRIGGER trg_space_active_routes_consistency_update\n"
    "BEFORE UPDATE ON space_active_routes\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')\n"
    " WHERE NOT EXISTS (SELECT 1 FROM space_bindings binding\n"
    "  JOIN account_assignments presence ON presence.id=NEW.presence_id AND presence.kind='yuki'\n"
    "  JOIN accounts account ON account.id=presence.account_id\n"
    "  WHERE binding.id=NEW.space_binding_id AND binding.space_id=NEW.space_id\n"
    "   AND binding.platform=account.platform);\n"
    "END",
    "CREATE TRIGGER trg_account_assignments_identity_immutable\n"
    "BEFORE UPDATE OF id,account_id,kind,person_id ON account_assignments\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'original assignment identity is immutable')\n"
    " WHERE NEW.id IS NOT OLD.id OR NEW.account_id IS NOT OLD.account_id\n"
    "    OR NEW.kind IS NOT OLD.kind OR NEW.person_id IS NOT OLD.person_id;\n"
    "END",
    "CREATE TRIGGER trg_accounts_native_identity_immutable\n"
    "BEFORE UPDATE OF id,platform,external_account_id ON accounts\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'stable account identity is immutable')\n"
    " WHERE NEW.id IS NOT OLD.id OR NEW.platform IS NOT OLD.platform\n"
    "    OR NEW.external_account_id IS NOT OLD.external_account_id;\n"
    "END",
    "CREATE TRIGGER trg_account_assignments_ended_immutable\n"
    "BEFORE UPDATE OF status ON account_assignments\n"
    "WHEN OLD.status='ended' AND NEW.status <> 'ended'\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'an ended assignment cannot be resumed');\n"
    "END",
    "CREATE TRIGGER trg_space_bindings_route_consistency_update\n"
    "BEFORE UPDATE OF space_id,platform ON space_bindings\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'space binding update would break space routes')\n"
    " WHERE EXISTS (SELECT 1 FROM space_binding_ingest_routes route\n"
    "  JOIN account_assignments presence ON presence.id=route.ingest_presence_id\n"
    "  JOIN accounts account ON account.id=presence.account_id\n"
    "  WHERE route.space_binding_id=NEW.id AND NEW.platform IS NOT account.platform)\n"
    " OR EXISTS (SELECT 1 FROM space_active_routes route\n"
    "  JOIN account_assignments presence ON presence.id=route.presence_id\n"
    "  JOIN accounts account ON account.id=presence.account_id\n"
    "  WHERE route.space_binding_id=NEW.id\n"
    "    AND (route.space_id IS NOT NEW.space_id OR NEW.platform IS NOT account.platform));\n"
    "END",
    "CREATE TRIGGER trg_canonical_conversations_identity_binding_id_kind_insert\n"
    "BEFORE INSERT ON canonical_conversations\n"
    "WHEN NEW.identity_binding_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.identity_binding_id AND kind='person'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_canonical_conversations_identity_binding_id_kind_update\n"
    "BEFORE UPDATE ON canonical_conversations\n"
    "WHEN NEW.identity_binding_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.identity_binding_id AND kind='person'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_canonical_conversations_entry_presence_id_kind_insert\n"
    "BEFORE INSERT ON canonical_conversations\n"
    "WHEN NEW.entry_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.entry_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_canonical_conversations_entry_presence_id_kind_update\n"
    "BEFORE UPDATE ON canonical_conversations\n"
    "WHEN NEW.entry_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.entry_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_identity_binding_id_kind_insert\n"
    "BEFORE INSERT ON person_active_routes\n"
    "WHEN NEW.identity_binding_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.identity_binding_id AND kind='person'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_identity_binding_id_kind_update\n"
    "BEFORE UPDATE ON person_active_routes\n"
    "WHEN NEW.identity_binding_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.identity_binding_id AND kind='person'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_presence_id_kind_insert\n"
    "BEFORE INSERT ON person_active_routes\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_person_active_routes_presence_id_kind_update\n"
    "BEFORE UPDATE ON person_active_routes\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_space_binding_ingest_routes_ingest_presence_id_kind_insert\n"
    "BEFORE INSERT ON space_binding_ingest_routes\n"
    "WHEN NEW.ingest_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.ingest_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_space_binding_ingest_routes_ingest_presence_id_kind_update\n"
    "BEFORE UPDATE ON space_binding_ingest_routes\n"
    "WHEN NEW.ingest_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.ingest_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_space_active_routes_presence_id_kind_insert\n"
    "BEFORE INSERT ON space_active_routes\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_space_active_routes_presence_id_kind_update\n"
    "BEFORE UPDATE ON space_active_routes\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_chat_events_author_presence_id_kind_insert\n"
    "BEFORE INSERT ON chat_events\n"
    "WHEN NEW.author_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.author_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_chat_events_author_presence_id_kind_update\n"
    "BEFORE UPDATE ON chat_events\n"
    "WHEN NEW.author_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.author_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_chat_events_ingress_presence_id_kind_insert\n"
    "BEFORE INSERT ON chat_events\n"
    "WHEN NEW.ingress_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.ingress_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_chat_events_ingress_presence_id_kind_update\n"
    "BEFORE UPDATE ON chat_events\n"
    "WHEN NEW.ingress_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.ingress_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_canonical_event_receipts_ingress_presence_id_kind_insert\n"
    "BEFORE INSERT ON canonical_event_receipts\n"
    "WHEN NEW.ingress_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.ingress_presence_id AND kind='yuki'\n"
    "  AND account_id=NEW.ingress_account_id);\n"
    "END",
    "CREATE TRIGGER trg_canonical_event_receipts_ingress_presence_id_kind_update\n"
    "BEFORE UPDATE ON canonical_event_receipts\n"
    "WHEN NEW.ingress_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.ingress_presence_id AND kind='yuki'\n"
    "  AND account_id=NEW.ingress_account_id);\n"
    "END",
    "CREATE TRIGGER trg_automations_canonical_presence_id_kind_insert\n"
    "BEFORE INSERT ON automations\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_automations_canonical_presence_id_kind_update\n"
    "BEFORE UPDATE ON automations\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_automation_versions_canonical_presence_id_kind_insert\n"
    "BEFORE INSERT ON automation_versions\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_automation_versions_canonical_presence_id_kind_update\n"
    "BEFORE UPDATE ON automation_versions\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_autonomy_initiative_runs_presence_id_kind_insert\n"
    "BEFORE INSERT ON autonomy_initiative_runs\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_autonomy_initiative_runs_presence_id_kind_update\n"
    "BEFORE UPDATE ON autonomy_initiative_runs\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_plugin_background_target_grants_canonical_presence_id_kind_insert\n"
    "BEFORE INSERT ON plugin_background_target_grants\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_plugin_background_target_grants_canonical_presence_id_kind_update\n"
    "BEFORE UPDATE ON plugin_background_target_grants\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_plugin_notification_outbox_canonical_presence_id_kind_insert\n"
    "BEFORE INSERT ON plugin_notification_outbox\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_plugin_notification_outbox_canonical_presence_id_kind_update\n"
    "BEFORE UPDATE ON plugin_notification_outbox\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_plugin_background_turn_jobs_canonical_presence_id_kind_insert\n"
    "BEFORE INSERT ON plugin_background_turn_jobs\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_plugin_background_turn_jobs_canonical_presence_id_kind_update\n"
    "BEFORE UPDATE ON plugin_background_turn_jobs\n"
    "WHEN NEW.canonical_presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.canonical_presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_social_operation_receipts_presence_id_kind_insert\n"
    "BEFORE INSERT ON social_operation_receipts\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER trg_social_operation_receipts_presence_id_kind_update\n"
    "BEFORE UPDATE ON social_operation_receipts\n"
    "WHEN NEW.presence_id IS NOT NULL\n"
    "BEGIN\n"
    " SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (\n"
    "  SELECT 1 FROM account_assignments WHERE id=NEW.presence_id AND kind='yuki'\n"
    "  );\n"
    "END",
    "CREATE TRIGGER\n"
    "    prompt_projection_event_metadata_update AFTER UPDATE OF id, bot_user_id, platform_message_id, scope_type, group_id, private_peer_user_id, sender_user_id, sender_nickname, sender_group_card, direction, event_kind, source_plugin_id, external_source, external_event_key, external_event_type, external_resume_wait, external_target_id, reply_to_message_id, reply_to_event_id, origin, automation_id, automation_run_id, occurred_at, observed_at, canonical_event_id, author_kind, author_person_id, author_presence_id, ingress_presence_id, utterance_fingerprint, ingress_provider, ingress_gateway_instance_id, caused_by_event_id, author_account_id ON chat_events\n"
    "    WHEN OLD.id IS NOT NEW.id OR OLD.bot_user_id IS NOT NEW.bot_user_id OR OLD.platform_message_id IS NOT NEW.platform_message_id OR OLD.scope_type IS NOT NEW.scope_type OR OLD.group_id IS NOT NEW.group_id OR OLD.private_peer_user_id IS NOT NEW.private_peer_user_id OR OLD.sender_user_id IS NOT NEW.sender_user_id OR OLD.sender_nickname IS NOT NEW.sender_nickname OR OLD.sender_group_card IS NOT NEW.sender_group_card OR OLD.direction IS NOT NEW.direction OR OLD.event_kind IS NOT NEW.event_kind OR OLD.source_plugin_id IS NOT NEW.source_plugin_id OR OLD.external_source IS NOT NEW.external_source OR OLD.external_event_key IS NOT NEW.external_event_key OR OLD.external_event_type IS NOT NEW.external_event_type OR OLD.external_resume_wait IS NOT NEW.external_resume_wait OR OLD.external_target_id IS NOT NEW.external_target_id OR OLD.reply_to_message_id IS NOT NEW.reply_to_message_id OR OLD.reply_to_event_id IS NOT NEW.reply_to_event_id OR OLD.origin IS NOT NEW.origin OR OLD.automation_id IS NOT NEW.automation_id OR OLD.automation_run_id IS NOT NEW.automation_run_id OR OLD.occurred_at IS NOT NEW.occurred_at OR OLD.observed_at IS NOT NEW.observed_at OR OLD.canonical_event_id IS NOT NEW.canonical_event_id OR OLD.author_kind IS NOT NEW.author_kind OR OLD.author_person_id IS NOT NEW.author_person_id OR OLD.author_presence_id IS NOT NEW.author_presence_id OR OLD.ingress_presence_id IS NOT NEW.ingress_presence_id OR OLD.utterance_fingerprint IS NOT NEW.utterance_fingerprint OR OLD.ingress_provider IS NOT NEW.ingress_provider OR OLD.ingress_gateway_instance_id IS NOT NEW.ingress_gateway_instance_id OR OLD.caused_by_event_id IS NOT NEW.caused_by_event_id OR OLD.author_account_id IS NOT NEW.author_account_id\n"
    "    BEGIN\n"
    "        UPDATE canonical_conversations SET prompt_source_revision=prompt_source_revision+1\n"
    "        WHERE id IN (OLD.canonical_conversation_id, NEW.canonical_conversation_id);\n"
    "    END",
)


def _query(connection, sql, **params):
    return list(connection.execute(text(sql), params).mappings())


def _insert_rows(connection, table, rows):
    if not rows:
        return
    columns = tuple(rows[0])
    connection.execute(
        text(
            f'INSERT INTO "{table}" ('
            + ",".join(f'"{name}"' for name in columns)
            + ") VALUES ("
            + ",".join(":" + name for name in columns)
            + ")"
        ),
        rows,
    )


def _ownership(connection, endings):
    bindings = _query(connection, "SELECT * FROM identity_bindings ORDER BY id")
    presences = _query(connection, "SELECT * FROM presences ORDER BY id")
    accounts = {}
    for row in (*bindings, *presences):
        key = row["platform"], row["external_account_id"]
        previous = accounts.get(key)
        observed = row.get("last_seen_at") or row["updated_at"]
        if previous is None:
            accounts[key] = dict(
                id=str(uuid4()),
                platform=key[0],
                external_account_id=key[1],
                display_name=row.get("display_name", ""),
                created_at=row["created_at"],
                updated_at=observed,
            )
        elif row.get("display_name") and observed > previous["updated_at"]:
            previous.update(display_name=row["display_name"], updated_at=observed)
    _insert_rows(connection, "accounts", list(accounts.values()))
    stages = []
    by_id = {}
    for kind, records in (("person", bindings), ("yuki", presences)):
        for row in records:
            status = (
                row["status"] if kind == "person" else ("active" if row["enabled"] else "disabled")
            )
            stage = dict(
                id=row["id"],
                account_id=accounts[(row["platform"], row["external_account_id"])]["id"],
                kind=kind,
                person_id=row.get("person_id"),
                status="ended" if status == "transferred" else status,
                started_at=row.get("history_starts_at") if kind == "person" else row["created_at"],
                ended_at=None,
                first_seen_at=row.get("first_seen_at", row["created_at"]),
                last_seen_at=row.get("last_seen_at", row["updated_at"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                revision=row["revision"],
                ingest_eligible=row.get("ingest_eligible", True),
            )
            proof = endings.get(row["id"])
            if proof is not None:
                if proof["expected_revision"] != row["revision"] or not proof["evidence"]:
                    raise ValueError(
                        f"assignment ending proof does not match original row: {row['id']}"
                    )
                stage["status"] = "ended"
                stage["ended_at"] = proof.get("ended_at")
            by_id[row["id"]] = stage
            stages.append(stage)
    for receipt in _query(
        connection,
        "SELECT effective_state_json FROM control_command_receipts WHERE operation_kind='identity.binding.transfer' AND status='succeeded'",
    ):
        value = json.loads(receipt["effective_state_json"] or "{}")
        old, new = by_id.get(value.get("old_binding_id")), by_id.get(value.get("binding_id"))
        if (
            old is not None
            and new is not None
            and old["status"] == "ended"
            and old["account_id"] == new["account_id"]
        ):
            old["ended_at"] = new["started_at"]
    unknown = set(endings) - by_id.keys()
    if unknown:
        raise ValueError(f"assignment ending proof names missing stages: {sorted(unknown)}")
    current = {}
    conflicts = []
    for stage in stages:
        if stage["status"] != "ended":
            previous = current.get(stage["account_id"])
            if previous is not None:
                conflicts.append((previous["id"], stage["id"]))
            current[stage["account_id"]] = stage
    if conflicts:
        raise ValueError(
            f"original ownership is contradictory; inspect actual control evidence: {conflicts}"
        )
    _insert_rows(connection, "account_assignments", stages)
    # A proof is an explicit offline resolution of original facts, never a new
    # enabled/updated_at heuristic or an ongoing runtime compatibility option.
    for stage_id, proof in endings.items():
        connection.execute(
            text("""INSERT INTO admin_operation_events
            (actor_user_id,trigger_message_id,conversation_key,capability,operation,target_type,target_id,
             before_json,after_json,success,duration_seconds,created_at)
            VALUES ('migration:0141','','','identity.assignment.end','identity.assignment.end',
             'account_assignment',:id,:before,:after,1,0,:now)"""),
            {
                "id": stage_id,
                "before": json.dumps({"expected_revision": proof["expected_revision"]}),
                "after": json.dumps(
                    {
                        "status": "ended",
                        "ended_at": proof.get("ended_at"),
                        "evidence": proof["evidence"],
                    }
                ),
                "now": datetime.now(UTC).isoformat(),
            },
        )
    return by_id


def _copy_table(connection, name, old_columns, stages):
    columns = [row["name"] for row in _query(connection, f'PRAGMA table_info("_0141_{name}")')]
    expressions = []
    for column in columns:
        if column in old_columns:
            expressions.append('o."' + column + '"')
        elif name == "canonical_event_receipts" and column == "ingress_account_id":
            expressions.append(
                "(SELECT account_id FROM account_assignments a WHERE a.id=o.ingress_presence_id)"
            )
        elif name == "canonical_event_receipts" and column == "transport_namespace":
            expressions.append("""(SELECT json_array(e.scope_type,coalesce(e.group_id,e.private_peer_user_id),c.thread_id)
                 FROM chat_events e JOIN canonical_conversations c ON c.id=e.canonical_conversation_id
                 WHERE e.canonical_event_id=o.canonical_event_id AND e.suppression_status='keeper')""")
        else:
            expressions.append("NULL")
    connection.execute(
        text(
            f'INSERT INTO "_0141_{name}" ('
            + ",".join('"' + c + '"' for c in columns)
            + ") SELECT "
            + ",".join(expressions)
            + f' FROM "{name}" o'
        )
    )


def migrate_membership_names(connection):
    """Keep unproven account cards as original Person/Space aliases before dropping the slot."""
    connection.execute(
        text("""INSERT INTO person_aliases
            (canonical_person_id,canonical_space_id,alias,alias_type,first_seen_at,last_seen_at)
            SELECT m.canonical_person_id,m.canonical_space_id,m.group_card,'group_card',
                m.first_seen_at,m.last_seen_at
            FROM memberships m
            WHERE m.group_card<>'' AND NOT EXISTS (
                SELECT 1 FROM person_aliases p
                WHERE p.canonical_person_id=m.canonical_person_id
                    AND p.canonical_space_id=m.canonical_space_id AND p.alias=m.group_card)
        """)
    )


def upgrade():
    connection = op.get_bind()
    endings = op.get_context().config.attributes.get("identity_assignment_endings", {})
    # Extract old facts before either physical owner table is removed.
    old_tables = {
        row["name"]
        for row in _query(connection, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    old_indexes = _query(
        connection,
        "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL",
    )
    old_triggers = _query(connection, "SELECT name,sql FROM sqlite_master WHERE type='trigger'")
    for row in old_triggers:
        connection.exec_driver_sql('DROP TRIGGER "' + row["name"] + '"')
    for name in (
        "accounts",
        "account_assignments",
        "account_space_profiles",
        "account_name_observations",
    ):
        connection.exec_driver_sql(_DDL[name]["table"])
    stages = _ownership(connection, endings)
    migrate_membership_names(connection)
    maxima = migrate_configurations(connection, now=datetime.now(UTC).isoformat())
    rebuilt = [name for name in _DDL if name in old_tables]
    for name in rebuilt:
        ddl = _DDL[name]["table"].replace(
            "CREATE TABLE " + name + " (", "CREATE TABLE _0141_" + name + " (", 1
        )
        connection.exec_driver_sql(ddl)
        columns = {row["name"] for row in _query(connection, f'PRAGMA table_info("{name}")')}
        _copy_table(connection, name, columns, stages)
    for name in rebuilt:
        connection.exec_driver_sql(f'DROP TABLE "{name}"')
        connection.exec_driver_sql(f'ALTER TABLE "_0141_{name}" RENAME TO "{name}"')
    normalize_native_snapshots(connection)
    normalize_publications(connection)
    migrate_account_observations(connection)
    close_unprovable_old_send_plans(connection)
    normalize_emoji_intents(connection)
    migrate_work_and_rebuild(connection)
    for name in ("identity_bindings", "presences"):
        connection.exec_driver_sql(f'DROP TABLE "{name}"')
    for entry in _DDL.values():
        for sql in entry["indexes"]:
            connection.exec_driver_sql(
                sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ").replace(
                    "CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS "
                )
            )
    # Keep independent query indexes introduced in frozen historical migrations.
    for row in old_indexes:
        if row["tbl_name"] in rebuilt:
            sql = (
                row["sql"]
                .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ")
                .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
            )
            connection.exec_driver_sql(sql)
    new_names = {sql.split()[2] for sql in _TRIGGERS}
    for row in old_triggers:
        if row["name"] not in new_names and not re.search(
            r"\b(identity_bindings|presences)\b", row["sql"]
        ):
            connection.exec_driver_sql(row["sql"])
    for sql in _TRIGGERS:
        connection.exec_driver_sql(sql)
    for table, maximum in maxima.items():
        connection.execute(
            text("UPDATE sqlite_sequence SET seq=max(seq,:max) WHERE name=:name"),
            {"max": maximum, "name": table},
        )
        connection.execute(
            text(
                "INSERT INTO sqlite_sequence(name,seq) SELECT :name,:max WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name=:name)"
            ),
            {"max": maximum, "name": table},
        )
    broken = _query(connection, "PRAGMA foreign_key_check")
    if broken:
        raise ValueError(f"0141 original references are inconsistent: {broken[:20]}")


def downgrade():
    raise RuntimeError("0141 is an irreversible account and execution contract upgrade")


"""Frozen 0140 config cleanup; current roots already own all effective rows."""


_TABLES = ("runtime_config_overrides", "plugin_config_values")
_SECRET_KEYS = (
    "api_key",
    "apikey",
    "access_token",
    "password",
    "secret",
    "credential",
    "authorization",
    "cookie",
)
_CONTENT_KEYS = (
    "content",
    "preference_value",
    "system_prompt",
    "prompt",
    "reasoning",
    "webpage",
    "raw_content",
    "html",
)


def _redact(value: object, key: str = "") -> object:
    if any(token in key.casefold() for token in (*_SECRET_KEYS, *_CONTENT_KEYS)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(name)[:128]: _redact(item, str(name)) for name, item in list(value.items())[:50]
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:512] + "[TRUNCATED]"
    return value


def migrate_configurations(connection: Connection, *, now: str) -> dict[str, int]:
    """Append snapshots, then erase dormant member rows without reviving unsets.

    0140 reads only the current root's config rows. Its merge already moved
    effective unique rows; leftovers on members are former conflict losers.
    SQLAlchemy owns the surrounding upgrade transaction; this function never
    commits and never alters pre-existing audit records or current root values.
    The returned original maxima seed AUTOINCREMENT after table reconstruction.
    """
    parents = dict(
        connection.execute(text("SELECT id, merged_into_person_id FROM persons")).tuples().all()
    )
    roots: dict[str, str] = {}
    for person_id in parents:
        path: list[str] = []
        member = person_id
        while member not in roots and parents[member] is not None:
            if member in path:
                raise ValueError(f"person_merge_cycle:{person_id}")
            path.append(member)
            member = parents[member]
        root = roots.get(member, member)
        roots[member] = root
        roots.update((item, root) for item in path)

    maxima: dict[str, int] = {}
    snapshots: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    deletions: dict[str, list[int]] = {}
    for table in _TABLES:
        maxima[table] = int(connection.scalar(text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")))
        rows = connection.execute(
            text(
                f"SELECT c.* FROM {table} c JOIN persons p ON p.id=c.canonical_person_id "
                "WHERE c.scope_type='user' AND p.merged_into_person_id IS NOT NULL "
                "ORDER BY c.canonical_person_id,c.id"
            )
        ).mappings()
        deletions[table] = []
        for current in rows:
            row = dict(current)
            source = row["canonical_person_id"]
            target = roots[source]
            key = row["config_key"] if table == "runtime_config_overrides" else row["key"]
            row["value_json"] = json.dumps(
                _redact(json.loads(row["value_json"]), key), ensure_ascii=False
            )
            snapshots[(source, target)].append(
                {
                    "table": table,
                    "id": row["id"],
                    "version": row["version"],
                    "from": source,
                    "to": target,
                    "row": row,
                }
            )
            deletions[table].append(row["id"])

    prepared = []
    for (source, target), discarded in snapshots.items():
        prepared.append(
            {
                "target": target,
                "before": json.dumps(
                    {
                        "migration": "0141",
                        "source_person_id": source,
                        "target_person_id": target,
                        "discarded_configurations": discarded,
                    },
                    ensure_ascii=False,
                ),
                "after": json.dumps(
                    {
                        "migration": "0141",
                        "person_ids": [source],
                        "person_id": target,
                        "configuration_moves": [],
                    },
                    ensure_ascii=False,
                ),
                "now": now,
            }
        )
    if prepared:
        connection.execute(
            text(
                "INSERT INTO admin_operation_events "
                "(actor_user_id,actor_principal_kind,actor_principal_id,control_request_id,"
                "trigger_message_id,conversation_key,capability,operation,target_type,target_id,"
                "before_json,after_json,success,error_category,duration_seconds,created_at) VALUES "
                "('migration:0141',NULL,NULL,NULL,'','','identity.person.merge',"
                "'identity.person.merge','person',:target,:before,:after,1,NULL,0,:now)"
            ),
            prepared,
        )
    for table, ids in deletions.items():
        if ids:
            connection.execute(
                text(f"DELETE FROM {table} WHERE id=:id"), [{"id": item} for item in ids]
            )
    return maxima


"""Frozen 0141 data transform. Caller owns DDL and the offline transaction."""


def _utc_bound(raw):
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def migrate_work_and_rebuild(connection):
    counts = {"journal_archived": 0, "rebuild_frozen": 0}
    # Keep the exact old private row (including the original payload string).
    # No Work state, counters, source, goal, call/receipt or external run changes.
    for row in connection.execute(text("SELECT * FROM runtime_work_journal")).mappings():
        if row.get("archived_journal_json") is not None:
            continue
        original = {key: value for key, value in row.items() if key != "archived_journal_json"}
        connection.execute(
            text("UPDATE runtime_work_journal SET archived_journal_json=:body WHERE work_id=:id"),
            {
                "body": json.dumps(original, ensure_ascii=False, separators=(",", ":")),
                "id": row["work_id"],
            },
        )
        counts["journal_archived"] += 1
    for row in connection.execute(
        text(
            "SELECT id, selection_json, selection_hash, snapshot_max_event_id, original_selection_json FROM memory_rebuild_runs"
        )
    ).mappings():
        if row["original_selection_json"] is not None:
            continue
        old = json.loads(row["selection_json"])
        criteria = ["id <= :snapshot"]
        params = {"snapshot": row["snapshot_max_event_id"]}
        for key, column in (
            ("bot_user_ids", "bot_user_id"),
            ("sender_user_ids", "sender_user_id"),
            ("group_ids", "group_id"),
            ("scope_types", "scope_type"),
        ):
            values = old.get(key, [])
            if values:
                names = []
                for index, value in enumerate(values):
                    name = f"{key}_{index}"
                    names.append(":" + name)
                    params[name] = value
                criteria.append(column + " IN (" + ",".join(names) + ")")
        for key, column, operator in (
            ("minimum_event_id", "id", ">="),
            ("maximum_event_id", "id", "<="),
            ("after", "occurred_at", ">="),
            ("before", "occurred_at", "<="),
        ):
            if old.get(key) is not None:
                criteria.append(f"{column} {operator} :{key}")
                params[key] = _utc_bound(old[key]) if key in {"after", "before"} else old[key]
        # Freeze the old selector's actual snapshot set, including its original
        # cross-platform behavior. Eligibility/maximum_events remain independent
        # run settings, so processed items/paid extraction are not re-selected.
        event_ids = list(
            connection.execute(
                text("SELECT id FROM chat_events WHERE " + " AND ".join(criteria) + " ORDER BY id"),
                params,
            ).scalars()
        )
        current = {
            key: value
            for key, value in old.items()
            if key not in {"bot_user_ids", "sender_user_ids", "group_ids"}
        }
        current.update(
            {
                "presence_ids": [],
                "person_ids": [],
                "space_ids": [],
                "conversation_ids": [],
                "event_ids": event_ids,
            }
        )
        encoded = json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute(
            text(
                "UPDATE memory_rebuild_runs SET original_selection_json=selection_json, original_selection_hash=selection_hash, selection_json=:body, selection_hash=:hash WHERE id=:id"
            ),
            {
                "body": encoded,
                "hash": hashlib.sha256(encoded.encode()).hexdigest(),
                "id": row["id"],
            },
        )
        counts["rebuild_frozen"] += 1
    return counts


"""Frozen 0140 -> 0141 evidence conversion. No product imports or runtime use."""


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def utc_token(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return (
        stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    ).isoformat()


def _rows(conn, sql, **params):
    return list(conn.execute(text(sql), params).mappings())


def _context(segments):
    for part in reversed(segments):
        if part.get("type") == "yuki_context" and isinstance(part.get("data"), dict):
            return part["data"]
    value = {}
    segments.append({"type": "yuki_context", "data": value})
    return value


def _source_person(conn, event, context, children, jobs):
    # Existing explicit Host actor is proof. Today's grant is never a default.
    values = {
        value
        for value in (
            context.get("source_person_id"),
            context.get("actor_person_id"),
            *(row.get("source_person_id") for row in (*children, *jobs)),
        )
        if value
    }
    for row in jobs:
        if row.get("work_id"):
            work = conn.execute(
                text("SELECT source_json FROM runtime_work WHERE id=:id"), {"id": row["work_id"]}
            ).scalar()
            if work:
                source = json.loads(work)
                if source.get("actor_person_id"):
                    values.add(source["actor_person_id"])
    return next(iter(values)) if len(values) == 1 else None


def normalize_publications(conn):
    """Run after new columns exist, before old publication layout is discarded."""
    events = conn.execute(
        text(
            "SELECT * FROM chat_events WHERE direction='external' AND source_plugin_id IS NOT NULL"
        )
    ).mappings()
    all_parts = _rows(conn, "SELECT * FROM plugin_notification_outbox ORDER BY id")
    all_jobs = _rows(conn, "SELECT * FROM plugin_background_turn_jobs ORDER BY id")
    artifacts = {
        row["handle_id"]: row for row in _rows(conn, "SELECT * FROM plugin_media_artifacts")
    }
    parts_by_event, jobs_by_event = {}, {}
    for row in all_parts:
        parts_by_event.setdefault(row["source_event_id"], []).append(row)
    for row in all_jobs:
        jobs_by_event.setdefault(row["source_event_id"], []).append(row)
    unknown_sources = []
    event_count = 0
    for event in events:
        event_count += 1
        children, jobs = parts_by_event.get(event["id"], []), jobs_by_event.get(event["id"], [])
        segments = json.loads(event["segments_json"] or "[]")
        context = _context(segments)
        creator = _source_person(conn, event, context, children, jobs)
        if creator is None and (children or jobs):
            unknown_sources.append(event["id"])
        media, handles = [], []
        text_part = next((row for row in children if row["part_key"] == "text"), None)
        for row in children:
            if row["part_type"] != "media":
                continue
            prefix, separator, token = row["part_key"].partition(":")
            index_text, separator, identity = token.partition(":")
            if prefix != "media" or not separator or not index_text.isdecimal():
                raise ValueError(f"original media part has no proven ordinal: {row['id']}")
            index = int(index_text)
            handle = row["media_handle_id"] or (
                identity if not re.fullmatch(r"[a-f0-9]{64}", identity) else None
            )
            artifact = artifacts.get(handle)
            digest = (
                identity
                if re.fullmatch(r"[a-f0-9]{64}", identity)
                else (artifact["sha256"] if artifact else None)
            )
            media.append(
                {
                    "index": index,
                    "sha256": digest,
                    "legacy_handle_id": identity if digest is None else None,
                }
            )
            handles.append((index, handle))
        handles.sort()
        media.sort(key=lambda item: item["index"])
        target = event["external_target_id"]
        publication = {
            "plugin_id": event["source_plugin_id"],
            "event_key": event["external_event_key"],
            "external_source": event["external_source"],
            "event_type": event["external_event_type"],
            "target_type": event["scope_type"],
            "target_id": target,
            "occurred_at": utc_token(event["occurred_at"]),
            "payload_json": canonical(json.loads(event["external_payload_json"] or "{}")),
            "summary": event["content"],
            "text": text_part["text"] if text_part else "",
            "ask_agent": bool(jobs),
            "resume_waiting_work": bool(event["external_resume_wait"]),
            "agent_intent": jobs[0]["agent_intent"] if jobs else "",
            "media": media,
        }
        original_ids = {row["notification_id"] for row in children}
        notification_id = (
            next(iter(original_ids))
            if len(original_ids) == 1
            else event["platform_message_id"].removeprefix("external2-").removeprefix("external-")
        )
        context.update(
            notification_id=notification_id,
            publication=publication,
            publication_hash=hashlib.sha256(canonical(publication).encode()).hexdigest(),
            media_handles=[handle for _, handle in handles],
            source_person_id=creator,
        )
        conn.execute(
            text("UPDATE chat_events SET segments_json=:value WHERE id=:id"),
            {"value": canonical(segments), "id": event["id"]},
        )
        for row in children:
            # Original plain @ never gains a new native effect. Layout happens later.
            content = {
                "text": row["text"],
                "mentions": [],
                "reply_reference": None,
                "media": row["part_type"] == "media",
            }
            prepared = {"content": content}
            conn.execute(
                text(
                    "UPDATE plugin_notification_outbox SET source_person_id=:creator, prepared_json=:prepared WHERE id=:id"
                ),
                {"creator": creator, "prepared": canonical(prepared), "id": row["id"]},
            )
        for row in jobs:
            conn.execute(
                text(
                    "UPDATE plugin_background_turn_jobs SET source_person_id=:creator WHERE id=:id"
                ),
                {"creator": creator, "id": row["id"]},
            )
    return {"publications": event_count, "unknown_source_event_ids": unknown_sources}


def normalize_native_snapshots(conn):
    """Convert proven old markers only; never rebuild cards, faces or attachments."""
    accounts = {
        (row["platform"], row["external_account_id"]): row["id"]
        for row in _rows(conn, "SELECT id,platform,external_account_id FROM accounts")
    }
    count, unlocated = 0, []
    for event in conn.execute(
        text(
            "SELECT id,content,audio_transcript,segments_json,scope_type,observed_at,bot_user_id FROM chat_events WHERE event_kind='message'"
        )
    ).mappings():
        original_json = event["segments_json"] or "[]"
        original = json.loads(original_json)
        if not any(part.get("type") in {"at", "mention"} for part in original):
            continue
        segments = json.loads(canonical(original))
        context = _context(segments)
        platform = context.get("platform") or "qq"
        content, source_cursor, projected_length, pieces = event["content"], 0, 0, []
        changed = False
        replaced_end = 0
        for index, part in enumerate(segments):
            kind, data = part.get("type"), part.get("data", {})
            if kind == "text":
                raw_text = str(data.get("text", "")).replace("\r\n", "\n").replace("\r", "\n")
                literal = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_text).strip()
                found = content.find(literal, source_cursor) if literal else source_cursor
                if found >= 0:
                    source_cursor = found + len(literal)
                continue
            if kind == "mention":
                source = data.get("source", {})
                native = source.get("data", {}) if isinstance(source, dict) else {}
                target = native.get("qq") or native.get("target_user_id") or data.get("external_id")
                data.setdefault(
                    "account_id",
                    accounts.get((platform, str(target))) if target is not None else None,
                )
                data.setdefault("assignment_id", None)
                data.setdefault("kind", "yuki" if data.get("presence_id") else "member")
                data.setdefault("display_name", "未识别成员")
                for field in ("person_id", "presence_id", "text_start", "text_end"):
                    data.setdefault(field, None)
                continue
            if kind != "at":
                continue
            target = str(data.get("qq", ""))
            if target != "all" and target.isdecimal() and (platform, target) not in accounts:
                account_id = str(uuid4())
                conn.execute(
                    text(
                        "INSERT INTO accounts(id,platform,external_account_id,display_name,created_at,updated_at) VALUES(:id,:platform,:native,:name,:now,:now)"
                    ),
                    {
                        "id": account_id,
                        "platform": platform,
                        "native": target,
                        "name": str(data.get("name") or ""),
                        "now": event["observed_at"],
                    },
                )
                accounts[(platform, target)] = account_id
            is_self = target == str(event["bot_user_id"])
            marker = (
                "[提及全体成员]"
                if target == "all"
                else ("[提及Yuki]" if is_self else f"[提及{target}]")
            )
            found = content.find(marker, source_cursor)
            name = (
                "全体成员"
                if target == "all"
                else str(data.get("name") or ("Yuki" if is_self else "未识别成员"))
            )
            snapshot = {
                "kind": "all" if target == "all" else ("yuki" if is_self else "member"),
                "display_name": name,
                "account_id": accounts.get((platform, target)),
                "assignment_id": None,
                "person_id": None,
                "presence_id": None,
                "text_start": None,
                "text_end": None,
                "source": part,
            }
            if found >= 0:
                # Keep every original byte between actual native-marker replacements.
                previous_end = 0 if not changed else replaced_end
                prefix = content[previous_end:found]
                pieces.append(prefix)
                projected_length += len(prefix)
                display = "@" + name
                snapshot["text_start"], snapshot["text_end"] = (
                    projected_length,
                    projected_length + len(display),
                )
                pieces.append(display)
                projected_length += len(display)
                replaced_end = source_cursor = found + len(marker)
                changed = True
            else:
                unlocated.append(event["id"])
            segments[index] = {"type": "mention", "data": snapshot}
        if changed:
            pieces.append(content[replaced_end:])
            new_content = "".join(pieces)
        else:
            new_content = content
        if new_content != content:
            # The original FTS triggers are offline during the table rebuild.
            # Change only these exact row terms, preserving the original rowid.
            conn.execute(
                text(
                    "INSERT INTO chat_events_fts(chat_events_fts,rowid,content,audio_transcript) "
                    "VALUES('delete',:id,:content,:audio)"
                ),
                {"id": event["id"], "content": content, "audio": event["audio_transcript"]},
            )
            conn.execute(
                text(
                    "INSERT INTO chat_events_fts(rowid,content,audio_transcript) "
                    "VALUES(:id,:content,:audio)"
                ),
                {"id": event["id"], "content": new_content, "audio": event["audio_transcript"]},
            )
        context["original_content"] = event["content"]
        context["original_segments_json"] = original_json
        context["platform"] = platform
        conn.execute(
            text("UPDATE chat_events SET content=:content, segments_json=:value WHERE id=:id"),
            {"content": new_content, "value": canonical(segments), "id": event["id"]},
        )
        count += 1
    return {"native_snapshots": count, "unlocated_event_ids": sorted(set(unlocated))}


def close_unprovable_old_send_plans(conn):
    """Close only steps proven never claimed; parent state cannot erase child ACKs."""
    rows = _rows(conn, "SELECT * FROM social_operation_receipts WHERE status='prepared'")
    changed = []
    for row in rows:
        if row["prepared_json"] is not None:
            continue
        if row["action"] == "send_message_sequence":
            # Its physical child receipts remain the authoritative facts.
            conn.execute(
                text(
                    "UPDATE social_operation_receipts SET status='failed',error_category='contract_retired_dispatch_stopped' WHERE id=:id AND status='prepared'"
                ),
                {"id": row["id"]},
            )
            changed.append(row["id"])
        elif row["action"] in {"send_message", "send_file_caption"}:
            conn.execute(
                text(
                    "UPDATE social_operation_receipts SET status='failed',error_category='contract_retired_never_dispatched' WHERE id=:id AND status='prepared'"
                ),
                {"id": row["id"]},
            )
            changed.append(row["id"])
    return {"stopped_old_receipts": changed}


def migrate_account_observations(connection):
    """Use actual account observations, never fan a Person card out to accounts."""
    accounts = {
        (row["platform"], row["external_account_id"]): dict(row)
        for row in _query(connection, "SELECT * FROM accounts")
    }
    stages = {}
    for row in _query(connection, "SELECT * FROM account_assignments"):
        stages.setdefault(row["account_id"], []).append(row)
    conversations = {
        row["id"]: row
        for row in _query(
            connection,
            "SELECT id,identity_binding_id,space_binding_id FROM canonical_conversations",
        )
    }
    profiles = {}
    names = {}
    new_accounts = []
    for event in connection.execute(
        text(
            "SELECT id,bot_user_id,sender_user_id,sender_nickname,sender_group_card,author_kind,author_person_id,author_presence_id,canonical_conversation_id,segments_json,scope_type,occurred_at,observed_at FROM chat_events WHERE event_kind='message' ORDER BY occurred_at,id"
        )
    ).mappings():
        segments = json.loads(event["segments_json"])
        context = next(
            (part["data"] for part in reversed(segments) if part.get("type") == "yuki_context"), {}
        )
        platform = context.get("platform") or "qq"
        conversation = conversations.get(event["canonical_conversation_id"])
        space = conversation["space_binding_id"] if conversation else None
        observed = event["occurred_at"]
        observations = [(event["sender_user_id"], event["sender_nickname"], None, None, "nickname")]
        if space is not None:
            observations.append(
                (event["sender_user_id"], event["sender_group_card"], space, None, "group_card")
            )
        for part in segments:
            if part.get("type") != "mention":
                continue
            data = part["data"]
            source = data.get("source", {})
            native = source.get("data", {})
            target = native.get("qq") or native.get("target_user_id") or data.get("external_id")
            if target and target != "all":
                observations.append(
                    (
                        str(target),
                        data.get("display_name", ""),
                        space,
                        data.get("assignment_id"),
                        "mention",
                    )
                )
        for native, name, scope, assignment, kind in observations:
            key = (platform, native)
            if key not in accounts:
                account = dict(
                    id=str(uuid4()),
                    platform=platform,
                    external_account_id=native,
                    display_name="",
                    created_at=event["observed_at"],
                    updated_at=event["observed_at"],
                )
                accounts[key] = account
                new_accounts.append(account)
            account = accounts[key]
            if native == event["sender_user_id"]:
                assignment = event["author_presence_id"] or (
                    conversation["identity_binding_id"] if conversation else None
                )
                if assignment is None:
                    proven = [
                        s
                        for s in stages.get(account["id"], [])
                        if s["person_id"] == event["author_person_id"]
                        and (s["started_at"] or s["first_seen_at"]) <= observed
                        and (
                            s["status"] != "ended"
                            or (s["ended_at"] is not None and observed < s["ended_at"])
                        )
                    ]
                    if len(proven) == 1:
                        assignment = proven[0]["id"]
            if scope is not None:
                profiles[(account["id"], scope)] = dict(
                    account_id=account["id"],
                    space_binding_id=scope,
                    display_name=name
                    if kind == "group_card"
                    else profiles.get((account["id"], scope), {}).get("display_name", ""),
                    first_seen_at=profiles.get((account["id"], scope), {}).get(
                        "first_seen_at", observed
                    ),
                    last_seen_at=observed,
                )
            if name:
                name_key = account["id"], assignment, scope, name
                if name_key not in names:
                    names[name_key] = dict(
                        account_id=account["id"],
                        assignment_id=assignment,
                        space_binding_id=scope,
                        name=name,
                        kind=kind,
                        first_seen_at=observed,
                        last_seen_at=observed,
                    )
                else:
                    names[name_key]["last_seen_at"] = observed
    _insert_rows(connection, "accounts", new_accounts)
    _insert_rows(connection, "account_space_profiles", list(profiles.values()))
    _insert_rows(connection, "account_name_observations", list(names.values()))


def normalize_emoji_intents(conn):
    """Carry only proven original account targets into common accepted content."""
    stages = {
        row["id"]: row
        for row in _rows(
            conn,
            "SELECT a.id,a.account_id,a.person_id,a.kind, c.platform,c.external_account_id,c.display_name FROM account_assignments a JOIN accounts c ON c.id=a.account_id",
        )
    }
    parents = {
        row["id"]: row["merged_into_person_id"]
        for row in _rows(conn, "SELECT id,merged_into_person_id FROM persons")
    }

    def root(person):
        while parents.get(person) is not None:
            person = parents[person]
        return person

    count = 0
    for row in _rows(conn, "SELECT id,source_json,intent_json FROM social_emoji_intents"):
        source = json.loads(row["source_json"])
        source["original_source_json"] = row["source_json"]
        source["original_intent_json"] = row["intent_json"]
        original_mentions = source.pop("mentions", [])
        source["context"].pop("visible_event_ids", None)
        words, mentions, length = [], [], 0
        for original in original_mentions if source.get("after_operation_id") is None else ():
            stage = stages.get(original.get("binding_id"))
            if (
                stage is None
                or stage["platform"] != source["platform"]
                or root(stage["person_id"]) != root(original.get("target_id"))
            ):
                conn.execute(
                    text(
                        "UPDATE social_emoji_intents SET closed=1,error_category='original_mention_target_unproven' WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
                continue
            label = "@" + (stage["display_name"] or "成员")
            mentions.append(
                {
                    "external_id": stage["external_account_id"],
                    "display_name": label[1:],
                    "text_start": length,
                    "text_end": length + len(label),
                    "account_id": stage["account_id"],
                    "assignment_id": stage["id"],
                    "person_id": stage["person_id"],
                    "presence_id": None,
                    "kind": "member",
                }
            )
            words.append(label)
            length += len(label)
        source["content"] = {
            "text": "".join(words),
            "mentions": mentions,
            "reply_reference": None,
            "media": False,
        }
        conn.execute(
            text("UPDATE social_emoji_intents SET source_json=:source WHERE id=:id"),
            {"source": canonical(source), "id": row["id"]},
        )
        count += 1
    return {"emoji_sources": count}
