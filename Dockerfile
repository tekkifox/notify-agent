# syntax=docker/dockerfile:1

# Deliberately based on the official Node slim image: it is multi-arch and
# carries the glibc/libstdc++ runtime the standalone OpenCode binary expects.
FROM node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da

ARG OPENCODE_VERSION=2.2.1
ARG SUPERCRONIC_VERSION=0.2.51
ARG ORACLEDB_VERSION=26.1.0

# Runtime tools only. supercronic is a static Go binary, so no Cron daemon and
# no root-owned cron spool are needed. python3 is only for the MCP server; the
# Oracle driver below runs in thin mode, so there is no Oracle client and no
# JDK in the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      jq \
      python3 \
      python3-pip \
      tar \
      tzdata \
 && rm -rf /var/lib/apt/lists/*

# Install supercronic, pinned by version and checksum for both supported arches.
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) asset="supercronic-linux-amd64"; sha256="cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123" ;; \
      arm64) asset="supercronic-linux-arm64"; sha256="4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94" ;; \
      *) echo "unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --proto '=https' -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/${asset}"; \
    echo "${sha256}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic; \
    supercronic -version

# python-oracledb, thin mode only: no Oracle client libraries, no JDK. Installed
# into /opt so the system Python stays untouched. The MCP server imports it
# lazily, so the SQL guard and its tests run without a database driver.
RUN python3 -m pip install --no-cache-dir --disable-pip-version-check \
      --target /opt/mcp-python "oracledb==${ORACLEDB_VERSION}"

# Fixed unprivileged identity. The numeric uid is what compose enforces.
RUN groupadd --gid 10001 agent \
 && useradd --uid 10001 --gid 10001 --create-home \
      --home-dir /home/agent --shell /usr/sbin/nologin agent \
 && mkdir -p \
      /home/agent/.local/share/opencode \
      /home/agent/.local/state \
      /home/agent/.config \
      /home/agent/.cache \
      /home/agent/.opencode \
      /opt/opencode \
 && chown -R 10001:10001 /home/agent /opt/opencode

USER 10001:10001

# Official standalone installer, pinned. OpenCode 2 (and later) use the /v2
# installer; the original /install script refuses 2.x releases. Install under
# /opt so the runtime HOME can stay fully read-only except for tmpfs and the
# data volume.
ENV PATH=/opt/opencode/.opencode/bin:$PATH
RUN HOME=/opt/opencode \
      curl -fsSL --proto '=https' https://opencode.ai/v2/install \
      | HOME=/opt/opencode bash -s -- --version "${OPENCODE_VERSION}" --no-modify-path \
 && test "$(opencode --version)" = "opencode v${OPENCODE_VERSION}"

WORKDIR /app

COPY --chown=10001:10001 --chmod=0755 scripts/ /app/scripts/
COPY --chown=10001:10001 --chmod=0755 mcp/ /app/mcp/
COPY --chown=10001:10001 crontab opencode.json /app/
COPY --chown=10001:10001 prompts/ /app/prompts/

ENV HOME=/home/agent \
    OPENCODE_CONFIG=/app/opencode.json \
    OPENCODE_DISABLE_PROJECT_CONFIG=1 \
    PYTHONPATH=/opt/mcp-python \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_DATA_HOME=/home/agent/.local/share \
    XDG_CONFIG_HOME=/home/agent/.config \
    XDG_CACHE_HOME=/home/agent/.cache \
    XDG_STATE_HOME=/home/agent/.local/state \
    TZ=UTC

USER 10001:10001

ENTRYPOINT ["supercronic", "/app/crontab"]
