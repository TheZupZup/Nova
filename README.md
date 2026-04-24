# Nexus

A self-hosted AI assistant with intelligent model routing, persistent memory, and a web interface accessible from any device.

## Overview

Nexus runs entirely on your local machine. It automatically routes each request to the most appropriate model based on complexity, balancing speed and capability without any manual intervention.

## Model Stack

| Model | Role |
|---|---|
| gemma3:1b | Router and simple requests |
| gemma4 | General use and vision |
| deepseek-coder-v2 | Code generation and debugging |
| qwen2.5:32b | Complex reasoning and analysis |

## Features

- Intelligent automatic routing across multiple local models
- Persistent memory via SQLite
- Secured web interface with JWT authentication
- Conversation history with sidebar navigation
- Fully accessible from mobile via any browser
- AMD GPU support via ROCm
- Runs as a systemd service

## Requirements

- Linux (tested on Fedora KDE)
- Python 3.11+
- Ollama
- AMD GPU with ROCm support (or CPU fallback)

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/TheZupZup/Nexus.git
cd Nexus
```

**2. Create a virtual environment and install dependencies**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**3. Pull the required models**

```bash
ollama pull gemma3:1b
ollama pull gemma4
ollama pull deepseek-coder-v2
ollama pull qwen2.5:32b
```

**4. Configure your credentials**

```bash
cp .env.example .env
nano .env
```

Edit `.env` with your chosen username, password, and a secure secret key.

**5. Run Nexus**

```bash
python web.py
```

Nexus will be available at `http://localhost:8080`.

## Running as a Service

To run Nexus automatically on boot:

```bash
sudo nano /etc/systemd/system/nexus.service
```

```ini
[Unit]
Description=Nexus AI
After=network.target ollama.service

[Service]
Type=simple
User=yourusername
WorkingDirectory=/path/to/nexus
ExecStart=/path/to/nexus/.venv/bin/uvicorn web:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
Environment="PATH=/path/to/nexus/.venv/bin:/usr/bin:/usr/local/bin"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable nexus
sudo systemctl start nexus
```

## Project Structure
nexus/
├── core/
│   ├── chat.py       # Conversation logic
│   ├── memory.py     # SQLite persistent memory
│   └── router.py     # Automatic model routing
├── static/
│   └── index.html    # Web interface
├── main.py           # Terminal interface
├── web.py            # FastAPI web server
├── config.py         # Central configuration
└── .env.example      # Credentials template

## Configuration

All model assignments are defined in `core/router.py`. To swap a model, update the `MODEL_MAP` dictionary.

All application settings are in `config.py`.

Credentials are loaded from `.env` and never committed to the repository.

## License

MIT
