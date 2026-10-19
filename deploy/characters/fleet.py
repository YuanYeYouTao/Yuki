"""Run the operator's existing release/backup procedure in each original Compose context."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from collaboration import instance


def run(
    registry: Path,
    operation: str,
    procedure: Path,
    arguments: list[str],
    *,
    slugs: list[str] | None = None,
) -> list[dict]:
    if not procedure.is_absolute() or not procedure.is_file():
        raise ValueError("select the existing absolute per-instance operator procedure")
    chosen = (
        slugs if slugs else sorted(path.parent.name for path in registry.glob("*/instance.json"))
    )
    if not chosen:
        raise ValueError("no registered instances selected")
    results = []
    for slug in chosen:
        metadata = instance(registry, slug)
        environment = {
            **os.environ,
            "YUKI_INSTANCE_REGISTRY_ENTRY": str(registry / slug),
            "YUKI_INSTANCE_OPERATION": operation,
            "YUKI_INSTANCE_SLUG": slug,
            "YUKI_INSTANCE_METADATA": json.dumps(metadata),
            "YUKI_INSTANCE_COMPOSE_ARGV": json.dumps(
                [
                    "docker",
                    "compose",
                    "--project-name",
                    metadata["compose_project"],
                    *[part for path in metadata["compose_files"] for part in ("--file", path)],
                    "--env-file",
                    metadata["env_file"],
                ]
            ),
            "COMPOSE_PROJECT_NAME": metadata["compose_project"],
            "COMPOSE_FILE": os.pathsep.join(metadata["compose_files"]),
            "COMPOSE_ENV_FILES": metadata["env_file"],
        }
        result = subprocess.run(
            [str(procedure), *arguments], cwd=metadata["project_root"], env=environment, check=False
        )
        results.append({"slug": slug, "exit_code": result.returncode})
        print(json.dumps(results[-1]), flush=True)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, [str(procedure), *arguments])
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("upgrade", "backup"))
    parser.add_argument("--registry", type=Path, default=Path("/srv/yuki-characters"))
    parser.add_argument("--instance", action="append")
    parser.add_argument("--procedure", type=Path, required=True)
    command = sys.argv[1:]
    divider = command.index("--") if "--" in command else len(command)
    args = parser.parse_args(command[:divider])
    arguments = command[divider + 1 :]
    run(args.registry, args.operation, args.procedure, arguments, slugs=args.instance)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as failure:
        raise SystemExit(
            f"Stopped after original procedure failed (exit {failure.returncode}); "
            "later instances were not changed."
        ) from None
    except (ValueError, OSError, KeyError) as failure:
        raise SystemExit(str(failure)) from None
