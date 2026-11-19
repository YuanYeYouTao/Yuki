#!/usr/bin/env bash
# E2 Codex realtime share + E1(c). Prereq: COMMON.sh.
R=$(python3 -c "import os;print(os.path.realpath('$X/codex.sock'))")
tmux -L p2codex new-session -d -s tui -x 200 -y 50 -c "$X/ws" \
  "codex --remote unix://$R -m gpt-6-luna -c model_reasoning_effort=\"low\" -a on-request -s workspace-write"
# (TUI rejects -a untrusted and config approval_policy="untrusted" in 0.169; app-server thread/start accepts it.)
# Type one prompt in the TUI first so the thread gets a rollout, then pass its (non-ephemeral) loaded thread id:
TUI_THREAD=<tui-thread-id> uv run --no-project --with websockets python e2.py
# --no-daemon TUI on the same thread (shows "This conversation is open in another app ... r retry / f fork"):
tmux -L p2codex new-session -d -s nd -c "$X/ws" "codex resume <tui-thread-id> --no-daemon -m gpt-6-luna"
# Second independent app-server resuming the same thread:
tmux -L p2codex new-session -d -s srv2 -c "$X/ws" "codex app-server --listen unix://$X/codex2.sock"
TUI_THREAD=<tui-thread-id> uv run --no-project --with websockets python e2conflict.py
