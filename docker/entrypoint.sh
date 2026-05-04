#!/bin/sh
# Nova container entrypoint.
#
# Nova's code uses a relative DB_PATH ("nova.db" in the working directory).
# To persist the database across container rebuilds without modifying app
# code, we mount a Docker volume at /data and symlink /app/nova.db to it.
# The first time the app writes to nova.db, the file is created inside the
# volume and survives container replacement.
set -eu

DATA_DIR="${NOVA_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

# If a legacy nova.db was baked into the image (it shouldn't be, but be safe)
# and the volume is empty, migrate it once. Otherwise the volume copy wins.
if [ -f /app/nova.db ] && [ ! -L /app/nova.db ] && [ ! -e "$DATA_DIR/nova.db" ]; then
    mv /app/nova.db "$DATA_DIR/nova.db"
fi

# Always replace /app/nova.db with a symlink into the volume so the running
# app reads/writes through to persistent storage.
rm -f /app/nova.db
ln -s "$DATA_DIR/nova.db" /app/nova.db

exec "$@"
