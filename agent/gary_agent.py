#!/usr/bin/env python3
"""
Gary Agent - Local code execution agent for Vibe With Gary
Connects to the relay server and executes code on your machine.
"""

import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Installing required packages...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    import websockets

# Configuration
RELAY_URL = os.environ.get("GARY_RELAY_URL", "wss://api.vibewithgary.com/ws/agent")
CONFIG_DIR = Path.home() / ".gary-agent"
CONFIG_FILE = CONFIG_DIR / "config.json"

class GaryAgent:
    def __init__(self):
        self.agent_id = None
        self.pairing_code = None
        self.websocket = None
        self.running = False
        self.load_config()

    def load_config(self):
        """Load saved configuration."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                    self.agent_id = config.get("agent_id")
                    self.pairing_code = config.get("pairing_code")
            except Exception as e:
                print(f"Error loading config: {e}")

    def save_config(self):
        """Save configuration."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump({
                "agent_id": self.agent_id,
                "pairing_code": self.pairing_code
            }, f)

    def generate_pairing_code(self):
        """Generate a new pairing code."""
        # 6-character alphanumeric code
        import random
        import string
        chars = string.ascii_uppercase + string.digits
        # Remove confusing characters
        chars = chars.replace("0", "").replace("O", "").replace("I", "").replace("1", "")
        return "".join(random.choices(chars, k=6))

    def get_system_info(self):
        """Get system information."""
        return {
            "os": platform.system(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "hostname": platform.node()
        }

    async def execute_code(self, code: str, language: str, timeout: int = 30) -> dict:
        """Execute code and return the result."""
        result = {
            "success": False,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "duration_ms": 0
        }

        import time
        start_time = time.time()

        try:
            # Create temp file for code
            suffix = {
                "python": ".py",
                "javascript": ".js",
                "bash": ".sh",
                "shell": ".sh"
            }.get(language.lower(), ".txt")

            with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
                f.write(code)
                temp_file = f.name

            try:
                # Determine command based on language
                if language.lower() == "python":
                    cmd = [sys.executable, temp_file]
                elif language.lower() == "javascript":
                    cmd = ["node", temp_file]
                elif language.lower() in ["bash", "shell"]:
                    cmd = ["bash", temp_file]
                else:
                    result["stderr"] = f"Unsupported language: {language}"
                    return result

                # Execute
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=Path.home()
                )

                result["stdout"] = proc.stdout
                result["stderr"] = proc.stderr
                result["exit_code"] = proc.returncode
                result["success"] = proc.returncode == 0

            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_file)
                except:
                    pass

        except subprocess.TimeoutExpired:
            result["stderr"] = f"Execution timed out after {timeout} seconds"
        except FileNotFoundError as e:
            result["stderr"] = f"Runtime not found: {e}"
        except Exception as e:
            result["stderr"] = str(e)

        result["duration_ms"] = int((time.time() - start_time) * 1000)
        return result

    async def handle_message(self, message: dict):
        """Handle incoming message from relay."""
        msg_type = message.get("type")

        if msg_type == "paired":
            print(f"\n  Connected to user session!")
            self.save_config()

        elif msg_type == "execute":
            code = message.get("code", "")
            language = message.get("language", "python")
            request_id = message.get("request_id")

            print(f"\n  Executing {language} code...")
            result = await self.execute_code(code, language)

            # Send result back
            await self.websocket.send(json.dumps({
                "type": "execution_result",
                "request_id": request_id,
                "result": result
            }))

            status = "OK" if result["success"] else "FAILED"
            print(f"  Execution {status} ({result['duration_ms']}ms)")

        elif msg_type == "ping":
            await self.websocket.send(json.dumps({"type": "pong"}))

        elif msg_type == "error":
            print(f"\n  Error: {message.get('message')}")

    async def connect(self):
        """Connect to the relay server."""
        if not self.agent_id:
            self.agent_id = str(uuid.uuid4())

        if not self.pairing_code:
            self.pairing_code = self.generate_pairing_code()
            self.save_config()

        print(f"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   ██████╗  █████╗ ██████╗ ██╗   ██╗                         ║
║  ██╔════╝ ██╔══██╗██╔══██╗╚██╗ ██╔╝                         ║
║  ██║  ███╗███████║██████╔╝ ╚████╔╝                          ║
║  ██║   ██║██╔══██║██╔══██╗  ╚██╔╝                           ║
║  ╚██████╔╝██║  ██║██║  ██║   ██║                            ║
║   ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   AGENT                    ║
║                                                              ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║   Your Pairing Code:  {self.pairing_code}                            ║
║                                                              ║
║   Enter this code at vibewithgary.com when prompted          ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
        """)

        self.running = True
        retry_delay = 1

        while self.running:
            try:
                print("  Connecting to Vibe With Gary...")

                async with websockets.connect(
                    RELAY_URL,
                    additional_headers={
                        "X-Agent-ID": self.agent_id,
                        "X-Pairing-Code": self.pairing_code
                    }
                ) as ws:
                    self.websocket = ws
                    print("  Connected! Waiting for pairing...")
                    retry_delay = 1  # Reset on successful connection

                    # Send registration
                    await ws.send(json.dumps({
                        "type": "register",
                        "agent_id": self.agent_id,
                        "pairing_code": self.pairing_code,
                        "system_info": self.get_system_info()
                    }))

                    # Listen for messages
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            await self.handle_message(data)
                        except json.JSONDecodeError:
                            print(f"  Invalid message received")

            except websockets.exceptions.ConnectionClosed:
                print("  Connection closed. Reconnecting...")
            except Exception as e:
                print(f"  Connection error: {e}")

            if self.running:
                print(f"  Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)  # Exponential backoff, max 30s

    def run(self):
        """Start the agent."""
        try:
            asyncio.run(self.connect())
        except KeyboardInterrupt:
            print("\n  Shutting down...")
            self.running = False


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Gary Agent - Local code execution for Vibe With Gary")
    parser.add_argument("--reset", action="store_true", help="Reset pairing code")
    parser.add_argument("--version", action="store_true", help="Show version")
    args = parser.parse_args()

    if args.version:
        print("Gary Agent v1.0.0")
        return

    agent = GaryAgent()

    if args.reset:
        agent.pairing_code = None
        agent.agent_id = None
        print("Configuration reset. A new pairing code will be generated.")

    agent.run()


if __name__ == "__main__":
    main()
