# Running Nova with Docker

Nova ships with a `Dockerfile` and `docker-compose.yml` for self-hosted deployments. This is a **local / self-hosted** setup: there is no cloud component, no remote sync, and no auto-deploy. You run Nova on a machine you control, talking to an Ollama you control.

> **Want one parent folder per install?** If you'd rather use **host
> bind mounts** under a single workspace directory (so the data,
> config, logs, and backups all live next to each other and move as
> one unit), see [`docs/portable-workspace.md`](portable-workspace.md)
> and the ready-to-edit
> [`deploy/docker/docker-compose.portable.yml`](../deploy/docker/docker-compose.portable.yml).
> The quickstart on this page uses a named Docker volume instead;
> both are supported.

> **Warning.** The container exposes Nova's web UI on the network. Do not publish port 8080 to the public internet without a reverse proxy and TLS in front of it. Defaults in `.env.example` are intentionally weak; change them before the first start.

## What ends up where

| Component | Location |
|---|---|
| Application code | Inside the image at `/app` |
| SQLite database (`nova.db`) | Docker volume `nova-data`, mounted at `/data` |
| Credentials | `.env` on the host, passed in via `docker compose` |
| Ollama models | **Outside the container.** Nova calls Ollama over the network. |

The image contains no secrets and no database. Pulling a new version cannot overwrite your data.

## Prerequisites

- Docker Engine 24+ and the `docker compose` plugin
- An Ollama instance reachable from the container (see [Connecting to Ollama](#connecting-to-ollama))
- The Ollama models referenced in `config.py` already pulled on that Ollama instance

## First run

```bash
git clone https://github.com/TheZupZup/Nova.git
cd Nova

cp .env.example .env
# Edit .env and set:
#   NOVA_USERNAME       — your login
#   NOVA_PASSWORD       — your password (change before first start)
#   NOVA_SECRET_KEY     — a long random string used to sign JWTs
#   OLLAMA_HOST         — see "Connecting to Ollama" below
# Optionally set NOVA_CHANNEL=stable|beta|alpha.

docker compose up -d
```

Nova is now reachable at `http://<host>:8080`. Log in with the credentials you set in `.env`.

To follow logs:

```bash
docker compose logs -f nova
```

## Updating

The image is published to GHCR as `ghcr.io/thezupzup/nova:latest`. To update:

```bash
docker compose pull
docker compose up -d
```

This replaces the container but leaves the `nova-data` volume — and therefore `nova.db` — untouched.

If you'd rather build from your local checkout, edit `docker-compose.yml`: comment the `image:` line and uncomment `build: .`, then:

```bash
docker compose build
docker compose up -d
```

## Where data is stored

`nova.db` lives in the named Docker volume `nova-data`, mounted inside the container at `/data`. The application's relative `nova.db` path is symlinked into that volume by the entrypoint, so app code sees it at the usual location without any code change.

To find the path on disk:

```bash
docker volume inspect nova-data --format '{{ .Mountpoint }}'
```

On a default Linux Docker install this is typically `/var/lib/docker/volumes/nova-data/_data`.

### Backing up `nova.db`

Stop the container before copying the file to avoid catching it mid-write:

```bash
docker compose stop nova
docker run --rm -v nova-data:/data -v "$PWD":/backup alpine \
    cp /data/nova.db /backup/nova.db.$(date +%Y%m%d-%H%M%S)
docker compose start nova
```

Restore by copying a backup back into the volume the same way before starting the container.

> **Backup note.** `nova.db` is a single SQLite file. Copy it while the app is stopped, or use SQLite's `.backup` command against a live DB. Don't copy it under load — you may capture an inconsistent snapshot.

## Connecting to Ollama

Nova does **not** bundle Ollama. The container reaches Ollama over the network via `OLLAMA_HOST`. Three common setups:

### 1. Ollama on the same host as Docker (Linux)

Use the `host.docker.internal` alias wired up by the `extra_hosts` entry in `docker-compose.yml`:

```env
OLLAMA_HOST=http://host.docker.internal:11434
```

Make sure Ollama is listening on an interface the container can reach. By default `ollama serve` binds to `127.0.0.1`, which is **not** reachable from inside the container. Bind to all interfaces:

```bash
OLLAMA_HOST=0.0.0.0 ollama serve
```

(or set `Environment=OLLAMA_HOST=0.0.0.0` in your Ollama systemd unit).

### 2. Ollama on the same host (Docker Desktop, macOS / Windows)

`host.docker.internal` resolves natively. The same `OLLAMA_HOST=http://host.docker.internal:11434` works without extra setup.

### 3. Ollama on a different machine

Point `OLLAMA_HOST` at that machine's LAN IP:

```env
OLLAMA_HOST=http://192.168.1.50:11434
```

The other machine must be running Ollama with `OLLAMA_HOST=0.0.0.0` so the port is reachable across the network.

## Changing the password before first start

Always set `NOVA_PASSWORD` in `.env` before the first `docker compose up`. The very first start creates the admin record from these environment variables.

If you've already started the container with the default password and want to rotate it before exposing Nova:

```bash
docker compose down
# Edit .env, set the new NOVA_PASSWORD
docker volume rm nova-data    # destructive: wipes nova.db, including conversations
docker compose up -d
```

If you don't want to wipe the database, change the password from inside Nova's settings UI after logging in instead.

## Environment variables forwarded to the container

| Variable | Required | Purpose |
|---|---|---|
| `NOVA_USERNAME` | yes | Admin login |
| `NOVA_PASSWORD` | yes | Admin password (change before first start) |
| `NOVA_SECRET_KEY` | yes | JWT signing secret — long random string |
| `OLLAMA_HOST` | yes | URL of your Ollama instance |
| `NOVA_CHANNEL` | no | `stable` (default), `beta`, or `alpha` |
| `NOVA_BRANCH` | no | Display-only branch label |
| `NOVA_ADMIN_UI` | no | `true` to expose admin-only UI controls |
| `NOVA_AUTO_WEB_LEARNING` | no | `true` to enable background RSS learning |

The `alpha` channel additionally requires `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `NOVA_ALPHA_ALLOWED_USERS`. See `.env.example` for details — add them under `environment:` in `docker-compose.yml` if you use that channel.

## What this deployment does NOT do

- No bundled Ollama. Models are not downloaded by the image.
- No automatic updates. You run `docker compose pull` when you want to update.
- No remote deploy hook. Nothing in this repo will push to your server for you.
- No telemetry, cloud sync, or third-party calls beyond Nova's existing optional integrations.

## Troubleshooting

**`Cannot connect to Ollama`**
Check `OLLAMA_HOST` from inside the container:
```bash
docker compose exec nova python -c "import os, httpx; print(httpx.get(os.environ['OLLAMA_HOST']+'/api/tags').text)"
```
If this fails, Ollama is either not running, not bound to a reachable interface, or blocked by a firewall.

**`docker compose pull` says credentials are missing**
The compose file requires `NOVA_USERNAME`, `NOVA_PASSWORD`, and `NOVA_SECRET_KEY` to be set. They live in `.env` next to `docker-compose.yml`. They are not needed for `docker compose pull` itself, but `docker compose up` validates them.

**Want to start over from a clean state**
```bash
docker compose down -v   # removes the nova-data volume — DESTRUCTIVE
docker compose up -d
```
