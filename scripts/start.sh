#!/bin/sh
set -eu

mkdir -p /app/data
mkdir -p /app/character
chown bot:bot /app/character
chmod 750 /app/character
for prompt in /app/character/system_prompt.md /app/character/persona.md; do
    if [ -f "$prompt" ]; then
        chown bot:bot "$prompt"
    fi
done
mkdir -p /app/workspace /app/social-transfer
chown bot:bot /app/workspace /app/social-transfer
chmod 755 /app/social-transfer
chown -R bot:bot /app/data

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

setpriv --reuid=10001 --regid=10001 --init-groups yuki init-db
exec setpriv --reuid=10001 --regid=10001 --init-groups yuki-bot
