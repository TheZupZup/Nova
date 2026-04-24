# Nova

A self-hosted AI assistant with intelligent model routing, persistent memory, and a web interface accessible from any device.

## Overview

Nova runs entirely on your local machine. It automatically routes each request to the most appropriate model based on complexity, balancing speed and capability without any manual intervention.

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
git clone https://github.com/TheZupZup/Nova.git
cd Nova
```bash

**2. Create a virtual environment and install dependencies**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```bash

**3. Pull the required models**

```bash
ollama pull gemma3:1b
ollama pull gemma4
ollama pull deepseek-coder-v2
ollama pull qwen2.5:32b
```bash

**4. Configure your credentials**

```bash
cp .env.example .env
nano .env
```bash

Edit `.env` with your chosen username, password, and a secure secret key.

**5. Run Nova**

```bash
python web.py
```bash

Nova will be available at `http://localhost:8080`.

## Running as a Service

To run Nova automatically on boot:

```bash
sudo nano /etc/systemd/system/nova.service
```bash

```ini
[Unit]
Description=Nova AI
After=network.target ollama.service

[Service]
Type=simple
User=yourusername
WorkingDirectory=/path/to/nova
ExecStart=/path/to/nova/.venv/bin/uvicorn web:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
Environment="PATH=/path/to/nova/.venv/bin:/usr/bin:/usr/local/bin"

[Install]
WantedBy=multi-user.target
```bash

```bash
sudo systemctl daemon-reload
sudo systemctl enable nova
sudo systemctl start nova
```bash

## Project Structure
nova/
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
