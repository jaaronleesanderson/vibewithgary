# Gary Agent

Local code execution agent for [Vibe With Gary](https://vibewithgary.com).

## What It Does

Gary Agent runs on your computer and executes code from your Vibe With Gary conversations locally. This gives you:

- **Full access** to your local files and environment
- **Your installed tools** - Python packages, Node modules, etc.
- **No limits** on execution time or resources
- **Privacy** - code runs on your machine, not in the cloud

## Installation

### Download

Download the latest release for your platform:

- **macOS**: `GaryAgent-mac.dmg`
- **Windows**: `GaryAgent-win.exe`
- **Linux**: `GaryAgent-linux.AppImage`

### From Source

```bash
# Clone the repo
git clone https://github.com/vibewithgary/agent.git
cd agent

# Install dependencies
pip install -r requirements.txt

# Run
python gary_agent.py
```

## Usage

1. Run Gary Agent
2. You'll see a **Pairing Code** (e.g., `ABC123`)
3. Go to [vibewithgary.com](https://vibewithgary.com)
4. Click **Run Locally** and enter your pairing code
5. Start coding with Gary!

## Commands

```bash
# Start the agent
python gary_agent.py

# Reset pairing code
python gary_agent.py --reset

# Show version
python gary_agent.py --version
```

## Building Releases

```bash
# Install build dependencies
pip install pyinstaller

# Build for your platform
python build.py
```

## Configuration

Config is stored in `~/.gary-agent/config.json`:

- `agent_id` - Unique identifier for this agent
- `pairing_code` - Your pairing code

## Security

- Gary Agent only executes code you approve in your Vibe With Gary conversations
- All communication is encrypted (WSS)
- You can review code before execution in the web interface
- Agent runs with your user permissions (not root)

## License

MIT
