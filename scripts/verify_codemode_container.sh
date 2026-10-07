#!/bin/sh
# Run only after the reviewed AppArmor profile is loaded. No Bot state is mounted.
set -eu
image=${1:?usage: verify_codemode_container.sh IMAGE}
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker run --rm --init --user 10001:10001 --cap-drop ALL \
  --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --pids-limit 64 --memory 1g \
  --security-opt "seccomp=$repo/deploy/security/yuki-codemode-seccomp.json" \
  --security-opt apparmor=yuki-bot-codemode \
  --entrypoint /app/.venv/bin/python "$image" \
  /app/scripts/verify_monty_isolation.py --output /tmp/monty-isolation.json
