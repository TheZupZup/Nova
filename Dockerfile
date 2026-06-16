# syntax=docker/dockerfile:1.7

# Nova — self-hosted local AI assistant
#
# Build:  docker build -t nova .
# Run:    docker compose up -d           (recommended — see docker-compose.yml)
#
# This image bundles only the application code and Python dependencies.
# It contains NO secrets, NO database, and NO Ollama models.
#   - Credentials are passed at runtime via environment variables (.env).
#   - All runtime data (nova.db, logs, exports, backups) lives on the
#     /data volume, never in the disposable container layer.
#   - Ollama runs as its own container and is reached over the Docker
#     network via OLLAMA_HOST (see docker-compose.yml).

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Persist all runtime state under /data by default so a stock
# `docker run -v nova-data:/data nova` keeps nothing important in the
# container layer. core/paths.py reads NOVA_DATA_DIR and places nova.db +
# backups/exports/memory-packs/logs underneath it. NOVA_PORT is the port
# uvicorn binds inside the container; both are overridable at runtime.
ENV NOVA_DATA_DIR=/data \
    NOVA_PORT=8000

WORKDIR /app

# Install runtime OS deps. tini gives us proper PID-1 signal handling so
# `docker stop` reaches uvicorn cleanly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application source. The .dockerignore prevents nova.db, .env, .venv,
# __pycache__, tests, and other local artefacts from entering the image.
COPY . .

# Drop privileges. /data is created and chowned here; the entrypoint also
# ensures it exists at runtime so a host bind-mount still works.
RUN useradd --system --create-home --uid 1000 nova \
    && mkdir -p /data \
    && chown -R nova:nova /app /data

COPY --chown=nova:nova docker/entrypoint.sh /usr/local/bin/nova-entrypoint
RUN chmod +x /usr/local/bin/nova-entrypoint

USER nova

EXPOSE 8000

VOLUME ["/data"]

# Liveness check: uvicorn is serving the web UI. Uses only the stdlib so
# no extra packages are needed. A <500 response means the app is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import os,sys,urllib.request; \
url='http://127.0.0.1:'+os.environ.get('NOVA_PORT','8000')+'/'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=3).status < 500 else 1)" \
    || exit 1

ENTRYPOINT ["tini", "--", "/usr/local/bin/nova-entrypoint"]
# Shell form so ${NOVA_PORT} is expanded at runtime; `exec` keeps uvicorn
# as the child tini supervises so signals propagate cleanly.
CMD ["sh", "-c", "exec uvicorn web:app --host 0.0.0.0 --port ${NOVA_PORT:-8000}"]
