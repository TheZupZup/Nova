# Running Nova with Docker

Nova ships a complete Docker Compose stack so you can run it **without
installing Python, Ollama, or any dependencies on the host**. Everything
runs in containers:

| Container | Image | Role |
|---|---|---|
| `nova` | built from this repo's `Dockerfile`, **or** pulled from GHCR | Nova backend + web UI |
| `nova-ollama` | `ollama/ollama` | local model server |

There are two ways to get the `nova` image, and both use the same volumes
and the same operations below:

- **Build from source** with `docker-compose.yml` — the default developer
  workflow ([First-time setup](#first-time-setup)).
- **Pull the prebuilt image** from the GitHub Container Registry with
  `docker-compose.ghcr.yml` — deploy without cloning or building the repo
  ([Run the prebuilt image](#run-the-prebuilt-image-no-local-build)).

This is a **local / self-hosted** setup: no cloud component, no remote
sync, no auto-deploy. It is designed for a Linux AI/project PC, a NAS
that runs Docker, and Windows machines that connect to Nova through a
browser.

> **Heads-up on exposure.** The stack publishes Nova's web UI on your
> LAN (`http://<host-ip>:8000`). Change `NOVA_USERNAME` / `NOVA_PASSWORD`
> before exposing it, and do **not** forward port 8000 to the public
> internet without a reverse proxy and TLS in front of it. Admin-only and
> alpha-only features are **off by default** and stay off unless you opt
> in (see [Admin / alpha features](#admin--alpha-features-stay-off)).

---

## What ends up where

Nova keeps **all** of its runtime state on Docker volumes, never in the
disposable container layer:

| Data | Volume | Path in container |
|---|---|---|
| Database — incl. **memory, settings, conversations** (`nova.db`) | `nova-data` | `/data/nova.db` |
| Logs | `nova-data` | `/data/logs/` |
| Exports / user-generated files | `nova-data` | `/data/exports/` |
| Memory packs | `nova-data` | `/data/memory-packs/` |
| Backups (sidecar) | `nova-data` | `/data/backups/` |
| Auto-generated session key | `nova-data` | `/data/secret_key` |
| **Ollama models** | `ollama-models` | `/root/.ollama` |

> **Why one volume for the database, memory, and settings?** In Nova,
> memory, settings, and conversations are not separate folders — they are
> tables **inside the single `nova.db` SQLite file**. So one `nova-data`
> volume cleanly persists the database + memory + settings + logs + user
> files together. Chat image attachments are stored inside `nova.db`
> (base64), so there is no separate uploads directory to mount.

The image contains **no secrets, no database, and no models**. Pulling or
rebuilding a new version cannot overwrite your data.

---

## Prerequisites

- Docker Engine 24+ with the `docker compose` plugin (Docker Desktop on
  Windows/macOS already includes it).
- Enough disk for the models you pull (see
  [Pulling Ollama models](#pulling--downloading-ollama-models)).

No Python, no Ollama, and no other host packages are required.

---

## First-time setup

```bash
git clone https://github.com/TheZupZup/Nova.git
cd Nova

cp .env.example .env
# Edit .env and set at least:
#   NOVA_USERNAME   — your admin login
#   NOVA_PASSWORD   — change it from the default
# Everything else has working defaults.

docker compose up -d
```

The first `up`:

1. builds the `nova` image from this checkout,
2. starts the `ollama` model server (waits until it's healthy),
3. starts Nova and creates the admin account from `.env`.

Open **http://localhost:8000** and log in. From another machine on the
same network use **http://&lt;host-ip&gt;:8000**.

Then pull at least one model so Nova can reply — see
[Pulling Ollama models](#pulling--downloading-ollama-models).

> The session signing key is generated automatically on first start and
> stored at `/data/secret_key`, so you don't have to set `NOVA_SECRET_KEY`
> yourself. Logins survive restarts and rebuilds.

---

## Run the prebuilt image (no local build)

Don't want to build Nova yourself? A prebuilt image is published to the
**GitHub Container Registry** on every push to `main` and every release
tag:

```
ghcr.io/thezupzup/nova:latest      # tracks the main branch
ghcr.io/thezupzup/nova:1.2.3        # a specific release (recommended for servers)
ghcr.io/thezupzup/nova:sha-abc1234  # an exact commit
```

The repo ships a ready-made compose file, `docker-compose.ghcr.yml`, that
is identical to the default stack except it **pulls** the Nova image
instead of building it. You don't even need to clone the repository — just
this one file and an `.env`.

```bash
# Fetch the compose file (or copy it out of the repo):
curl -fsSLO https://raw.githubusercontent.com/TheZupZup/Nova/main/docker-compose.ghcr.yml

# Create a minimal .env next to it (only credentials are required):
cat > .env <<'EOF'
NOVA_USERNAME=admin
NOVA_PASSWORD=change-me-please
EOF

# Pull the image + Ollama, then start the stack:
docker compose -f docker-compose.ghcr.yml up -d

# Pull at least one model, then open http://localhost:8000
docker compose -f docker-compose.ghcr.yml exec ollama ollama pull gemma3:1b
```

Open **http://localhost:8000** and log in. From another machine on the
same network use **http://&lt;host-ip&gt;:8000** (see
[Using Nova from Windows](#using-nova-from-windows-as-a-browser-client)).

**Pin a version** instead of tracking `latest` by setting `NOVA_IMAGE_TAG`
in `.env` (recommended for anything you don't want changing under you):

```env
NOVA_IMAGE_TAG=1.2.3
```

**Update the container** to the newest published image — your data and
models are untouched:

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

**Switching between build and prebuilt is lossless.** Both compose files
declare the same `nova-data` and `ollama-models` volumes in the same
project directory, so you can move from `docker-compose.yml` (build) to
`docker-compose.ghcr.yml` (prebuilt) — or back — and keep your database
and models. Don't mix the two at once; pick one file per `docker compose`
invocation. Everything in [Everyday operations](#everyday-operations),
[Pulling models](#pulling--downloading-ollama-models), and
[Backing up](#backing-up-persistent-data) below applies to both — just add
`-f docker-compose.ghcr.yml` to the commands when running the prebuilt
stack.

> **Without Compose.** You can also run the image directly, but you must
> provide an Ollama endpoint and a data volume yourself:
> ```bash
> docker run -d --name nova -p 8000:8000 \
>   -e NOVA_USERNAME=admin -e NOVA_PASSWORD=change-me-please \
>   -e OLLAMA_HOST=http://host.docker.internal:11434 \
>   -v nova-data:/data \
>   ghcr.io/thezupzup/nova:latest
> ```
> Compose is recommended because it wires up Ollama and the volumes for
> you.

---

## Everyday operations

### Starting Nova

```bash
docker compose up -d
```

### Stopping Nova

```bash
docker compose stop          # stop containers, keep them
# or
docker compose down          # stop and remove containers (data volumes kept)
```

`down` removes the containers but **not** the `nova-data` /
`ollama-models` volumes — your database and models are safe.

### Viewing logs

```bash
docker compose logs -f            # both services, follow
docker compose logs -f nova       # just Nova
docker compose logs -f ollama     # just the model server
docker compose logs --tail=200 nova
```

### Checking status

```bash
docker compose ps                 # health/status of both containers
```

### Updating Nova

Because the image is built from this checkout, update the code and
rebuild:

```bash
git pull
docker compose up -d --build      # rebuild Nova, restart, keep all data
```

To also update the model server image:

```bash
docker compose pull ollama
docker compose up -d
```

> **Prefer a prebuilt image?** Use `docker-compose.ghcr.yml`, which pulls
> `ghcr.io/thezupzup/nova` instead of building, and update with
> `docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d`.
> See [Run the prebuilt image](#run-the-prebuilt-image-no-local-build).

### Resetting containers without deleting data

Recreate the containers from scratch while keeping the database and
models:

```bash
docker compose down               # removes containers only (NOT volumes)
docker compose up -d --force-recreate
```

Your `nova-data` and `ollama-models` volumes are untouched. The
**destructive** variant is `docker compose down -v`, which deletes the
volumes (database, conversations, and downloaded models). Only use it
when you truly want a clean slate.

---

## Pulling / downloading Ollama models

No models are bundled. Pull the ones Nova uses into the running `ollama`
container — they land in the `ollama-models` volume and persist across
rebuilds:

```bash
# Lightweight router/classifier + general chat (start here):
docker compose exec ollama ollama pull gemma3:1b
docker compose exec ollama ollama pull gemma4

# Optional, for coding and "advanced" requests (larger downloads):
docker compose exec ollama ollama pull deepseek-coder-v2
docker compose exec ollama ollama pull qwen2.5:32b
```

These are the model names Nova references in `config.py`. `qwen2.5:32b`
needs significant disk and RAM; if your hardware is constrained, skip it
— the router falls back to `gemma4` for advanced requests.

List and remove models:

```bash
docker compose exec ollama ollama list
docker compose exec ollama ollama rm <model>
```

Because models live in the `ollama-models` volume, you only download them
once. `docker compose down`, `up --build`, and image updates do not delete
them — only `docker compose down -v` does.

---

## Backing up persistent data

Everything important is in two volumes. Stop the app first so the SQLite
file is copied in a consistent state.

**Back up the Nova database + files (`nova-data`):**

```bash
docker compose stop nova
docker run --rm -v nova-data:/data -v "$PWD":/backup alpine \
    tar czf /backup/nova-data-$(date +%Y%m%d-%H%M%S).tar.gz -C /data .
docker compose start nova
```

**Back up the Ollama models (`ollama-models`, optional — they can be
re-pulled):**

```bash
docker run --rm -v ollama-models:/models -v "$PWD":/backup alpine \
    tar czf /backup/ollama-models-$(date +%Y%m%d-%H%M%S).tar.gz -C /models .
```

**Restore** by extracting an archive back into the volume while the
stack is stopped:

```bash
docker compose down
docker run --rm -v nova-data:/data -v "$PWD":/backup alpine \
    sh -c "rm -rf /data/* && tar xzf /backup/nova-data-YYYYMMDD-HHMMSS.tar.gz -C /data"
docker compose up -d
```

> Nova also keeps an in-app export/restore flow (Settings → admin) that
> writes to `/data/exports`. The volume-level backup above is the
> simplest full-image snapshot.

---

## Using Nova from Windows as a browser client

You do **not** install Nova on Windows. Run the stack on your Linux PC or
NAS, then just open a browser on the Windows machine:

1. Start the stack on the host (`docker compose up -d`).
2. Find the host's LAN IP (on Linux: `ip addr` / `hostname -I`).
3. On the Windows machine, browse to **`http://<host-ip>:8000`** — for
   example `http://192.168.1.42:8000`.
4. Log in with your `NOVA_USERNAME` / `NOVA_PASSWORD`.

If it doesn't load from Windows but works as `http://localhost:8000` on
the host:

- Make sure the host firewall allows inbound TCP **8000**
  (e.g. `sudo firewall-cmd --add-port=8000/tcp` on Fedora, or
  `sudo ufw allow 8000/tcp` on Ubuntu).
- Confirm both machines are on the same network/subnet.
- The container publishes on all interfaces by default, so no Nova-side
  change is needed.

You can pin Nova to a different host port by setting `NOVA_HOST_PORT` in
`.env` (e.g. `NOVA_HOST_PORT=9000` → browse to `:9000`).

---

## Connecting to an external Ollama instead of the bundled one

By default Nova talks to the bundled `ollama` container. To use an Ollama
running elsewhere (another machine, your NAS, a GPU box), set `OLLAMA_HOST`
in `.env`:

```env
OLLAMA_HOST=http://192.168.1.50:11434
```

That other Ollama must listen on a reachable interface
(`OLLAMA_HOST=0.0.0.0 ollama serve`). You can also stop the bundled model
server with `docker compose stop ollama` if you don't need it.

---

## Optional: NVIDIA GPU acceleration

CPU works out of the box. To let Ollama use an NVIDIA GPU:

1. Install the NVIDIA driver and the
   [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
   on the host.
2. In `docker-compose.yml`, uncomment the `deploy:` GPU block under the
   `ollama` service.
3. `docker compose up -d` and verify with:

   ```bash
   docker compose exec ollama nvidia-smi
   ```

Only the `ollama` container needs the GPU — Nova itself is CPU-only.

---

## Admin / alpha features stay off

The default Docker configuration does **not** expose admin-only or
alpha-only functionality:

- `NOVA_ADMIN_UI=false` — admin-only UI controls are hidden.
- `NOVA_CHANNEL=stable` — the alpha GitHub-OAuth gate is inactive.
- Maintenance Center, Dev Workspace, and all integrations
  (SilentGuard, NexaNote, GitHub, Jellyfin) default to **off**.

Leave these as-is unless you intentionally opt in. See `.env.example` for
each switch.

---

## Troubleshooting

**Nova replies with a model/connection error.**
You probably haven't pulled a model yet, or the model name Nova requested
isn't present. Check:

```bash
docker compose exec ollama ollama list
docker compose exec nova python -c \
  "import os,urllib.request; print(urllib.request.urlopen(os.environ['OLLAMA_HOST']+'/api/tags',timeout=5).read()[:200])"
```

**`docker compose up` fails saying `.env` is missing.**
Run `cp .env.example .env` first.

**Port 8000 is already in use.**
Set `NOVA_HOST_PORT` to a free port in `.env`, then `docker compose up -d`.

**Permission errors on the data volume.**
The default named volume is initialised with the right ownership
automatically. If you switched `nova-data` to a host **bind mount**, make
the host directory writable by UID 1000 (the `nova` user):
`sudo chown -R 1000:1000 /your/host/path`.

**Start over completely (deletes data).**

```bash
docker compose down -v
docker compose up -d
```

---

## What this deployment does NOT do

- No telemetry, cloud sync, or third-party calls beyond Nova's existing
  optional integrations.
- No automatic model downloads — you pull models explicitly.
- No automatic updates — you run `docker compose up -d --build` when you
  want to update.
- No reverse proxy or TLS is bundled. Add your own if you expose Nova
  beyond a trusted LAN (see [`docs/secure-deployment.md`](secure-deployment.md)).

For a host-bind-mount layout where data/config/logs live under one parent
folder, see [`docs/portable-workspace.md`](portable-workspace.md) and
[`deploy/docker/docker-compose.portable.yml`](../deploy/docker/docker-compose.portable.yml).
