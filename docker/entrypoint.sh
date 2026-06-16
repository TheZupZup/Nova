#!/bin/sh
# Nova container entrypoint.
#
# Responsibilities (all idempotent — safe to re-run on every start):
#   1. Make sure the persistent data directory exists and is usable.
#   2. Migrate a legacy baked-in nova.db into the volume, once, if present.
#   3. Provide a stable per-install JWT secret when the operator did not
#      set one, so logins survive restarts without shipping a known key.
#
# All real runtime state (nova.db, backups/, exports/, memory-packs/,
# logs/) is written under $NOVA_DATA_DIR by the application itself
# (see core/paths.py), which is why this script does NOT symlink nova.db
# anymore: NOVA_DATA_DIR=/data makes the app read/write the volume path
# directly.
set -eu

DATA_DIR="${NOVA_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

# One-time migration: if an older image baked nova.db into /app and the
# volume has no database yet, move it across so history is not lost. New
# images never bake nova.db (see .dockerignore), so this is a no-op for
# fresh installs.
if [ -f /app/nova.db ] && [ ! -L /app/nova.db ] && [ ! -e "$DATA_DIR/nova.db" ]; then
    mv /app/nova.db "$DATA_DIR/nova.db"
fi

# Persist a per-install JWT signing secret. We deliberately do NOT ship a
# default NOVA_SECRET_KEY: a known key would let anyone on the network
# forge session tokens. When the operator has not supplied one (unset or
# empty), generate a random key once and keep it on the data volume so
# sessions remain valid across restarts and rebuilds.
if [ -z "${NOVA_SECRET_KEY:-}" ]; then
    KEY_FILE="$DATA_DIR/secret_key"
    if [ ! -f "$KEY_FILE" ]; then
        ( umask 077; python -c "import secrets; print(secrets.token_hex(32))" > "$KEY_FILE" )
    fi
    NOVA_SECRET_KEY="$(cat "$KEY_FILE")"
    export NOVA_SECRET_KEY
fi

exec "$@"
