# E4 cross-session messaging. Receiver = terminal session e3-73 (pid 99553) started by the experiment in private tmux.
cd packages/yuki-agent-connector  # claude-agent-sdk 0.2.172 venv; CLI /opt/homebrew/bin/claude 2.1.311; model haiku
.venv/bin/python drivers/e4.py default_accept $X/proj/e4 frames_default.jsonl e3-73 <e3-session-id> 99553   # receiver crossSessionInbound unset
echo '{"crossSessionInbound":"refuse"}' > $X/proj/e3/.claude/settings.json; WATCH=30 .venv/bin/python drivers/e4.py refuse ...
echo '{"crossSessionInbound":"hold"}'   > $X/proj/e3/.claude/settings.json; WATCH=30 .venv/bin/python drivers/e4.py hold ...
# first run had SendMessage auto-allowed (no can_use_tool); later runs add --settings ask:[SendMessage] so the gate only allows the experiment session
python3 drivers/sanitize_transcript.py <receiver transcript> ; tmux -L p2claude capture-pane -p -t e3
