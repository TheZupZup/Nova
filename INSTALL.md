# Install Nova

Nova is Docker-first for normal users: you should not need to install Python,
Ollama, or project dependencies on the host.

There are two supported paths:

| Path | Best for | Command style |
|---|---|---|
| **Prebuilt image** | Users and servers | `docker compose -f docker-compose.ghcr.yml up -d` |
| **Build from source** | Contributors / local development | `docker compose up -d` |

Both paths use the same persistent volumes:

- `nova-data` stores `nova.db`, conversations, memory, settings, exports, logs,
  and the generated session key.
- `ollama-models` stores downloaded Ollama models.

Updating the container does **not** delete those volumes. The destructive command
is `docker compose down -v`; avoid it unless you intentionally want a clean
slate.

---

## Recommended: install from the prebuilt Docker image

Use this when you want Nova to behave like an installable app.

```bash
mkdir nova-docker
cd nova-docker
curl -fsSLO https://raw.githubusercontent.com/TheZupZup/Nova/main/docker-compose.ghcr.yml
docker compose -f docker-compose.ghcr.yml up -d
```

Open Nova:

```text
http://localhost:8000
```

Default first-run login:

```text
username: admin
password: changeme
```

Change those credentials before exposing Nova beyond your own machine:

```bash
cat > .env <<'EOF'
NOVA_USERNAME=admin
NOVA_PASSWORD=change-me-please
EOF

docker compose -f docker-compose.ghcr.yml up -d
```

Pull at least one model so Nova can answer:

```bash
docker compose -f docker-compose.ghcr.yml exec ollama ollama pull gemma3:1b
```

---

## Contributor mode: build from source

Use this when you cloned the repository because you want to work on Nova.

```bash
git clone https://github.com/TheZupZup/Nova.git
cd Nova
docker compose up -d
```

For live-reload development:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

---

## Updating Nova safely

### Prebuilt image

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

### Build from source

```bash
git pull
docker compose up -d --build
```

Your database, memory, settings, and models are stored in Docker volumes and are
kept across these updates.

---

## Optional: automatic image updates

Nova ships an opt-in Watchtower overlay for the prebuilt image stack:

```bash
docker compose -f docker-compose.ghcr.yml -f docker-compose.watchtower.yml up -d
```

This lets Watchtower check GHCR for a newer Nova image and recreate only the
Nova container. It does not delete `nova-data` or `ollama-models`.

Read the full Docker guide before enabling automatic updates:

```text
docs/docker.md
```

---

## Docker Desktop users

Docker Desktop is great as the visual control panel, but Compose should create
the stack. Do not start Nova by clicking **Run** on the image alone unless you
manually configure ports, environment, volumes, and Ollama.

Use Compose first, then manage the containers visually in Docker Desktop:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

Then open Docker Desktop → **Containers** → `nova`.

See the dedicated guide:

```text
docs/docker-desktop.md
```

---

## Troubleshooting shortcut

From the repository root, run:

```bash
./scripts/docker-doctor.sh
```

It checks Docker, Compose, compose-file validity, container state, published
ports, and the common data-loss warning around `down -v`.
