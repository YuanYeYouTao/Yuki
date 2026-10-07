"""Destructively retire Genie output tables and owned runtime configuration.

Before upgrading, stop writers, reconcile pending speech effects under their
original execution IDs, and verify a consistent private backup of speech facts,
referenced WAV files and their checksums. This migration does not create a backup
or infer QQ delivery from synthesis status. Downgrade cannot restore these facts.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

# Frozen final 0096 shapes from 0048 and the canonical-only 0049 bridge.
# Include defaults, CHECKs, the six legitimate FKs, and the partial unique index.
_TABLE_DDL = {
    "speech_voice_profiles": """CREATE TABLE speech_voice_profiles (
	profile_id VARCHAR(64) NOT NULL,
	display_name VARCHAR(128) NOT NULL,
	provider VARCHAR(32) NOT NULL,
	engine_model_version VARCHAR(32) NOT NULL,
	language VARCHAR(32) NOT NULL,
	model_relative_path VARCHAR(512) NOT NULL,
	model_checksum VARCHAR(64) NOT NULL,
	default_style VARCHAR(128) NOT NULL,
	enabled BOOLEAN DEFAULT 1 NOT NULL,
	is_default BOOLEAN DEFAULT 0 NOT NULL,
	source VARCHAR(64) NOT NULL,
	source_note TEXT DEFAULT '' NOT NULL,
	license_note TEXT DEFAULT '' NOT NULL,
	manifest_hash VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL, supported_languages_json TEXT DEFAULT '[]' NOT NULL,
	PRIMARY KEY (profile_id),
	CONSTRAINT ck_speech_profiles_provider CHECK (provider = 'genie'),
	CONSTRAINT ck_speech_profiles_model_version CHECK (engine_model_version IN ('v2', 'v2proplus'))
)""",
    "speech_voice_references": """CREATE TABLE speech_voice_references (
	id INTEGER NOT NULL,
	profile_id VARCHAR(64) NOT NULL,
	reference_key VARCHAR(128) NOT NULL,
	style VARCHAR(128) NOT NULL,
	aliases_json TEXT DEFAULT '[]' NOT NULL,
	audio_relative_path VARCHAR(512) NOT NULL,
	audio_checksum VARCHAR(64) NOT NULL,
	transcript TEXT NOT NULL,
	language VARCHAR(32) NOT NULL,
	enabled BOOLEAN DEFAULT 1 NOT NULL,
	priority INTEGER DEFAULT '0' NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(profile_id) REFERENCES speech_voice_profiles (profile_id) ON DELETE CASCADE,
	CONSTRAINT uq_speech_references_profile_key UNIQUE (profile_id, reference_key)
)""",
    "person_speech_preferences": """CREATE TABLE person_speech_preferences (
	canonical_person_id VARCHAR(36) NOT NULL, 
	mode VARCHAR(32) NOT NULL, 
	source_message_id VARCHAR(128) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (canonical_person_id), 
	CONSTRAINT ck_person_speech_preferences_mode CHECK (mode IN ('text_only', 'auto', 'prefer_voice')), 
	FOREIGN KEY(canonical_person_id) REFERENCES persons (id) ON DELETE RESTRICT ON UPDATE RESTRICT
)""",
    "speech_generations": """CREATE TABLE speech_generations (
	id INTEGER NOT NULL,
	request_id VARCHAR(64) NOT NULL,
	conversation_key_hash VARCHAR(64) NOT NULL,
	trigger_event_id INTEGER,
	profile_id VARCHAR(64) NOT NULL,
	reference_id INTEGER,
	engine_version VARCHAR(32) NOT NULL,
	text_hash VARCHAR(64) NOT NULL,
	normalized_text_hash VARCHAR(64) NOT NULL,
	character_count INTEGER NOT NULL,
	cache_key VARCHAR(64) NOT NULL,
	output_relative_path VARCHAR(512) DEFAULT '' NOT NULL,
	output_format VARCHAR(16) DEFAULT 'wav' NOT NULL,
	sample_rate INTEGER,
	channels INTEGER,
	duration_milliseconds INTEGER,
	status VARCHAR(16) NOT NULL,
	error_category VARCHAR(64),
	created_at DATETIME NOT NULL,
	expires_at DATETIME, target_language VARCHAR(16) DEFAULT 'zh' NOT NULL, canonical_conversation_id VARCHAR(36) REFERENCES canonical_conversations(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
	PRIMARY KEY (id),
	CONSTRAINT ck_speech_generations_character_count CHECK (character_count > 0),
	CONSTRAINT ck_speech_generations_status CHECK (status IN ('queued', 'generating', 'succeeded', 'failed', 'cancelled', 'sent', 'expired')),
	FOREIGN KEY(trigger_event_id) REFERENCES chat_events (id) ON DELETE SET NULL,
	FOREIGN KEY(profile_id) REFERENCES speech_voice_profiles (profile_id) ON DELETE RESTRICT,
	FOREIGN KEY(reference_id) REFERENCES speech_voice_references (id) ON DELETE SET NULL,
	CONSTRAINT uq_speech_generations_request_id UNIQUE (request_id)
)""",
}
_INDEX_DDL = {
    "ix_speech_profiles_enabled_updated": "CREATE INDEX ix_speech_profiles_enabled_updated ON speech_voice_profiles (enabled, updated_at)",
    "uq_speech_profiles_one_default": "CREATE UNIQUE INDEX uq_speech_profiles_one_default ON speech_voice_profiles (is_default) WHERE is_default = 1 AND enabled = 1",
    "ix_speech_references_profile_enabled": "CREATE INDEX ix_speech_references_profile_enabled ON speech_voice_references (profile_id, enabled, priority)",
    "ix_person_speech_preferences_updated": "CREATE INDEX ix_person_speech_preferences_updated ON person_speech_preferences (updated_at)",
    "ix_speech_generations_cache_key": "CREATE INDEX ix_speech_generations_cache_key ON speech_generations (cache_key)",
    "ix_speech_generations_canonical_conversation_id": "CREATE INDEX ix_speech_generations_canonical_conversation_id ON speech_generations (canonical_conversation_id)",
    "ix_speech_generations_expires": "CREATE INDEX ix_speech_generations_expires ON speech_generations (expires_at)",
    "ix_speech_generations_status_created": "CREATE INDEX ix_speech_generations_status_created ON speech_generations (status, created_at)",
}

_CONFIG_KEYS = (
    "speech.enabled",
    "speech.provider",
    "speech.socket_path",
    "speech.root",
    "genie.data_dir",
    "speech.default_profile",
    "speech.agent_delivery_enabled",
    "speech.default_mode",
    "speech.split_sentence",
    "speech.max_synthesis_characters",
    "speech.queue_max_pending",
    "speech.cache_retention_hours",
    "speech.private_enabled",
    "speech.group_enabled",
    "speech.automation_enabled",
    "speech.plugin_enabled",
    "speech.text_fallback_enabled",
    # Frozen 0063 identifies these former speech-only keys explicitly.
    "speech.agent_effects_enabled",
    "speech.spontaneous_frequency",
)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _shape(sql: str) -> tuple[str, ...]:
    # Whitespace/identifier quoting may differ after SQLite table rebuilds.
    # Quoted literal values remain exact, including spaces and case.
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\w+|[^\s]", sql)
    return tuple(
        token
        if token.startswith("'")
        else token[1:-1].replace('""', '"').casefold()
        if token.startswith('"')
        else token.casefold()
        for token in tokens
    )


def _preflight() -> None:
    bind = op.get_bind()
    objects = bind.execute(sa.text("SELECT type,name,tbl_name,sql FROM sqlite_schema")).all()
    expected = {**_TABLE_DDL, **_INDEX_DDL}
    actual = {
        name: sql
        for kind, name, owner, sql in objects
        if owner in _TABLE_DDL and kind in {"table", "index"} and sql is not None
    }
    if set(actual) != set(expected) or any(
        _shape(actual[name]) != _shape(sql) for name, sql in expected.items()
    ):
        raise RuntimeError("Speech retirement schema shape mismatch")
    autoindexes = {
        name
        for kind, name, owner, sql in objects
        if owner in _TABLE_DDL and kind == "index" and sql is None
    }
    if autoindexes != {f"sqlite_autoindex_{table}_1" for table in _TABLE_DDL}:
        raise RuntimeError("Speech retirement index shape mismatch")
    references = re.compile(r"\b(?:" + "|".join(_TABLE_DDL) + r")\b", re.IGNORECASE)
    for kind, name, owner, sql in objects:
        if kind == "table" and name not in _TABLE_DDL:
            # Known outward and internal FKs are frozen in each owned table DDL.
            # Even an empty external child is an ownership boundary, not permission.
            foreign_keys = bind.exec_driver_sql(f"PRAGMA foreign_key_list({_quoted(name)})")
            if any(str(row[2]).casefold() in _TABLE_DDL for row in foreign_keys):
                raise RuntimeError("Speech retirement external foreign key dependency")
        elif kind in {"view", "trigger"} and (owner in _TABLE_DDL or references.search(sql or "")):
            raise RuntimeError("Speech retirement schema dependency")
    # A bounded indexed existence check, not a historical payload scan. Never
    # turn an in-flight generation into a failed/sent fact merely to allow DROP.
    if (
        bind.execute(
            sa.text(
                "SELECT id FROM speech_generations WHERE status IN ('queued','generating') LIMIT 1"
            )
        ).first()
        is not None
    ):
        raise RuntimeError("Speech retirement requires reconciliation of pending generations")


def upgrade() -> None:
    # All checks precede configuration DELETE as well as the first DROP. env.py
    # owns one explicit SQLite writer transaction and rolls back any later fault.
    _preflight()
    parameters = {f"key{index}": key for index, key in enumerate(_CONFIG_KEYS)}
    placeholders = ",".join(":" + key for key in parameters)
    op.get_bind().execute(
        sa.text(f"DELETE FROM runtime_config_overrides WHERE config_key IN ({placeholders})"),
        parameters,
    )
    for table in (
        "speech_generations",
        "speech_voice_references",
        "speech_voice_profiles",
        "person_speech_preferences",
    ):
        op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError("Speech retirement cannot restore deleted facts; use forward repair")
