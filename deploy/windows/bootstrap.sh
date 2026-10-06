#!/usr/bin/env bash
# Only invoked inside the independently owned Yuki-Bocchi WSL distribution.
set -euo pipefail
umask 077
BOOTSTRAP=/opt/yuki-bootstrap
APP=/home/yuki/app
PHASE=${1:?phase required}
stage() { printf '\n[%s] %s - %s\n' "$(date '+%H:%M:%S')" "$PHASE" "$1"; }

case "$PHASE" in
  system)
    test "$(id -u)" = 0
    test -f /etc/yuki-deployment-id
    if ! test -f /opt/yuki-system-prepared; then
      export DEBIAN_FRONTEND=noninteractive
      stage '1/4 Refreshing Ubuntu package indexes'
      apt-get update
      stage '2/4 Upgrading Ubuntu packages'
      apt-get upgrade -y
      stage '3/4 Installing Python, Docker Engine, fonts and system tools'
      apt-get install -y ca-certificates curl git gcc libc6-dev python3 python3-venv \
        bubblewrap util-linux fonts-wqy-microhei ffmpeg xz-utils docker.io docker-compose-v2 \
        systemd systemd-sysv dbus
      if ! id yuki >/dev/null 2>&1; then useradd --create-home --shell /bin/bash yuki; fi
      python3 -m venv /opt/yuki-tools
      stage '4/4 Installing fixed uv and downloading Node.js'
      /opt/yuki-tools/bin/pip install 'uv==0.11.31'
      for tool in uv uvx; do
        if test -e "/usr/local/bin/$tool" || test -L "/usr/local/bin/$tool"; then
          test "$(readlink "/usr/local/bin/$tool")" = "/opt/yuki-tools/bin/$tool"
        else
          ln -s "/opt/yuki-tools/bin/$tool" "/usr/local/bin/$tool"
        fi
      done
      chmod -R a+rX /opt/yuki-tools
      mkdir -p /opt/yuki-node
      curl --fail --location --progress-bar --connect-timeout 30 --speed-time 120 --speed-limit 1024 --max-time 3600 --retry 3 --output /opt/yuki-node/node.tar.xz \
        https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz
      printf '%s  %s\n' fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6 \
        /opt/yuki-node/node.tar.xz | sha256sum --check -
      tar -xJf /opt/yuki-node/node.tar.xz -C /opt/yuki-node --strip-components=1
      rm /opt/yuki-node/node.tar.xz
      chmod -R a+rX /opt/yuki-node
      touch /opt/yuki-system-prepared
    fi
    chown -R root:yuki "$BOOTSTRAP"
    find "$BOOTSTRAP" -type d -exec chmod 750 '{}' +
    find "$BOOTSTRAP" -type f -exec chmod 640 '{}' +
    cat >/etc/wsl.conf <<'EOF'
