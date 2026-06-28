# syntax=docker/dockerfile:1.6
# gemini2api v1.1 — production image
# Build:  docker build -t gemini2api:1.1 .
# Run:    docker run --rm -p 1800:1800 -v "$(pwd)/.env:/app/.env:ro" -v "$(pwd)/logs:/app/logs" gemini2api:1.1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=1800

# curl is handy for HEALTHCHECK; tini reaps zombies on PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy the application source.
COPY server.py adapter.py account_pool.py auth.py rate_limit.py logger.py har_parser.py config_tool.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY start.bat ./
COPY pytest.ini ./

# A non-root user is a hardening best practice; the app reads .env
# from the working directory which is mapped in at runtime.
RUN useradd --create-home --shell /bin/bash gemini \
    && chown -R gemini:gemini /app
USER gemini

EXPOSE 1800

# HEALTHCHECK pings the unauthenticated /health endpoint so Docker /
# compose can detect a crashed server.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec python -u server.py ${PORT}"]
