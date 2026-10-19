#!/bin/sh
# Copy the frozen client venv from a selected local image into the original environment.
# No environment replacement, HOME write, gateway change, image pull or service restart.
set -eu
container=""
image=""
replace=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --container) test "$#" -ge 2; container=$2; shift 2 ;;
        --image) test "$#" -ge 2; image=$2; shift 2 ;;
        --replace) replace=true; shift ;;
        *) echo 'Usage: install-yuki-client.sh --container ORIGINAL --image SAME_RELEASE_ENVIRONMENT_IMAGE [--replace]' >&2; exit 1 ;;
    esac
done
test -n "$container" && test -n "$image"
test "$(docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}' "$container")" = persistent-v1
docker exec --user 0:0 "$container" /usr/local/bin/python -I -c 'import sys; assert sys.version_info[:2] == (3, 12)'
original_image=$(docker inspect --format '{{.Image}}' "$container")
original_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$original_image")
client_platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")
test "$original_platform" = "$client_platform" || {
    echo 'Client image and original environment use different native platforms.' >&2
    exit 1
}
docker exec --user 0:0 "$container" /usr/local/bin/python -I -c 'from pathlib import Path; root=Path("/opt"); target=Path("/opt/yuki-client"); assert root.resolve() == root; assert not target.is_symlink(); assert not target.exists() or target.resolve() == target'
stage=$(mktemp -d)
holder=""
cleanup() {
    if [ -n "$holder" ]; then docker rm "$holder" >/dev/null; fi
    rm -rf "$stage"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
holder=$(docker create --entrypoint /bin/true "$image")
docker cp "$holder:/opt/yuki-client" "$stage/client"
test -f "$stage/client/INSTALLATION.json"
if docker exec --user 0:0 "$container" test -e /opt/yuki-client; then
    if [ "$replace" = true ]; then
        docker cp "$stage/client/." "$container:/opt/yuki-client"
    elif ! docker cp "$container:/opt/yuki-client/INSTALLATION.json" "$stage/original.json" || ! cmp -s "$stage/original.json" "$stage/client/INSTALLATION.json"; then
        echo 'A different or incomplete client is already installed. Close its ACP peers before an explicit --replace.' >&2
        exit 1
    fi
else
    docker cp "$stage/client" "$container:/opt/yuki-client"
fi
docker exec --user 0:0 "$container" chown -R 0:0 /opt/yuki-client
docker exec --user 0:0 "$container" /opt/yuki-client/bin/python -I /opt/yuki-client/verify-installation.py --verify
docker exec --user 0:0 "$container" /opt/yuki-client/bin/yuki --version
docker exec --user 0:0 "$container" /opt/yuki-client/bin/yuki client --help >/dev/null
docker exec --user 0:0 "$container" /opt/yuki-client/bin/yuki acp --help >/dev/null
echo 'Verified Yuki client in the original environment; HOME and original execution receipts remain in place.'
