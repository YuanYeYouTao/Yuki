#!/bin/sh
# Run after installing verified runsc binaries, loading locally built images,
# and starting the dedicated egress stack. Never restarts Docker or gateways.
set -eu
deployment_root=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --deployment-root)
            test "$#" -ge 2 || { echo '--deployment-root requires a path' >&2; exit 1; }
            deployment_root=$2
            shift 2
            ;;
        *) echo 'Usage: install-manager.sh [--deployment-root ABSOLUTE_PATH]' >&2; exit 1 ;;
    esac
done
test "$(id -u)" = 0
test -f /opt/yuki-sandbox/src/qq_ai_bot/sandbox/manager.py
python3 -c 'import sys; assert sys.version_info >= (3,12)'
test -x /opt/yuki-sandbox/venv/bin/python
/opt/yuki-sandbox/venv/bin/python -c 'import websockets; assert websockets.__version__ == "15.0.1"'
helper=/opt/yuki-sandbox/deploy/sandbox/configure-manager.py
configured=$(python3 "$helper" --check)
if [ -n "$deployment_root" ]; then
    workspace=$(python3 "$helper" --check --deployment-root "$deployment_root")
    if [ "$workspace" != "$configured" ] && systemctl is-active --quiet yuki-sandbox; then
        echo 'An active Manager uses another artifact store; stop and plan the migration explicitly.' >&2
        exit 1
    fi
else
    workspace=$configured
fi
test ! -L "$workspace"
getent group 10001 >/dev/null || groupadd --gid 10001 yuki-sandbox
install -d -o 10001 -g 10001 -m 700 "$workspace"
if [ -n "$deployment_root" ]; then
    python3 "$helper" --deployment-root "$deployment_root" >/dev/null
fi
install -m 644 /opt/yuki-sandbox/deploy/sandbox/yuki-sandbox.service /etc/systemd/system/yuki-sandbox.service
install -m 644 /opt/yuki-sandbox/deploy/sandbox/yuki-environment-storage.service /etc/systemd/system/yuki-environment-storage.service
systemctl daemon-reload
systemctl enable --now yuki-sandbox
systemctl is-active yuki-sandbox
attempt=0
while [ ! -S /run/yuki-sandbox/manager.sock ] && [ "$attempt" -lt 30 ]; do
    attempt=$((attempt + 1))
    sleep 1
done
test -S /run/yuki-sandbox/manager.sock
test "$(stat -c %g /run/yuki-sandbox/manager.sock)" = 10001
echo 'Verify from Bot UID: docker compose exec --user 10001:10001 bot qq-ai-bot-cli setup environment-check'
