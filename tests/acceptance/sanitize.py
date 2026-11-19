"""Copy acceptance artifacts into docs with local paths replaced and no native text.

``python -m tests.acceptance.sanitize`` reads ``test-results/external-agent-phase2-
acceptance/*.json`` and writes ``docs/operations/external-agent-phase2-acceptance/``.
Artifacts already carry only IDs, states, kinds and timings; this pass additionally
replaces the scratch root and home directory with placeholders and reduces any
structured native tool object (ACP toolCall) to its kind.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "test-results" / "external-agent-phase2-acceptance"
SCRATCH = Path(os.environ["YUKI_ACCEPTANCE_SCRATCH"])

TARGET = REPO / "docs" / "operations" / "external-agent-phase2-acceptance"
_DROP = {"rawInput", "content", "title", "native_request", "text", "message"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        if "toolCallId" in value:  # an ACP toolCall: keep its kind only
            return {"kind": value.get("kind")}
        return {key: _clean(item) for key, item in value.items() if key not in _DROP}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return value.replace(str(SCRATCH), "<scratch>").replace(str(Path.home()), "<home>")
    return value


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for path in sorted(RESULTS.glob("*.json")):
        value = _clean(json.loads(path.read_text()))
        (TARGET / path.name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        print(os.path.relpath(TARGET / path.name, REPO))


if __name__ == "__main__":
    main()
