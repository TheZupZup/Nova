#!/bin/sh
# Scaffold a Nova Portable Workspace.
#
# Usage:
#     scripts/init-portable-workspace.sh <parent>
#
# Example:
#     scripts/init-portable-workspace.sh /mnt/fastdata/NovaPortable
#
# This is a thin shell wrapper around ``python -m core.paths
# init-workspace <parent>``. It creates the workspace directory layout
# (data/, logs/, backups/, config/, scripts/, app/) and writes a single
# example env file at config/nova.env.example pointing at the
# workspace's data/ directory. It is safe to run repeatedly — existing
# files are never overwritten.
#
# No data is copied. The Nova checkout is NOT cloned into app/. See
# docs/portable-workspace.md for the full walkthrough, including how
# to wire the workspace to systemd.
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <parent>" >&2
    echo "" >&2
    echo "Scaffold a Nova Portable Workspace under <parent>." >&2
    echo "See docs/portable-workspace.md for the full walkthrough." >&2
    exit 2
fi

PARENT="$1"

# Locate the Nova checkout so we can run the helper module without
# requiring the user to ``cd`` first.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON="${PYTHON:-python3}"
cd "$REPO_ROOT"
exec "$PYTHON" -m core.paths init-workspace "$PARENT"
