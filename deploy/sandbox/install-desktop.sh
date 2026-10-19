#!/bin/sh
# Explicit in-place rootFS upgrade. Never recreates or starts an environment.
set -eu
test "$#" -eq 2 && test "$1" = --container || {
    echo 'Usage: install-desktop.sh --container ORIGINAL_ENVIRONMENT_NAME' >&2
    exit 1
}
container=$2
source_root=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
test "$(docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}' "$container")" = persistent-v1
test "$(docker inspect --format '{{.State.Running}}' "$container")" = true
docker exec --user 0:0 "$container" sh -ec 'apt-get update; apt-get install -y --no-install-recommends tigervnc-standalone-server openbox chromium socat fonts-noto-cjk fonts-noto-color-emoji xdotool x11-utils imagemagick; rm -rf /var/lib/apt/lists/*; /usr/local/bin/python -I -m pip --isolated install --no-user --no-cache-dir websockets==15.0.1; mkdir -p /opt/yuki-runtime'
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
docker exec --user 0:0 "$container" sh -ec 'chmod 755 /usr/local/bin/yuki-computer; python /opt/yuki-runtime/desktop-manifest.py; yuki-computer --help; cat /opt/yuki-runtime/manifest.json'
