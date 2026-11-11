#!/bin/sh
# Move a sandbox from the retired host Manager (systemd, loop-mounted ext4) to the
# Compose Manager. Run once as root on the Docker host, after Yuki's work has finished:
#
#   sudo sh deploy/sandbox/migrate-host-manager.sh --deployment-root /path/to/deployment
#   (then docker compose up -d, verify, and finally)
#   sudo sh deploy/sandbox/migrate-host-manager.sh --deployment-root ... --cleanup
#
# The first run copies; it never deletes. The old loop images, jobs.sqlite3 and media
# cache stay where they are, and the old container is renamed, until --cleanup.
set -eu
deployment=""
cleanup=false
while [ "$#" -gt 0 ]; do
    case "$1" in
        --deployment-root) deployment=$2; shift 2 ;;
        --cleanup) cleanup=true; shift ;;
        *) echo 'Usage: migrate-host-manager.sh --deployment-root PATH [--cleanup]' >&2; exit 1 ;;
    esac
done
test "$(id -u)" = 0 || { echo 'Run as root on the Docker host.' >&2; exit 1; }
test -f "$deployment/docker-compose.yml" || { echo '--deployment-root must hold docker-compose.yml' >&2; exit 1; }
settings=/etc/yuki-sandbox/deployment.env
old_value() { [ -f "$settings" ] && sed -n "s/^$1=//p" "$settings" | tail -n 1 | tr -d '"' || true; }
root=$(old_value YUKI_SANDBOX_ROOT)
root=${root:-/var/lib/yuki-sandbox}
media=$(old_value YUKI_MANAGER_MEDIA_CACHE)
media=${media:-$deployment/data/conversation-media-cache}
retired=yuki-environment-retired

if "$cleanup"; then
    docker inspect --format '{{index .Config.Labels "io.yuki.sandbox.project"}}' yuki-environment \
        | grep -q . || { echo 'The Compose Manager has not created its environment yet.' >&2; exit 1; }
    docker rm "$retired" >/dev/null 2>&1 || true
    for unit in yuki-sandbox yuki-environment-storage; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        rm -rf "/etc/systemd/system/$unit.service" "/etc/systemd/system/$unit.service.d"
    done
    systemctl daemon-reload
    for target in "$root/home" "$root/environment-jobs"; do
        if mountpoint -q "$target"; then umount "$target"; fi
    done
    rm -rf "$root" /opt/yuki-sandbox /etc/yuki-sandbox /run/yuki-sandbox
    echo "Removed the host Manager, its loop volumes and $root."
    echo "Remove $media yourself once the media-cache volume is confirmed."
    exit 0
fi

labels=$(docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}/{{index .Config.Labels "io.yuki.sandbox.project"}}' yuki-environment)
test "$labels" = persistent-v1/ || { echo 'yuki-environment is not a host-Manager environment.' >&2; exit 1; }
runtime=$(docker inspect --format '{{.HostConfig.Runtime}}' yuki-environment)
if [ "$runtime" = runsc ] && ! docker info --format '{{json .Runtimes}}' | grep -q '"runsc".*--overlay2=none'; then
    echo 'runsc keeps this rootFS outside the Docker layer (no --overlay2=none); export it with' >&2
    echo '`runsc tar rootfs-upper` first (docs/operations/mac-arm64-migration-20261107.md).' >&2
    exit 1
fi
active=$(python3 - "$root/jobs.sqlite3" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(db.execute(
    "SELECT count(*) FROM jobs JOIN environment_jobs USING(id) "
    "WHERE state IN ('queued','running') AND kind <> 'service'"
).fetchone()[0])
PY
)
test "$active" = 0 || { echo "$active runs are still active; let them finish first." >&2; exit 1; }

systemctl stop yuki-sandbox
systemctl disable yuki-sandbox yuki-environment-storage >/dev/null 2>&1 || true
docker stop --time 30 yuki-environment >/dev/null
checkpoint="yuki-environment:migrated-$(date -u +%Y%m%d%H%M%S)"
docker commit yuki-environment "$checkpoint" >/dev/null
echo "Saved the environment's rootFS as $checkpoint (private: it holds the old execd token)."

sh "$deployment/deploy/sandbox/prepare.sh"
copy() {
    docker run --rm --network none --entrypoint sh -v "$1:/from:ro" -v "$2:/to" "$checkpoint" -ec \
        'test -z "$(find /to -mindepth 1 -maxdepth 1 -print -quit)" || { echo "volume $0 is not empty" >&2; exit 1; }; cp -a /from/. /to/' "$2"
}
copy "$root/home" yuki-sandbox-home
copy "$root/environment-jobs" yuki-sandbox-runtime
if [ -d "$media" ]; then copy "$media" yuki-media-cache; fi
docker run --rm --network none --entrypoint python -v "$root:/old:ro" -v yuki-sandbox-state:/state \
    "$checkpoint" -c '
import sqlite3, sys
source = sqlite3.connect("file:/old/jobs.sqlite3?mode=ro", uri=True)
target = sqlite3.connect("/state/jobs.sqlite3")
source.backup(target)
with target:
    target.execute("INSERT OR REPLACE INTO environment_state VALUES (?,?)", ("checkpoint_image", sys.argv[1]))
' "$checkpoint"

docker rename yuki-environment "$retired"
# The old Squid gateway and its internal bridge are gone from Yuki.
docker rm -f yuki-sandbox-egress >/dev/null 2>&1 || true
docker network rm yuki-sandbox-internal >/dev/null 2>&1 || true
for binary in iptables-nft iptables-legacy; do
    command -v "$binary" >/dev/null 2>&1 || continue
    while "$binary" -w 10 -D DOCKER-USER -i yuki-sandbox0 ! -s 172.30.251.2/32 -j YUKI-SANDBOX 2>/dev/null; do :; done
    while "$binary" -w 10 -D INPUT -i yuki-sandbox0 -j DROP 2>/dev/null; do :; done
    while "$binary" -w 10 -D INPUT -i yuki-sandbox0 -p tcp --sport 44772 -m conntrack --ctstate ESTABLISHED -j ACCEPT 2>/dev/null; do :; done
    "$binary" -w 10 -F YUKI-SANDBOX 2>/dev/null || true
    "$binary" -w 10 -X YUKI-SANDBOX 2>/dev/null || true
done

# Carry the old settings into the deployment's .env as explicit overrides.
env_file="$deployment/.env"
set_env() {
    if grep -q "^$1=" "$env_file" 2>/dev/null; then return; fi
    printf '%s=%s\n' "$1" "$2" >> "$env_file"
}
case "$(old_value YUKI_SANDBOX_BROWSER_MODE)" in
    headed) set_env YUKI_SANDBOX_PROFILE desktop ;;
    headless) set_env YUKI_SANDBOX_PROFILE headless ;;
    *) set_env YUKI_SANDBOX_PROFILE lite ;;
esac
set_env YUKI_SANDBOX_RUNTIME "$runtime"
for name in MEMORY_MIB SWAP_MIB CPUS PIDS TMP_MIB SHM_MIB PACKAGES; do
    value=$(old_value "YUKI_SANDBOX_$name")
    if [ -n "$value" ]; then set_env "YUKI_SANDBOX_$name" "$value"; fi
done
echo "Copied HOME, runtime records, media cache and jobs.sqlite3 into Docker volumes."
echo "Next: cd $deployment && docker compose up -d, then verify the terminal, files and services."
echo "After verifying, run this script again with --cleanup."
