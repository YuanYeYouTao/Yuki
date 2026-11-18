# E7 self-started turns under an SDK owner + X2. replay-user-messages on, can_use_tool allows.
cd packages/yuki-agent-connector  # claude-agent-sdk 0.2.172 venv; CLI /opt/homebrew/bin/claude 2.1.311; model haiku
for c in bg_notify bg_stop_then_complete schedule_wakeup x2_one_long_tool; do .venv/bin/python drivers/e7.py $c $X/proj/e7/$c frames_$c.jsonl & done; wait
