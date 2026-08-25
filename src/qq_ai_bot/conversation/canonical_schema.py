"""Frozen C3 trigger SQL shared by ORM create_all.

Revision 0044 must embed identical literals and must not import this module.
"""

from __future__ import annotations

CONVERSATION_PRIMARY_POINTER_TRIGGERS: tuple[str, ...] = (
    """
CREATE TRIGGER trg_canonical_conversations_primary_alias_immutable
BEFORE UPDATE OF primary_alias_id, primary_marker ON canonical_conversations
BEGIN
    SELECT RAISE(ABORT, 'canonical conversation primary alias pointer is immutable')
    WHERE NEW.primary_alias_id IS NOT OLD.primary_alias_id
       OR NEW.primary_marker IS NOT OLD.primary_marker;
END
""".strip(),
)

ALIAS_PRIMARY_LOCK_TRIGGERS: tuple[str, ...] = (
    """
CREATE TRIGGER trg_conversation_legacy_aliases_primary_update
BEFORE UPDATE ON conversation_legacy_aliases
BEGIN
    SELECT RAISE(ABORT, 'pinned primary alias cannot be changed')
    WHERE EXISTS (
        SELECT 1 FROM canonical_conversations
        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id
    )
    AND (
        NEW.id IS NOT OLD.id
        OR NEW.conversation_id IS NOT OLD.conversation_id
        OR NEW.is_primary IS NOT OLD.is_primary
        OR NEW.scope_key IS NOT OLD.scope_key
    );
END
""".strip(),
    """
CREATE TRIGGER trg_conversation_legacy_aliases_primary_delete
BEFORE DELETE ON conversation_legacy_aliases
BEGIN
    SELECT RAISE(ABORT, 'pinned primary alias cannot be deleted')
    WHERE EXISTS (
        SELECT 1 FROM canonical_conversations
        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id
    );
END
""".strip(),
)

PERSON_ACTIVE_ROUTE_TRIGGERS: tuple[str, ...] = (
    """
CREATE TRIGGER trg_person_active_routes_consistency_insert
BEFORE INSERT ON person_active_routes
BEGIN
    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM identity_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.identity_binding_id
          AND binding.person_id = NEW.person_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_person_active_routes_consistency_update
BEFORE UPDATE ON person_active_routes
BEGIN
    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM identity_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.identity_binding_id
          AND binding.person_id = NEW.person_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
)

SPACE_BINDING_INGEST_ROUTE_TRIGGERS: tuple[str, ...] = (
    """
CREATE TRIGGER trg_space_binding_ingest_routes_consistency_insert
BEFORE INSERT ON space_binding_ingest_routes
BEGIN
    SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.ingest_presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_binding_ingest_routes_consistency_update
BEFORE UPDATE ON space_binding_ingest_routes
BEGIN
    SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.ingest_presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
)

SPACE_ACTIVE_ROUTE_TRIGGERS: tuple[str, ...] = (
    """
CREATE TRIGGER trg_space_active_routes_consistency_insert
BEFORE INSERT ON space_active_routes
BEGIN
    SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.space_id = NEW.space_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_active_routes_consistency_update
BEFORE UPDATE ON space_active_routes
BEGIN
    SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.space_id = NEW.space_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
)

PARENT_ROUTE_GUARD_TRIGGERS: tuple[str, ...] = (
    """
CREATE TRIGGER trg_identity_bindings_route_consistency_update
BEFORE UPDATE OF person_id, platform ON identity_bindings
BEGIN
    SELECT RAISE(ABORT, 'identity binding update would break person active route')
    WHERE EXISTS (
        SELECT 1
        FROM person_active_routes AS route
        JOIN presences AS presence ON presence.id = route.presence_id
        WHERE route.identity_binding_id = NEW.id
          AND (
            route.person_id IS NOT NEW.person_id
            OR NEW.platform IS NOT presence.platform
          )
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_bindings_route_consistency_update
BEFORE UPDATE OF space_id, platform ON space_bindings
BEGIN
    SELECT RAISE(ABORT, 'space binding update would break space routes')
    WHERE EXISTS (
        SELECT 1
        FROM space_binding_ingest_routes AS route
        JOIN presences AS presence ON presence.id = route.ingest_presence_id
        WHERE route.space_binding_id = NEW.id
          AND NEW.platform IS NOT presence.platform
    )
    OR EXISTS (
        SELECT 1
        FROM space_active_routes AS route
        JOIN presences AS presence ON presence.id = route.presence_id
        WHERE route.space_binding_id = NEW.id
          AND (
            route.space_id IS NOT NEW.space_id
            OR NEW.platform IS NOT presence.platform
          )
    );
END
""".strip(),
    """
CREATE TRIGGER trg_presences_route_consistency_update
BEFORE UPDATE OF platform ON presences
BEGIN
    SELECT RAISE(ABORT, 'presence platform update would break routes')
    WHERE EXISTS (
        SELECT 1
        FROM person_active_routes AS route
        JOIN identity_bindings AS binding ON binding.id = route.identity_binding_id
        WHERE route.presence_id = NEW.id
          AND binding.platform IS NOT NEW.platform
    )
    OR EXISTS (
        SELECT 1
        FROM space_binding_ingest_routes AS route
        JOIN space_bindings AS binding ON binding.id = route.space_binding_id
        WHERE route.ingest_presence_id = NEW.id
          AND binding.platform IS NOT NEW.platform
    )
    OR EXISTS (
        SELECT 1
        FROM space_active_routes AS route
        JOIN space_bindings AS binding ON binding.id = route.space_binding_id
        WHERE route.presence_id = NEW.id
          AND binding.platform IS NOT NEW.platform
    );
END
""".strip(),
)

C3_TRIGGER_SQL: tuple[str, ...] = (
    *CONVERSATION_PRIMARY_POINTER_TRIGGERS,
    *ALIAS_PRIMARY_LOCK_TRIGGERS,
    *PERSON_ACTIVE_ROUTE_TRIGGERS,
    *SPACE_BINDING_INGEST_ROUTE_TRIGGERS,
    *SPACE_ACTIVE_ROUTE_TRIGGERS,
    *PARENT_ROUTE_GUARD_TRIGGERS,
)

C3_TRIGGER_NAMES: tuple[str, ...] = (
    "trg_canonical_conversations_primary_alias_immutable",
    "trg_conversation_legacy_aliases_primary_update",
    "trg_conversation_legacy_aliases_primary_delete",
    "trg_person_active_routes_consistency_insert",
    "trg_person_active_routes_consistency_update",
    "trg_space_binding_ingest_routes_consistency_insert",
    "trg_space_binding_ingest_routes_consistency_update",
    "trg_space_active_routes_consistency_insert",
    "trg_space_active_routes_consistency_update",
    "trg_identity_bindings_route_consistency_update",
    "trg_space_bindings_route_consistency_update",
    "trg_presences_route_consistency_update",
)
