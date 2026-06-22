# syntax=docker/dockerfile:1
#
# Nalanda -- the `serve` webhook daemon, packaged for a Linux host (NAS, plain Docker, ...).
#
# Design:
#   - Rootless: the daemon runs as a baked non-root user (uid/gid 1000), never as root.
#   - /config layout: one mounted directory holds config.yml, the state file, and the
#     co-located .env. It is the only path Nalanda needs to write, so the rest of the
#     filesystem can be read-only (see docker/compose.example.yml).
#   - tini is PID 1 so `docker stop` shuts the daemon down promptly.
#
# Build:  docker build -t nalanda .
# Run:    see docker/compose.example.yml (the recommended way)

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# - PYTHONUNBUFFERED: logs stream straight to `docker logs`.
# - PYTHONDONTWRITEBYTECODE: no .pyc files, so the app filesystem can stay read-only.
# - HOME / XDG_CACHE_HOME = /tmp: keep any stray cache writes on the tmpfs, not a read-only path.
# - NALANDA_*: container defaults. A real env var (e.g. from /config/.env) overrides them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp \
    NALANDA_HOST=0.0.0.0 \
    NALANDA_PORT=8842 \
    NALANDA_CONFIG=/config/config.yml \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# tini: PID 1 for clean signals / fast `docker stop`. tzdata: lets the TZ env var select a real
# timezone so the daemon's cron fires on your local wall-clock (glibc reads TZ directly).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tini tzdata \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, in their own cached layer (re-resolved only when the lockfile changes).
# --frozen: honour uv.lock exactly. --no-dev: skip test deps. --no-install-project: deps only
# (Nalanda has no build system; it's run from source as `python -m nalanda`).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code, including the bundled first-run templates (nalanda/templates/).
COPY nalanda ./nalanda

# Run as a baked non-root user. /config is the writable mount; hand it (and /app) to uid 1000.
RUN mkdir -p /config && chown -R 1000:1000 /config /app
USER 1000:1000

EXPOSE 8842

# Liveness via the daemon's own /health route, using the bundled Python (no curl). Honors
# NALANDA_PORT so it follows a custom port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('NALANDA_PORT','8842')+'/health', timeout=3).status==200 else 1)"]

# tini as PID 1, then `python -m nalanda` from the venv. CMD is the subcommand, so
# `docker run <image>` defaults to the daemon while `docker run <image> run "<name>"` works too.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/.venv/bin/python", "-m", "nalanda"]
CMD ["serve"]
