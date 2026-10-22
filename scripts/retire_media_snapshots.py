"""Audit retired media intermediates in a stopped deployment or complete offline copy.

Use the actual Bot/Manager databases, artifact store, persistent workspace and
private evidence roots together. The default is read-only; --apply repeats the
audit before deleting only proven, unreferenced duplicate intermediates. This
does not stop services, rewrite receipts, remove user files or collect evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError, WorkspaceStore

UUID_PATTERN = re.compile(rb"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)


@contextmanager
def read_database(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        yield db


def tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def references(value: str | bytes, identities: set[str]) -> set[str]:
    encoded = value.encode() if isinstance(value, str) else value
    return {match.decode().lower() for match in UUID_PATTERN.findall(encoded)} & identities


def copy_path(path: str) -> str:
    return "/workspace/" + "/".join(FileWorkspace.parts(path))


def scan_database(
    path: Path,
    candidates: dict[str, dict[str, Any]],
    found: dict[str, set[str]],
    copies: dict[str, set[str]],
    *,
    manager: bool = False,
    exclude_tables: frozenset[str] = frozenset(),
) -> None:
    identities = set(candidates)
    with read_database(path) as db:
        for table in sorted(tables(db)):
            if table.startswith("sqlite_") or table in exclude_tables:
                continue
            for row in db.execute(f"SELECT * FROM {quote(table)}"):
                if manager and table == "workspace_migrations":
                    item = candidates.get(row["artifact_id"])
                    if item is not None and row["sha256"] == item["sha256"]:
                        copies[item["id"]].add(copy_path(row["path"]))
                        item["legacy_mapping"] = dict(row)
                    continue
                for column, value in dict(row).items():
                    if not isinstance(value, (str, bytes)):
                        continue
                    if (
                        manager
                        and table == "environment_mutations"
                        and column in {"request_id", "digest"}
                    ):
                        # Execution keys identify the receipt, not a retained blob.
                        continue
                    if manager and table == "environment_mutations" and column == "result":
                        body = json.loads(value)
                        # The retired checkout returned exactly these copy facts.
                        # Keep that original receipt; it is not a platform delivery.
                        if isinstance(body, dict) and set(body) == {
                            "artifact_id",
                            "path",
                            "version",
                            "size",
                        }:
                            item = candidates.get(body["artifact_id"])
                            if item is not None and (body["version"], body["size"]) == (
                                item["sha256"],
                                item["size"],
                            ):
                                copies[item["id"]].add(copy_path(body["path"]))
                                continue
                    for identity in references(value, identities):
                        found[identity].add(f"{path}:{table}.{column}")


def scan_file(path: Path, identities: set[str]) -> tuple[set[str], str, int]:
    owner = FileWorkspace(path.parent)
    with owner.open_file(path.name) as fd:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        found: set[str] = set()
        tail = b""
        size = 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            digest.update(chunk)
            found.update(references(tail + chunk, identities))
            tail = chunk[-36:]
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise WorkspaceError("file_changed_during_read")
        return found, digest.hexdigest(), size


def scan_private_evidence(
    database: Path,
    protocol_root: Path,
    tool_artifacts: Path,
    identities: set[str],
    found: dict[str, set[str]],
    incomplete: list[str],
) -> None:
    objects: list[tuple[Path, str | None, int]] = []
    with read_database(database) as db:
        current = tables(db)
        incomplete.extend(
            "missing_private_evidence_metadata:" + table
            for table in ("runtime_protocol_refs", "runtime_protocol_objects", "tool_artifacts")
            if table not in current
        )
        if "runtime_protocol_refs" in current:
            for digest, size in db.execute(
                "SELECT DISTINCT o.sha256,o.byte_size FROM runtime_protocol_objects o "
                "JOIN runtime_protocol_refs r ON r.sha256=o.sha256"
            ):
                if re.fullmatch("[0-9a-f]{64}", digest) is None:
                    incomplete.append("invalid_owned_protocol_id")
                    continue
                objects.append((protocol_root / digest[:2] / digest[2:], digest, size))
        if "tool_artifacts" in current:
            for relative, digest, size in db.execute(
                "SELECT relative_path,sha256,byte_size FROM tool_artifacts WHERE deleting=0"
            ):
                try:
                    parts = FileWorkspace.parts(relative)
                except WorkspaceError:
                    incomplete.append("invalid_owned_tool_artifact_path")
                    continue
                objects.append((tool_artifacts.joinpath(*parts), digest, size))
    for path, expected, size in objects:
        try:
            matches, actual, actual_size = scan_file(path, identities)
        except (OSError, WorkspaceError) as exc:
            incomplete.append(f"{path}:{type(exc).__name__}")
            continue
        for identity in matches:
            found[identity].add(str(path))
        if actual_size != size or (expected is not None and actual != expected):
            incomplete.append(f"{path}:private_evidence_corrupt")


def audit(
    *,
    workspace: Path,
    home_workspace: Path,
    database: Path,
    manager_database: Path,
    protocol_root: Path,
    tool_artifacts: Path,
    extra_databases: tuple[Path, ...] = (),
    extra_reference_roots: tuple[Path, ...] = (),
    apply: bool = False,
) -> dict[str, Any]:
    with read_database(workspace / "manifest.sqlite3") as db:
        candidates = {
            row["id"]: dict(row)
            for row in db.execute(
                "SELECT a.*,s.source_key FROM artifacts a JOIN artifact_snapshots s ON s.id=a.id "
                "WHERE s.source_key LIKE 'sticker:%' OR s.source_key LIKE 'attachment:%'"
            )
        }
        blob_counts = dict(db.execute("SELECT blob,count(*) FROM artifacts GROUP BY blob"))
    found: dict[str, set[str]] = defaultdict(set)
    copies: dict[str, set[str]] = defaultdict(set)
    incomplete: list[str] = []
    identities = set(candidates)
    scan_database(
        workspace / "manifest.sqlite3",
        candidates,
        found,
        copies,
        exclude_tables=frozenset({"artifacts", "artifact_snapshots"}),
    )
    scan_database(database, candidates, found, copies)
    scan_database(manager_database, candidates, found, copies, manager=True)
    for path in extra_databases:
        scan_database(path, candidates, found, copies)
    scan_private_evidence(database, protocol_root, tool_artifacts, identities, found, incomplete)
    for root in extra_reference_roots:
        if not root.is_dir():
            incomplete.append(f"{root}:reference_root_missing")
            continue
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    matches, _digest, _size = scan_file(path, identities)
                except (OSError, WorkspaceError) as exc:
                    incomplete.append(f"{path}:{type(exc).__name__}")
                    continue
                for identity in matches:
                    found[identity].add(str(path))
    files = FileWorkspace(home_workspace)
    blobs = FileWorkspace(workspace)
    results = []
    eligible = []
    for identity, item in candidates.items():
        reasons = []
        if found[identity]:
            reasons.append("referenced")
        if incomplete:
            reasons.append("reference_evidence_incomplete")
        if blob_counts[item["blob"]] != 1:
            reasons.append("blob_shared_by_manifest_rows")
        if item["source_key"].startswith("sticker:"):
            _prefix, platform, digest = item["source_key"].split(":", 2)
            try:
                with files.open_file(f"stickers/{platform}/index/{identity}.json") as fd:
                    with os.fdopen(os.dup(fd), "r", encoding="utf-8") as stream:
                        record = json.load(stream)
                if record["id"] == identity and record["sha256"] == digest == item["sha256"]:
                    copies[identity].add(copy_path(record["relative_path"]))
            except (OSError, WorkspaceError, ValueError, KeyError) as exc:
                item["library_lookup"] = type(exc).__name__
        matching = []
        for copy_name in sorted(copies[identity]):
            try:
                with files.open_file(copy_name) as fd:
                    digest, info = files.fingerprint(fd)
                if (digest, info.st_size) == (item["sha256"], item["size"]):
                    matching.append(copy_name)
            except (OSError, WorkspaceError):
                continue
        if not matching:
            reasons.append("no_matching_persistent_file")
        try:
            # Validate the actual blob before changing its manifest, without a writer.
            WorkspaceStore(workspace)._blob(item["blob"])
            with blobs.open_file(item["blob"]) as fd:
                digest, info = blobs.fingerprint(fd)
            if (digest, info.st_size) != (item["sha256"], item["size"]):
                reasons.append("blob_content_mismatch")
        except (OSError, WorkspaceError) as exc:
            reasons.append(f"blob_unavailable:{type(exc).__name__}")
        result = {
            **item,
            "matching_persistent_files": matching,
            "references": sorted(found[identity]),
            "keep_reasons": reasons,
            "removed": False,
        }
        results.append(result)
        if not reasons:
            eligible.append(result)
    if apply:
        # All reference scans and file hashes are already complete. Services and
        # terminal owners must stay stopped throughout this explicit operation.
        with closing(sqlite3.connect(workspace / "manifest.sqlite3")) as db, db:
            for item in eligible:
                deleted = db.execute(
                    "DELETE FROM artifacts WHERE id=? AND blob=? AND sha256=? AND size=? "
                    "AND revision=?",
                    (item["id"], item["blob"], item["sha256"], item["size"], item["revision"]),
                ).rowcount
                if deleted != 1:
                    raise RuntimeError("offline_manifest_changed")
                db.execute("DELETE FROM artifact_snapshots WHERE id=?", (item["id"],))
        for item in eligible:
            (workspace / item["blob"]).unlink()
            item["removed"] = True
        with closing(sqlite3.connect(manager_database)) as db, db:
            if "workspace_migrations" in tables(db):
                db.executemany(
                    "DELETE FROM workspace_migrations WHERE artifact_id=?",
                    ((item["id"],) for item in eligible),
                )
    return {
        "applied": apply,
        "candidate_count": len(results),
        "eligible_count": len(eligible),
        "removed_count": sum(item["removed"] for item in results),
        "removed_bytes": sum(item["size"] for item in results if item["removed"]),
        "reference_evidence_incomplete": incomplete,
        "candidates": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "workspace",
        "home-workspace",
        "database",
        "manager-database",
        "protocol-root",
        "tool-artifacts",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--extra-database", action="append", default=[], type=Path)
    parser.add_argument("--extra-reference-root", action="append", default=[], type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            audit(
                workspace=args.workspace,
                home_workspace=args.home_workspace,
                database=args.database,
                manager_database=args.manager_database,
                protocol_root=args.protocol_root,
                tool_artifacts=args.tool_artifacts,
                extra_databases=tuple(args.extra_database),
                extra_reference_roots=tuple(args.extra_reference_root),
                apply=args.apply,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
