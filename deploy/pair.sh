#!/bin/sh
# A new one-time code for Yuki Desktop, for the owner the installer created (valid 10 minutes).
set -eu
cd "$(dirname "$0")/.."
person=$(sed -n 's/^YUKI_OWNER_PERSON_ID=//p' .env | tail -n 1)
if [ -z "$person" ]; then
    echo 'No owner is recorded in .env; see docs/operations/person-admin-control-plane-upgrade.md.' >&2
    exit 1
fi
docker compose exec -T bot sh -c \
    'YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin client pair --person "$1"' \
    sh "$person"
