# Nova and Docker Desktop

This guide is for people who like managing containers **visually** with
[Docker Desktop](https://www.docker.com/products/docker-desktop/) on
Windows, macOS, or Linux. It shows how Nova's stack should look in the
Docker Desktop UI, what is safe to click, and what to avoid.

It assumes the stack from [INSTALL.md](../INSTALL.md) /
[docs/docker.md](docker.md): the Nova web app plus a bundled Ollama
model server, with data on named volumes.

---

## The one rule

> **Use Docker Compose to create the stack, then use Docker Desktop to
> monitor and manage it.**

Docker Desktop is excellent at showing you what is running, tailing
logs, and starting/stopping things you already created. It is **not**
the right tool for creating Nova's multi-container stack in the first
place — that is Compose's job, and it takes exactly one terminal
command:

```bash
# Prebuilt image (recommended):
docker compose -f docker-compose.ghcr.yml up -d

# Or build from source, inside a checkout:
docker compose up -d
```

After that one command, everything else in this guide happens in the
Docker Desktop window.

### Why not just click "Run" on the `ghcr.io/thezupzup/nova` image?

Docker Desktop lets you search for an image and press **Run**. For Nova
that starts a *single bare container*, which is **not** the supported
setup:

- it **skips the bundled Ollama service**, so Nova has no model server
  to talk to and cannot answer;
- it **skips the named volumes**, so your database, memory, and
  conversations land in an unnamed, anonymous volume that is easy to
  lose track of — and gone for good if the container is removed
  together with its volumes;
- it may **not publish the right ports**, so `http://localhost:8000`
  never loads;
- it creates **one container, not the Compose stack**, so none of the
  service wiring (networking, health checks, restart policy) exists.

The compose files encode all of that wiring. One `docker compose up -d`
gives you the correct stack; from then on Docker Desktop shows and
manages it like anything else.

---

## What you should see in Docker Desktop

After `up -d` finishes, the UI should look like this:

**Containers** — one Compose application (named after the folder, e.g.
`nova`) containing:

| Container | Status |
|---|---|
| `nova` | Running (healthy) |
| `nova-ollama` | Running (healthy) |
| `nova-watchtower` | Running — *only* if you opted into the auto-update overlay |

**Images:**

| Image | Used by |
|---|---|
| `ghcr.io/thezupzup/nova` (prebuilt) **or** `nova:local` (built from source) | `nova` |
| `ollama/ollama` | `nova-ollama` |

**Volumes** — the two persistent volumes, usually shown with the Compose
project prefix (e.g. `nova_nova-data`):

| Volume | Contains |
|---|---|
| `nova-data` | database (memory, conversations, settings), logs, exports, backups, session key |
| `ollama-models` | downloaded Ollama models |

**Logs** — select the `nova` container and open **Logs**. A healthy
start ends with a line like:

```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

Then open **http://localhost:8000** in your browser (Docker Desktop's
port link on the `nova` container takes you there too) and log in —
default account `admin` / `changeme`, and change it before exposing
Nova beyond this machine.

If something is missing from this picture — no `nova-ollama`, no
volumes, no published port — the stack was probably started by clicking
**Run** on the image instead of via Compose. Remove that lone container
and start over with `docker compose up -d`.

---

## Safe to do from the Docker Desktop UI

- **Start / Stop / Restart** the `nova` or `nova-ollama` containers.
- **View logs** of both containers, live.
- **Open in browser** via the port shown on the `nova` container.
- **Exec / Open terminal** into `nova-ollama` to manage models, e.g.
  `ollama list`, `ollama pull gemma3:1b`.
- **Inspect** volumes, files, and resource usage.

Be careful with these:

- **Deleting volumes** (`nova-data`, `ollama-models`) permanently
  deletes your database, memory, conversations, settings, and models.
- **Deleting the Compose application** with the *"also remove volumes"*
  option checked does the same thing.
- **Running the Nova image directly** — see
  [the one rule](#the-one-rule) above.

---

## Signing in (and the Linux fallback)

You do **not** need any registry account to pull Nova:
`ghcr.io/thezupzup/nova` and `ollama/ollama` are public images.
Signing in to Docker Desktop is only useful for Docker Hub rate limits
and Docker's own cloud features.

On **Linux**, the Docker Desktop **Sign in** button sometimes does
nothing (a known pain point with browser hand-off). If that happens,
sign in from the terminal instead — Docker Desktop picks up the
credentials:

```bash
docker login              # Docker Hub account
docker login ghcr.io      # GitHub Container Registry (username + a
                          # GitHub personal access token as password)
```

`docker login ghcr.io` is only needed if you ever pull *private* GHCR
images — Nova itself does not require it.

---

## Resetting the stack

**Safe reset** — recreate the containers, keep all data and models:

```bash
docker compose down
docker compose up -d
```

(Add `-f docker-compose.ghcr.yml` to both commands if you run the
prebuilt stack.)

**Destructive reset** — delete *everything*, including the database,
memory, conversations, settings, and downloaded models:

```bash
docker compose down -v    # ⚠️ deletes the nova-data and ollama-models volumes
```

There is no undo. Take a backup first if in doubt
([docs/docker.md — Backing up](docker.md#backing-up-persistent-data)).

---

## Troubleshooting

Run the read-only diagnostic script from a checkout — it checks the
Docker CLI, the daemon, the Compose plugin, the compose files, and the
Nova containers/volumes without changing anything:

```bash
scripts/docker-doctor.sh

# For the prebuilt stack:
NOVA_COMPOSE_FILES=docker-compose.ghcr.yml ./scripts/docker-doctor.sh

# For the prebuilt stack with the auto-update overlay:
NOVA_COMPOSE_FILES=docker-compose.ghcr.yml:docker-compose.watchtower.yml \
    ./scripts/docker-doctor.sh
```

For everything else (models, backups, GPU, LAN access) see the full
guide: [docs/docker.md](docker.md).
