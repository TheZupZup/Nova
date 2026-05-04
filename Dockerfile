# syntax=docker/dockerfile:1.7

# Nova — self-hosted local AI assistant
# Build: docker build -t nova .
# Run:   docker run -p 8080:8080 --env-file .env -v nova-data:/data nova
#
# This image bundles only the application code and Python dependencies.
# It contains NO secrets, NO database, and NO Ollama models.
#   - Credentials are passed at runtime via environment variables.
#   - The SQLite database is stored on a host-managed volume (/data).
#   - Ollama runs externally and is reached over the network via OLLAMA_HOST.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

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

# Drop privileges. The /data volume is chowned by the entrypoint at runtime
# so a host bind-mount with arbitrary ownership still works.
RUN useradd --system --create-home --uid 1000 nova \
    && mkdir -p /data \
    && chown -R nova:nova /app /data

COPY --chown=nova:nova docker/entrypoint.sh /usr/local/bin/nova-entrypoint
RUN chmod +x /usr/local/bin/nova-entrypoint

USER nova

EXPOSE 8080

VOLUME ["/data"]

ENTRYPOINT ["/usr/sbin/tini", "--", "/usr/local/bin/nova-entrypoint"]
CMD ["uvicorn", "web:app", "--host", "0.0.0.0", "--port", "8080"]
