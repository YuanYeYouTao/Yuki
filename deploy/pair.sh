#!/bin/sh
# A new one-time code for Yuki Desktop, for the owner the installer created (valid 24 hours).
# The link names CLIENT_PUBLIC_URL; without one only the code is shown, as install.sh does.
set -eu
cd "$(dirname "$0")/.."
person=$(sed -n 's/^YUKI_OWNER_PERSON_ID=//p' .env | tail -n 1)
if [ -z "$person" ]; then
    echo 'No owner is recorded in .env; see docs/operations/person-admin-control-plane-upgrade.md.' >&2
    exit 1
fi
paired=$(docker compose exec -T bot sh -ec '
YUKI_CONTROL_CREDENTIAL="$YUKI_CONTROL_OPERATOR_TOKEN" yuki admin client pair --person "$1" \
  | python -c "import json, sys; from yuki.config import Settings; from yuki.services.client_link import pairing_link; result = json.load(sys.stdin); server = Settings().client_public_url; print(result[\"code\"], pairing_link(server, result[\"code\"]) if server else \"\")"' \
    sh "$person")
read -r code link <<EOF
$paired
EOF
printf 'Connect Yuki Desktop (https://github.com/Institute-of-Agent-Life/Yuki-Desktop):\n'
if [ -n "$link" ]; then
    printf '  Open this link, or paste it into Yuki Desktop:\n  %s\n' "$link"
else
    printf '  Pairing code: %s\n' "$code"
    printf '  No public address (CLIENT_PUBLIC_URL) is set, so there is no link another\n'
    printf '  computer can open. In Yuki Desktop choose "部署到我的服务器 / 通过 SSH 连接"\n'
    printf '  (Deploy to my server / Connect over SSH) and use this code.\n'
fi
printf '  One-time, valid for 24 hours; run sh deploy/pair.sh again for a new one.\n'
