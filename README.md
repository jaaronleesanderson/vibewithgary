# Vibe With Gary

AI-powered coding assistant with a chat interface. Code anywhere from desktop or mobile.

## Architecture

```
┌──────────────────────────────────────────┐
│  Web UI (vibewithgary.com)               │
│  - Chat interface                        │
│  - Project/session management            │
│  - GitHub OAuth login                    │
└─────────────────┬────────────────────────┘
                  │ WebSocket
                  ▼
┌──────────────────────────────────────────┐
│  Relay Server                            │
│  - Routes messages between UI & agents   │
│  - Manages sessions                      │
└─────────────────┬────────────────────────┘
                  │ WebSocket
                  ▼
┌──────────────────────────────────────────┐
│  Agent (Local or Cloud VM)               │
│  - Wraps AI coding capabilities          │
│  - File/terminal access                  │
│  - Syncs chat history to GitHub          │
└──────────────────────────────────────────┘
```

## Project Structure

```
vibewithgary/
├── web/          # Chat UI (deployed to vibewithgary.com)
├── agent/        # Desktop agent (runs locally or on VM)
├── relay/        # WebSocket relay server (Fly.io)
└── docs/         # Documentation
```

## Features

- Chat-based coding assistant
- Works on desktop and mobile
- GitHub integration for project storage
- Session persistence (chat history in repo)
- Spin up cloud VM when local machine unavailable

## Quick Start

```bash
# 1. Start the agent on your machine
cd agent
pip install -r requirements.txt
python agent.py

# 2. Open vibewithgary.com
# 3. Login with GitHub
# 4. Start coding!
```
