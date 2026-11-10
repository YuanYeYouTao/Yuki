#!/bin/sh
# Explicit in-place rootFS upgrade. Never recreates or starts an environment.
set -eu
test "$#" -ge 2 && test "$1" = --container || {
    echo 'Usage: install-desktop.sh --container ORIGINAL_ENVIRONMENT_NAME [headed|headless|none [DEBIAN_PACKAGE...]]' >&2
    exit 1
}
container=$2
source_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
test "$(docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}' "$container")" = persistent-v1
test "$(docker inspect --format '{{.State.Running}}' "$container")" = true
shift 2
if [ "$#" -eq 0 ]; then set -- headed; fi
# Write through the container: under gVisor the sandbox keeps its own rootFS
# overlay, so `docker cp` lands in a Docker layer the running sandbox does not read.
put() {
    docker exec -i --user 0:0 "$container" sh -c 'cat > "$1.new" && mv -f "$1.new" "$1"' sh "$2" < "$1"
}
docker exec --user 0:0 "$container" mkdir -p /opt/yuki-runtime/yuki_agent_connector
# Each entry is <path under /opt/yuki-runtime>=<path under the source tree>.
for entry in \
    yuki_computer.py=src/yuki/sandbox/yuki_computer.py \
    desktop_service.py=src/yuki/sandbox/desktop_service.py \
    yuki_agent_connector/cdp.py=packages/yuki-agent-connector/src/yuki_agent_connector/cdp.py
do
    file=${entry%%=*}
    source=$source_root/${entry#*=}
    mounted=$(docker inspect --format "{{range .Mounts}}{{if eq .Destination \"/opt/yuki-runtime/$file\"}}{{.RW}}{{end}}{{end}}" "$container")
    if [ "$mounted" = false ]; then
        expected=$(sha256sum "$source" | cut -d' ' -f1)
        actual=$(docker exec "$container" sha256sum "/opt/yuki-runtime/$file" | cut -d' ' -f1)
        test "$expected" = "$actual" || {
            echo "Read-only $file differs; update its Manager source mount using the documented environment upgrade." >&2
            exit 1
        }
    else
        put "$source" "/opt/yuki-runtime/$file"
    fi
done
put "$source_root/deploy/sandbox/desktop-manifest.py" /opt/yuki-runtime/desktop-manifest.py
put "$source_root/deploy/sandbox/yuki-computer.sh" /usr/local/bin/yuki-computer
put "$source_root/deploy/sandbox/install-programs.sh" /opt/yuki-runtime/install-programs.sh
docker exec --user 0:0 "$container" sh /opt/yuki-runtime/install-programs.sh "$@"
