#!/bin/sh
# Enable the bundled SnowLuma QQ gateway with your own image, then start it.
# Yuki publishes no SnowLuma image; see docs/deployment/snowluma.md.
# Compose files follow COMPOSE_FILE, as for any docker compose command here.
set -eu
cd "$(dirname "$0")/.."
if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo 'Usage: sh deploy/snowluma.sh <your SnowLuma image>' >&2
    exit 2
fi
# As install.sh migrates an upgrade: root in a one-off Bot; .env keeps its owner.
docker compose run --rm --no-deps -T --user 0:0 --entrypoint yuki \
    --volume "$(pwd):/deploy" bot \
    setup snowluma --deployment-root /deploy --image "$1" >/dev/null
docker compose up -d bot snowluma
printf 'SnowLuma is starting with %s.\n' "$1"
printf 'Sign in to QQ: in Yuki Desktop open the computer window, choose "QQ 登入"\n'
printf '(QQ sign-in) and scan the code with QQ on your phone.\n'
