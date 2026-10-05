#!/usr/bin/env bash
# Build the pinned Monty native worker and Python binding for Code Mode.
#
#   scripts/build_monty_worker.sh [WORK_DIR]
#
# Produces WORK_DIR/monty/target/release/monty and a pydantic-monty-client wheel
# in WORK_DIR/wheels, installs the wheel into the project venv, and prints the
# sha256 digests to configure CODE_MODE_WORKER_PATH / CODE_MODE_WORKER_SHA256.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK_DIR=${1:-"$ROOT/.cache/monty-build"}
bash "$ROOT/scripts/build_monty_distribution.sh" "$WORK_DIR" "$ROOT/.venv/bin/python"

WHEEL=$(ls -t "$WORK_DIR"/wheels/pydantic_monty_client-1.0.1-*.whl | head -1)
uv pip install --python "$ROOT/.venv/bin/python" --no-deps --reinstall "$WHEEL"

echo "worker: $WORK_DIR/monty/target/release/monty"
shasum -a 256 "$WORK_DIR/monty/target/release/monty" "$WHEEL"
