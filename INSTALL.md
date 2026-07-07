# Installing Nova

Nova is a self-hosted, local-first AI assistant. **The recommended way to
install Nova is Docker** — one command brings up the whole stack: the Nova
web app **and** a bundled [Ollama](https://ollama.com) model server.
Nothing else is installed on your machine, and everything runs locally
with no cloud component.

Want to work on Nova's code instead of just running it? See
[Path B](#path-b--build-from-source-contributors--developers) and the
from-source setup in the [README](README.md#running-locally).

---

## What you need

- **Docker Desktop** (Windows / macOS / Linux), or **Docker Engine 24+**
  with the Compose plugin on a Linux server. Compose **v2.24+** is
  required (shipped with Docker Desktop / Engine since early 2024).
- Disk space for the models you pull (a small starter model is ~1 GB;
  larger models need tens of GB).

No Python, no Ollama, and no other host packages are required.

---

## Path A — prebuilt image (recommended)

The normal way to **use** Nova on a PC, home server, or NAS. It pulls the
published `ghcr.io/thezupzup/nova` image — no cloning, no building.

```bash
# 1. Get the compose file (or copy it out of the repository):
curl -fsSLO https://raw.githubusercontent.com/TheZupZup/Nova/main/docker-compose.ghcr.yml

# 2. Start the stack (Nova + Ollama):
docker compose -f docker-compose.ghcr.yml up -d

# 3. Pull at least one model so Nova can reply:
docker compose -f docker-compose.ghcr.yml exec ollama ollama pull gemma3:1b
```

Open **http://localhost:8000** and log in with the default account
**`admin` / `changeme`**.

> **Change the default login before exposing Nova beyond localhost** —
> see [Change the default login](#change-the-default-login) below.

To pin a specific release instead of tracking `latest`, set
`NOVA_IMAGE_TAG=1.2.3` in an `.env` file next to the compose file
(recommended for servers).

---

## Path B — build from source (contributors / developers)

The same stack, but the Nova image is built from your checkout:

```bash
git clone https://github.com/TheZupZup/Nova.git
cd Nova
docker compose up -d
docker compose exec ollama ollama pull gemma3:1b
```

Both compose files declare the same volumes, so you can switch between
the prebuilt and the from-source stack later without losing data — **as
long as you run both from the same directory**. Docker Compose prefixes
volume names with the directory name (the "project"), so running a
compose file from a different directory creates a *separate* set of
volumes.

For active development there is a live-reload overlay
(`docker compose -f docker-compose.yml -f docker-compose.dev.yml up`),
and a fully local Python setup (no Docker at all) is documented in the
[README](README.md#running-locally). Docker is the recommended way to
*run* Nova; local source setup remains fully supported for contributors.

---

## Running on a small or low-RAM machine

Nova is model-flexible — you choose which local model it runs. Its
full-size defaults (`gemma4`, `deepseek-coder-v2`, `qwen2.5:32b`) need a
capable host, so on a modest **CPU-only** box (e.g. a Ryzen 5 3600 with
16 GB RAM and no GPU) point every role at one small model instead. Add
this to your `.env` and pull just that model:

```env
NOVA_ROUTER_MODEL=gemma3:1b
NOVA_DEFAULT_MODEL=gemma3:1b
NOVA_CODE_MODEL=gemma3:1b
NOVA_ADVANCED_MODEL=gemma3:1b
```

```bash
docker compose -f docker-compose.ghcr.yml exec ollama ollama pull gemma3:1b
```

Nova then never tries to load a model your host can't run. Full details,
including per-role mixes and the opt-in bootstrap, are in
[docs/docker.md → Low-RAM profile](docs/docker.md#low-ram-profile-small-machines).

---

## Where your data lives

Nova keeps all of its state in two named Docker volumes — never inside
the disposable containers:

| Volume | Contains |
|---|---|
| `nova-data` | the database (including **memory, conversations, and settings**), logs, exports, memory packs, backups, and the auto-generated session key |
| `ollama-models` | the Ollama models you downloaded |

Because of this, `docker compose down`, image updates, and rebuilds
**never** touch your data.

> ⚠️ **`docker compose down -v` DELETES both volumes** — your database,
> memory, conversations, settings, and downloaded models. Only run it
> when you truly want to start from scratch.

---

## Updating

Updates are manual by default and never touch the data volumes.

**Prebuilt image (Path A):**

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

**Build from source (Path B):**

```bash
git pull
docker compose up -d --build
```

Prefer automatic updates? An opt-in Watchtower overlay exists for the
prebuilt stack — see
[docs/docker.md](docs/docker.md#automatic-updates-optional).

---

## Change the default login

The stack starts with a deliberately weak default account
(`admin` / `changeme`) so that the first run needs zero configuration.
Change it before you expose Nova beyond localhost — even just to your
LAN.

`NOVA_USERNAME` / `NOVA_PASSWORD` are used **on the very first start
only**, when Nova creates the account in its (empty) database. So:

**Fresh install (nothing started yet):** create an `.env` file next to
your compose file *before* the first `up`, and the account is created
with your credentials:

```bash
cat > .env <<'EOF'
NOVA_USERNAME=admin
NOVA_PASSWORD=pick-a-strong-password
EOF
docker compose -f docker-compose.ghcr.yml up -d   # or: docker compose up -d
```

**Already-running install:** editing `.env` afterwards does **not**
change the existing account — the login already lives in the database
on the `nova-data` volume. Instead, log in as the admin, open the admin
panel, and use **Users → reset password**. (The nuclear alternative,
`docker compose down -v` + set `.env` + `up -d`, re-seeds the login but
deletes all data.)

---

## Something not working?

- Run the read-only diagnostic helper from a checkout:
  `scripts/docker-doctor.sh` (it changes nothing; it only reports).
- Full Docker guide — first run, logs, backups, models, GPU:
  [docs/docker.md](docs/docker.md).
- Using Docker Desktop's UI to manage Nova:
  [docs/docker-desktop.md](docs/docker-desktop.md).
