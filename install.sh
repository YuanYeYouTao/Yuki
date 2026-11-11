#!/bin/sh
# Install or upgrade a Yuki deployment with Docker Compose, including its sandbox.
set -eu

VERSION="4.0.0"
INSTALL_DIR=""
PROFILE=""
YES=false
ANSWERS=""
BUNDLE=""
REPOSITORY="YuanYeYouTao/Yuki"
BOT_IMAGE="ghcr.io/yuanyeyoutao/yuki"
ENVIRONMENT_IMAGE="ghcr.io/yuanyeyoutao/yuki-environment"

usage() {
    cat <<'EOF'
Usage: install.sh [--dir PATH] [--version X.Y.Z] [--profile lite|headless|desktop]
                  [--yes [--answers FILE]] [--bundle FILE]

Run it again in the same directory to upgrade; .env and data are kept.
  --profile   sandbox profile: lite (terminal only), headless (adds a browser),
              desktop (adds a desktop you can watch and take over)
  --yes       no prompts; a first install then reads its model settings from
              --answers, a JSON file: {"protocol", "base_url", "api_key", "model"}
  --bundle    use this yuki-X.Y.Z-deploy.tar.gz instead of downloading the release
EOF
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir|--version|--profile|--answers|--bundle)
            [ "$#" -ge 2 ] || fail "$1 requires a value"
            case "$1" in
                --dir) INSTALL_DIR=$2 ;;
                --version) VERSION=$2 ;;
                --profile) PROFILE=$2 ;;
                --answers) ANSWERS=$(cd "$(dirname "$2")" && pwd)/$(basename "$2") ;;
                --bundle) BUNDLE=$(cd "$(dirname "$2")" && pwd)/$(basename "$2") ;;
            esac
            shift 2
            ;;
        --yes) YES=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done

printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || fail "version must use X.Y.Z"
case "$PROFILE" in ''|lite|headless|desktop) ;; *) fail "unknown profile: $PROFILE" ;; esac
if [ "$YES" = false ]; then
    [ -t 0 ] && [ -t 1 ] || fail "no terminal: pass --yes (and --profile, --answers for a first install)"
fi

command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose CLI plugin is not available"
docker info >/dev/null 2>&1 || fail "Docker Engine is not running"
[ "$(docker info --format '{{.OSType}}')" = linux ] || fail "Docker must be running Linux containers"
case "$(docker info --format '{{.Architecture}}')" in
    amd64|x86_64) platform=linux/amd64 ;;
    arm64|aarch64) platform=linux/arm64 ;;
    *) fail "Yuki supports linux/amd64 and linux/arm64" ;;
esac

if [ -z "$INSTALL_DIR" ]; then
    if [ -f docker-compose.yml ] && [ -f .env.example ]; then INSTALL_DIR=$PWD; else INSTALL_DIR=$PWD/yuki; fi
fi
mkdir -p "$INSTALL_DIR"
INSTALL_DIR=$(cd "$INSTALL_DIR" && pwd)
[ -w "$INSTALL_DIR" ] || fail "installation directory is not writable"
if ! { [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -f "$INSTALL_DIR/.env.example" ]; } \
    && [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    fail "installation directory is not empty and is not a Yuki deployment"
fi

# Release files replace the previous ones; .env, data and docker-compose.override.yml stay.
temporary=$(mktemp -d "${TMPDIR:-/tmp}/yuki-install.XXXXXX")
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
download() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --show-error --location "$1" --output "$2"
    else
        wget -q "$1" -O "$2"
    fi
}
archive="yuki-$VERSION-deploy.tar.gz"
if [ -n "$BUNDLE" ]; then
    cp "$BUNDLE" "$temporary/$archive"
else
    base="https://github.com/$REPOSITORY/releases/download/v$VERSION"
    download "$base/$archive" "$temporary/$archive"
    download "$base/SHA256SUMS" "$temporary/SHA256SUMS"
    expected=$(awk -v name="$archive" '$2 == name {print $1}' "$temporary/SHA256SUMS")
    [ -n "$expected" ] || fail "release checksum does not list $archive"
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$temporary/$archive" | awk '{print $1}')
    else
        actual=$(shasum -a 256 "$temporary/$archive" | awk '{print $1}')
    fi
    [ "$actual" = "$expected" ] || fail "release archive checksum mismatch"
fi
tar -xzf "$temporary/$archive" -C "$temporary"
[ -d "$temporary/yuki-$VERSION-deploy" ] || fail "release archive layout is invalid"
cp -R "$temporary/yuki-$VERSION-deploy/." "$INSTALL_DIR/"
cd "$INSTALL_DIR"

