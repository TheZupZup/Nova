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

New here? The short version of this page is [INSTALL.md](../INSTALL.md).
If you manage containers through the **Docker Desktop** UI, read
[docs/docker-desktop.md](docker-desktop.md) — in short: create the stack
with Compose, then monitor/manage it in Docker Desktop.

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
docker compose up -d
```

That's the whole setup — **no file copying and no manual configuration.**
The stack comes up with safe built-in defaults, including an admin login of
`admin` / `changeme`.

The first `up`:

1. builds the `nova` image from this checkout,
2. starts the `ollama` model server (waits until it's healthy),
3. starts Nova, which on first boot creates `nova.db`, seeds the admin
   account, and generates a per-install session key — all automatically.

Open **http://localhost:8000** and log in with `admin` / `changeme`. From
another machine on the same network use **http://&lt;host-ip&gt;:8000**.

> **Change the admin password before exposing Nova beyond localhost.**
> `NOVA_USERNAME` / `NOVA_PASSWORD` seed the admin account **on the very
> first start only** (when the database is still empty). To start with
> your own credentials, copy the sample env file and edit it *before*
> the first `up` — this is the *only* reason you need an `.env`, and it
> stays optional:
> ```bash
> cp .env.example .env     # then set NOVA_USERNAME / NOVA_PASSWORD, etc.
> docker compose up -d
> ```
> On a stack that has already started once, editing `.env` does **not**
> change the existing account — log in as the admin and use the admin
> panel (**Users → reset password**) instead.

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
ghcr.io/thezupzup/nova:latest        # tracks main — moves on every merge (Watchtower follows this)
ghcr.io/thezupzup/nova:v1.2.3        # a specific release (recommended for servers)
ghcr.io/thezupzup/nova:1.2.3         # same release, without the leading v
ghcr.io/thezupzup/nova:main-abc1234  # an exact main commit
```

`latest` only ever moves when a change is **merged to main** (CI validates
it first). A pull request is built to prove the image still compiles, but
it is **never published** — no PR can move `latest` or ship code to your
server. Release tags (`vX.Y.Z`) are versioned snapshots and do **not**
touch `latest`.

The repo ships a ready-made compose file, `docker-compose.ghcr.yml`, that
is identical to the default stack except it **pulls** the Nova image
instead of building it. You don't even need to clone the repository — just
this one file and an `.env`.

```bash
# Fetch the compose file (or copy it out of the repo):
curl -fsSLO https://raw.githubusercontent.com/TheZupZup/Nova/main/docker-compose.ghcr.yml

# Pull the image + Ollama and start the stack. No .env is needed — it comes
# up with admin / changeme by default:
docker compose -f docker-compose.ghcr.yml up -d

# Pull at least one model, then open http://localhost:8000
docker compose -f docker-compose.ghcr.yml exec ollama ollama pull gemma3:1b
```

> **Set real credentials before exposing it.** `NOVA_USERNAME` /
> `NOVA_PASSWORD` seed the account on the **very first start only**, so
> create the `.env` next to the compose file *before* that first `up -d`:
> ```bash
> cat > .env <<'EOF'
> NOVA_USERNAME=admin
> NOVA_PASSWORD=change-me-please
> EOF
> docker compose -f docker-compose.ghcr.yml up -d
> ```
> If the stack has already started once, change the password from the
> in-app admin panel (**Users → reset password**) instead.

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

## Production vs development

The default `docker compose up -d` is the **production-shaped** setup: a
self-contained image that runs as a non-root user, restarts automatically,
and serves the code baked in at build time. That's what you want on a
server or NAS.

For **active development** — editing Nova's source and seeing changes live —
layer the opt-in dev overlay on top. It bind-mounts your checkout into the
container and runs uvicorn with `--reload`:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

