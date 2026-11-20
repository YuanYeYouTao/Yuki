"""Compare normalized provider wire digests of two request-kind E2E runs.

Usage: python tests/support/compare_wire_digests.py BEFORE_DIR AFTER_DIR [OUT.json]

Each directory holds the ``latest.json`` evidence of every scenario (as
copied from test-results/model-invocation-request-kind/<scenario>/). Equal
digests in sending order mean the same request content reached the provider.
"""

import json
import sys
from pathlib import Path


def scenarios(root):
    result = {}
    for path in sorted(Path(root).glob("*/latest.json")):
        evidence = json.loads(path.read_text("utf-8"))
        result[evidence["nodeid"]] = evidence
    return result


def main(before_dir, after_dir, out=None):
    before, after = scenarios(before_dir), scenarios(after_dir)
    report = {"before": str(before_dir), "after": str(after_dir), "scenarios": {}}
    equal = bool(before) and set(before) == set(after)
    for nodeid in sorted(set(before) | set(after)):
        left = [item["body_digest"] for item in before.get(nodeid, {}).get("wire", [])]
        right = [item["body_digest"] for item in after.get(nodeid, {}).get("wire", [])]
        same = bool(left) and left == right
        equal &= same
        report["scenarios"][nodeid] = {
            "before_revision": before.get(nodeid, {}).get("source_revision"),
            "before_patch_sha256": before.get(nodeid, {}).get("source_patch_sha256"),
            "after_revision": after.get(nodeid, {}).get("source_revision"),
            "after_patch_sha256": after.get(nodeid, {}).get("source_patch_sha256"),
            "requests": [len(left), len(right)],
            "identical": same,
            "first_difference": next(
                (
                    index
                    for index, pair in enumerate(zip(left, right, strict=False))
                    if pair[0] != pair[1]
                ),
                None,
            ),
        }
    report["identical"] = equal
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if out:
        Path(out).write_text(text, "utf-8")
    sys.stdout.write(text)
    return 0 if equal else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