# An environment created by the retired host Manager is moved once, explicitly.
if [ "$(docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}/{{index .Config.Labels "io.yuki.sandbox.project"}}' yuki-environment 2>/dev/null || true)" = "persistent-v1/" ]; then
    fail "this Docker host runs an environment of the old host Manager; the release files are now in $INSTALL_DIR; run: sudo sh $INSTALL_DIR/deploy/sandbox/migrate-host-manager.sh --deployment-root $INSTALL_DIR, then run this installer again"
fi


env_value() {
    [ -f .env ] && sed -n "s/^$1=//p" .env | tail -n 1 || true
}
set_env() {
    if [ -f .env ] && grep -q "^$1=" .env; then
        sed "s|^$1=.*|$1=$2|" .env > .env.next && cat .env.next > .env && rm -f .env.next
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

# Version tags are immutable: an image already present is the release image.
pull() {
    if ! docker image inspect "$1" >/dev/null 2>&1; then
        printf 'Pulling %s\n' "$1"
        docker pull --quiet --platform "$platform" "$1" >/dev/null
    fi
}
image="$BOT_IMAGE:$VERSION"
pull "$image"

if [ ! -f .env ]; then
    if [ "$YES" = true ]; then
        [ -n "$ANSWERS" ] && [ -f "$ANSWERS" ] || fail "a first install with --yes needs --answers FILE"
        docker run --rm -i --platform "$platform" --user "$(id -u):$(id -g)" --entrypoint yuki \
            --volume "$INSTALL_DIR:/deploy" --workdir /deploy \
            "$image" setup desktop-configure --deployment-root /deploy < "$ANSWERS" >/dev/null
    else
        docker run --rm -it --platform "$platform" --user "$(id -u):$(id -g)" --entrypoint yuki \
            --volume "$INSTALL_DIR:/deploy" --workdir /deploy \
            "$image" setup --deployment-root /deploy
    fi
fi
set_env YUKI_VERSION "$VERSION"

if [ -z "$PROFILE" ] && [ -z "$(env_value YUKI_SANDBOX_PROFILE)" ]; then
    memory=$(( $(docker info --format '{{.MemTotal}}') / 1048576 ))
    if [ "$memory" -lt 2048 ]; then PROFILE=lite
    elif [ "$memory" -lt 4096 ]; then PROFILE=headless
    else PROFILE=desktop
    fi
    if [ "$YES" = false ]; then
        printf 'Docker reports %s MiB of memory. Sandbox profile:\n' "$memory"
        printf '  lite      terminal and files only (about 512 MiB)\n'
        printf '  headless  adds a headless browser (about 1 GiB)\n'
        printf '  desktop   adds a desktop you can watch and take over (about 3 GiB)\n'
        printf 'Profile [%s]: ' "$PROFILE"
        read -r answer
        PROFILE=${answer:-$PROFILE}
        case "$PROFILE" in lite|headless|desktop) ;; *) fail "unknown profile: $PROFILE" ;; esac
    fi
fi
[ -z "$PROFILE" ] || set_env YUKI_SANDBOX_PROFILE "$PROFILE"
PROFILE=$(env_value YUKI_SANDBOX_PROFILE)

# Chosen once and kept: a later install never silently moves the sandbox off gVisor.
if [ -z "$(env_value YUKI_SANDBOX_RUNTIME)" ]; then
    if docker info --format '{{json .Runtimes}}' | grep -q '"runsc".*--overlay2=none'; then
        set_env YUKI_SANDBOX_RUNTIME runsc
    else
        set_env YUKI_SANDBOX_RUNTIME runc
    fi
fi

sh deploy/sandbox/prepare.sh
environment_image=$(env_value YUKI_ENVIRONMENT_IMAGE)
environment_image=${environment_image:-$ENVIRONMENT_IMAGE:$VERSION}
[ "$PROFILE" = lite ] || environment_image="$environment_image-desktop"
pull "$environment_image"
docker compose up -d --pull missing --remove-orphans

printf 'Waiting for Yuki and its sandbox'
uid=$(env_value YUKI_RUNTIME_UID)
gid=$(env_value YUKI_RUNTIME_GID)
attempt=0
until docker compose exec -T --user "${uid:-10001}:${gid:-10001}" bot \
    yuki setup environment-check >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 120 ]; then
        printf '\n'
        docker compose logs --tail 40 manager bot >&2 || true
        fail "the sandbox did not become ready; the logs above show why"
    fi
    printf '.'
    sleep 5
done
printf '\nYuki %s is running in %s (sandbox profile: %s, runtime: %s).\n' \
    "$VERSION" "$INSTALL_DIR" "$PROFILE" "$(env_value YUKI_SANDBOX_RUNTIME)"
printf 'Release notes: https://github.com/%s/blob/v%s/docs/releases/v%s.md\n' \
    "$REPOSITORY" "$VERSION" "$VERSION"
