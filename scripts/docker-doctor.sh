#!/bin/sh
# Nova — Docker environment doctor (strictly read-only).
#
# Checks that this machine can run the Nova Docker stack and reports the
# current state of the stack's containers and volumes. It NEVER creates,
# starts, stops, or deletes anything — every command it runs is a
# read-only query (command -v, docker info/version/config/ps/volume ls/
# inspect/port). It is always safe to run.
#
# Usage (from a Nova checkout, any working directory):
#     scripts/docker-doctor.sh
#
# Check an alternative stack by listing compose files, colon-separated,
# in the same order you would pass them with -f:
#     NOVA_COMPOSE_FILES=docker-compose.ghcr.yml ./scripts/docker-doctor.sh
#     NOVA_COMPOSE_FILES=docker-compose.ghcr.yml:docker-compose.watchtower.yml \
#         ./scripts/docker-doctor.sh
#
# Exit status: 0 when no check failed (warnings are fine), 1 otherwise.
set -u

PASS=0
WARN=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf '[ OK ] %s\n' "$1"; }
warn() { WARN=$((WARN + 1)); printf '[WARN] %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '[FAIL] %s\n' "$1"; }
info() { printf '       %s\n' "$1"; }

summary_and_exit() {
    printf '\nSummary: %d ok, %d warning(s), %d failure(s).\n' \
        "$PASS" "$WARN" "$FAIL"
    printf '\nData safety reminder:\n'
    info '`docker compose down` keeps your data (containers only).'
    info '`docker compose down -v` DELETES the nova-data and ollama-models'
    info 'volumes: database, memory, conversations, settings, and all'
    info 'downloaded models. There is no undo.'
    if [ "$FAIL" -eq 0 ]; then
        exit 0
    fi
    exit 1
}

# Compose files live at the repo root; resolve names relative to the
# current directory first, then to the checkout containing this script,
# so the doctor works from any working directory. CDPATH is cleared for
# the `cd` calls: an exported CDPATH makes POSIX cd echo the resolved
# path, which would corrupt the command substitution.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

COMPOSE_FILES=${NOVA_COMPOSE_FILES:-docker-compose.yml}

printf 'Nova docker-doctor — read-only diagnostics (changes nothing)\n'
printf 'Compose files: %s\n\n' "$COMPOSE_FILES"

# ── 1. Docker CLI ───────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
    ok "docker CLI found: $(command -v docker)"
else
    fail 'docker CLI not found on PATH.'
    info 'Install Docker Desktop (Windows/macOS/Linux) or Docker Engine:'
    info 'https://docs.docker.com/get-started/get-docker/'
    summary_and_exit
fi

# ── 2. Docker daemon ────────────────────────────────────────────────
DAEMON=0
if docker info >/dev/null 2>&1; then
    ok 'Docker daemon is reachable.'
    DAEMON=1
else
    fail 'Docker daemon is not reachable.'
    info 'Start Docker Desktop, or on a Linux server start the service'
    info '(usually: systemctl start docker) and make sure your user may'
    info 'talk to it (member of the "docker" group, or use sudo).'
fi

# ── 3. Compose v2 plugin ────────────────────────────────────────────
COMPOSE=0
if docker compose version >/dev/null 2>&1; then
    ok "docker compose plugin: $(docker compose version --short 2>/dev/null || printf 'present')"
    COMPOSE=1
else
    fail '`docker compose` (the v2 plugin) is not available.'
    info "Nova needs Compose v2.24+ for its optional-.env stack (shipped"
    info 'with Docker Desktop / Docker Engine since early 2024).'
fi

# ── 4. Compose files exist and render ───────────────────────────────
# Split NOVA_COMPOSE_FILES on ":" and rebuild "$@" as "-f <file>" pairs:
# consume the file names from the front of the parameter list while
# appending resolved flag pairs at the back (arrays are not POSIX).
FILES_OK=1
DISPLAY_FLAGS=''
OLD_IFS=$IFS
IFS=:
set -f
# shellcheck disable=SC2086
set -- $COMPOSE_FILES
set +f
IFS=$OLD_IFS
N=$#
I=0
while [ "$I" -lt "$N" ]; do
    F=$1
    shift
    if [ -f "$F" ]; then
        set -- "$@" -f "$F"
    elif [ -f "$REPO_ROOT/$F" ]; then
        set -- "$@" -f "$REPO_ROOT/$F"
    else
        fail "compose file not found: $F"
        info "Looked in $PWD and $REPO_ROOT."
        FILES_OK=0
    fi
    DISPLAY_FLAGS="$DISPLAY_FLAGS -f $F"
    I=$((I + 1))
done
if [ "$FILES_OK" -eq 1 ] && [ "$#" -gt 0 ]; then
    ok 'all listed compose files exist.'
fi

if [ "$COMPOSE" -eq 1 ] && [ "$FILES_OK" -eq 1 ] && [ "$#" -gt 0 ]; then
    if RENDER_ERRORS=$(docker compose "$@" config 2>&1 >/dev/null); then
        ok 'compose configuration renders cleanly (docker compose config).'
    else
        fail 'compose configuration does not render:'
        printf '%s\n' "$RENDER_ERRORS" | while IFS= read -r LINE; do
            info "$LINE"
        done
    fi
elif [ "$COMPOSE" -eq 0 ]; then
    warn 'skipping `docker compose config` — compose plugin unavailable.'
else
    warn 'skipping `docker compose config` — compose file(s) missing.'
fi

# ── 5. Nova containers ──────────────────────────────────────────────
EXPECTED_CONTAINERS='nova nova-ollama'
case $COMPOSE_FILES in
    *watchtower*) EXPECTED_CONTAINERS="$EXPECTED_CONTAINERS nova-watchtower" ;;
