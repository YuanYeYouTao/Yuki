"""Current conversation and route trigger SQL for ORM-created schemas.

Initial conversation guards were frozen in 0044; 0129 freezes the current
same-human Person route guards independently of these mutable ORM literals.
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

PERSON_ACTIVE_ROUTE_TRIGGERS: tuple[str, ...] = tuple(
    f"""CREATE TRIGGER trg_person_active_routes_consistency_{operation.lower()}
BEFORE {operation} ON person_active_routes
BEGIN
 SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')
 WHERE NOT EXISTS (
  SELECT 1 FROM account_assignments binding
  JOIN accounts ba ON ba.id=binding.account_id
  JOIN account_assignments presence ON presence.id=NEW.presence_id AND presence.kind='yuki'
  JOIN accounts pa ON pa.id=presence.account_id
  JOIN persons owner ON owner.id=binding.person_id
  JOIN persons target ON target.id=NEW.person_id
  WHERE binding.id=NEW.identity_binding_id AND binding.kind='person'
   AND COALESCE(owner.merged_into_person_id,owner.id)
       =COALESCE(target.merged_into_person_id,target.id)
   AND ba.platform=pa.platform);
END"""
    for operation in ("INSERT", "UPDATE")
)

SPACE_BINDING_INGEST_ROUTE_TRIGGERS: tuple[str, ...] = tuple(
    f"""CREATE TRIGGER trg_space_binding_ingest_routes_consistency_{operation.lower()}
BEFORE {operation} ON space_binding_ingest_routes
BEGIN
 SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')
 WHERE NOT EXISTS (SELECT 1 FROM space_bindings binding
  JOIN account_assignments presence ON presence.id=NEW.ingest_presence_id AND presence.kind='yuki'
  JOIN accounts account ON account.id=presence.account_id
  WHERE binding.id=NEW.space_binding_id AND binding.platform=account.platform);
END"""
    for operation in ("INSERT", "UPDATE")
)

SPACE_ACTIVE_ROUTE_TRIGGERS: tuple[str, ...] = tuple(
    f"""CREATE TRIGGER trg_space_active_routes_consistency_{operation.lower()}
BEFORE {operation} ON space_active_routes
BEGIN
 SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')
 WHERE NOT EXISTS (SELECT 1 FROM space_bindings binding
  JOIN account_assignments presence ON presence.id=NEW.presence_id AND presence.kind='yuki'
  JOIN accounts account ON account.id=presence.account_id
  WHERE binding.id=NEW.space_binding_id AND binding.space_id=NEW.space_id
   AND binding.platform=account.platform);
END"""
    for operation in ("INSERT", "UPDATE")
)

PARENT_ROUTE_GUARD_TRIGGERS: tuple[str, ...] = (
    """CREATE TRIGGER trg_account_assignments_identity_immutable
BEFORE UPDATE OF id,account_id,kind,person_id ON account_assignments
BEGIN
 SELECT RAISE(ABORT, 'original assignment identity is immutable')
 WHERE NEW.id IS NOT OLD.id OR NEW.account_id IS NOT OLD.account_id
    OR NEW.kind IS NOT OLD.kind OR NEW.person_id IS NOT OLD.person_id;
END""",
    """CREATE TRIGGER trg_accounts_native_identity_immutable
BEFORE UPDATE OF id,platform,external_account_id ON accounts
BEGIN
 SELECT RAISE(ABORT, 'stable account identity is immutable')
 WHERE NEW.id IS NOT OLD.id OR NEW.platform IS NOT OLD.platform
    OR NEW.external_account_id IS NOT OLD.external_account_id;
END""",
    """CREATE TRIGGER trg_account_assignments_ended_immutable
BEFORE UPDATE OF status ON account_assignments
WHEN OLD.status='ended' AND NEW.status <> 'ended'
BEGIN
 SELECT RAISE(ABORT, 'an ended assignment cannot be resumed');
END""",
    """CREATE TRIGGER trg_space_bindings_route_consistency_update
BEFORE UPDATE OF space_id,platform ON space_bindings
BEGIN
 SELECT RAISE(ABORT, 'space binding update would break space routes')
 WHERE EXISTS (SELECT 1 FROM space_binding_ingest_routes route
  JOIN account_assignments presence ON presence.id=route.ingest_presence_id
  JOIN accounts account ON account.id=presence.account_id
  WHERE route.space_binding_id=NEW.id AND NEW.platform IS NOT account.platform)
 OR EXISTS (SELECT 1 FROM space_active_routes route
  JOIN account_assignments presence ON presence.id=route.presence_id
  JOIN accounts account ON account.id=presence.account_id
  WHERE route.space_binding_id=NEW.id
    AND (route.space_id IS NOT NEW.space_id OR NEW.platform IS NOT account.platform));
END""",
)

# These role constraints replace the types formerly enforced by two FK tables.
ASSIGNMENT_ROLE_REFERENCES: tuple[tuple[str, str, str], ...] = (
    ("canonical_conversations", "identity_binding_id", "person"),
    ("canonical_conversations", "entry_presence_id", "yuki"),
    ("person_active_routes", "identity_binding_id", "person"),
    ("person_active_routes", "presence_id", "yuki"),
    ("space_binding_ingest_routes", "ingest_presence_id", "yuki"),
    ("space_active_routes", "presence_id", "yuki"),
    ("chat_events", "author_presence_id", "yuki"),
    ("chat_events", "ingress_presence_id", "yuki"),
    ("canonical_event_receipts", "ingress_presence_id", "yuki"),
    ("automations", "canonical_presence_id", "yuki"),
    ("automation_versions", "canonical_presence_id", "yuki"),
    ("autonomy_initiative_runs", "presence_id", "yuki"),
    ("plugin_background_target_grants", "canonical_presence_id", "yuki"),
    ("plugin_notification_outbox", "canonical_presence_id", "yuki"),
    ("plugin_background_turn_jobs", "canonical_presence_id", "yuki"),
    ("social_operation_receipts", "presence_id", "yuki"),
)
ASSIGNMENT_ROLE_TRIGGER_SQL: tuple[str, ...] = tuple(
    f"""CREATE TRIGGER trg_{table}_{column}_kind_{operation.lower()}
BEFORE {operation} ON {table}
WHEN NEW.{column} IS NOT NULL
BEGIN
 SELECT RAISE(ABORT, 'assignment kind mismatch') WHERE NOT EXISTS (
  SELECT 1 FROM account_assignments WHERE id=NEW.{column} AND kind='{kind}'
  {"AND account_id=NEW.ingress_account_id" if table == "canonical_event_receipts" else ""});
END"""
    for table, column, kind in ASSIGNMENT_ROLE_REFERENCES
    for operation in ("INSERT", "UPDATE")
)

C3_TRIGGER_SQL: tuple[str, ...] = (
    *CONVERSATION_PRIMARY_POINTER_TRIGGERS,
    *ALIAS_PRIMARY_LOCK_TRIGGERS,
    *PERSON_ACTIVE_ROUTE_TRIGGERS,
    *SPACE_BINDING_INGEST_ROUTE_TRIGGERS,
    *SPACE_ACTIVE_ROUTE_TRIGGERS,
    *PARENT_ROUTE_GUARD_TRIGGERS,
)
C3_TRIGGER_NAMES: tuple[str, ...] = tuple(sql.split()[2] for sql in C3_TRIGGER_SQL)
