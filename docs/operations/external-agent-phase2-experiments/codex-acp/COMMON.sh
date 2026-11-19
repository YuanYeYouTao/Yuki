#!/usr/bin/env bash
# Shared setup for the Codex/ACP experiments (2026-11-18, macOS arm64).
# Versions: codex-cli 0.169.0 (app-server from the same binary), model gpt-6-luna effort low;
#           claude-agent-acp 0.108.0 (@agentclientprotocol, bundles claude-agent-sdk 0.3.326) on the
#           existing Claude login, ANTHROPIC_MODEL=haiku; codex-acp 2.7.0 (@agentclientprotocol);
#           Gemini CLI 0.68.0 (capabilities only, no credentials).
# $X = a scratch directory; scripts are in ../scripts and are run from $X with these files copied in.
# Never point any of this at the user's managed Codex daemon (~/.codex/app-server-control).
set -euo pipefail
X=${X:?scratch dir}
cd "$X"; mkdir -p ws acpws
codex app-server generate-json-schema --experimental --out "$X/schema-exp"
tmux -L p2codex new-session -d -s srv -c "$X/ws" "codex app-server --listen unix://$X/codex.sock"
# codex.sock becomes a symlink to /private/tmp/codex-daemon-<uid>/<hash>; clients connect to the
# resolved path (AF_UNIX length limit). Python clients: uv run --no-project --with websockets python <script>
npm install --prefix "$X/acp" @google/gemini-cli@0.68.0 @agentclientprotocol/claude-agent-acp@0.108.0 @agentclientprotocol/codex-acp@2.7.0
