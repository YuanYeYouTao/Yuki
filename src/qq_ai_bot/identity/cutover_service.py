"""Application service for identity-cutover --plan/--apply.

Does not import argparse, a renderer, or ORM models.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from qq_ai_bot.identity.cutover_repository import IdentityCutoverRepository
from qq_ai_bot.identity.cutover_source import manifests_equal
from qq_ai_bot.identity.cutover_types import (
    CUTOVER_INVENTORY_VERSION,
    CutoverCounts,
    CutoverMode,
    CutoverPlan,
    CutoverReport,
    CutoverSettingsInput,
    SnapshotEvidence,
    blocked_cutover_report,
    failed_cutover_report,
)
from qq_ai_bot.identity.errors import IdentityCutoverError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.persistence.instance_lock import (
    ApplicationAlreadyActiveError,
    SQLiteApplicationLock,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 3


class IdentityCutoverService:
    """Read-mostly plan and atomic apply for the identity epoch flip."""

    def __init__(
        self,
        sqlite_path: Path,
        settings: CutoverSettingsInput,
        *,
        failpoint: object | None = None,
    ) -> None:
        self._path = sqlite_path
        self._settings = settings
        self._repository = IdentityCutoverRepository(
            sqlite_path, failpoint=failpoint if callable(failpoint) else None
        )

    def plan(self) -> CutoverReport:
        lock = SQLiteApplicationLock(self._path)
        try:
            lock.acquire()
        except ApplicationAlreadyActiveError:
            return blocked_cutover_report("plan", "downtime_evidence")
        try:
            return self._plan_locked()
        finally:
            lock.release()

    def _plan_locked(self) -> CutoverReport:
        snapshot: SnapshotEvidence | None = None
        try:
            connection = self._repository.connect()
        except IdentityCutoverError as exc:
            return failed_cutover_report("plan", exc.category)
        try:
            connection.execute("BEGIN")
            before_changes = int(connection.total_changes)
            before_schema = self._repository.schema_signature(connection)
            before_business = self._repository.business_signature(connection)
            self._repository.require_c27_ready(connection)
            self._repository.trip("after_schema_ready")
            snapshot = self._repository.snapshot_evidence(self._settings)
            plan = self._repository.build_plan(connection, self._settings, snapshot)
            self._repository.persist_manifest(connection, plan)
            self._repository.record_run(
                connection,
                self._settings,
                mode="plan",
                status="succeeded",
                source_fingerprint=plan.source_fingerprint,
                error_category=None,
                snapshot=snapshot,
            )
            connection.execute("COMMIT")
            after_schema = self._repository.schema_signature(connection)
            after_business = self._repository.business_signature(connection)
            if after_schema != before_schema:
                raise RuntimeError("identity cutover plan mutated schema")
            if after_business != before_business:
                raise RuntimeError("identity cutover plan mutated business sources")
            del before_changes
            return self._report("plan", "succeeded", plan, business_diff=0, run_recorded=True)
        except IdentityCutoverError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            try:
                connection.execute("BEGIN")
                self._repository.record_run(
                    connection,
                    self._settings,
                    mode="plan",
                    status="blocked" if exc.category != "operational_error" else "failed",
                    source_fingerprint=None,
                    error_category=exc.category,
                    snapshot=snapshot,
                )
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            return CutoverReport(
                mode="plan",
                status="blocked" if exc.category != "operational_error" else "failed",
                source_fingerprint="",
                inventory_version=CUTOVER_INVENTORY_VERSION,
                platform=IDENTITY_PLATFORM,
                git_revision=self._settings.git_revision,
                counts=CutoverCounts(
                    conversations=0,
                    aliases=0,
                    routes=0,
                    mapped_events=0,
                    suppressed_events=0,
                    baselines=0,
                ),
                run_recorded=True,
                error_category=exc.category,
            )
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return failed_cutover_report("plan", "operational_error")
        finally:
            connection.close()

    def apply(self, manifest_fingerprint: str) -> CutoverReport:
        lock = SQLiteApplicationLock(self._path)
        try:
            lock.acquire()
        except ApplicationAlreadyActiveError:
            return blocked_cutover_report("apply", "downtime_evidence")
        try:
            return self._apply_locked(manifest_fingerprint)
        finally:
            lock.release()

    def _apply_locked(self, manifest_fingerprint: str) -> CutoverReport:
        try:
            connection = self._repository.connect()
        except IdentityCutoverError as exc:
            return failed_cutover_report("apply", exc.category)
        plan: CutoverPlan | None = None
        snapshot: SnapshotEvidence | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._repository.require_c27_ready(connection)
            self._repository.trip("after_schema_ready")
            stored_payload = self._repository.load_manifest_payload(
                connection, manifest_fingerprint
            )
            snapshot = self._repository.snapshot_evidence(self._settings)
            live_source = self._repository.require_source_fresh(
                connection, self._settings, stored_payload, snapshot
            )
            stored_cutoff = stored_payload.get("c21_readable_cutoff")
            if not isinstance(stored_cutoff, str) or not stored_cutoff.strip():
                raise IdentityCutoverError("state_mismatch")
            plan = self._repository.build_plan(
                connection,
                self._settings,
                snapshot,
                c21_cutoff=stored_cutoff,
            )
            stored_fingerprint = str(stored_payload.get("source_fingerprint") or "")
            if plan.source_fingerprint != stored_fingerprint or not manifests_equal(
                plan.source_manifest, live_source
            ):
                raise IdentityCutoverError("source_fingerprint")
            if plan.decision_digest != self._repository.manifest_decision_digest(stored_payload):
                raise IdentityCutoverError("decision_digest")
            before_business = self._repository.business_signature(connection)
            self._repository.apply_plan(connection, plan)
            self._repository.record_run(
                connection,
                self._settings,
                mode="apply",
                status="succeeded",
                source_fingerprint=plan.source_fingerprint,
                error_category=None,
                snapshot=snapshot,
            )
            self._repository.trip("before_commit")
            connection.execute("COMMIT")
            after_business = self._repository.business_signature(connection)
            return self._report(
                "apply",
                "succeeded",
                plan,
                business_diff=0 if after_business == before_business else 1,
                run_recorded=True,
            )
        except IdentityCutoverError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return failed_cutover_report("apply", exc.category)
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return failed_cutover_report("apply", "operational_error")
        finally:
            connection.close()

    def _report(
        self,
        mode: CutoverMode,
        status: str,
        plan: CutoverPlan,
        *,
        business_diff: int,
        run_recorded: bool,
    ) -> CutoverReport:
        return CutoverReport(
            mode=mode,
            status=status,  # type: ignore[arg-type]
            source_fingerprint=plan.source_fingerprint,
            inventory_version=CUTOVER_INVENTORY_VERSION,
            platform=IDENTITY_PLATFORM,
            git_revision=plan.git_revision,
            counts=CutoverCounts(
                conversations=len(plan.conversations),
                aliases=len(plan.conversations),
                routes=len(plan.conversations),
                mapped_events=sum(1 if item.last_event_id else 0 for item in plan.conversations),
                suppressed_events=len(plan.suppress_event_ids),
                baselines=len(plan.conversations),
            ),
            run_recorded=run_recorded,
            business_diff=business_diff,
        )

    @staticmethod
    def exit_code(report: CutoverReport) -> int:
        if report.status == "blocked":
            return EXIT_BLOCKED
        if report.status == "failed":
            return EXIT_ERROR
        return EXIT_OK
