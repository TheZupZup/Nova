# Nova

A self-hosted, local AI assistant built on FastAPI and Ollama.

## What Nova is

Nova is a personal AI assistant that runs entirely on your own machine. It routes each conversation to the most appropriate local model based on complexity, maintains a persistent memory database across sessions, and serves a web interface reachable from any browser on your network.

Nova is under active development. Core features are functional. Some subsystems — the natural language memory retriever and the embedding pipeline — are present in the codebase but are not yet fully validated for production use.

## What works today

- Multi-model routing: a lightweight classifier (`gemma3:1b`) decides which model handles each request
- Persistent memory stored in SQLite with automatic extraction from conversations
- Manual memory commands for explicit fact storage (`Retiens ça:`, `Souviens-toi:`, `Souviens-toi de ça:`)
- JWT-secured web interface accessible from desktop and mobile browsers on your network
- Conversation history with a sidebar for navigation
- Mode selector: Auto / Chat / Code / Deep
- Real-time weather via Open-Meteo (no API key required)
- Web search via DuckDuckGo (manual trigger)
- Settings panel: view, add, edit, and delete stored memories; RAM budget control
- Login rate limiting (5 attempts per 60-second window, configurable via environment variables)
- Nova identity contract: Nova presents itself as a named assistant rather than exposing the underlying model name
- Background RSS/web learning (disabled by default, opt-in via `NOVA_AUTO_WEB_LEARNING=true`)
- AMD GPU acceleration via ROCm; falls back to CPU automatically
- systemd service configuration for unattended startup

## Privacy and local-first principles

- Model inference runs locally through Ollama. Optional tools such as web search and weather can contact external services only when explicitly triggered.
- The memory database is a local SQLite file under your control.
- Credentials live in `.env` and are never committed to the repository.
- No telemetry, no cloud sync, no third-party analytics.

## Architecture overview

```
web.py              FastAPI application and REST endpoints
main.py             Terminal interface (no web server required)
config.py           Central configuration loaded from .env

core/
  router.py         Model selection via gemma3:1b classifier
  chat.py           Conversation logic and message assembly
  memory.py         SQLite memory: facts, conversations, settings
  memory_command.py Manual memory command parser
  identity.py       Nova identity contract injected into the system prompt
  auth.py           JWT creation and verification
  rate_limiter.py   Per-IP sliding-window rate limiter for the login endpoint
  learner.py        Background RSS feed ingestion (opt-in)
  weather.py        Open-Meteo integration
  search.py         DuckDuckGo integration
  updater.py        Model version management

memory/
  store.py          Natural language memory store
  retriever.py      Semantic memory retrieval
  extractor.py      Memory extraction pipeline
  schema.py         Memory data schema
  policy.py         Retention and cleanup policy

static/
  index.html        Web interface

tests/              Pytest test suite
```

## Getting started

### Requirements

