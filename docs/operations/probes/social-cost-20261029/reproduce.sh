#!/bin/sh
# Usage: sh reproduce.sh BEFORE_SNAPSHOT AFTER_SNAPSHOT PYTHON [RESULT_PARENT]
set -eu
before_snapshot=$1
after_snapshot=$2
bench_python=$3
result_parent=${4:-/tmp}
probe_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
result_root=$(mktemp -d "$result_parent/yuki-social-cost-replay.XXXXXX")
cp "$probe_directory/probe.py" "$before_snapshot/tests/unit/_social_cost_probe.py"
cp "$probe_directory/probe.py" "$after_snapshot/tests/unit/_social_cost_probe.py"
cd "$before_snapshot"
PYTHONPATH=src PYTEST_ADDOPTS="--basetemp=$result_root/pytest-before" \
  SOCIAL_COST_COUNT=64 SOCIAL_COST_TRIALS=3 SOCIAL_COST_REVISION=afb509e7 \
  SOCIAL_COST_CAPTURE="$result_root/seed-before.db" \
  SOCIAL_COST_OUTPUT="$result_root/measured-before.json" \
  "$bench_python" "$probe_directory/runner.py"
cd "$after_snapshot"
PYTHONPATH=src SOUL_FILE=config/SOUL.md \
  DATABASE_URL="sqlite+aiosqlite:///$result_root/seed-after.db" \
  "$bench_python" "$probe_directory/prepare_seed.py" \
  "$result_root/seed-before.db" "$result_root/seed-after.db"
PYTHONPATH=src PYTEST_ADDOPTS="--basetemp=$result_root/pytest-after" \
  SOCIAL_COST_COUNT=64 SOCIAL_COST_TRIALS=3 SOCIAL_COST_REVISION=27c71ee2 \
  SOCIAL_COST_TEMPLATE="$result_root/seed-after.db" \
  SOCIAL_COST_OUTPUT="$result_root/measured-after.json" \
  "$bench_python" "$probe_directory/runner.py"
"$bench_python" "$probe_directory/audit.py" --root "$result_root" \
  --before-databases "$result_root/pytest-before" \
  --after-databases "$result_root/pytest-after"
printf '%s\n' "$result_root"
