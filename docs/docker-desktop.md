# Docker Desktop guide for Nova

This guide is for users who want Docker Desktop as the visual dashboard for
Nova containers.

The key rule:

> Use Docker Compose to create the Nova stack, then use Docker Desktop to watch
> logs, restart containers, inspect images, and see volumes.

Docker Desktop's **Run** button on the image is useful for simple single-container
apps, but Nova is a two-service stack (`nova` + `ollama`) with persistent
volumes. Compose wires those pieces together correctly.

---

## What you should see in Docker Desktop

After starting Nova with Compose, Docker Desktop should show:

| Area | What you should see |
|---|---|
| Containers | `nova`, `nova-ollama` |
| Images | `ghcr.io/thezupzup/nova` or `nova:local`, plus `ollama/ollama` |
| Volumes | `nova-data`, `ollama-models` |
| Logs | `Uvicorn running on http://0.0.0.0:8000` for Nova |

If you see only one `nova` container and no `nova-ollama`, you probably used
**Run** on the image directly instead of Compose.

---

## Recommended install: prebuilt image

Use this on a home server, NAS, or AI PC when you want Nova to behave like an
installed app.

```bash
mkdir nova-docker
cd nova-docker
curl -fsSLO https://raw.githubusercontent.com/TheZupZup/Nova/main/docker-compose.ghcr.yml
docker compose -f docker-compose.ghcr.yml up -d
```

Open:

```text
http://localhost:8000
```

From another device on the same LAN:

```text
http://<host-ip>:8000
```

Default login:

```text
admin / changeme
```

Set real credentials in `.env` before exposing Nova beyond your own machine:

```bash
cat > .env <<'EOF'
NOVA_USERNAME=admin
NOVA_PASSWORD=change-me-please
EOF

docker compose -f docker-compose.ghcr.yml up -d
```

Pull a starter model:

```bash
docker compose -f docker-compose.ghcr.yml exec ollama ollama pull gemma3:1b
```

---

## Contributor install: build from source

Use this if you cloned the repository and want to work on Nova.

```bash
git clone https://github.com/TheZupZup/Nova.git
cd Nova
docker compose up -d
```

For live reload while editing source code:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

---

## Updating from Docker Desktop

Docker Desktop is a visual control panel; Compose is still the safest way to
apply the update because it preserves ports, volumes, and service relationships.

### Prebuilt image

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

### Source build

```bash
git pull
docker compose up -d --build
```

Then go back to Docker Desktop → **Containers** and watch `nova` restart.

Your memory, conversations, settings, and models survive because they live in
`nova-data` and `ollama-models`, not inside the disposable container layer.

---

## Optional: automatic update checks

The Watchtower overlay is for users who want the prebuilt image stack to check
for newer GHCR images automatically:

```bash
docker compose -f docker-compose.ghcr.yml -f docker-compose.watchtower.yml up -d
```

Only the `nova` container opts in. Watchtower does not delete the `nova-data` or
`ollama-models` volumes.

Use this only on a Docker host you trust, because Watchtower needs access to the
Docker socket.

---

## Docker Desktop sign-in on Linux

If the **Sign in** button does nothing on Linux, use the terminal login flow:

```bash
docker login
```

Docker may print a device-code link. Open it in your browser, enter the code,
and Docker Desktop should show your account in the top-right menu afterward.

For GitHub Container Registry access, use:

```bash
docker login ghcr.io
```

Username: your GitHub username.
Password: a GitHub token with the package scopes you need.

---

## Common mistakes

### I clicked Run on `ghcr.io/thezupzup/nova` and the page does not load

That starts only the Nova image. It does not automatically create the bundled
Ollama service, the named volumes, or the correct Compose network.

Remove that manual container and start with Compose instead:

```bash
docker rm -f nova || true
docker compose -f docker-compose.ghcr.yml up -d
```

### Docker Desktop shows the container, but no port

Check whether the stack was started with Compose:

```bash
docker compose ps
```

A healthy Compose deployment should publish Nova as `8000:8000` unless you set a
different `NOVA_HOST_PORT`.

### Nova works on the host but not from Windows / another PC

Allow inbound TCP 8000 on the host firewall and browse to the host LAN IP:

```text
http://<host-ip>:8000
```

On Fedora:

```bash
sudo firewall-cmd --add-port=8000/tcp --permanent
sudo firewall-cmd --reload
```

### I want to reset containers but keep memory

Safe:

```bash
docker compose down
docker compose up -d
```

Destructive:

```bash
docker compose down -v
```

`-v` deletes the volumes that store Nova memory and Ollama models.
