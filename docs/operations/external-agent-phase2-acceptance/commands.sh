#!/usr/bin/env bash
# Phase-2 external-agent delegation acceptance (taskbook §10.3 A1, A2, A4–A8) with REAL
# native agents. Paid model calls (Claude haiku, Codex gpt-6-luna low). macOS, 2026-11-18.
# Versions: Claude Code 2.1.311 + claude-agent-sdk 0.2.172 (connector pin); codex-cli 0.169.0
# (own app-server); claude-agent-acp 0.108.0 (ACP, existing Claude login, haiku).
# Never point anything here at the user's managed Codex daemon (~/.codex/app-server-control),
# the ChatGPT app, or the user's own Claude/Codex sessions. "User's terminal" = private tmux.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# Scratch root (agents' projects, the private app-server socket, npm install, Bot data dirs).
export YUKI_ACCEPTANCE_SCRATCH=${YUKI_ACCEPTANCE_SCRATCH:?scratch dir}
X=$YUKI_ACCEPTANCE_SCRATCH
mkdir -p "$X/ws" "$X/acp"

# 1. Our own Codex app-server in a private tmux server (-L p2acc). The socket path becomes a
#    symlink to /private/tmp/codex-daemon-<uid>/<hash>; the harness resolves it.
tmux -L p2acc new-session -d -s srv -x 200 -y 50 -c "$X/ws" \
  "codex app-server --listen unix://$X/codex.sock -c model=\"gpt-6-luna\" -c model_reasoning_effort=\"low\""

# 2. The ACP agent, installed into scratch only (never global).
npm install --prefix "$X/acp" @agentclientprotocol/claude-agent-acp@0.108.0

# 3. All scenarios (each writes test-results/external-agent-phase2-acceptance/<scenario>.json;
#    the session writes summary.json). Without YUKI_REAL_AGENT_E2E=1 every test is skipped;
#    CI never collects tests/acceptance (it runs tests/unit and tests/integration).
YUKI_REAL_AGENT_E2E=1 uv run --no-sync --with claude-agent-sdk==0.2.172 \
  pytest tests/acceptance -q -s -p no:cacheprovider
# One scenario:  ... pytest tests/acceptance/test_a4_codex_realtime_sharing.py -q -s
# Marker:        real_agent (registered in pyproject.toml)

# 4. Sanitized copies for docs (scratch/home paths replaced, no native text).
uv run --no-sync python -m tests.acceptance.sanitize   # needs YUKI_ACCEPTANCE_SCRATCH

# 5. Cleanup (only what this run created).
#    - stop the private tmux server:            tmux -L p2acc kill-server
#    - delete our Codex threads:                thread/delete via the private app-server for
#                                               threads whose cwd is under $X (see README)
#    - purge our Claude scratch projects:       claude purge <each $X/proj/* project>
#    - drop trust entries for $X/proj/* from ~/.claude.json projects and ~/.codex/config.toml
#    - rm -rf "$X/acp/node_modules"
