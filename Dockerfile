# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.31 AS uv
# The sandbox Manager service drives the Docker engine through its mounted socket.
FROM docker:29.5.2-cli AS docker-cli

FROM rust:1.96.0-slim AS rust
FROM python:3.12-slim-trixie AS monty
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=uv /uv /uvx /bin/
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup \
    PATH="/usr/local/cargo/bin:$PATH" CARGO_BUILD_JOBS=1
# Optional native build tuning; omitted values keep Monty's release profile.
ARG CARGO_PROFILE_RELEASE_LTO
ARG CARGO_PROFILE_RELEASE_CODEGEN_UNITS
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY scripts/build_monty_distribution.sh /build/scripts/
COPY vendor/patches /build/vendor/patches
COPY vendor/monty/launcher.c /build/vendor/monty/launcher.c
RUN bash /build/scripts/build_monty_distribution.sh /build/distribution /usr/local/bin/python build
COPY scripts/export_monty_notices.py /build/scripts/
RUN bash /build/scripts/build_monty_distribution.sh /build/distribution /usr/local/bin/python notices

FROM python:3.12-slim-trixie AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
RUN /app/.venv/bin/python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute(\"CREATE VIRTUAL TABLE f USING fts5(tokens, content='', contentless_delete=1, detail=none, tokenize='ascii')\"); c.execute(\"INSERT INTO f(rowid,tokens) VALUES(1,'capability')\"); c.execute('DELETE FROM f WHERE rowid=1')"
COPY src ./src
COPY hatch_build.py ./
COPY scripts/build_vec1.py ./scripts/
COPY vendor/sqlite-vec1 ./vendor/sqlite-vec1
RUN uv sync --frozen --no-dev --no-editable
FROM builder AS builder-code
# uv sync removes packages outside uv.lock. Install the wheel from the pinned
# source build only after the final sync; no index or embedded worker discovery.
COPY --from=monty /build/distribution/wheels /opt/monty-wheels
RUN uv pip install --python /app/.venv/bin/python --no-deps /opt/monty-wheels/*.whl

FROM python:3.12-slim-trixie AS runtime-base
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei ffmpeg bubblewrap iptables \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --home-dir /app --no-create-home bot
WORKDIR /app
COPY --from=builder --chown=bot:bot /app/.venv /app/.venv
COPY --chown=bot:bot LICENSE ./
COPY --chown=bot:bot alembic.ini ./
COPY --chown=bot:bot migrations ./migrations
COPY --chown=bot:bot scripts ./scripts
COPY --chown=bot:bot config/SOUL.md ./config/SOUL.md
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
# Files the Manager mounts into the environment, at their repository paths
# (yuki.sandbox.persistent.ENVIRONMENT_FILES).
ENV YUKI_ENVIRONMENT_SOURCE=/app/environment-source
COPY src/yuki/sandbox/environment_supervisor.py src/yuki/sandbox/environment_programs.py \
     src/yuki/sandbox/desktop_service.py src/yuki/sandbox/yuki_computer.py \
     /app/environment-source/src/yuki/sandbox/
COPY src/yuki/emoji/sticker_index.py src/yuki/emoji/media.py \
     /app/environment-source/src/yuki/emoji/
COPY packages/yuki-agent-connector/src/yuki_agent_connector/cdp.py \
     /app/environment-source/packages/yuki-agent-connector/src/yuki_agent_connector/
COPY deploy/sandbox/yuki-computer.sh /app/environment-source/deploy/sandbox/
RUN chmod +x /app/scripts/start.sh \
    && mkdir -p /app/data /app/character /app/snowluma-data/config
ARG YUKI_VERSION=dev
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Yuki" \
      org.opencontainers.image.source="https://github.com/YuanYeYouTao/Yuki" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
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
