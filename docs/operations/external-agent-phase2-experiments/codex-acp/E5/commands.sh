#!/usr/bin/env bash
# E5 ACP. Prereq: COMMON.sh npm installs in $X/acp. Gemini runs with HOME=$X/acp/ghome (no credentials).
uv run --no-project python e5init.py                     # capabilities: gemini 0.68.0 + claude-agent-acp 0.108.0
uv run --no-project python e5.py perm                    # session mode inherited from user settings (was "auto")
P2_MODE=default uv run --no-project python e5.py perm    # unanswered request_permission 120 s, then session/cancel
P2_MODE=default uv run --no-project python e5.py fs      # client rejects fs/write_text_file (agent never calls it)
P2_MODE=default uv run --no-project python e5.py term    # client rejects terminal/* (agent never calls it)
P2_MODE=default uv run --no-project python e5.py bg      # session/update with no prompt in flight
P2_MODE=default uv run --no-project python e5.py bg2     # request_permission with no prompt in flight
P2_MODE=default uv run --no-project python e5.py load    # live-elsewhere load, resume vs load after exit (also E8-ACP)
uv run --no-project python e5codex.py                    # codex-acp 2.7.0 default mode "agent"
P2_MODE=workspace-write uv run --no-project python e5codex.py
# frames-codex-acp-0.16.0-*: @zed-industries/codex-acp 0.16.0 failed (model unsupported / needs newer Codex).
