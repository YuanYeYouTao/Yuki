#!/usr/bin/env bash
# E8 Codex part: user TUI exits; script takes over on the same app-server, runs a turn, releases
# (thread/unsubscribe + disconnect); independent writer probe; TUI resumes via --remote and is checked.
TUI_THREAD=<tui-thread-id> uv run --no-project --with websockets python e8codex.py
uv run --no-project --with websockets python lt2.py      # loaded state long after release
uv run --no-project --with websockets python e8unload.py # time from last unsubscribe to thread/closed
# E8 ACP part: E5/frames-load-mode-default.jsonl
