"""Index every tracked migration and runtime table/trigger definition for review."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return name(node.value) + "." + node.attr
    return "?"


def main() -> None:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    migrations = []
    definitions = []
    for path in sorted(p for p in paths if p.endswith(".py")):
        if not path.startswith(("src/", "migrations/")):
            continue
        source = (ROOT / path).read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        if path.startswith("migrations/"):
            revisions = {}
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in {
                            "revision",
                            "down_revision",
                        }:
                            revisions[target.id] = ast.literal_eval(node.value)
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id in {"revision", "down_revision"}
                    and node.value is not None
                ):
                    revisions[node.target.id] = ast.literal_eval(node.value)
            migrations.append(
                {
                    "path": path,
                    "lines": len(source.splitlines()),
                    **revisions,
                    "purpose": ast.get_docstring(tree),
                    "imports": [
                        ast.unparse(node)
                        for node in tree.body
                        if isinstance(node, (ast.Import, ast.ImportFrom))
                    ],
                    "functions": [
                        {
                            "name": node.name,
                            "line": node.lineno,
                            "end": node.end_lineno,
                            "calls": sorted(
                                set(
                                    name(call.func)
                                    for call in ast.walk(node)
                                    if isinstance(call, ast.Call)
                                )
                            ),
                        }
                        for node in tree.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ],
                    "sql": [
                        {"line": node.lineno, "sql": node.value}
                        for node in ast.walk(tree)
                        if isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and node.value.lstrip()
                        .upper()
                        .startswith(
                            (
                                "CREATE",
                                "DROP",
                                "ALTER",
                                "INSERT",
                                "UPDATE",
                                "DELETE",
                                "PRAGMA",
                                "WITH",
                                "SELECT",
                            )
                        )
                    ],
                }
            )
        elif path.startswith("src/"):
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, ast.Assign) and any(
                            isinstance(target, ast.Name) and target.id == "__tablename__"
                            for target in child.targets
                        ):
                            definitions.append(
                                {
                                    "path": path,
                                    "line": child.lineno,
                                    "kind": "ORM table",
                                    "name": ast.literal_eval(child.value),
                                }
                            )
                elif (
                    isinstance(node, ast.Call)
                    and name(node.func).rsplit(".", 1)[-1] == "Table"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    definitions.append(
                        {
                            "path": path,
                            "line": node.lineno,
                            "kind": "Core table",
                            "name": node.args[0].value,
                        }
                    )
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.lstrip().upper().startswith("CREATE")
                    and any(
                        word in node.value.upper() for word in ("TRIGGER", "VIRTUAL TABLE", "INDEX")
                    )
                ):
                    definitions.append(
                        {
                            "path": path,
                            "line": node.lineno,
                            "kind": "SQL schema/trigger",
                            "sql": node.value,
                        }
                    )
    (OUT / "schema.json").write_text(
        json.dumps(
            {
                "baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
                .decode()
                .strip(),
                "migrations": migrations,
                "definitions": definitions,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for row in migrations:
        purpose = " ".join((row["purpose"] or "").split())[:130]
        functions = ",".join(item["name"] for item in row["functions"])
        print(
            f"{row['path']} | {row.get('revision')} <- {row.get('down_revision')} "
            f"| {purpose} | {functions}"
        )
    print(
        json.dumps(
            {
                "migrations": len(migrations),
                "definitions": len(definitions),
                "migration_lines": sum(row["lines"] for row in migrations),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
