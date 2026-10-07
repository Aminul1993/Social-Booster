# syntax=docker/dockerfile:1.7
# -----------------------------------------------------------------------------
# AI Marketing Post Builder - production image
#   * multi-stage: build tools never reach the runtime image
#   * small: images are described by an online vision model, no ML runtime
#   * non-root user, read-only friendly, health-checked
# -----------------------------------------------------------------------------
ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- builder ----
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

RUN python -m venv /opt/venv

WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# ---------------------------------------------------------------- runtime ----
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="marketing-ai-builder" \
      org.opencontainers.image.description="AI-powered social post builder (FastAPI, HTMX, Ollama Cloud, Buffer)" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.licenses="MIT"

# MALLOC_MMAP_THRESHOLD_: a fixed threshold stops glibc from raising it after
# Pillow frees its 16 MB blocks, so decoded images go back to the OS instead of
# staying in per-thread arenas (~200 MB RSS kept per worker after a large upload).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ENVIRONMENT=production \
    LOG_FORMAT=json \
    HOST=0.0.0.0 \
    PORT=8000 \
    WEB_CONCURRENCY=2 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    MALLOC_MMAP_THRESHOLD_=1048576

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app app ./app
COPY --chown=app:app services ./services
COPY --chown=app:app templates ./templates
COPY --chown=app:app static ./static
COPY --chown=app:app gunicorn.conf.py pyproject.toml ./

RUN mkdir -p /app/uploads /app/data \
 && chown -R app:app /app/uploads /app/data

USER app
EXPOSE 8000
VOLUME ["/app/uploads", "/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]
