#!/bin/sh
# Explicitly upgrade SDK packages in the original persistent Linux rootFS.
# This script never recreates a container or changes HOME, jobs or credentials.
set -eu
test "$#" -eq 2 && test "$1" = --container || {
    echo 'Usage: install-acp-sdk.sh --container ORIGINAL_ENVIRONMENT_NAME' >&2
    exit 1
}
container=$2
test "$(docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}' "$container")" = persistent-v1
docker exec --user 0:0 "$container" /usr/local/bin/python -m pip install --quiet --no-cache-dir agent-client-protocol==0.12.1
docker exec --user 0:0 "$container" /usr/local/bin/python -c 'from importlib.metadata import version; assert version("agent-client-protocol") == "0.12.1"; print("ACP SDK 0.12.1 installed in the original environment")'
mount=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/opt/yuki-runtime/acp_peer.py"}}{{.Source}} {{.RW}}{{end}}{{end}}' "$container")
case "$mount" in
    *' false') echo 'Original environment has a read-only ACP peer source mount.' ;;
    *)
        echo 'SDK is installed. The ACP peer read-only mount still needs the documented explicit environment replacement.' >&2
        exit 1
        ;;
esac
