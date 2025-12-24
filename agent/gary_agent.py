#!/usr/bin/env python3
"""
Gary Agent - Full-featured local agent for Vibe With Gary
Provides Claude Code-like capabilities: file operations, search, persistent shell.
"""

import asyncio
import fnmatch
import glob as glob_module
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

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

# Operations that require user approval
DANGEROUS_OPERATIONS = {
    "write_file": "📝 Write file",
    "edit_file": "✏️  Edit file",
    "delete_file": "🗑️  Delete file",
    "bash": "🖥️  Run command",
    "execute": "▶️  Execute code"
}

# ANSI color codes
class Colors:
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


class GaryAgent:
    def __init__(self):
        self.agent_id = None
        self.pairing_code = None
        self.websocket = None
        self.running = False
        self.cwd = Path.home()  # Current working directory
        self.pending_approvals = {}  # request_id -> operation details
        self.auto_approve = False  # Set to True to skip approval prompts
        self.trust_session = False  # Trust all operations for this session
        self.load_config()

    def load_config(self):
        """Load saved configuration."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                    self.agent_id = config.get("agent_id")
                    self.pairing_code = config.get("pairing_code")
                    if config.get("cwd"):
                        self.cwd = Path(config["cwd"])
            except Exception as e:
                print(f"Error loading config: {e}")

    def save_config(self):
        """Save configuration."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump({
                "agent_id": self.agent_id,
                "pairing_code": self.pairing_code,
                "cwd": str(self.cwd)
            }, f)

    def generate_pairing_code(self):
        """Generate a new pairing code."""
        import random
        import string
        chars = string.ascii_uppercase + string.digits
        chars = chars.replace("0", "").replace("O", "").replace("I", "").replace("1", "")
        return "".join(random.choices(chars, k=6))

    def get_system_info(self):
        """Get system information."""
        return {
            "os": platform.system(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "hostname": platform.node(),
            "cwd": str(self.cwd),
            "capabilities": [
                "read_file", "write_file", "edit_file", "delete_file",
                "list_dir", "glob", "grep", "bash", "execute"
            ]
        }

    def resolve_path(self, path: str) -> Path:
        """Resolve a path relative to cwd."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()

    # =========================================================================
    # Approval Flow
    # =========================================================================

    def format_operation_summary(self, op_type: str, message: dict) -> dict:
        """Format operation details for sending to web UI."""
        summary = {
            "operation": op_type,
            "operation_name": DANGEROUS_OPERATIONS.get(op_type, op_type),
        }

        if op_type == "write_file":
            content = message.get("content", "")
            summary["path"] = message.get("path", "")
            summary["preview"] = content[:500] + ("..." if len(content) > 500 else "")
            summary["total_lines"] = len(content.split('\n'))

        elif op_type == "edit_file":
            summary["path"] = message.get("path", "")
            summary["old_string"] = message.get("old_string", "")[:200]
            summary["new_string"] = message.get("new_string", "")[:200]

        elif op_type == "delete_file":
            summary["path"] = message.get("path", "")

        elif op_type == "bash":
            summary["command"] = message.get("command", "")
            summary["cwd"] = str(self.cwd)

        elif op_type == "execute":
            code = message.get("code", "")
            summary["language"] = message.get("language", "python")
            summary["preview"] = code[:500] + ("..." if len(code) > 500 else "")
            summary["total_lines"] = len(code.split('\n'))

        return summary

    async def prompt_approval(self, op_type: str, message: dict, request_id: str) -> bool:
        """Request approval from web UI for dangerous operation."""
        if self.auto_approve or self.trust_session:
            return True

        c = Colors
        op_name = DANGEROUS_OPERATIONS.get(op_type, op_type)

        # Generate approval ID
        approval_id = f"approval_{request_id}"

        # Send approval request to web UI via server
        print(f"\n{c.YELLOW}  ⏳ Waiting for approval in web UI: {op_name}{c.RESET}")

        await self.websocket.send(json.dumps({
            "type": "approval_request",
            "approval_id": approval_id,
            "request_id": request_id,
            "details": self.format_operation_summary(op_type, message)
        }))

        # Store pending approval and wait for response
        self.pending_approvals[approval_id] = asyncio.Event()

        try:
            # Wait for approval response (timeout after 60 seconds)
            await asyncio.wait_for(self.pending_approvals[approval_id].wait(), timeout=60.0)
            result = self.pending_approvals.get(f"{approval_id}_result", False)

            if result == "trust":
                self.trust_session = True
                print(f"  {c.GREEN}✓ Approved (trusting session){c.RESET}")
                return True
            elif result:
                print(f"  {c.GREEN}✓ Approved{c.RESET}")
                return True
            else:
                print(f"  {c.RED}✗ Denied{c.RESET}")
                return False

        except asyncio.TimeoutError:
            print(f"  {c.RED}✗ Approval timed out{c.RESET}")
            return False
        finally:
            # Clean up
            self.pending_approvals.pop(approval_id, None)
            self.pending_approvals.pop(f"{approval_id}_result", None)

    # =========================================================================
    # File Operations
    # =========================================================================

    async def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> dict:
        """Read file contents."""
        try:
            file_path = self.resolve_path(path)
            if not file_path.exists():
                return {"success": False, "error": f"File not found: {path}"}
            if not file_path.is_file():
                return {"success": False, "error": f"Not a file: {path}"}

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            # Apply offset and limit
            selected_lines = lines[offset:offset + limit]

            # Format with line numbers
            content = ""
            for i, line in enumerate(selected_lines, start=offset + 1):
                content += f"{i:6}\t{line}"

            return {
                "success": True,
                "content": content,
                "path": str(file_path),
                "total_lines": len(lines),
                "offset": offset,
                "limit": limit
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def write_file(self, path: str, content: str) -> dict:
        """Write content to file."""
        try:
            file_path = self.resolve_path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return {
                "success": True,
                "path": str(file_path),
                "bytes_written": len(content.encode("utf-8"))
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
        """Edit file by replacing text."""
        try:
            file_path = self.resolve_path(path)
            if not file_path.exists():
                return {"success": False, "error": f"File not found: {path}"}

            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            if old_string not in content:
                return {"success": False, "error": "old_string not found in file"}

            if not replace_all:
                # Check if old_string is unique
                count = content.count(old_string)
                if count > 1:
                    return {"success": False, "error": f"old_string found {count} times. Use replace_all=True or provide more context."}

            if replace_all:
                new_content = content.replace(old_string, new_string)
                replacements = content.count(old_string)
            else:
                new_content = content.replace(old_string, new_string, 1)
                replacements = 1

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            return {
                "success": True,
                "path": str(file_path),
                "replacements": replacements
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete_file(self, path: str) -> dict:
        """Delete a file."""
        try:
            file_path = self.resolve_path(path)
            if not file_path.exists():
                return {"success": False, "error": f"File not found: {path}"}

            if file_path.is_dir():
                return {"success": False, "error": "Cannot delete directory with delete_file. Use bash rm -r."}

            file_path.unlink()
            return {"success": True, "path": str(file_path)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Directory Operations
    # =========================================================================

    async def list_dir(self, path: str = ".") -> dict:
        """List directory contents."""
        try:
            dir_path = self.resolve_path(path)
            if not dir_path.exists():
                return {"success": False, "error": f"Directory not found: {path}"}
            if not dir_path.is_dir():
                return {"success": False, "error": f"Not a directory: {path}"}

            entries = []
            for entry in sorted(dir_path.iterdir()):
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat.st_size if entry.is_file() else None,
                    "modified": stat.st_mtime
                })

            return {
                "success": True,
                "path": str(dir_path),
                "entries": entries
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def glob_search(self, pattern: str, path: str = ".") -> dict:
        """Find files matching a glob pattern."""
        try:
            search_path = self.resolve_path(path)
            full_pattern = str(search_path / pattern)

            matches = glob_module.glob(full_pattern, recursive=True)

            # Sort by modification time (newest first)
            matches.sort(key=lambda x: os.path.getmtime(x), reverse=True)

            # Limit results
            matches = matches[:100]

            return {
                "success": True,
                "pattern": pattern,
                "path": str(search_path),
                "matches": matches
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def grep_search(self, pattern: str, path: str = ".", file_pattern: str = "*") -> dict:
        """Search for pattern in files."""
        try:
            search_path = self.resolve_path(path)
            results = []
            regex = re.compile(pattern, re.IGNORECASE)

            def search_file(file_path: Path):
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if regex.search(line):
                                results.append({
                                    "file": str(file_path),
                                    "line": line_num,
                                    "content": line.rstrip()[:200]  # Limit line length
                                })
                                if len(results) >= 100:
                                    return True  # Stop searching
                except:
                    pass
                return False

            # Walk directory tree
            if search_path.is_file():
                search_file(search_path)
            else:
                for root, dirs, files in os.walk(search_path):
                    # Skip hidden and common ignore directories
                    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', '__pycache__', 'venv', '.git']]

                    for filename in files:
                        if fnmatch.fnmatch(filename, file_pattern):
                            file_path = Path(root) / filename
                            if search_file(file_path):
                                break
                    if len(results) >= 100:
                        break

            return {
                "success": True,
                "pattern": pattern,
                "path": str(search_path),
                "results": results
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # =========================================================================
    # Shell Operations
    # =========================================================================

    async def run_bash(self, command: str, timeout: int = 120) -> dict:
        """Run a bash command in the current working directory."""
        try:
            import time
            start_time = time.time()

            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.cwd
            )

            duration_ms = int((time.time() - start_time) * 1000)

            return {
                "success": proc.returncode == 0,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "exit_code": proc.returncode,
                "duration_ms": duration_ms,
                "cwd": str(self.cwd)
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Command timed out after {timeout} seconds"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def change_dir(self, path: str) -> dict:
        """Change the current working directory."""
        try:
            new_cwd = self.resolve_path(path)
            if not new_cwd.exists():
                return {"success": False, "error": f"Directory not found: {path}"}
            if not new_cwd.is_dir():
                return {"success": False, "error": f"Not a directory: {path}"}

            self.cwd = new_cwd
            self.save_config()

            return {"success": True, "cwd": str(self.cwd)}
        except Exception as e:
            return {"success": False, "error": str(e)}

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
                if language.lower() == "python":
                    cmd = [sys.executable, temp_file]
                elif language.lower() == "javascript":
                    cmd = ["node", temp_file]
                elif language.lower() in ["bash", "shell"]:
                    cmd = ["bash", temp_file]
                else:
                    result["stderr"] = f"Unsupported language: {language}"
                    return result

                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=self.cwd
                )

                result["stdout"] = proc.stdout
                result["stderr"] = proc.stderr
                result["exit_code"] = proc.returncode
                result["success"] = proc.returncode == 0

            finally:
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

    # =========================================================================
    # Message Handling
    # =========================================================================

    async def handle_message(self, message: dict):
        """Handle incoming message from relay."""
        msg_type = message.get("type")
        request_id = message.get("request_id")

        if msg_type == "paired":
            print(f"\n  ✓ Connected to user session!")
            self.save_config()

        elif msg_type == "registered":
            pass  # Already printed "Waiting for pairing..."

        elif msg_type == "ping":
            await self.websocket.send(json.dumps({"type": "pong"}))

        elif msg_type == "error":
            print(f"\n  ✗ Error: {message.get('message')}")

        # Handle approval responses from web UI
        elif msg_type == "approval_response":
            approval_id = message.get("approval_id")
            approved = message.get("approved", False)
            trust = message.get("trust", False)

            if approval_id in self.pending_approvals:
                # Store result and signal the waiting coroutine
                self.pending_approvals[f"{approval_id}_result"] = "trust" if trust else approved
                self.pending_approvals[approval_id].set()

        # File operations
        elif msg_type == "read_file":
            result = await self.read_file(
                message.get("path", ""),
                message.get("offset", 0),
                message.get("limit", 2000)
            )
            await self.send_result(request_id, msg_type, result)

        elif msg_type == "write_file":
            if await self.prompt_approval(msg_type, message, request_id):
                result = await self.write_file(
                    message.get("path", ""),
                    message.get("content", "")
                )
                status = "✓" if result["success"] else "✗"
                print(f"  {status} Write {'completed' if result['success'] else 'failed'}")
            else:
                result = {"success": False, "error": "Operation denied by user"}
            await self.send_result(request_id, msg_type, result)

        elif msg_type == "edit_file":
            if await self.prompt_approval(msg_type, message, request_id):
                result = await self.edit_file(
                    message.get("path", ""),
                    message.get("old_string", ""),
                    message.get("new_string", ""),
                    message.get("replace_all", False)
                )
                status = "✓" if result["success"] else "✗"
                print(f"  {status} Edit {'completed' if result['success'] else 'failed'}")
            else:
                result = {"success": False, "error": "Operation denied by user"}
            await self.send_result(request_id, msg_type, result)

        elif msg_type == "delete_file":
            if await self.prompt_approval(msg_type, message, request_id):
                result = await self.delete_file(message.get("path", ""))
                status = "✓" if result["success"] else "✗"
                print(f"  {status} Delete {'completed' if result['success'] else 'failed'}")
            else:
                result = {"success": False, "error": "Operation denied by user"}
            await self.send_result(request_id, msg_type, result)

        # Directory operations
        elif msg_type == "list_dir":
            result = await self.list_dir(message.get("path", "."))
            await self.send_result(request_id, msg_type, result)

        elif msg_type == "glob":
            result = await self.glob_search(
                message.get("pattern", "*"),
                message.get("path", ".")
            )
            await self.send_result(request_id, msg_type, result)

        elif msg_type == "grep":
            result = await self.grep_search(
                message.get("pattern", ""),
                message.get("path", "."),
                message.get("file_pattern", "*")
            )
            await self.send_result(request_id, msg_type, result)

        elif msg_type == "cd":
            result = await self.change_dir(message.get("path", ""))
            if result["success"]:
                print(f"\n  → Changed directory to: {result['cwd']}")
            await self.send_result(request_id, msg_type, result)

        # Shell/code execution
        elif msg_type == "bash":
            if await self.prompt_approval(msg_type, message, request_id):
                cmd = message.get("command", "")
                result = await self.run_bash(cmd, message.get("timeout", 120))
                status = "✓" if result["success"] else "✗"
                print(f"  {status} Command {'completed' if result['success'] else 'failed'} ({result.get('duration_ms', 0)}ms)")
            else:
                result = {"success": False, "error": "Operation denied by user"}
            await self.send_result(request_id, msg_type, result)

        elif msg_type == "execute":
            if await self.prompt_approval(msg_type, message, request_id):
                code = message.get("code", "")
                language = message.get("language", "python")
                result = await self.execute_code(code, language)
                status = "✓" if result["success"] else "✗"
                print(f"  {status} Execution {'completed' if result['success'] else 'failed'} ({result['duration_ms']}ms)")
            else:
                result = {"success": False, "error": "Operation denied by user", "stdout": "", "stderr": "Operation denied by user", "exit_code": -1}
            await self.send_result(request_id, "execution_result", result)

    async def send_result(self, request_id: str, operation: str, result: dict):
        """Send operation result back to server."""
        await self.websocket.send(json.dumps({
            "type": f"{operation}_result",
            "request_id": request_id,
            "result": result
        }))

    async def connect(self):
        """Connect to the relay server."""
        if not self.agent_id:
            self.agent_id = str(uuid.uuid4())

        if not self.pairing_code:
            self.pairing_code = self.generate_pairing_code()
            self.save_config()

        # Determine approval mode status
        if self.auto_approve:
            approval_status = "⚠️  AUTO-APPROVE (no prompts)"
        elif self.trust_session:
            approval_status = "⚠️  TRUSTED SESSION"
        else:
            approval_status = "✓  Approval required for writes"

        print(f"""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   ██████╗  █████╗ ██████╗ ██╗   ██╗                         ║
║  ██╔════╝ ██╔══██╗██╔══██╗╚██╗ ██╔╝                         ║
║  ██║  ███╗███████║██████╔╝ ╚████╔╝                          ║
║  ██║   ██║██╔══██║██╔══██╗  ╚██╔╝                           ║
║  ╚██████╔╝██║  ██║██║  ██║   ██║                            ║
║   ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   AGENT v2.0               ║
║                                                              ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║   Your Pairing Code:  {self.pairing_code}                            ║
║                                                              ║
║   Enter this code at vibewithgary.com when prompted          ║
║                                                              ║
║   Capabilities: read, write, edit, search, bash, execute     ║
║   Working Dir:  {str(self.cwd)[:40]:<40} ║
║   Security:     {approval_status:<40} ║
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
                    retry_delay = 1

                    await ws.send(json.dumps({
                        "type": "register",
                        "agent_id": self.agent_id,
                        "pairing_code": self.pairing_code,
                        "system_info": self.get_system_info()
                    }))

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
                retry_delay = min(retry_delay * 2, 30)

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

    parser = argparse.ArgumentParser(description="Gary Agent v2.0 - Full-featured local agent for Vibe With Gary")
    parser.add_argument("--reset", action="store_true", help="Reset pairing code")
    parser.add_argument("--version", action="store_true", help="Show version")
    parser.add_argument("--cwd", type=str, help="Set initial working directory")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-approve all operations (use with caution)")
    parser.add_argument("--trust", action="store_true", help="Trust all operations for this session")
    args = parser.parse_args()

    if args.version:
        print("Gary Agent v2.0.0")
        return

    agent = GaryAgent()

    if args.reset:
        agent.pairing_code = None
        agent.agent_id = None
        print("Configuration reset. A new pairing code will be generated.")

    if args.cwd:
        agent.cwd = Path(args.cwd).expanduser().resolve()
        print(f"Working directory set to: {agent.cwd}")

    if args.auto_approve:
        agent.auto_approve = True
        print(f"{Colors.YELLOW}⚠️  Auto-approve mode: All operations will be executed without confirmation{Colors.RESET}")

    if args.trust:
        agent.trust_session = True
        print(f"{Colors.YELLOW}⚠️  Trust mode: All operations will be approved for this session{Colors.RESET}")

    agent.run()


if __name__ == "__main__":
    main()
