# syntax=docker/dockerfile:1.7

# ── 1. Web app ─────────────────────────────────────────────────────────────────────────────
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

# ── 2. Runtime: API and worker share this image ────────────────────────────────────────────
FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEDIA_STORAGE_DIR=/app/uploads

# FFmpeg compresses videos above MAX_VIDEO_MB before they go to Gemini.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY --from=web /web/dist ./frontend/dist

RUN useradd --system --uid 10001 --home-dir /app mediaflow \
    && mkdir -p /app/uploads \
    && chown -R mediaflow:mediaflow /app/uploads
USER mediaflow

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2).status != 200)"
# Open live-update streams (app/api/live.py) don't hold up a restart: after 5 s they are cut,
# and browsers reconnect to the new process on their own.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--timeout-graceful-shutdown", "5"]
