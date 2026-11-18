# E8 release and hand-back (Claude). cwd = trusted scratch project $X/proj/e3.
cd packages/yuki-agent-connector  # claude-agent-sdk 0.2.172 venv; CLI /opt/homebrew/bin/claude 2.1.311; model haiku
.venv/bin/python drivers/e8.py new $X/proj/e3 frames_a_sdk_new.jsonl            # SDK owner: one turn, then client closes at the turn boundary (process exits)
tmux -L p2claude new-session -d -s e8a -c $X/proj/e3 "claude --model haiku --resume <SID>"   # terminal resumes; sees SDK turn; does one turn
python3 drivers/chain.py ~/.claude/projects/<slug>/<SID>.jsonl > a_chain.json     # linear, 0 branch points
.venv/bin/python drivers/e8.py resume $X/proj/e3 frames_b_sdk_resume_live.jsonl <SID>   # negative control while terminal live (G8)
tmux -L p2claude send-keys -t e8a -l "<trivial>"; tmux -L p2claude send-keys -t e8a Enter
python3 drivers/chain.py ... > b_chain.json                                       # 1 branch point: [sdk-py, cli]
claude agents --json  # live_registry_row_for_target.json: the live terminal row carries sessionId == <SID>
.venv/bin/python drivers/e8.py fork $X/proj/e3 frames_c_sdk_fork.jsonl <SID>     # fork_session=True -> new id; original sha unchanged
