# Changelog

## Unreleased
### Added
- Local response feedback turns thumbs up / thumbs down into a per-user
  preference signal. Ratings are stored locally in SQLite (scoped per
  user, never sent off-host), and a short, deterministic preference
  block is appended to future system prompts below the identity
  contract and the personalization block. Thumbs-down accepts an
  optional short reason; reasons that look like they contain a
  credential are refused at write time. Ratings can be listed and
  deleted via `GET /feedback` and `DELETE /feedback/{id}`.

## v0.4.0 - 2026-04-24
### Added
- Manual web search button in interface
- Adaptive response length — shorter and more direct answers
- Expanded RSS learning sources (HN, Reddit, Ars Technica, Wired)
- Increased knowledge memory limit to 500 entries
- Auto-cleanup of old knowledge memories
- Settings panel with memory management (view, edit, add, delete)
- Copy button on Nova responses
- Automatic language detection FR/EN
- Model selection mode toggle (Auto/Chat/Code/Deep)
- Real-time weather via Open-Meteo API
- Web search via DuckDuckGo
- Automatic knowledge learning via RSS feeds every 6 hours

### Fixed
- Router no longer misclassifies conversational requests as code
- Auto-memory no longer saves web search results as user facts
- Search query cleaning for better results

## v0.3.0 - 2026-04-23
### Added
- Settings panel with memory management
- Copy button on responses
- Automatic language detection FR/EN
- Mode selector (Auto/Chat/Code/Deep)
- Real-time weather via Open-Meteo
- Web search via DuckDuckGo
- Automatic knowledge learning via RSS

## v0.2.0 - 2026-04-23
### Added
- Conversation history with sidebar navigation
- JWT authentication with username and password
- Persistent memory via SQLite with auto-extraction
- Intelligent model routing (gemma3:1b router)
- Mobile-friendly responsive web interface
- Cloudflare Tunnel support

## v0.1.0 - 2026-04-22
### Added
- Initial release
- Basic chat interface
- Ollama integration
- AMD ROCm support
- Multi-model support (gemma4, deepseek-coder-v2, qwen2.5:32b)
- Terminal interface
- FastAPI web server
- systemd service
