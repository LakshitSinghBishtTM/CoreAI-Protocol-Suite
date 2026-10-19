# CoreAI Protocol Suite — Dockerfile
#
# Builds a lean runtime image for the FastAPI service (api/server.py).
# Installs requirements.txt (runtime dependencies only; no dev tools).
#
# Build:
#   docker build -t coreai:latest .
# Run:
#   docker run -d -p 8743:8743 --env-file .env coreai:latest

# ---------------------------------------------------------------------------
# Stage 1: build dependencies into a virtualenv
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

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
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# No apt packages: the healthcheck uses Python, which keeps the image small.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin coreai

COPY --from=builder /opt/venv /opt/venv

# App code
COPY --chown=coreai:coreai api/ ./api/
COPY --chown=coreai:coreai coreai/ ./coreai/
COPY --chown=coreai:coreai providers/ ./providers/
COPY --chown=coreai:coreai agents/ ./agents/
COPY --chown=coreai:coreai middleware/ ./middleware/
COPY --chown=coreai:coreai protocols/ ./protocols/
COPY --chown=coreai:coreai database/ ./database/
COPY --chown=coreai:coreai neural/ ./neural/
COPY --chown=coreai:coreai utils/ ./utils/
COPY --chown=coreai:coreai config/ ./config/
COPY --chown=coreai:coreai migrations/ ./migrations/

USER coreai

EXPOSE 8743

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8743/health', timeout=4)" || exit 1

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8743"]
