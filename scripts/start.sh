#!/bin/sh
set -eu

yuki_uid=${YUKI_RUNTIME_UID:-10001}
yuki_gid=${YUKI_RUNTIME_GID:-10001}

mkdir -p /app/data
mkdir -p /app/workspace /app/social-transfer
mkdir -p /app/character
chown -R "$yuki_uid:$yuki_gid" /app/character
chmod 750 /app/character
chown "$yuki_uid:$yuki_gid" /app/workspace /app/social-transfer
chmod 755 /app/social-transfer
chown -R "$yuki_uid:$yuki_gid" /app/data

case ",${COMPOSE_PROFILES:-}," in
  *,snowluma,*)
    if [ -n "${SNOWLUMA_CONFIG_OUTPUT:-}" ]; then
    mkdir -p /app/snowluma-data/config
    snowluma_uid=${SNOWLUMA_UID:-1000}
    snowluma_gid=${SNOWLUMA_GID:-1000}
    case "$snowluma_uid" in
        ''|*[!0-9]*) printf '%s\n' "Invalid SnowLuma UID" >&2; exit 1 ;;
    esac
    case "$snowluma_gid" in
        ''|*[!0-9]*) printf '%s\n' "Invalid SnowLuma GID" >&2; exit 1 ;;
    esac
    yuki render-snowluma-config --output "$SNOWLUMA_CONFIG_OUTPUT"
    chown -R "$snowluma_uid:$snowluma_gid" /app/snowluma-data/config
    chmod 600 "$SNOWLUMA_CONFIG_OUTPUT"
    fi
    ;;
esac

setpriv --reuid="$yuki_uid" --regid="$yuki_gid" --clear-groups yuki init-db
exec setpriv --reuid="$yuki_uid" --regid="$yuki_gid" --clear-groups yuki-bot
