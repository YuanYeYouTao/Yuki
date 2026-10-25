#!/bin/sh
# Run after installing verified runsc binaries, loading locally built images,
# and starting the dedicated egress stack. Never restarts Docker or gateways.
set -eu
deployment_root=""
bot_container=""
deployment_files=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
while [ "$#" -gt 0 ]; do
    case "$1" in
        --deployment-root)
            test "$#" -ge 2 || { echo '--deployment-root requires a path' >&2; exit 1; }
            deployment_root=$2
            shift 2
            ;;
        --bot-container)
            test "$#" -ge 2 || { echo '--bot-container requires a container' >&2; exit 1; }
            bot_container=$2
            shift 2
            ;;
        *) echo 'Usage: install-manager.sh --bot-container CONTAINER [--deployment-root ABSOLUTE_PATH]' >&2; exit 1 ;;
    esac
done
test "$(id -u)" = 0
test -n "$bot_container" || { echo '--bot-container is required' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,12)'
test -x /opt/yuki-sandbox/venv/bin/python
/opt/yuki-sandbox/venv/bin/python -c 'import websockets; assert websockets.__version__ == "15.0.1"'
helper="$deployment_files/configure-manager.py"
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
sync_result=$(python3 "$deployment_files/sync-manager.py" --bot-container "$bot_container")
source_root=$(printf '%s\n' "$sync_result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["source"])')
if [ "$deployment_files" != /opt/yuki-sandbox/deploy/sandbox ]; then
    install -d /opt/yuki-sandbox/deploy/sandbox
    for file in network-guard.sh environment-storage.sh; do
        install -m 644 "$deployment_files/$file" "/opt/yuki-sandbox/deploy/sandbox/$file"
    done
fi
if [ ! -f /etc/systemd/system/yuki-sandbox.service ]; then
    install -m 644 "$deployment_files/yuki-sandbox.service" /etc/systemd/system/yuki-sandbox.service
fi
if [ ! -f /etc/systemd/system/yuki-environment-storage.service ]; then
    install -m 644 "$deployment_files/yuki-environment-storage.service" /etc/systemd/system/yuki-environment-storage.service
fi
systemctl daemon-reload
systemctl enable yuki-sandbox
systemctl restart yuki-sandbox
systemctl is-active yuki-sandbox
attempt=0
while ! PYTHONPATH="$source_root" setpriv --reuid=10001 --regid=10001 --clear-groups \
    /opt/yuki-sandbox/venv/bin/python -m yuki.sandbox.deployment_check \
    --socket /run/yuki-sandbox/manager.sock --artifact-store "$workspace"; do
    attempt=$((attempt + 1))
    test "$attempt" -lt 30 || exit 1
    sleep 1
done
