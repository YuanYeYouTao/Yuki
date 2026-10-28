"""Audit completed benchmark databases against their recorded event/receipt IDs."""

import argparse
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--before-databases", type=Path, required=True)
    parser.add_argument("--after-databases", type=Path, required=True)
    args = parser.parse_args()
    audit = []
    for label, folder in (("before", args.before_databases), ("after", args.after_databases)):
        data = json.loads((args.root / f"measured-{label}.json").read_text())
        for trial in data["trials"]:
            index = trial["trial"]
            paths = [
                path
                for path in folder.glob(f"test_real_local_social_cost_{index}_*/test.db")
                if not path.parent.is_symlink()
            ]
            assert len(paths) == 1, paths
            with sqlite3.connect(f"file:{paths[0]}?mode=ro", uri=True) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_events)")}
                assert ("origin_key" in columns) == (label == "after")
                outbound = connection.execute(
                    "SELECT count(*) FROM chat_events WHERE direction='outbound'"
                ).fetchone()[0]
                receipts = connection.execute(
                    "SELECT count(*) FROM social_operation_receipts"
                ).fetchone()[0]
                succeeded = connection.execute(
                    "SELECT count(*) FROM social_operation_receipts WHERE status='succeeded'"
                ).fetchone()[0]
                expected = 1 + sum(
                    phase["warmup_count"] + phase["count"] for phase in trial["phases"]
                )
                assert outbound == receipts == succeeded == expected
                for phase in trial["phases"]:
                    turn_ids = [f"perf-{index}-{sample['request']}" for sample in phase["samples"]]
                    actual = connection.execute(
                        "SELECT source_turn_id,event_id,status FROM social_operation_receipts "
                        "WHERE source_turn_id IN (" + ",".join("?" for _ in turn_ids) + ")",
                        turn_ids,
                    ).fetchall()
                    assert len(actual) == phase["count"]
                    assert {row[0] for row in actual} == set(turn_ids)
                    assert {row[1] for row in actual} == {
                        sample["event_id"] for sample in phase["samples"]
                    }
                    assert all(row[2] == "succeeded" for row in actual)
                assert not connection.execute("PRAGMA foreign_key_check").fetchall()
            audit.append(
                {
                    "revision_label": label,
                    "trial": index,
                    "total_outbound": outbound,
                    "total_receipts": receipts,
                    "succeeded_receipts": succeeded,
                    "expected_including_setup_and_warmups": expected,
                    "measured_events_receipts_one_to_one": True,
                    "foreign_key_check": "passed",
                }
            )
    output = {"all_passed": True, "trials": audit}
    (args.root / "delivery-audit.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output))


if __name__ == "__main__":
    main()
