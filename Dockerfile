# Production image: one container that builds the React SPA and serves it from
# FastAPI (same origin, one port). Build from the repo root:
#
#   docker build -t lectern .
#   docker run -p 8000:8000 -p 25565:25565 -v lectern_data:/data lectern
#
# or via docker-compose.prod.yml.

# --- stage 1: build the SPA -------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build            # → /app/dist

# --- stage 2: backend + built SPA -------------------------------------------
FROM python:3.12-slim
WORKDIR /app

# curl/ca-certificates for outbound HTTPS (Mojang/Fabric/Modrinth/Adoptium).
# Lectern auto-downloads the per-version JRE at runtime, so no JRE is baked in.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the backend. Copy just the packaging inputs first so the dependency
# layer caches across source-only changes.
COPY README.md /README.md
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/lectern ./lectern
RUN pip install --no-cache-dir .

# The built SPA, served by FastAPI (see main.py / LECTERN_STATIC_DIR).
COPY --from=frontend /app/dist ./static

ENV LECTERN_DATA=/data \
    LECTERN_STATIC_DIR=/app/static \
    LECTERN_HOST=0.0.0.0 \
    LECTERN_PORT=8000

VOLUME ["/data"]
EXPOSE 8000 25565

CMD ["uvicorn", "lectern.main:app", "--host", "0.0.0.0", "--port", "8000"]
