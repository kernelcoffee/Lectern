# Production image: one container that builds the React SPA and serves it from
# FastAPI (same origin, one port). Build from the repo root:
#
#   docker build -t lectern .
#   docker run -p 8000:8000 -p 25565:25565 -v lectern_data:/data lectern
#
# or via docker-compose.yml. Lectern runs as user lectern (uid 1000, gid 1000);
# scripts/container-smoke.sh checks a built image with real containers.

# --- stage 1: build the SPA -------------------------------------------------
FROM node:24-alpine AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build            # → /app/dist

# --- stage 2: backend + built SPA -------------------------------------------
FROM python:3.14-slim
WORKDIR /app

# curl/ca-certificates for outbound HTTPS (Mojang/Fabric/Modrinth/Adoptium).
# Lectern auto-downloads the per-version JRE at runtime, so no JRE is baked in.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The unprivileged user Lectern and every Minecraft server run as. The id is
# fixed at 1000 (free in the base image), so a bind-mounted data folder shows
# up as owned by the host's first user. A real home directory, because Java
# and installers resolve the home from the user database and write there.
RUN groupadd --gid 1000 lectern \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin lectern

# Install the backend. Copy just the packaging inputs first so the dependency
# layer caches across source-only changes.
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/lectern ./lectern
RUN pip install --no-cache-dir .
# uvicorn imports lectern from /app (its working directory), which lectern
# cannot write to: compile it here, or every start would compile it again.
RUN python -m compileall -q lectern

# The built SPA, served by FastAPI (see main.py / LECTERN_STATIC_DIR).
COPY --from=frontend /app/dist ./static

ENV LECTERN_DATA=/data \
    LECTERN_STATIC_DIR=/app/static \
    LECTERN_HOST=0.0.0.0 \
    LECTERN_PORT=8000

# /data belongs to lectern in the image itself: Docker and podman give a new
# named or anonymous volume the owner of the image's folder, so a new install
# starts with nothing to hand over and also works with user: "1000:1000".
# It must come before VOLUME, which discards later changes to /data.
RUN install -d -o 1000 -g 1000 /data
VOLUME ["/data"]
EXPOSE 8000 25565

# No USER line on purpose: the container starts as root only so the startup
# script can hand an existing root-owned /data over to lectern (installs from
# before this image ran as root); it then drops root for good and execs the
# CMD below as lectern, so uvicorn stays process 1. See the script for details.
COPY --chmod=0755 scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]

# Docker marks the container healthy while the API answers. The start period
# covers Lectern's startup, including a first-start handover of a small /data.
# The engine starts the probe as root (no USER line), so the probe is a script
# that drops to lectern before it calls the API with curl; see the script for
# the address and for why curl gives up before the 5 s timeout.
COPY --chmod=0755 scripts/docker-healthcheck.sh /usr/local/bin/docker-healthcheck.sh
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["docker-healthcheck.sh"]

# On SIGTERM uvicorn waits for open requests before it runs Lectern's shutdown,
# which stops the Minecraft servers. The timeout keeps one long request (a large
# backup download, say) from using up the container's stop grace period: after
# 15 s uvicorn cancels what is still open and the servers get their stop command.
CMD ["uvicorn", "lectern.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "15"]
