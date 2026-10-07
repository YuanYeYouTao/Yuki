# syntax=docker/dockerfile:1.7
FROM node:24-alpine AS webui
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

FROM rust:1.96.0-slim AS rust
FROM python:3.12-slim AS monty
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=uv /uv /uvx /bin/
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup \
    PATH="/usr/local/cargo/bin:$PATH" CARGO_BUILD_JOBS=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY scripts/build_monty_distribution.sh /build/scripts/
COPY vendor/patches /build/vendor/patches
COPY vendor/monty/launcher.c /build/vendor/monty/launcher.c
RUN bash /build/scripts/build_monty_distribution.sh /build/distribution /usr/local/bin/python build
COPY scripts/export_monty_notices.py /build/scripts/
RUN bash /build/scripts/build_monty_distribution.sh /build/distribution /usr/local/bin/python notices

FROM python:3.12-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=webui /app/src/qq_ai_bot/webui/assets ./src/qq_ai_bot/webui/assets
RUN uv sync --frozen --no-dev --no-editable
FROM builder AS builder-code
# uv sync removes packages outside uv.lock. Install the wheel from the pinned
# source build only after the final sync; no index or embedded worker discovery.
COPY --from=monty /build/distribution/wheels /opt/monty-wheels
RUN uv pip install --python /app/.venv/bin/python --no-deps /opt/monty-wheels/*.whl

FROM python:3.12-slim AS runtime-base
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei ffmpeg bubblewrap \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --home-dir /app --no-create-home bot
WORKDIR /app
COPY --from=builder --chown=bot:bot /app/.venv /app/.venv
COPY --chown=bot:bot alembic.ini ./
COPY --chown=bot:bot migrations ./migrations
COPY --chown=bot:bot scripts ./scripts
COPY --chown=bot:bot config/persona.md ./config/persona.md
RUN chmod +x /app/scripts/start.sh \
    && mkdir -p /app/data /app/napcat-config /app/snowluma-data/config
ARG YUKI_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Yuki QQ Bot" \
      org.opencontainers.image.source="https://github.com/YuanYeYouTao/Yuki-QQbot" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${YUKI_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
EXPOSE 8080
ENTRYPOINT ["/app/scripts/start.sh"]

FROM runtime-base AS direct
ENV CODE_MODE_ENABLED=false

FROM runtime-base AS codemode
ENV CODE_MODE_ENABLED=true
COPY --from=builder-code --chown=bot:bot /app/.venv /app/.venv
COPY --from=monty /build/distribution/monty/target/release/monty /opt/yuki-monty/monty
COPY --from=monty /build/distribution/monty-isolated /opt/yuki-monty/monty-isolated
COPY --from=monty /build/distribution/artifacts.json /opt/yuki-monty/artifacts.json
COPY --from=monty /build/distribution/THIRD_PARTY_NOTICES.json /opt/yuki-monty/THIRD_PARTY_NOTICES.json
COPY LICENSE /opt/yuki-monty/licenses/Yuki-LICENSE
COPY vendor/monty/LICENSE /opt/yuki-monty/licenses/Monty-LICENSE
COPY vendor/monty/TYPESHED-LICENSE /opt/yuki-monty/licenses/TYPESHED-LICENSE

FROM direct AS runtime
