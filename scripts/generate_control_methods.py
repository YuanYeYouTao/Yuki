"""Generate the frontend method names from the authoritative Control descriptors."""

import json
from pathlib import Path

from qq_ai_bot.control_plane.surface import _METHODS


def render() -> str:
    lines = ["// Generated from control_plane/surface.py; do not add methods here."]
    for kind in ("query", "command"):
        names = [method.name for method in _METHODS if method.kind == kind]
        lines.append(f"export const {kind}Methods = [")
        lines.extend(f"  {json.dumps(name)}," for name in names)
        lines.append("] as const;")
    lines.extend(
        (
            "export type QueryMethod = (typeof queryMethods)[number];",
            "export type CommandMethod = (typeof commandMethods)[number];",
        )
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    Path(__file__).resolve().parents[1].joinpath("frontend/src/control-methods.ts").write_text(
        render(), encoding="utf-8"
    )
