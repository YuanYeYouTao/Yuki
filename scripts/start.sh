#!/bin/sh
set -eu

mkdir -p /app/data /app/napcat-config /app/snowluma-data/config
mkdir -p /app/workspace /app/social-transfer
chown bot:bot /app/workspace /app/social-transfer
chmod 755 /app/social-transfer
chown -R bot:bot /app/data

if [ -n "${NAPCAT_CONFIG_OUTPUT:-}" ]; then
    qq-ai-bot-cli render-napcat-config --output "$NAPCAT_CONFIG_OUTPUT"
fi

if [ -n "${SNOWLUMA_CONFIG_OUTPUT:-}" ]; then
    snowluma_uid=${SNOWLUMA_UID:-1000}
    snowluma_gid=${SNOWLUMA_GID:-1000}
    case "$snowluma_uid" in
        ''|*[!0-9]*) printf '%s\n' "Invalid SnowLuma UID" >&2; exit 1 ;;
    esac
    case "$snowluma_gid" in
        ''|*[!0-9]*) printf '%s\n' "Invalid SnowLuma GID" >&2; exit 1 ;;
    esac
    qq-ai-bot-cli render-snowluma-config --output "$SNOWLUMA_CONFIG_OUTPUT"
    chown -R "$snowluma_uid:$snowluma_gid" /app/snowluma-data/config
    chmod 600 "$SNOWLUMA_CONFIG_OUTPUT"
fi

setpriv --reuid=10001 --regid=10001 --init-groups qq-ai-bot-cli init-db
exec setpriv --reuid=10001 --regid=10001 --init-groups qq-ai-bot
