#!/usr/bin/env bash
# Build the fixed native worker, matching CPython binding and dependency notices.
# No Bot installation/startup or image publication is performed.
# Usage: build_monty_distribution.sh OUTPUT_DIR ABSOLUTE_PYTHON [all|build|notices]
set -euo pipefail
MONTY_SHA=3f9d6ef413fb951e5b80113b7088d535bd028fcb
RUST_TOOLCHAIN=1.96.0
MATURIN_VERSION=1.9.6
ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD_DIR=$1
PYTHON_BIN=$2
MODE=${3:-all}
case "$MODE" in all|build|notices) ;; *) echo "invalid build mode: $MODE" >&2; exit 2 ;; esac
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTUP_TOOLCHAIN=$RUST_TOOLCHAIN
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Monty distribution requires CPython 3.12"'
mkdir -p "$BUILD_DIR"
if [ ! -d "$BUILD_DIR/monty/.git" ]; then
  if [ "$MODE" = notices ]; then
    echo "notices require an existing pinned build" >&2
    exit 2
  fi
  git init "$BUILD_DIR/monty"
  git -C "$BUILD_DIR/monty" remote add origin https://github.com/pydantic/monty.git
fi
cd "$BUILD_DIR/monty"
# Reused build directories may contain later user edits. Only the exact patch
# produced by this builder can be restored; all other work is left untouched.
SOURCE_STATE=$("$PYTHON_BIN" - "$MONTY_SHA" <<'PY'
import pathlib, subprocess, sys
def git(*args):
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout
# P04's separate Cargo output is retained, never checked out or removed.
if git("ls-files", "--others", "--exclude-standard", "--exclude=target-worker/").strip():
    raise SystemExit("Monty build directory contains untracked user work")
if git("diff", "--cached", "--name-only").strip():
    raise SystemExit("Monty build directory contains staged user work")
modified = git("diff", "--name-only").decode().splitlines()
owned = "crates/monty/src/modules/json/string_cache.rs"
if modified:
    if modified != [owned] or git("rev-parse", "HEAD").decode().strip() != sys.argv[1]:
        raise SystemExit("Monty build directory contains unrelated modifications")
    original = git("show", "HEAD:" + owned)
    before = b"for entry in &mut inner.entries {"
    after = b"for entry in inner.entries.iter_mut() {"
    if original.count(before) != 1 or pathlib.Path(owned).read_bytes() != original.replace(before, after):
        raise SystemExit("Monty build patch has later modifications; refusing to reset")
print("patched" if modified else "clean")
PY
)
if [ "$MODE" != notices ]; then
  git fetch --quiet --depth=1 origin "$MONTY_SHA"
  if [ "$SOURCE_STATE" != patched ]; then
    git checkout --quiet --detach "$MONTY_SHA"
    git apply "$ROOT/vendor/patches/monty-3f9d6ef-string-cache-iterator.patch"
  fi
# Reusing the exact owned patch also preserves source mtimes and Cargo's cache.
# It never overwrites later modifications or bypasses the preflight above.
cargo build --release --locked -p monty-runtime --no-default-features
uvx --from "maturin==$MATURIN_VERSION" maturin build --release --locked \
  -m crates/monty-python/Cargo.toml -i "$PYTHON_BIN" -o "$BUILD_DIR/wheels"
WHEEL=$(ls -t "$BUILD_DIR"/wheels/pydantic_monty_client-1.0.1-*.whl | head -1)
if [ "$(uname -s)" = Linux ]; then
  cc -O2 -Wall -Wextra -Werror -o "$BUILD_DIR/monty-isolated" "$ROOT/vendor/monty/launcher.c"
fi
"$PYTHON_BIN" - "$BUILD_DIR" "$WHEEL" <<'PY'
import hashlib, json, pathlib, sys
root, wheel = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
files = {"worker": root / "monty/target/release/monty", "binding": wheel}
if (root / "monty-isolated").is_file():
    files["launcher"] = root / "monty-isolated"
report = {name: {"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
          for name, path in files.items()}
(root / "artifacts.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
PY
fi

if [ "$MODE" != build ]; then
  # Auditing is a separate Docker layer so a transient public license download
  # failure cannot discard the successful native compilation. Verify the exact
  # built artifacts before associating dependency notices with them.
  if [ "$SOURCE_STATE" != patched ] && [ "$MODE" = notices ]; then
    echo "notices require the exact build patch" >&2
    exit 2
  fi
  WHEEL=$("$PYTHON_BIN" - "$BUILD_DIR" "$MONTY_SHA" <<'PY'
import hashlib, json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
if head != sys.argv[2]:
    raise SystemExit("notices require the pinned Monty source revision")
report = json.loads((root / "artifacts.json").read_text())
wheel = root / "wheels" / report["binding"]["filename"]
if wheel.name != report["binding"]["filename"]:
    raise SystemExit("invalid recorded wheel filename")
files = {"worker": root / "monty/target/release/monty", "binding": wheel}
if "launcher" in report:
    files["launcher"] = root / "monty-isolated"
for name, path in files.items():
    if hashlib.sha256(path.read_bytes()).hexdigest() != report[name]["sha256"]:
        raise SystemExit("build artifact changed before notices audit: " + name)
print(wheel)
PY
)
  cargo fetch --locked
  TARGET=$(rustc -vV | sed -n 's/^host: //p')
  "$PYTHON_BIN" "$ROOT/scripts/export_monty_notices.py" "$BUILD_DIR/monty" \
    "$BUILD_DIR/THIRD_PARTY_NOTICES.json" --cargo-home "${CARGO_HOME:-$HOME/.cargo}" \
    --target "$TARGET" --worker "$BUILD_DIR/monty/target/release/monty" --wheel "$WHEEL"
fi