- Linux (tested on Fedora)
- Python 3.11+
- [Ollama](https://ollama.com) installed and running
- AMD GPU with ROCm (optional — falls back to CPU)

### 1. Clone the repository

```bash
git clone https://github.com/TheZupZup/Nova.git
cd Nova
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Pull the required Ollama models

```bash
ollama pull gemma3:1b
ollama pull gemma4
ollama pull deepseek-coder-v2
ollama pull qwen2.5:32b
```

`qwen2.5:32b` requires significant disk space and RAM. If your hardware is constrained, you can skip it; the router falls back to `gemma4` for advanced requests.

### 4. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```
NOVA_USERNAME=your_username
NOVA_PASSWORD=your_password
NOVA_SECRET_KEY=a-long-random-string
```

The defaults in `.env.example` are intentionally weak placeholders. Change them before any deployment, especially if Nova is exposed beyond localhost.

### 5. Start Nova

```bash
python web.py
```

Nova is available at `http://localhost:8080`.

## Running as a systemd service

Create `/etc/systemd/system/nova.service`:

```ini
[Unit]
Description=Nova AI Assistant
After=network.target ollama.service

[Service]
Type=simple
User=yourusername
WorkingDirectory=/path/to/Nova
ExecStart=/path/to/Nova/.venv/bin/uvicorn web:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
Environment="PATH=/path/to/Nova/.venv/bin:/usr/bin:/usr/local/bin"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable nova
sudo systemctl start nova
```

For an optional hardened unit file with systemd sandbox restrictions
(`NoNewPrivileges`, `ProtectSystem=strict`, capability drop, etc.), see
[deploy/systemd/README.md](deploy/systemd/README.md). It is a drop-in
replacement for the minimal unit above and does not change Nova's behavior.

## Voice / read-aloud

Every assistant message has a "Read aloud" button. By default, Nova uses
the browser's built-in `speechSynthesis` API — zero install, fully
local, and pleasant on most platforms (Apple Samantha, Microsoft Aria,
Google Female, …).

On Fedora and other Linux desktops the platform voices sometimes fall
back to a robotic engine. In that case Nova can drive a local
[Piper](https://github.com/rhasspy/piper) neural voice instead. Piper
is opt-in, runs entirely on this host, and is never downloaded
automatically.

### Optional: enable Piper

1. Install the Piper binary. The simplest path is the official release:

   ```bash
   # Pick the build for your CPU; this example is x86_64 Linux.
   curl -L -o piper.tar.gz \
     https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz
   tar -xzf piper.tar.gz
   sudo mv piper /usr/local/bin/   # or anywhere on PATH
   ```

2. Download a calm voice model. Nova suggests these soft, natural
   voices (`.onnx` model + `.onnx.json` config, side by side):

   - `en_US-amy-medium`
   - `en_GB-jenny_dioco-medium`
   - `en_US-lessac-medium`
   - `fr_FR-siwis-medium` (French)

   Models live in the
   [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)
   repository. Place the `.onnx` and `.onnx.json` together in a folder
   you control (for example `~/.local/share/piper/voices/`).

3. Configure Nova by adding to your `.env`:

   ```ini
   NOVA_PIPER_BINARY=/usr/local/bin/piper
   NOVA_PIPER_VOICE_MODEL=/home/you/.local/share/piper/voices/en_US-amy-medium.onnx
   ```

   Both variables must be set. Leave `NOVA_PIPER_BINARY` blank if
   `piper` is already on the system `PATH`. `NOVA_PIPER_VOICE_CONFIG`
   is optional — Piper auto-discovers the sibling `.onnx.json`.

4. Restart Nova. Open Settings → Voice. A new "Voice engine" row will
   appear with `Browser` and `Piper (local neural)` options. Select
   Piper and click "Test voice" to confirm it works.

If Piper is missing, the model is unreadable, the subprocess fails, or
synthesis times out, Nova silently falls back to the browser engine —
the read-aloud experience is never lost.

### Privacy

- Piper runs offline. No audio bytes leave this host.
- Nova never auto-installs a Piper binary or voice model.
- No telemetry, no microphone capture, no always-on listening — the
  read-aloud button is the only voice surface in Nova today.

## Configuration

All configuration is read from `.env` at startup. Key variables:

| Variable | Default | Description |
|---|---|---|
| `NOVA_USERNAME` | — | Login username |
| `NOVA_PASSWORD` | — | Login password |
| `NOVA_SECRET_KEY` | — | JWT signing secret |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL |
| `NOVA_AUTO_WEB_LEARNING` | `false` | Enable background RSS/web learning |
| `LOGIN_RATE_LIMIT_MAX` | `5` | Max login attempts per window |
| `LOGIN_RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds (sliding) |
| `LOGIN_RATE_LIMIT_TRUSTED_PROXIES` | — | Comma-separated proxy IPs to trust for `X-Forwarded-For` |
| `NOVA_PIPER_BINARY` | — | Path to the optional Piper TTS binary (blank = auto-detect on `PATH`) |
| `NOVA_PIPER_VOICE_MODEL` | — | Path to a Piper `.onnx` voice model. Blank disables Piper. |
| `NOVA_PIPER_VOICE_CONFIG` | — | Path to the `.onnx.json` config (blank = auto-discover sibling) |
| `NOVA_PIPER_TIMEOUT_SECONDS` | `20` | Synthesis timeout, in seconds |

Model assignments are defined in `config.py` in the `MODELS` dictionary. To swap a model, update that dictionary and restart Nova.

A note on language: the default system prompt and Nova's persona are written in French. Nova auto-detects the language of each message and replies in kind, so English conversations work without any configuration change.

## Development workflow

```bash
# Run the full test suite
pytest

# Run a specific test file with verbose output
pytest tests/test_router.py -v
```

The test suite covers model routing, memory storage and parsing, manual memory commands, rate limiting, the identity contract, and weather integration.

## Docker deployment

A `Dockerfile` and `docker-compose.yml` are included for self-hosted container deployments. The compose stack persists `nova.db` in a Docker volume, keeps Ollama external (reachable via `OLLAMA_HOST`), and supports clean updates via `docker compose pull`.

See [docs/docker.md](docs/docker.md) for the full guide: first run, updates, where data is stored, how to point Nova at an Ollama on the host or another machine, and backup notes.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch and pull request rules.

Short version:

- Branch from `main` with a descriptive name (`feature/…`, `fix/…`, `refactor/…`)
- One change per PR
- Avoid modifying unrelated files
- Keep changes small and readable

## Good first issues

Check the [open issues](https://github.com/TheZupZup/Nova/issues) on GitHub, particularly those labelled `good first issue`. Current tracked issues suitable for new contributors:

- [#23](https://github.com/TheZupZup/Nova/issues/23) Persist the selected model mode across sessions
- [#49](https://github.com/TheZupZup/Nova/issues/49) Add `max_length` constraints to relevant input fields
- [#66](https://github.com/TheZupZup/Nova/issues/66) Replace deprecated `datetime.utcnow()` calls

## Roadmap

The following are areas of active interest, not commitments:

- Hardening the natural language memory pipeline for production use
- Multi-user support (currently single-user via one set of credentials) — see the design plan in [docs/multi-user-architecture.md](docs/multi-user-architecture.md)
- Improved model fallback and error reporting in the web UI
- Broader test coverage

For the longer-term direction — turning Nova into a local-first cognitive copilot with semantic memory, temporal awareness, and Git-aware workflows — see the design document in [docs/cognitive-copilot-roadmap.md](docs/cognitive-copilot-roadmap.md). Like the multi-user plan, it is a design document only; nothing in it is implemented yet.

For the read-only, opt-in integration with SilentGuard (the dedicated network monitoring tool) — including the connector abstraction, JSON contract, and Phase 1 scope — see [docs/silentguard-integration-roadmap.md](docs/silentguard-integration-roadmap.md). Nova reads SilentGuard's local state and explains it; SilentGuard remains the security/enforcement layer.

The integration is **optional** and **off by default**. Nova works normally whether SilentGuard is installed or not. The security provider in `core/security/` probes SilentGuard's on-disk memory file; if `NOVA_SILENTGUARD_API_URL` is set, it instead probes SilentGuard's optional **read-only loopback API** (`GET /status` and a small fixed endpoint list — no writes, no firewall actions, no shell calls). Either way, a missing, unreachable, or malformed response maps to a calm `available=False` snapshot.

## License

[Mozilla Public License 2.0](LICENSE)
