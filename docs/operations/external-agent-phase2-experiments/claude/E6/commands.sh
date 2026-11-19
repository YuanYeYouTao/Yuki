# E6 Claude scope precedence. Project .claude/settings.local.json stands in for the user's own allow rules:
#   allow: Bash(touch:*), Write(//<outside>/**), Edit(//<outside>/**). can_use_tool always denies (records whether it was consulted).
cd packages/yuki-agent-connector  # claude-agent-sdk 0.2.172 venv; CLI /opt/homebrew/bin/claude 2.1.311; model haiku
for v in A_connector_today B_scope_all_sources C_scope_no_sources D_scope_disallow E_scope_settings_deny D2_scope_disallow_edit E2_scope_settings_ask_edit G_settings_ask_all; do
  .venv/bin/python drivers/e6.py $X/proj/e6 $v frames_${v%%_*}.jsonl > result_${v%%_*}.json
done
