#!/bin/sh
# Create the sandbox's Docker volumes once; existing volumes are kept as they are.
# They are external to Compose so that `docker compose down -v` never removes them.
set -eu
for volume in yuki-sandbox-state yuki-sandbox-home yuki-sandbox-runtime yuki-sandbox-code \
    yuki-media-cache; do
    docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null
done
