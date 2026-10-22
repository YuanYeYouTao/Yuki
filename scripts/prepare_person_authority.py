"""Export legacy QQ administrator candidates without opening Yuki or changing the database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


def _accounts(value: str) -> tuple[str, ...]:
    value = value.strip()
    if not value or value.casefold().startswith("replace-with-"):
        return ()
    raw = json.loads(value) if value.startswith("[") else value.split(",")
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError("SUPERUSERS must contain a CSV or JSON array of account strings")
    return tuple(dict.fromkeys(account.strip() for account in raw if account.strip()))


def export_candidates(database: Path, accounts: tuple[str, ...]) -> dict[str, Any]:
    """Include transferred/disabled bindings and both historical and current root facts."""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(persons)")}
        # 0119–0128 have permanent Person IDs but no names or merge relation yet.
        name_column = "display_name" if "display_name" in columns else "'' AS display_name"
        merge_column = (
            "merged_into_person_id"
            if "merged_into_person_id" in columns
            else "NULL AS merged_into_person_id"
        )
        people = {
            row["id"]: dict(row)
            for row in connection.execute(
                f"SELECT id, {name_column}, enabled, {merge_column} FROM persons"
            )
        }
        candidates = []
        for account in accounts:
            bindings = []
            for row in connection.execute(
                "SELECT id, person_id, platform, external_account_id, status "
                "FROM identity_bindings WHERE platform = 'qq' AND external_account_id = ? "
                "ORDER BY created_at, id",
                (account,),
            ):
                person = people.get(row["person_id"])
                root = person
                chain = []
                while root is not None:
                    chain.append(root["id"])
                    parent = root["merged_into_person_id"]
                    if parent is None:
                        break
                    if parent in chain:
                        raise ValueError("Person merge chain contains a cycle")
                    root = people.get(parent)
                bindings.append(
                    {
                        **dict(row),
                        "person": person,
                        "current_root": root,
                        "merge_chain": chain,
                    }
                )
            candidates.append({"legacy_qq_account": account, "bindings": bindings})
    return {
        "schema_version": 1,
        "database": str(database.resolve()),
        "qualification_granted": False,
        "next_steps": [
            "Keep this candidate list before removing SUPERUSERS from .env "
            "and actual environments.",
            "Start the upgraded Bot and authenticate with the existing operator policy.",
            "Review Person IDs, current roots and all historical bindings; grant individually.",
            "Use person create for an administrator who has no Person; accounts are optional.",
        ],
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-root", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path, help="SQLite file; defaults to .env DATABASE_URL")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.deployment_root.resolve()
    environment = dotenv_values(root / ".env")
    accounts = _accounts(environment.get("SUPERUSERS") or os.environ.get("SUPERUSERS", ""))
    database = args.database
    if database is None:
        url = environment.get("DATABASE_URL") or "sqlite+aiosqlite:///./data/yuki.db"
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            raise ValueError("Pass --database with the existing SQLite file")
        database = Path(url.removeprefix(prefix))
    if not database.is_absolute():
        database = root / database
    result = export_candidates(database, accounts)
    # Exclusive creation preserves a previous reviewed candidate list.
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(
        f"Saved {len(accounts)} legacy account candidates to {args.output.resolve()}; "
        "no grants made."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
