# CoreAI Protocol Suite — Dockerfile
#
# Builds a lean runtime image for the FastAPI service (api/server.py).
# Deliberately does NOT install requirements.txt as-is — that file also
# pulls in the project's offline ML/data-science extras (torch, tensorflow,
# jax, opencv, etc.) which aren't used by the server and bloat the image
# by several GB. See requirements-docker.txt for the trimmed dependency set.
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

# Build deps for psycopg2 / asyncpg / bcrypt wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime image
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Runtime-only system deps: libpq5 for psycopg2, curl for the healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin coreai

COPY --from=builder /opt/venv /opt/venv

# App code
COPY --chown=coreai:coreai api/ ./api/
COPY --chown=coreai:coreai coreai/ ./coreai/
COPY --chown=coreai:coreai providers/ ./providers/
COPY --chown=coreai:coreai agents/ ./agents/
COPY --chown=coreai:coreai middleware/ ./middleware/
COPY --chown=coreai:coreai protocols/ ./protocols/
COPY --chown=coreai:coreai runtime/ ./runtime/
COPY --chown=coreai:coreai database/ ./database/
COPY --chown=coreai:coreai neural/ ./neural/
COPY --chown=coreai:coreai utils/ ./utils/
COPY --chown=coreai:coreai config/ ./config/
COPY --chown=coreai:coreai migrations/ ./migrations/

USER coreai

EXPOSE 8743

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8743/health || exit 1

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8743"]