esac

if [ "$DAEMON" -eq 1 ]; then
    ALL_CONTAINERS=$(docker ps --all --format '{{.Names}}' 2>/dev/null || printf '')
    for NAME in $EXPECTED_CONTAINERS; do
        if printf '%s\n' "$ALL_CONTAINERS" | grep -qx "$NAME"; then
            STATE=$(docker inspect --format '{{.State.Status}}' "$NAME" 2>/dev/null || printf 'unknown')
            if [ "$STATE" = 'running' ]; then
                ok "container '$NAME' is running."
            else
                warn "container '$NAME' exists but is not running (state: $STATE)."
            fi
        else
            warn "container '$NAME' not found."
            info "Create the stack with: docker compose${DISPLAY_FLAGS} up -d"
        fi
    done
else
    warn 'skipping container checks — daemon not reachable.'
fi

# ── 6. Persistent volumes ───────────────────────────────────────────
# Compose prefixes named volumes with the project name (e.g. the
# `nova-data` volume usually appears as `nova_nova-data`), so match on
# the suffix.
if [ "$DAEMON" -eq 1 ]; then
    ALL_VOLUMES=$(docker volume ls --format '{{.Name}}' 2>/dev/null || printf '')
    for VOL in nova-data ollama-models; do
        MATCH=$(printf '%s\n' "$ALL_VOLUMES" | grep -E "(^|_)${VOL}\$" | head -n 1)
        if [ -n "$MATCH" ]; then
            ok "volume '$MATCH' exists ($VOL)."
        else
            warn "volume '$VOL' not found — it is created automatically on the first \`up\`."
        fi
    done
else
    warn 'skipping volume checks — daemon not reachable.'
fi

# ── 7. Published port ───────────────────────────────────────────────
if [ "$DAEMON" -eq 1 ]; then
    PORTS=$(docker port nova 2>/dev/null || printf '')
    if [ -n "$PORTS" ]; then
        ok "container 'nova' publishes:"
        printf '%s\n' "$PORTS" | while IFS= read -r LINE; do
            info "$LINE"
        done
        info 'Open Nova in a browser via the host side of the mapping'
        info '(default: http://localhost:8000).'
    else
        warn "no published ports found for container 'nova'."
        info 'Once the stack is up, Nova defaults to http://localhost:8000'
        info '(host port overridable with NOVA_HOST_PORT in .env).'
    fi
else
    info 'Port hint: when the stack is up, Nova listens on'
    info 'http://localhost:8000 by default (NOVA_HOST_PORT overrides it).'
fi

# ── 8. Optional .env (informational) ────────────────────────────────
if [ -f .env ] || [ -f "$REPO_ROOT/.env" ]; then
    info 'An .env file is present — it overrides the stack defaults.'
else
    info 'No .env file — the stack uses its built-in defaults, including'
    info 'the admin/changeme login. That is fine on localhost; set real'
    info 'credentials in .env before exposing Nova to your network.'
fi

summary_and_exit
