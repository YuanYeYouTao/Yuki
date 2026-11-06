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
docker exec --user 0:0 "$container" mkdir -p /opt/yuki-runtime
for file in yuki_computer.py desktop_service.py; do
    mounted=$(docker inspect --format "{{range .Mounts}}{{if eq .Destination \"/opt/yuki-runtime/$file\"}}{{.RW}}{{end}}{{end}}" "$container")
    if [ "$mounted" = false ]; then
        expected=$(sha256sum "$source_root/src/yuki/sandbox/$file" | cut -d' ' -f1)
        actual=$(docker exec "$container" sha256sum "/opt/yuki-runtime/$file" | cut -d' ' -f1)
        test "$expected" = "$actual" || {
            echo "Read-only $file differs; update its Manager source mount using the documented environment upgrade." >&2
            exit 1
        }
    else
        docker cp "$source_root/src/yuki/sandbox/$file" "$container:/opt/yuki-runtime/$file"
    fi
done
docker cp "$source_root/deploy/sandbox/desktop-manifest.py" "$container:/opt/yuki-runtime/desktop-manifest.py"
docker cp "$source_root/deploy/sandbox/yuki-computer.sh" "$container:/usr/local/bin/yuki-computer"
docker cp "$source_root/deploy/sandbox/install-programs.sh" "$container:/opt/yuki-runtime/install-programs.sh"
docker exec --user 0:0 "$container" sh /opt/yuki-runtime/install-programs.sh "$@"
