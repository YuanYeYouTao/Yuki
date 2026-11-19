#!/usr/bin/env bash
# E1 Codex unowned turns and approvals. Prereq: COMMON.sh (srv on $X/codex.sock).
# Threads use approvalPolicy "untrusted" + sandbox workspace-write so `touch` deterministically asks.
uv run --no-project --with websockets python e1.py a    # approval pending, client A disconnects, B resumes later
uv run --no-project --with websockets python e1.py a2   # A disconnects before the approval is emitted
uv run --no-project --with websockets python e1.py b    # queue/add on an idle thread, unsubscribe+disconnect
uv run --no-project --with websockets python e1.py b2   # queue/add behind a running turn, disconnect before it starts
uv run --no-project --with websockets python e1.py d    # subscribed client stays silent 150 s; late joiner B
# E1(c) (two clients incl. TUI on one thread) is part of E2/frames.jsonl.
