#!/usr/bin/env sh
set -eu

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

ok() {
  printf 'OK: %s\n' "$*"
}

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

section() {
  printf '\n== %s ==\n' "$*"
}

compose_files=${NOVA_COMPOSE_FILES:-docker-compose.yml}

# Accept either ':' or ',' separated file lists so users can run, for example:
#   NOVA_COMPOSE_FILES=docker-compose.ghcr.yml:docker-compose.watchtower.yml ./scripts/docker-doctor.sh
old_ifs=$IFS
IFS=':, '
set -- $compose_files
IFS=$old_ifs

compose_args=""
for file in "$@"; do
  [ -n "$file" ] || continue
  if [ ! -f "$file" ]; then
    fail "compose file not found: $file"
  fi
  compose_args="$compose_args -f $file"
done

if [ -z "$compose_args" ]; then
  fail "no compose files selected"
fi

compose() {
  # shellcheck disable=SC2086
  docker compose $compose_args "$@"
}

section "Docker client"
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH"
docker --version || fail "docker command exists but failed"
ok "docker command is available"

section "Docker daemon"
if docker info >/dev/null 2>&1; then
  ok "daemon is reachable"
else
  fail "daemon is not reachable. Start Docker Desktop / Docker Engine first."
fi

section "Docker Compose"
if docker compose version >/dev/null 2>&1; then
  docker compose version
  ok "compose plugin is available"
else
  fail "docker compose plugin is not available"
fi

section "Selected compose files"
printf '%s\n' "$compose_files"

section "Compose config"
compose config >/tmp/nova-compose-rendered.yml
ok "compose files render successfully"

if grep -q 'required: false' /tmp/nova-compose-rendered.yml 2>/dev/null; then
  ok ".env is optional in the rendered config"
else
  warn "optional .env marker was not found in rendered config; this may be normal on older compose output formats"
fi

section "Published ports"
if command -v awk >/dev/null 2>&1; then
  if compose ps 2>/dev/null | awk 'NR == 1 || /nova/ { print }'; then
    :
  else
    warn "could not read compose status yet; stack may not be started"
  fi
else
  compose ps || warn "could not read compose status yet; stack may not be started"
fi

section "Containers"
if docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | grep -E '(^NAMES|nova)' ; then
  ok "Nova-related containers are visible"
else
  warn "no running Nova containers found. Start with: docker compose up -d"
fi

section "Volumes"
if docker volume ls --format '{{.Name}}' | grep -E '(^|_)nova-data$|^nova-data$' >/dev/null 2>&1; then
  ok "nova-data volume exists"
else
  warn "nova-data volume not found yet; it is created on first start"
fi

if docker volume ls --format '{{.Name}}' | grep -E '(^|_)ollama-models$|^ollama-models$' >/dev/null 2>&1; then
  ok "ollama-models volume exists"
else
  warn "ollama-models volume not found yet; it is created on first start"
fi

section "HTTP hint"
if compose ps 2>/dev/null | grep -q '8000'; then
  ok "Nova should be reachable at http://localhost:8000 unless NOVA_HOST_PORT overrides it"
else
  warn "port 8000 was not found in compose ps output. If Nova is running, check NOVA_HOST_PORT and Docker Desktop's Ports column."
fi

section "Data-loss reminder"
printf '%s\n' "Safe reset: docker compose down && docker compose up -d"
printf '%s\n' "Destructive reset: docker compose down -v  # deletes nova-data and ollama-models"

section "Done"
ok "docker diagnostics completed"
