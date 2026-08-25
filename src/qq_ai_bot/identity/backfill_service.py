"""Application service for audited identity backfill.

This layer does not import argparse, a renderer, or ORM models. CLI and
tests call it with already-parsed settings and a sqlite file path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from qq_ai_bot.identity.backfill_repository import (
    Failpoint,
    IdentityBackfillRepository,
    utc_now_text,
)
from qq_ai_bot.identity.backfill_types import (
    BackfillCounts,
    BackfillMode,
    BackfillPlan,
    BackfillReport,
    BackfillSettingsInput,
    BackfillStatus,
    ConflictReport,
    MemoryOwnerAssignment,
    MemoryOwnerCounts,
    failed_report,
)
from qq_ai_bot.identity.c22_automation import plan_c22_automation_targets
from qq_ai_bot.identity.c23_plugin import plan_c23_plugin_owners
from qq_ai_bot.identity.canonical_memory_owners import (
    merge_source_fingerprint,
    plan_c21_memory_owners,
)
from qq_ai_bot.identity.classifier import build_plan
from qq_ai_bot.identity.errors import IdentityBackfillError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM, INVENTORY_VERSION

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICTS = 3


class IdentityBackfillService:
    """Read-only dry-run and idempotent apply for canonical identity."""

    def __init__(
        self,
        sqlite_path: Path,
        settings: BackfillSettingsInput,
        *,
        failpoint: Failpoint | None = None,
    ) -> None:
        self._settings = settings
        self._repository = IdentityBackfillRepository(sqlite_path, failpoint=failpoint)

    def dry_run(self) -> BackfillReport:
        try:
            connection = self._repository.connect(readonly=True)
        except IdentityBackfillError as exc:
            return failed_report("dry_run", exc.category)
        try:
            connection.execute("BEGIN")
            before_changes = int(connection.total_changes)
            self._repository.require_c7_ready(connection)
            self._repository.trip("after_c7_ready")
            self._repository.require_c7_ready(connection)
            before_schema = self._repository.schema_signature(connection)
            before_business = self._repository.business_signature(connection)
            plan = self._plan(connection)
            after_changes = int(connection.total_changes)
            after_schema = self._repository.schema_signature(connection)
            after_business = self._repository.business_signature(connection)
            if (
                after_changes != before_changes
                or after_schema != before_schema
                or after_business != before_business
            ):
                raise RuntimeError("identity dry-run mutated the database")
            status: BackfillStatus = "conflicted" if plan.conflicts else "succeeded"
            return self._report("dry_run", status, plan, business_diff=0, run_recorded=False)
        except IdentityBackfillError as exc:
            return failed_report("dry_run", exc.category)
        finally:
            connection.close()

    def apply(self) -> BackfillReport:
        started_at = utc_now_text()
        source_fingerprint = ""
        gates_ok = False
        try:
            connection = self._repository.connect()
        except IdentityBackfillError as exc:
            return failed_report("apply", exc.category)
        plan: BackfillPlan | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._repository.require_c7_ready(connection)
            gates_ok = True
            self._repository.trip("after_c7_ready")
            self._repository.require_c7_ready(connection)
            before_business = self._repository.business_signature(connection)
            plan = self._plan(connection)
            source_fingerprint = plan.source_fingerprint
            if plan.conflicts:
                connection.execute("ROLLBACK")
            else:
                self._repository.apply_plan(connection, plan, utc_now_text())
                now = utc_now_text()
                after_business = self._repository.business_signature(connection)
                business_diff = 0 if after_business == before_business else 1
                self._repository.record_succeeded_run(
                    connection, plan, now=now, started_at=started_at
                )
                self._repository.trip("before_commit")
                connection.execute("COMMIT")
                connection.close()
                return self._report(
                    "apply",
                    "succeeded",
                    plan,
                    business_diff=business_diff,
                    run_recorded=True,
                )
        except IdentityBackfillError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            connection.close()
            return failed_report("apply", exc.category)
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            connection.close()
            if gates_ok:
                self._record_aborted(started_at, source_fingerprint)
            raise
        connection.close()
        if plan is None:
            raise RuntimeError("identity apply produced no plan")
        return self._record_conflicts(plan, started_at)

    def _record_conflicts(self, plan: BackfillPlan, started_at: str) -> BackfillReport:
        try:
            connection = self._repository.connect()
        except IdentityBackfillError as exc:
            return failed_report("apply", exc.category)
        try:
            self._repository.trip("before_conflict_audit")
            now = utc_now_text()
            connection.execute("BEGIN IMMEDIATE")
            self._repository.require_c7_ready(connection)
            self._repository.record_conflict_audit(connection, plan, now=now, started_at=started_at)
            connection.execute("COMMIT")
        except IdentityBackfillError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return failed_report("apply", exc.category)
        finally:
            connection.close()
        return self._report(
            "apply",
            "conflicted",
            plan,
            business_diff=0,
            run_recorded=True,
            error_category="identity_conflict",
        )

    def _record_aborted(self, started_at: str, source_fingerprint: str) -> None:
        try:
            connection = self._repository.connect()
        except IdentityBackfillError:
            return
        try:
            self._repository.trip("before_aborted_audit")
            now = utc_now_text()
            connection.execute("BEGIN IMMEDIATE")
            self._repository.require_c7_ready(connection)
            self._repository.record_failed_run(
                connection,
                source_fingerprint=source_fingerprint or "apply_aborted",
                now=now,
                started_at=started_at,
                error_category="apply_aborted",
            )
            connection.execute("COMMIT")
        except IdentityBackfillError:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        finally:
            connection.close()

    def _plan(self, connection: sqlite3.Connection) -> BackfillPlan:
        (
            accounts,
            spaces,
            persons,
            bindings,
            canonical_spaces,
            space_bindings,
            presences,
            shadows,
            fingerprint,
        ) = self._repository.load_snapshot(connection, self._settings)
        plan = build_plan(
            accounts=accounts,
            spaces=spaces,
            persons=persons,
            identity_bindings=bindings,
            canonical_spaces=canonical_spaces,
            space_bindings=space_bindings,
            presences=presences,
            shadows=shadows,
            source_fingerprint=fingerprint,
        )
        planned_person_bindings = {
            item.external_id: item.person_id
            for item in plan.accounts
            if item.classification == "person" and item.person_id
        }
        planned_space_bindings = {item.external_id: item.space_id for item in plan.spaces}
        non_person_accounts = frozenset(
            item.external_id
            for item in plan.accounts
            if item.classification in {"yuki_presence", "external_bot"}
        )
        memory_owners, memory_conflicts, memory_counts, memory_material = plan_c21_memory_owners(
            connection,
            planned_shadows=plan.shadows,
            planned_person_bindings=planned_person_bindings,
            planned_space_bindings=planned_space_bindings,
            non_person_accounts=non_person_accounts,
        )
        (
            automation_targets,
            automation_conflicts,
            automation_filled,
            automation_material,
        ) = plan_c22_automation_targets(
            connection,
            planned_person_bindings=planned_person_bindings,
            planned_space_bindings=planned_space_bindings,
            non_person_accounts=non_person_accounts,
        )
        planned_presence_bindings = {
            item.external_id: item.presence_id
            for item in plan.accounts
            if item.classification == "yuki_presence" and item.presence_id
        }
        plugin_targets, plugin_conflicts, plugin_filled, plugin_material = plan_c23_plugin_owners(
            connection,
            planned_person_bindings=planned_person_bindings,
            planned_space_bindings=planned_space_bindings,
            planned_presence_bindings=planned_presence_bindings,
            non_person_accounts=non_person_accounts,
        )
        all_conflicts = tuple(
            (*plan.conflicts, *memory_conflicts, *automation_conflicts, *plugin_conflicts)
        )
        event_authors: tuple[MemoryOwnerAssignment, ...] = ()
        event_author_material: tuple[tuple[object, ...], ...] = ()
        if not all_conflicts:
            external_bot_accounts = frozenset(
                item.external_id for item in plan.accounts if item.classification == "external_bot"
            )
            event_authors, event_author_material = self._repository.load_event_author_owners(
                connection,
                person_bindings=planned_person_bindings,
                presence_bindings=planned_presence_bindings,
                external_bot_accounts=external_bot_accounts,
            )
        fingerprint = merge_source_fingerprint(
            merge_source_fingerprint(
                merge_source_fingerprint(plan.source_fingerprint, memory_material),
                automation_material,
            ),
            plugin_material,
        )
        fingerprint = merge_source_fingerprint(fingerprint, event_author_material)
        return BackfillPlan(
            accounts=plan.accounts,
            spaces=plan.spaces,
            conflicts=all_conflicts,
            shadows=tuple((*plan.shadows, *plugin_targets)),
            skipped_external_bots=plan.skipped_external_bots,
            source_fingerprint=fingerprint,
            processed_subjects=plan.processed_subjects,
            memory_owners=memory_owners,
            memory_owner_counts=MemoryOwnerCounts(
                jobs=memory_counts.jobs,
                receipts=memory_counts.receipts,
                reflection_states=memory_counts.reflection_states,
                reflection_runs=memory_counts.reflection_runs,
                dream_clusters=memory_counts.dream_clusters,
                facts_verified=memory_counts.facts_verified,
                automation_targets=automation_filled,
                plugin_targets=plugin_filled,
            ),
            automation_targets=automation_targets,
            plugin_targets=plugin_targets,
            event_authors=event_authors,
        )

    def _report(
        self,
        mode: BackfillMode,
        status: BackfillStatus,
        plan: BackfillPlan,
        *,
        business_diff: int,
        run_recorded: bool,
        error_category: str | None = None,
    ) -> BackfillReport:
        persons = sum(1 for item in plan.accounts if item.classification == "person")
        presences = sum(1 for item in plan.accounts if item.classification == "yuki_presence")
        bots = sum(1 for item in plan.accounts if item.classification == "external_bot")
        return BackfillReport(
            mode=mode,
            status=status,
            business_diff=business_diff,
            source_fingerprint=plan.source_fingerprint,
            inventory_version=INVENTORY_VERSION,
            platform=IDENTITY_PLATFORM,
            counts=BackfillCounts(
                processed=plan.processed_subjects,
                persons=persons,
                identity_bindings=persons,
                spaces=len(plan.spaces),
                space_bindings=len(plan.spaces),
                presences=presences,
                conflicts=len(plan.conflicts),
                skipped=plan.skipped_external_bots,
                shadows_filled=len(plan.shadows),
                person_class=persons,
                yuki_presence_class=presences,
                external_bot_class=bots,
                space_class=len(plan.spaces),
                memory_job_owners=plan.memory_owner_counts.jobs,
                memory_receipt_owners=plan.memory_owner_counts.receipts,
                memory_reflection_state_owners=plan.memory_owner_counts.reflection_states,
                memory_reflection_run_owners=plan.memory_owner_counts.reflection_runs,
                memory_dream_cluster_owners=plan.memory_owner_counts.dream_clusters,
                memory_facts_verified=plan.memory_owner_counts.facts_verified,
                automation_targets=plan.memory_owner_counts.automation_targets,
                plugin_targets=plan.memory_owner_counts.plugin_targets,
                event_authors=len(plan.event_authors),
            ),
            conflicts=tuple(
                ConflictReport(
                    subject_kind=item.subject_kind,
                    conflict_kind=item.conflict_kind,
                    error_category=item.error_category,
                    fingerprint=item.fingerprint,
                )
                for item in plan.conflicts
            ),
            run_recorded=run_recorded,
            error_category=error_category,
        )

    @staticmethod
    def exit_code(report: BackfillReport) -> int:
        if report.status == "conflicted":
            return EXIT_CONFLICTS
        if report.status == "failed":
            return EXIT_ERROR
        return EXIT_OK
