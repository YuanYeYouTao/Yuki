#!/usr/bin/env bash
# E6 Codex part. Permissive user config is SIMULATED on a separate app-server (real config untouched):
tmux -L p2codex new-session -d -s srv3 -c "$X/ws" \
  "codex app-server --listen unix://$X/codex3.sock -c approval_policy=\"never\" -c sandbox_mode=\"danger-full-access\""
uv run --no-project --with websockets python e6codex.py  # per-turn policy vs permissive config; stickiness
# run1 file: prompt without "request escalation" -> model did not attempt the command (no evidence either way)
mkdir -p ~/p2codex-exp-root   # extra writable root, removed afterwards
uv run --no-project --with websockets python e6b.py      # writableRoots effect; Yuki scope leaking into a TUI turn (srv1)
rm -rf ~/p2codex-exp-root
# E6 ACP part: see E5 (frames-perm-mode-from-user-settings, frames-codex-acp-mode-*).
