# CoreAI Protocol Suite — Dockerfile
#
# Builds a lean runtime image for the FastAPI service (api/server.py).
# Installs requirements.txt (runtime dependencies only; no dev tools).
#
# Build:
#   docker build -t coreai:latest .
# Run:
#   docker run -d -p 6969:6969 --env-file .env coreai:latest

# ---------------------------------------------------------------------------
# Stage 1: build dependencies into a virtualenv
# ---------------------------------------------------------------------------
FROM python:3.11.9-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime image
# ---------------------------------------------------------------------------
FROM python:3.11.9-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

WORKDIR /app

# No apt packages: the healthcheck uses Python, which keeps the image small.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin coreai
RUN mkdir /data && chown coreai:coreai /data
ENV CAIPS_KEYSTORE=/data/provider_keys.json

COPY --from=builder /opt/venv /opt/venv

# App code (src/ layout; config/ and migrations/ stay beside the app)
COPY --chown=coreai:coreai src/ ./src/
COPY --chown=coreai:coreai config/ ./config/
COPY --chown=coreai:coreai migrations/ ./migrations/

USER coreai

EXPOSE 6969

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:6969/health', timeout=4)" || exit 1

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "6969"]
