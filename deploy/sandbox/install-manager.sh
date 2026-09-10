#!/bin/sh
# Run after installing verified runsc binaries, loading locally built images,
# and starting the dedicated egress stack. Never restarts Docker or gateways.
set -eu
test "$(id -u)" = 0
test -f /opt/yuki-sandbox/src/qq_ai_bot/sandbox/manager.py
python3 -c 'import sys; assert sys.version_info >= (3,12)'
getent group 10001 >/dev/null || groupadd --gid 10001 yuki-sandbox
install -d -o 10001 -g 10001 -m 700 /opt/yuki-qqbot/workspace
install -m 644 /opt/yuki-sandbox/deploy/sandbox/yuki-sandbox.service /etc/systemd/system/yuki-sandbox.service
systemctl daemon-reload
systemctl enable --now yuki-sandbox
systemctl is-active yuki-sandbox