The overlay only changes the `nova` service (live source mount, `--reload`,
no auto-restart); Ollama, the volumes, and the database are untouched. Set
it once for your shell so plain `docker compose` commands pick up both files:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml
docker compose up        # now runs with live reload
```

To keep development data completely separate from a production stack on the
same host, give the dev stack its own project name (separate containers and
volumes):

```bash
docker compose -p nova-dev -f docker-compose.yml -f docker-compose.dev.yml up
```

After changing `requirements.txt`, rebuild: add `--build` to the `up`
command.

---

## Automatic updates (optional)

Updates are **manual by default** — nothing changes under you. To update
deliberately:

```bash
# Prebuilt (GHCR) stack:
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d

# Build-from-source stack:
git pull
docker compose up -d --build
```

Your `nova-data` and `ollama-models` volumes are never touched by an
update, so the database, conversations, and downloaded models survive.

If you'd rather Nova update **itself**, the repo ships an opt-in Watchtower
overlay for the prebuilt stack. Watchtower periodically checks GHCR, pulls a
newer image, and recreates the container — keeping your volumes intact:

```bash
docker compose -f docker-compose.ghcr.yml -f docker-compose.watchtower.yml up -d
```

Trade-offs to understand before enabling it:

- **Prebuilt image only.** Watchtower pulls published images; it cannot
  rebuild the build-from-source stack.
- **Pin for stability.** With `NOVA_IMAGE_TAG=latest` (the default)
  Watchtower tracks `main`. Pin a release (`NOVA_IMAGE_TAG=1.2.3` in `.env`)
  to receive only patches within that line, or to freeze entirely.
- **It needs the Docker socket** — host-level container control. Only
  enable it on a host you trust; a socket-proxy can narrow the surface.
- Tune the cadence with `WATCHTOWER_POLL_INTERVAL` (seconds; default 24h).

Only the `nova` container opts in (via a label); Ollama and Watchtower
itself are never auto-updated.

### Update channels (planned)

Today there is **one published channel**: `:latest`, built from `main`
after every merge (and validated by CI first). That is the stable image
normal users track — directly, or via Watchtower.

The tagging scheme leaves room for parallel channels off other branches,
each its own GHCR tag, so you could opt into how much change you want by
picking a tag in `.env`:

| Channel | Tag | Source | For |
|---|---|---|---|
| Stable | `ghcr.io/thezupzup/nova:latest` | `main` | everyone (default) |
| Beta | `ghcr.io/thezupzup/nova:beta` | `beta` branch | early testers |
| Alpha | `ghcr.io/thezupzup/nova:alpha` | experimental branch | developers |

Switching would be a one-line change (`NOVA_IMAGE_TAG=beta`) with no data
migration — every channel uses the same `nova-data` / `ollama-models`
volumes.

> **`beta` and `alpha` are not published yet.** Only `:latest` and the
> per-release version tags (`:vX.Y.Z`, `:X.Y.Z`) exist today. This section
> documents the intended direction, not a switch you can flip now — pulling
> `:beta` currently just fails to find the tag.

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

These are the **default** model names for Nova's four routing roles
(router / default / code / advanced). They are not hard-coded: each role
is configurable, so on a constrained host you can point every role at one
small model instead of pulling the large ones — see
[Low-RAM profile](#low-ram-profile-small-machines) below.

Nova never downloads models on its own — you pull them explicitly (or opt
in to a bootstrap; see [Operator-installed models](#operator-installed-models-no-surprise-downloads)).

List and remove models:

```bash
docker compose exec ollama ollama list
docker compose exec ollama ollama rm <model>
```

Because models live in the `ollama-models` volume, you only download them
once. `docker compose down`, `up --build`, and image updates do not delete
them — only `docker compose down -v` does.

---

## Low-RAM profile (small machines)

Nova is a **local assistant runtime, not a fixed model** — you choose
which model fills each of its four routing roles. The full-size defaults
(`gemma4`, `deepseek-coder-v2`, `qwen2.5:32b`) are comfortable on a
workstation with a GPU or plenty of RAM, but a modest **CPU-only** box —
say a **Ryzen 5 3600, 16 GB RAM, no GPU** — cannot load them. Instead of
failing when Nova reaches for a model the host can't run, point **every
role at one small model**.

Set these in your `.env` (next to the compose file) *before* `up -d`, or
add them and `docker compose up -d` to apply:

```env
# Low-RAM profile: one lightweight model for every role.
NOVA_ROUTER_MODEL=gemma3:1b
NOVA_DEFAULT_MODEL=gemma3:1b
NOVA_CODE_MODEL=gemma3:1b
NOVA_ADVANCED_MODEL=gemma3:1b
```

Then pull just that one model:

```bash
docker compose exec ollama ollama pull gemma3:1b
```

That is the whole profile. With it, Nova **never tries to load a larger
model like `gemma4`, `deepseek-coder-v2`, or `qwen2.5:32b`** on a host
that can't handle them — routing still classifies each turn, but every
role resolves to `gemma3:1b`. You can mix and match too: e.g. keep
`gemma3:1b` for the router and default while pointing `NOVA_CODE_MODEL` at
a coding model you know your host can run. Leave any variable unset to
keep that role's full-size default.

> **Why this matters.** A model Ollama can't load (out of memory / killed)
> now surfaces as a clear *model runtime failure* in chat — not a generic
> "Ollama unreachable" — and a model you haven't pulled surfaces as a
> *"model not installed"* message naming the exact model. The low-RAM
> profile avoids both by keeping Nova on a model your host can actually
> run. Check the configured map and what's installed at a glance via the
> admin-only `GET /admin/models/status`.

---

## Operator-installed models (no surprise downloads)

Nova assumes **you** install the models it uses; it never downloads one
implicitly. Every model-download switch is **off by default**, so a fresh
stack pulls nothing until you ask:

- **Weekly auto-update is off by default.** Older builds silently ran
  `ollama pull` on the whole model map once a week. That is now gated
  behind `NOVA_AUTO_UPDATE_MODELS=true` — leave it unset and Nova never
  re-downloads models behind your back. (The manual "check for updates"
  admin action still works whenever you trigger it.)
- **Optional first-run bootstrap.** If you *want* Nova to pull a small
  starter model on first boot, opt in explicitly:

  ```env
  NOVA_AUTO_PULL_MODELS=true
  NOVA_BOOTSTRAP_MODELS=gemma3:1b
  ```

  When (and only when) auto-pull is on, Nova pulls the listed models in
  the **background**, **logged**, and **without blocking startup** — and
  skips anything already installed. With auto-pull off (the default),
  nothing is fetched.

Nova never runs model-generated shell commands and never escalates
privilege to install a model — pulling a model is always an explicit
Ollama action you (or an admin, through the model-pull surface) take.

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

**First step: run the doctor.** `scripts/docker-doctor.sh` is a strictly
read-only diagnostic that checks the Docker CLI, daemon, Compose plugin,
compose files, containers, volumes, and published ports — and changes
nothing:

```bash
scripts/docker-doctor.sh
# or, for the prebuilt / auto-update stack:
NOVA_COMPOSE_FILES=docker-compose.ghcr.yml:docker-compose.watchtower.yml \
    ./scripts/docker-doctor.sh
```

**Nova replies with a model/connection error.**
You probably haven't pulled a model yet, or the model name Nova requested
isn't present. Check:

```bash
docker compose exec ollama ollama list
docker compose exec nova python -c \
  "import os,urllib.request; print(urllib.request.urlopen(os.environ['OLLAMA_HOST']+'/api/tags',timeout=5).read()[:200])"
```

**`docker compose up` errors on the `env_file` / `.env` entry.**
`.env` is optional — the stack runs without it. This error means your
Docker Compose predates the `required: false` field (added in Compose
v2.24, early 2024). Upgrade Docker Desktop / the Compose plugin, or just
create the file once: `cp .env.example .env`.

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
