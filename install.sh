#!/bin/sh
set -eu

VERSION="3.8.4"
INSTALL_DIR=""
REPOSITORY="YuanYeYouTao/Yuki-QQbot"
BOT_IMAGE="ghcr.io/yuanyeyoutao/yuki-qqbot"

usage() {
    printf '%s\n' "Usage: install.sh [--dir PATH] [--version X.Y.Z] (configure only)"
}

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir)
            [ "$#" -ge 2 ] || fail "--dir requires a path"
            INSTALL_DIR=$2
            shift 2
            ;;
        --version)
            [ "$#" -ge 2 ] || fail "--version requires X.Y.Z"
            VERSION=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || \
    fail "version must use X.Y.Z"

[ -t 0 ] && [ -t 1 ] || fail "configuration requires an interactive terminal"

command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose CLI plugin is not available"
docker info >/dev/null 2>&1 || fail "Docker Engine is not running"

architecture=$(docker info --format '{{.Architecture}}' 2>/dev/null || true)
operating_system=$(docker info --format '{{.OSType}}' 2>/dev/null || true)
[ "$operating_system" = linux ] || fail "Docker must be running Linux containers"
case "$architecture" in
    amd64|x86_64) ;;
    *) fail "Yuki $VERSION officially supports linux/amd64; Docker reports $architecture" ;;
esac

if [ -z "$INSTALL_DIR" ]; then
    if [ -f "docker-compose.yml" ] && [ -f ".env.example" ]; then
        INSTALL_DIR=$PWD
    else
        INSTALL_DIR=$PWD/yuki
    fi
fi
mkdir -p "$INSTALL_DIR"
INSTALL_DIR=$(cd "$INSTALL_DIR" && pwd)
[ -w "$INSTALL_DIR" ] || fail "installation directory is not writable"

existing=false
if [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -f "$INSTALL_DIR/.env.example" ]; then
    existing=true
elif [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    fail "installation directory is not empty and is not a Yuki deployment"
fi

# Bootstrap only an empty directory; existing deployments keep every managed file.
if [ "$existing" = false ]; then
    temporary=$(mktemp -d "${TMPDIR:-/tmp}/yuki-install.XXXXXX")
    trap 'rm -rf "$temporary"' EXIT HUP INT TERM

    download() {
        url=$1
        output=$2
        if command -v curl >/dev/null 2>&1; then
            curl --fail --silent --show-error --location "$url" --output "$output"
        elif command -v wget >/dev/null 2>&1; then
            wget -q "$url" -O "$output"
        else
            fail "curl or wget is required to download the release"
        fi
    }

    base="https://github.com/$REPOSITORY/releases/download/v$VERSION"
    archive="yuki-$VERSION-deploy.tar.gz"
    download "$base/$archive" "$temporary/$archive"
    download "$base/SHA256SUMS" "$temporary/SHA256SUMS"
    expected=$(awk -v name="$archive" '$2 == name {print $1}' "$temporary/SHA256SUMS")
    [ -n "$expected" ] || fail "release checksum does not list $archive"
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$temporary/$archive" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$temporary/$archive" | awk '{print $1}')
    else
        fail "sha256sum or shasum is required"
    fi
    [ "$actual" = "$expected" ] || fail "release archive checksum mismatch"
    tar -xzf "$temporary/$archive" -C "$temporary"
    source="$temporary/yuki-$VERSION-deploy"
    [ -d "$source" ] || fail "release archive layout is invalid"

    cp -R "$source/." "$INSTALL_DIR/"
fi

image="$BOT_IMAGE:$VERSION"
printf '%s\n' "Pulling $image"
docker pull "$image"

docker run --rm -it \
    --user "$(id -u):$(id -g)" \
    --entrypoint qq-ai-bot-cli \
    --volume "$INSTALL_DIR:/deploy" \
    --workdir /deploy \
    "$image" setup --deployment-root /deploy

printf '%s\n' "Configuration saved. No services were stopped or started and no database was upgraded."
printf '%s\n' "Review $INSTALL_DIR/Yuki-$VERSION-Upgrade.md before starting or upgrading the deployment."
printf '%s\n' "Upgrade guide: https://github.com/$REPOSITORY/blob/v$VERSION/docs/upgrade-$VERSION.md"
