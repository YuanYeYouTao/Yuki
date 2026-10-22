"""Collect static audit evidence without importing or executing Yuki.

This is an audit artifact, not a runtime check or a claim of semantic coverage.
Run with Python 3.12+ from any directory. Source paths are repository-relative.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
DB_IMPORTS = ("sqlalchemy", "sqlite3", "aiosqlite", "alembic")
DB_HINT = re.compile(
    r"\b(?:Database|AsyncSession|Session|sqlite3|aiosqlite|sessionmaker|"
    r"read_session|immediate_session|apply_snapshot_write|apply_evidence_write|"
    r"optional_session|read_snapshot|BEGIN IMMEDIATE|sqlite_master|"
    r"alembic|database_url|session\.execute|session\.commit|session\.flush)\b"
)
WRITE_CALLS = {
    "read_session",
    "immediate_session",
    "apply_snapshot_write",
    "apply_evidence_write",
    "optional_session",
    "begin",
    "begin_nested",
    "commit",
    "flush",
    "rollback",
    "invalidate",
    "execute",
    "executemany",
    "exec_driver_sql",
    "add",
    "add_all",
    "delete",
    "update",
    "insert",
    "transaction",
    "connect",
    "create_async_engine",
    "create_engine",
    "sessions",
}
A = {
    "persistence",
    "identity",
    "conversation",
    "admin",
    "admin_api",
    "automation",
    "character",
    "capabilities",
    "gateway",
    "adapters",
    "acp_adapter",
    "client_api",
    "client_sdk",
    "emoji",
    "workspace",
    "web",
    "deployment_setup",
}
C = {"runtime", "model_runtime", "execution_trace", "social", "plugin_host", "sandbox", "services"}


def domain(path: str) -> str:
    parts = path.split("/")
    if len(parts) > 2 and parts[:2] == ["src", "yuki"]:
        if parts[2] in A:
            return "A"
        if parts[2] == "memory":
            return "B"
        if parts[2] in C:
            return "C"
    return "D"


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func) + "()"
    return "?"


def main() -> None:
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    records: list[dict[str, object]] = []
    for path in sorted(p for p in paths if p):
        if (path.startswith("docs/") and not path.endswith(".py")) or path.endswith(".lock"):
            continue
        file = ROOT / path
        if file.suffix not in {
            ".py",
            ".sql",
            ".sh",
            ".ps1",
            ".toml",
            ".ini",
            ".yaml",
            ".yml",
            ".js",
            ".mjs",
            ".ts",
            ".json",
            ".mako",
        } and file.name not in {".env.example", "Dockerfile", "Makefile"}:
            continue
        source = file.read_text(encoding="utf-8-sig")
        imports: list[str] = []
        calls: list[dict[str, object]] = []
        sql: list[dict[str, object]] = []
        parse_error = None
        if file.suffix == ".py":
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError as error:
                parse_error = str(error)
            else:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imports.append("." * node.level + (node.module or ""))
                    elif isinstance(node, ast.Call):
                        name = dotted(node.func)
                        if name.rsplit(".", 1)[-1] in WRITE_CALLS:
                            calls.append({"line": node.lineno, "name": name})
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        value = node.value.strip()
                        if re.match(
                            r"(?is)^(?:BEGIN|PRAGMA|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE|VACUUM|SELECT|WITH)\b",
                            value,
                        ):
                            sql.append({"line": node.lineno, "head": " ".join(value.split())[:160]})
        tags: list[str] = []
        if any(name.startswith(DB_IMPORTS) for name in imports):
            tags.append("driver_or_orm_import")
        if DB_HINT.search(source):
            tags.append("database_api_or_configuration")
        if sql:
            tags.append("sql_literal_or_template")
        if path.startswith("migrations/") or file.suffix == ".sql":
            tags.append("schema_or_migration")
        if any(
            "repository" in name
            or ".persistence" in name
            or "db_models" in name
            or "_schema" in name
            for name in imports
        ):
            tags.append("repository_or_schema_consumer")
        if re.search(
            r"sqlite|alembic|database_url|DATABASE_URL|init-db|\.backup|wal_checkpoint",
            source,
            re.I,
        ):
            tags.append("storage_literal_or_ops")
        # Keep every source and test module as an exclusion/caller-review
        # universe. Other code needs an explicit static signal.
        if not tags and not path.startswith(("src/", "tests/")) and file.suffix != ".py":
            continue
        records.append(
            {
                "path": path,
                "domain": domain(path),
                "kind": "source"
                if path.startswith("src/")
                else "migration"
                if path.startswith("migrations/")
                else "test"
                if path.startswith("tests/")
                else "historical_artifact"
                if path.startswith("artifacts/")
                else "historical_experiment"
                if path.startswith("docs/")
                else "extension_or_example"
                if path.startswith(("plugins/", "examples/"))
                else "operations",
                "lines": len(source.splitlines()),
                "tags": tags,
                "imports": sorted(set(imports)),
                "calls": sorted(calls, key=lambda item: item["line"]),
                "sql": sorted(sql, key=lambda item: item["line"]),
                "parse_error": parse_error,
            }
        )
    payload = {
        "baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
        .decode()
        .strip(),
        "method": (
            "Tracked text + Python AST; all src modules retained for explicit exclusion/caller "
            "review. Static signals are candidates, not proof of writes or human review."
        ),
        "files": records,
    }
    (OUT / "surface.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "files": len(records),
        "by_kind": dict(Counter(str(row["kind"]) for row in records)),
        "source_by_domain": dict(
            Counter(str(row["domain"]) for row in records if row["kind"] == "source")
        ),
        "tagged_source_by_domain": dict(
            Counter(
                str(row["domain"]) for row in records if row["kind"] == "source" and row["tags"]
            )
        ),
        "parse_errors": [row["path"] for row in records if row["parse_error"]],
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
