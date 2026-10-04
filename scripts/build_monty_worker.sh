#!/usr/bin/env bash
# Build the pinned Monty native worker and Python binding for Code Mode.
#
#   scripts/build_monty_worker.sh [WORK_DIR]
#
# Produces WORK_DIR/monty/target/release/monty and a pydantic-monty-client wheel
# in WORK_DIR/wheels, installs the wheel into the project venv, and prints the
# sha256 digests to configure CODE_MODE_WORKER_PATH / CODE_MODE_WORKER_SHA256.
set -euo pipefail

MONTY_SHA=3f9d6ef413fb951e5b80113b7088d535bd028fcb
RUST_TOOLCHAIN=1.96.0
MATURIN_VERSION=1.9.6
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK_DIR=${1:-"$ROOT/.cache/monty-build"}
PATCH="$ROOT/vendor/patches/monty-3f9d6ef-string-cache-iterator.patch"
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTUP_TOOLCHAIN=$RUST_TOOLCHAIN

mkdir -p "$WORK_DIR"
if [ ! -d "$WORK_DIR/monty/.git" ]; then
  git clone --filter=blob:none https://github.com/pydantic/monty.git "$WORK_DIR/monty"
fi
cd "$WORK_DIR/monty"
git fetch --quiet origin "$MONTY_SHA"
git checkout --quiet --detach "$MONTY_SHA"
# Reset only the patched file so a re-run applies the patch exactly once.
git checkout --quiet -- crates/monty/src/modules/json/string_cache.rs
git apply "$PATCH"

# Worker only: the default `standalone` feature adds the interactive CLI stack.
cargo build --release --locked -p monty-runtime --no-default-features
uvx --from "maturin==$MATURIN_VERSION" maturin build --release --locked \
  -m crates/monty-python/Cargo.toml -i "$ROOT/.venv/bin/python" -o "$WORK_DIR/wheels"

WHEEL=$(ls -t "$WORK_DIR"/wheels/pydantic_monty_client-1.0.1-*.whl | head -1)
uv pip install --python "$ROOT/.venv/bin/python" --no-deps --reinstall "$WHEEL"

echo "worker: $WORK_DIR/monty/target/release/monty"
shasum -a 256 "$WORK_DIR/monty/target/release/monty" "$WHEEL"
