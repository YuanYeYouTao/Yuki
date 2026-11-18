# E3 live-session detection. $X = isolated scratch dir. Private tmux server only.
cd packages/yuki-agent-connector  # claude-agent-sdk 0.2.172 venv; CLI /opt/homebrew/bin/claude 2.1.311; model haiku
mkdir -p $X/proj/e3 && cd $X/proj/e3 && git init -q .
python3 drivers/poll_agents.py $X/proj agents_poll.jsonl 600 $T0 &        # polls `claude agents --json` every 0.5s, logs changes (cwd-filtered)
tmux -L p2claude new-session -d -s e3 -c $X/proj/e3 "claude --model haiku"  # trust prompt accepted at t=13.3 (marks.txt)
tmux -L p2claude send-keys -t e3 -l "<running-turn prompt: run sleep 12 via Bash>"; tmux -L p2claude send-keys -t e3 Enter
tmux -L p2claude send-keys -t e3 BTab                                        # cycle auto -> manual mode
tmux -L p2claude send-keys -t e3 -l "<permission prompt: touch a file via Bash>"; tmux -L p2claude send-keys -t e3 Enter
claude agents --json; cat ~/.claude/sessions/<pid>.json                      # waiting row + registry file fields
tmux -L p2claude send-keys -t e3 Enter                                       # "user" answers Yes in terminal
CLAUDE_CONFIG_DIR=$X/cfg2 claude agents --json                             # other profile: []
CLAUDE_CONFIG_DIR=$X/cfg2 claude -p --model haiku "<trivial>"              # -> Not logged in (cannot test live 2nd profile)
python3 drivers/sanitize_transcript.py ~/.claude/projects/<slug>/<session>.jsonl > transcript_structure.jsonl
.venv/bin/python drivers/e3_sdk_wait.py $X/proj/e3sdk                      # SDK session registry while can_use_tool pending