[boot]
systemd=true
[user]
default=yuki
EOF
    ;;
  build)
    test "$(id -un)" = yuki
    export PATH="/opt/yuki-node/bin:$HOME/.cargo/bin:$PATH"
    python3 "$BOOTSTRAP/deployment.py" extract --bundle "$BOOTSTRAP" --app "$APP"
    cd "$APP"
    mkdir -p data workspace social-transfer gateway deployment-evidence
    chmod 755 social-transfer
    if ! test -f .yuki-built; then
      stage '1/4 Installing fixed Rust toolchain'
      if ! command -v rustup >/dev/null 2>&1; then
        curl --fail --location --retry 3 --output /home/yuki/rustup-init.sh https://sh.rustup.rs
        sh /home/yuki/rustup-init.sh -y --no-modify-path --profile minimal --default-toolchain 1.96.0
        rm /home/yuki/rustup-init.sh
      fi
      rustup toolchain install 1.96.0 --profile minimal
      stage '2/4 Installing frontend packages and building the WebUI'
      (cd frontend && npm ci && npm run build)
      stage '3/4 Installing locked Python runtime dependencies'
      uv sync --frozen --no-dev --python 3.12
      stage '4/4 Compiling Monty and its Python binding (Cargo output below)'
      bash scripts/build_monty_worker.sh /home/yuki/.cache/yuki-monty-build
      touch .yuki-built
    fi
    ;;
  install-worker)
    stage 'Installing root-owned verified Monty artifacts'
    test "$(id -u)" = 0
    if ! test -f /opt/yuki-monty/artifacts.json; then
      install -d -o root -g root -m 755 /opt/yuki-monty /opt/yuki-monty/licenses
      install -o root -g root -m 755 /home/yuki/.cache/yuki-monty-build/monty/target/release/monty /opt/yuki-monty/monty
      install -o root -g root -m 755 /home/yuki/.cache/yuki-monty-build/monty-isolated /opt/yuki-monty/monty-isolated
      install -o root -g root -m 644 /home/yuki/.cache/yuki-monty-build/THIRD_PARTY_NOTICES.json /opt/yuki-monty/THIRD_PARTY_NOTICES.json
      install -o root -g root -m 644 "$APP/LICENSE" /opt/yuki-monty/licenses/Yuki-LICENSE
      install -o root -g root -m 644 "$APP/vendor/monty/LICENSE" /opt/yuki-monty/licenses/Monty-LICENSE
      install -o root -g root -m 644 "$APP/vendor/monty/TYPESHED-LICENSE" /opt/yuki-monty/licenses/TYPESHED-LICENSE
      install -o root -g root -m 644 /home/yuki/.cache/yuki-monty-build/artifacts.json /opt/yuki-monty/artifacts.json
    fi
    ;;
  verify)
    test "$(id -un)" = yuki
    cd "$APP"
    # This observes real namespaces, permissions, no network, watchdog and owner death.
    # No global sysctl, seccomp or AppArmor restriction is disabled to obtain a pass.
    stage '1/4 Checking actual Monty isolation and owner death'
    .venv/bin/python scripts/verify_monty_isolation.py --output deployment-evidence/monty-isolation.json
    stage '2/4 Loading the preserved private persona and provider'
    .venv/bin/python "$BOOTSTRAP/deployment.py" configure --bundle "$BOOTSTRAP" --app "$APP" --admin "${2:?human administrator QQ required}"
    stage '3/4 Checking the real API image, tool call and continuation'
    .venv/bin/python "$BOOTSTRAP/deployment.py" api-probe --app "$APP"
    stage '4/4 Applying normal database migrations and rendering the QQ gateway'
    .venv/bin/qq-ai-bot-cli init-db
    .venv/bin/python "$BOOTSTRAP/deployment.py" database --app "$APP"
    NAPCAT_REVERSE_WS_URL=ws://host.docker.internal:18765/onebot/v11/ws \
      .venv/bin/qq-ai-bot-cli render-napcat-config --output gateway/config/onebot11.json
    ;;
  start)
    test "$(id -u)" = 0
    test -s "$APP/deployment-evidence/monty-isolation.json"
    test -s "$APP/deployment-evidence/api-probe.json"
    install -o root -g root -m 644 "$BOOTSTRAP/yuki.service" /etc/systemd/system/yuki-bocchi.service
    install -o yuki -g yuki -m 600 "$BOOTSTRAP/gateway.compose.yaml" "$APP/gateway/compose.yaml"
    # Root operates only the owned NapCat container. Bot and Monty owner stay unprivileged.
    systemctl daemon-reload
    systemctl enable --now docker
    systemctl enable --now yuki-bocchi
    for attempt in $(seq 1 90); do
      if curl --fail --silent --max-time 3 http://127.0.0.1:18765/livez >/dev/null; then break; fi
      if ! systemctl is-active --quiet yuki-bocchi; then
        echo 'Yuki failed to start; inspect journalctl -u yuki-bocchi.' >&2; exit 1
      fi
      sleep 2
    done
    curl --fail --silent --max-time 5 http://127.0.0.1:18765/livez >/dev/null
    cd "$APP/gateway"
    stage 'Pulling and starting the fixed NapCat QQ gateway image'
    docker compose --progress plain --project-name yuki-bocchi-gateway up -d
    for attempt in $(seq 1 90); do
      if curl --fail --silent --max-time 3 http://127.0.0.1:6099/ >/dev/null; then break; fi
      sleep 2
    done
    curl --fail --silent --max-time 5 http://127.0.0.1:6099/ >/dev/null
    echo 'Yuki and NapCat are running. Scan the QR code in NapCat to log QQ in.'
    # Remove only the build directory created above, after installation and probes passed.
    if test -d /home/yuki/.cache/yuki-monty-build; then
      rm -rf -- /home/yuki/.cache/yuki-monty-build
    fi
    ;;
  *) echo "Unknown deployment phase" >&2; exit 2 ;;
esac
