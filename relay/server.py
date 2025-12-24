"""
Vibe With Gary - Relay Server
Routes WebSocket traffic between web clients and desktop agents.
"""

import asyncio
import secrets
import time
import sqlite3
import os
import json
import base64
import hashlib
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dataclasses import dataclass, field
from typing import Optional
from contextlib import contextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
import uvicorn
import httpx

app = FastAPI(title="Vibe With Gary API")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

# =============================================================================
# Database
# =============================================================================

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "gary.db")


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    # Users table - one per user
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            github_id TEXT,
            github_username TEXT,
            github_token TEXT,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_github_id ON users(github_id)")

    # API keys/tokens table - multiple per user (agent api_key + web session tokens)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            api_key TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            key_type TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_apikey_user ON api_keys(user_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pairing_codes (
            code TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT,
            title TEXT,
            messages TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chatsessions_user ON chat_sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chatsessions_project ON chat_sessions(project_id)")

    # Cloud VMs table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cloud_vms (
            vm_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            machine_id TEXT,
            status TEXT NOT NULL,
            region TEXT,
            created_at REAL NOT NULL,
            last_active REAL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vms_user ON cloud_vms(user_id)")

    # Usage tracking for billing
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_logs (
            log_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT,
            session_id TEXT,
            execution_type TEXT NOT NULL,
            code_size INTEGER,
            start_time REAL NOT NULL,
            end_time REAL,
            duration_ms INTEGER,
            vm_id TEXT,
            exit_code INTEGER,
            billable_units INTEGER DEFAULT 0,
            billed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_logs(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_billed ON usage_logs(billed)")

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# =============================================================================
# Models
# =============================================================================

@dataclass
class DesktopAgent:
    websocket: WebSocket
    user_id: Optional[str] = None  # None until paired
    agent_id: Optional[str] = None
    pairing_code: Optional[str] = None
    connected_at: float = field(default_factory=time.time)
    mobile_client: Optional[WebSocket] = None


@dataclass
class User:
    user_id: str
    github_id: Optional[str] = None
    github_username: Optional[str] = None
    github_token: Optional[str] = None
    created_at: float = field(default_factory=time.time)


# In-memory (WebSocket connections only)
desktop_agents: dict[str, DesktopAgent] = {}  # user_id -> agent
pending_agents: dict[str, DesktopAgent] = {}  # pairing_code -> agent (awaiting pairing)


# =============================================================================
# Auth Helpers
# =============================================================================

def generate_api_key() -> str:
    return f"gary_{secrets.token_urlsafe(32)}"


def generate_pairing_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def get_user_from_api_key(api_key: str) -> Optional[User]:
    """Look up user by API key or session token."""
    with get_db() as conn:
        # First find the user_id from api_keys table
        key_row = conn.execute(
            "SELECT user_id FROM api_keys WHERE api_key = ?",
            (api_key,)
        ).fetchone()

        if not key_row:
            return None

        user_id = key_row["user_id"]

        # Then get the full user info
        row = conn.execute(
            "SELECT user_id, github_id, github_username, github_token, created_at FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row:
            return User(
                user_id=row["user_id"],
                github_id=row["github_id"],
                github_username=row["github_username"],
                github_token=row["github_token"],
                created_at=row["created_at"]
            )
    return None


def get_user_from_github_id(github_id: str) -> Optional[User]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, github_id, github_username, github_token, created_at FROM users WHERE github_id = ?",
            (github_id,)
        ).fetchone()
        if row:
            return User(
                user_id=row["user_id"],
                github_id=row["github_id"],
                github_username=row["github_username"],
                github_token=row["github_token"],
                created_at=row["created_at"]
            )
    return None


def save_user(user: User):
    """Save or update a user."""
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO users
               (user_id, github_id, github_username, github_token, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user.user_id, user.github_id, user.github_username, user.github_token, user.created_at)
        )
        conn.commit()


def save_api_key(api_key: str, user_id: str, key_type: str = "agent"):
    """Save an API key or session token for a user."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_keys (api_key, user_id, key_type, created_at) VALUES (?, ?, ?, ?)",
            (api_key, user_id, key_type, time.time())
        )
        conn.commit()


def save_pairing_code(code: str, user_id: str, expires_at: float):
    with get_db() as conn:
        conn.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT OR REPLACE INTO pairing_codes (code, user_id, expires_at) VALUES (?, ?, ?)",
            (code, user_id, expires_at)
        )
        conn.commit()


def get_pairing_code(code: str) -> Optional[tuple[str, float]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM pairing_codes WHERE code = ?",
            (code,)
        ).fetchone()
        if row:
            return (row["user_id"], row["expires_at"])
    return None


def delete_pairing_code(code: str):
    with get_db() as conn:
        conn.execute("DELETE FROM pairing_codes WHERE code = ?", (code,))
        conn.commit()


# =============================================================================
# Project Helpers
# =============================================================================

def create_project(user_id: str, name: str) -> str:
    """Create a new project for a user."""
    project_id = secrets.token_urlsafe(8)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (project_id, user_id, name, created_at) VALUES (?, ?, ?, ?)",
            (project_id, user_id, name, time.time())
        )
        conn.commit()
    print(f"[Gary] Created project '{name}' for user {user_id[:8]}")
    return project_id


def get_projects(user_id: str) -> list:
    """Get all projects for a user."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT project_id, name, created_at FROM projects WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
    return [{"id": r["project_id"], "name": r["name"], "created_at": r["created_at"]} for r in rows]


def get_project(project_id: str, user_id: str) -> dict:
    """Get a specific project."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT project_id, name, created_at FROM projects WHERE project_id = ? AND user_id = ?",
            (project_id, user_id)
        ).fetchone()
    if row:
        return {"id": row["project_id"], "name": row["name"], "created_at": row["created_at"]}
    return None


def update_project(project_id: str, user_id: str, name: str) -> bool:
    """Update a project name."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE projects SET name = ? WHERE project_id = ? AND user_id = ?",
            (name, project_id, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_project(project_id: str, user_id: str) -> bool:
    """Delete a project and all its chats."""
    with get_db() as conn:
        # Delete associated chats first
        conn.execute("DELETE FROM chat_sessions WHERE project_id = ? AND user_id = ?", (project_id, user_id))
        cursor = conn.execute("DELETE FROM projects WHERE project_id = ? AND user_id = ?", (project_id, user_id))
        conn.commit()
        return cursor.rowcount > 0


def get_project_chats(project_id: str, user_id: str) -> list:
    """Get all chats for a project."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT session_id, title, created_at, updated_at FROM chat_sessions WHERE project_id = ? AND user_id = ? ORDER BY updated_at DESC",
            (project_id, user_id)
        ).fetchall()
    return [{"id": r["session_id"], "title": r["title"], "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


# =============================================================================
# REST Endpoints
# =============================================================================

@app.post("/api/register")
async def register():
    """Register a new user."""
    user_id = secrets.token_urlsafe(16)
    api_key = generate_api_key()
    user = User(user_id=user_id)
    save_user(user)
    save_api_key(api_key, user_id, "agent")
    return {"user_id": user_id, "api_key": api_key}


@app.post("/api/pairing-code")
async def create_pairing_code(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Generate a pairing code."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    code = generate_pairing_code()
    save_pairing_code(code, user.user_id, time.time() + 300)
    return {"pairing_code": code, "expires_in": 300}


@app.post("/api/pair")
async def pair_with_code(code: str):
    """Exchange pairing code for session token."""
    result = get_pairing_code(code)
    if not result:
        raise HTTPException(status_code=400, detail="Invalid or expired pairing code")

    user_id, expires_at = result
    if time.time() > expires_at:
        delete_pairing_code(code)
        raise HTTPException(status_code=400, detail="Pairing code expired")

    delete_pairing_code(code)

    session_token = f"session_{secrets.token_urlsafe(32)}"
    save_api_key(session_token, user_id, "session")

    return {"session_token": session_token, "user_id": user_id}


@app.get("/api/status")
async def status(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Check if desktop agent is connected."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    agent = desktop_agents.get(user.user_id)
    return {
        "desktop_connected": agent is not None,
        "connected_since": agent.connected_at if agent else None,
    }


@app.post("/api/pair-agent")
async def pair_with_agent(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Pair with a desktop agent using its pairing code."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    data = await request.json()
    code = data.get("code", "").upper().strip()

    if not code:
        raise HTTPException(status_code=400, detail="Pairing code required")

    # Find pending agent with this code
    agent = pending_agents.get(code)
    if not agent:
        raise HTTPException(status_code=404, detail="No agent found with this code. Make sure the agent is running.")

    # Pair the agent with this user
    agent.user_id = user.user_id
    desktop_agents[user.user_id] = agent
    del pending_agents[code]

    # Notify the agent it's paired
    try:
        await agent.websocket.send_json({
            "type": "paired",
            "user_id": user.user_id,
            "message": "Successfully paired with user"
        })
    except:
        pass

    print(f"[Agent] Paired: {code} -> user {user.user_id[:8]}...")
    return {"success": True, "message": "Agent paired successfully"}


@app.get("/api/me")
async def get_me(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get current user info."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {
        "user_id": user.user_id,
        "github_username": user.github_username,
        "created_at": user.created_at
    }


# =============================================================================
# Project API
# =============================================================================

@app.get("/api/projects")
async def list_projects(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """List all projects for a user."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    projects = get_projects(user.user_id)
    return {"projects": projects}


@app.post("/api/projects")
async def create_project_endpoint(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Create a new project."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    data = await request.json()
    name = data.get("name", "New Project")

    project_id = create_project(user.user_id, name)
    return {"project_id": project_id, "name": name}


@app.get("/api/projects/{project_id}")
async def get_project_endpoint(project_id: str, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get a project with its chats."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    project = get_project(project_id, user.user_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    chats = get_project_chats(project_id, user.user_id)
    return {**project, "chats": chats}


@app.put("/api/projects/{project_id}")
async def update_project_endpoint(project_id: str, request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Update a project."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    data = await request.json()
    name = data.get("name")

    if not name:
        raise HTTPException(status_code=400, detail="Name required")

    success = update_project(project_id, user.user_id, name)
    if not success:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"success": True}


@app.delete("/api/projects/{project_id}")
async def delete_project_endpoint(project_id: str, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Delete a project."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    success = delete_project(project_id, user.user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"success": True}


# =============================================================================
# GitHub OAuth
# =============================================================================

GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
APP_URL = os.environ.get("APP_URL", "https://www.vibewithgary.com")
API_URL = os.environ.get("API_URL", "https://api.vibewithgary.com")


@app.get("/auth/github")
async def github_auth():
    """Redirect to GitHub OAuth."""
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured")

    redirect_uri = f"{API_URL}/auth/github/callback"
    scope = "read:user,repo"
    return RedirectResponse(
        f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={redirect_uri}&scope={scope}"
    )


@app.get("/auth/github/callback")
async def github_callback(code: str):
    """Handle GitHub OAuth callback."""
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured")

    # Exchange code for access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"}
        )
        token_data = token_resp.json()

        if "error" in token_data:
            raise HTTPException(status_code=400, detail=token_data.get("error_description", "OAuth failed"))

        access_token = token_data["access_token"]

        # Get user info from GitHub
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            }
        )
        github_user = user_resp.json()

    github_id = str(github_user["id"])
    github_username = github_user["login"]

    # Check if user exists
    user = get_user_from_github_id(github_id)

    is_new_user = False
    if user:
        # Update existing user with new token
        user.github_token = access_token
        user.github_username = github_username
        save_user(user)
    else:
        # Create new user
        is_new_user = True
        user_id = secrets.token_urlsafe(16)
        user = User(
            user_id=user_id,
            github_id=github_id,
            github_username=github_username,
            github_token=access_token
        )
        save_user(user)
        # Also create an agent API key for this user
        api_key = generate_api_key()
        save_api_key(api_key, user_id, "agent")

    # Create Gary repo if it doesn't exist
    async with httpx.AsyncClient() as client:
        # Check if Gary repo exists
        repo_check = await client.get(
            f"https://api.github.com/repos/{github_username}/Gary",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            }
        )

        if repo_check.status_code == 404:
            # Create the Gary repo
            create_resp = await client.post(
                "https://api.github.com/user/repos",
                json={
                    "name": "Gary",
                    "description": "My coding sessions with Gary - vibewithgary.com",
                    "private": True,
                    "auto_init": True
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json"
                }
            )
            if create_resp.status_code in [200, 201]:
                print(f"[Gary] Created Gary repo for {github_username}")

    # Create first project for new users
    if is_new_user:
        create_project(user.user_id, "Chillin and Feelin with Gary")

    # Create a session token for the web client
    session_token = f"session_{secrets.token_urlsafe(32)}"
    save_api_key(session_token, user.user_id, "session")

    # Redirect back to app with session token
    return RedirectResponse(f"{APP_URL}?token={session_token}&github_user={github_username}")


# =============================================================================
# Cloud VM Management
# =============================================================================

FLY_API_TOKEN = os.environ.get("FLY_API_TOKEN", "")
FLY_ORG = os.environ.get("FLY_ORG", "personal")
VM_APP_NAME = "gary-vms"  # Fly app for user VMs
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# =============================================================================
# Gary Chat (Direct Claude API - no VMs needed)
# =============================================================================

GARY_SYSTEM_PROMPT = """You are Gary, a chill coding buddy at vibewithgary.com.

YOUR PERSONALITY:
- Laid back, friendly, use casual language naturally
- Words like "bro", "dude", "sick", "fire", "vibes" when it fits
- Helpful and knowledgeable but keep it chill
- Give concise, practical answers with code examples
- When in doubt, just help - don't over-ask for clarification

FOR NEW USERS:
Welcome them warmly! Ask what they want to build or learn.
If they ask "how do I code here", explain they can just ask you coding questions and you'll help.

FOR POWER USERS who want local access:
- Mac: curl -fsSL https://raw.githubusercontent.com/vibewithgary/gary/main/agent/install-mac.sh | bash
- Linux: curl -fsSL https://raw.githubusercontent.com/vibewithgary/gary/main/agent/install-linux.sh | bash
- Windows: irm https://raw.githubusercontent.com/vibewithgary/gary/main/agent/install-windows.ps1 | iex
"""

class GaryChat:
    """Direct Claude API chat - handles all user conversations."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.conversations = {}  # user_id -> message history

    async def chat(self, user_id: str, message: str) -> str:
        """Send a message to Claude and get a response."""
        if not self.api_key:
            return "Hey bro, I'm not configured yet. Admin needs to set up the API key."

        # Get or create conversation history
        if user_id not in self.conversations:
            self.conversations[user_id] = []

        history = self.conversations[user_id]
        history.append({"role": "user", "content": message})

        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "system": GARY_SYSTEM_PROMPT,
            "messages": history[-20:]
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01"
                    },
                    timeout=120.0
                )

                if resp.status_code != 200:
                    print(f"[Gary] API error: {resp.status_code} {resp.text}")
                    return "Yo, hit a snag. Try again in a sec, bro."

                result = resp.json()
                response_text = result["content"][0]["text"]
                history.append({"role": "assistant", "content": response_text})
                return response_text

        except Exception as e:
            print(f"[Gary] API error: {e}")
            return "Something went sideways, dude. Try again?"

    def clear_history(self, user_id: str):
        if user_id in self.conversations:
            del self.conversations[user_id]

    def get_history(self, user_id: str) -> list:
        return self.conversations.get(user_id, [])


# Global chat instance
gary_chat: GaryChat = None


class VMManager:
    """Manages cloud VMs for users using Fly.io Machines API."""

    def __init__(self, fly_token: str, org: str = "personal"):
        self.fly_token = fly_token
        self.org = org
        self.api_base = "https://api.machines.dev/v1"

    async def create_vm(self, user_id: str, github_token: str = None, encryption_key: str = None) -> dict:
        """Create a new VM for a user."""
        import urllib.request
        import urllib.error

        vm_id = secrets.token_urlsafe(8)

        # Prepare environment variables for the VM
        env_vars = {
            "GARY_USER_ID": user_id,
            "GARY_RELAY_URL": f"wss://api.vibewithgary.com",
        }
        if github_token:
            env_vars["GARY_GITHUB_TOKEN"] = github_token
        if encryption_key:
            env_vars["GARY_ENCRYPTION_KEY"] = encryption_key
        if ANTHROPIC_API_KEY:
            env_vars["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

        # Create machine config
        machine_config = {
            "name": f"gary-{vm_id}",
            "config": {
                "image": "registry.fly.io/gary-vms:latest",
                "env": env_vars,
                "guest": {
                    "cpu_kind": "shared",
                    "cpus": 1,
                    "memory_mb": 512
                },
                "auto_destroy": True,
                "restart": {
                    "policy": "no"
                }
            }
        }

        # Create machine via Fly API
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.api_base}/apps/{VM_APP_NAME}/machines",
                    json=machine_config,
                    headers={
                        "Authorization": f"Bearer {self.fly_token}",
                        "Content-Type": "application/json"
                    },
                    timeout=60.0
                )

                if resp.status_code != 200:
                    print(f"[VMManager] Failed to create VM: {resp.status_code} {resp.text}")
                    return None

                machine_data = resp.json()
                machine_id = machine_data["id"]

                # Save to database
                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO cloud_vms (vm_id, user_id, machine_id, status, region, created_at, last_active)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (vm_id, user_id, machine_id, "running", machine_data.get("region", "unknown"),
                         time.time(), time.time())
                    )
                    conn.commit()

                print(f"[VMManager] Created VM {vm_id} (machine: {machine_id}) for user {user_id[:8]}")
                return {"vm_id": vm_id, "machine_id": machine_id, "status": "running"}

        except Exception as e:
            print(f"[VMManager] Error creating VM: {e}")
            return None

    async def destroy_vm(self, vm_id: str, user_id: str) -> bool:
        """Destroy a VM."""
        # Get machine ID
        with get_db() as conn:
            row = conn.execute(
                "SELECT machine_id FROM cloud_vms WHERE vm_id = ? AND user_id = ?",
                (vm_id, user_id)
            ).fetchone()

        if not row:
            return False

        machine_id = row["machine_id"]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(
                    f"{self.api_base}/apps/{VM_APP_NAME}/machines/{machine_id}?force=true",
                    headers={"Authorization": f"Bearer {self.fly_token}"},
                    timeout=30.0
                )

                # Update database
                with get_db() as conn:
                    conn.execute(
                        "UPDATE cloud_vms SET status = 'destroyed' WHERE vm_id = ?",
                        (vm_id,)
                    )
                    conn.commit()

                print(f"[VMManager] Destroyed VM {vm_id}")
                return True

        except Exception as e:
            print(f"[VMManager] Error destroying VM: {e}")
            return False

    async def get_vm_status(self, vm_id: str, user_id: str) -> dict:
        """Get VM status."""
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM cloud_vms WHERE vm_id = ? AND user_id = ?",
                (vm_id, user_id)
            ).fetchone()

        if not row:
            return None

        return {
            "vm_id": row["vm_id"],
            "status": row["status"],
            "region": row["region"],
            "created_at": row["created_at"],
            "last_active": row["last_active"]
        }

    async def list_user_vms(self, user_id: str) -> list:
        """List all VMs for a user."""
        with get_db() as conn:
            rows = conn.execute(
                "SELECT vm_id, status, region, created_at, last_active FROM cloud_vms WHERE user_id = ? AND status != 'destroyed' ORDER BY created_at DESC",
                (user_id,)
            ).fetchall()

        return [dict(row) for row in rows]


# Global VM manager
vm_manager: VMManager = None


@app.post("/api/vm/create")
async def create_vm(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Create a cloud VM for the user."""
    global vm_manager
    if not vm_manager:
        raise HTTPException(status_code=503, detail="VM provisioning not configured")

    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check if user already has a running VM
    existing = await vm_manager.list_user_vms(user.user_id)
    running_vms = [v for v in existing if v["status"] == "running"]
    if running_vms:
        return {"error": "You already have a running VM", "vm": running_vms[0]}

    # Create new VM
    result = await vm_manager.create_vm(
        user_id=user.user_id,
        github_token=user.github_token
    )

    if not result:
        raise HTTPException(status_code=500, detail="Failed to create VM")

    return result


@app.delete("/api/vm/{vm_id}")
async def destroy_vm(vm_id: str, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Destroy a cloud VM."""
    global vm_manager
    if not vm_manager:
        raise HTTPException(status_code=503, detail="VM provisioning not configured")

    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    success = await vm_manager.destroy_vm(vm_id, user.user_id)
    if not success:
        raise HTTPException(status_code=404, detail="VM not found")

    return {"success": True}


@app.get("/api/vm")
async def list_vms(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """List user's VMs."""
    global vm_manager
    if not vm_manager:
        return {"vms": [], "message": "VM provisioning not configured"}

    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    vms = await vm_manager.list_user_vms(user.user_id)
    return {"vms": vms}


@app.get("/api/vm/{vm_id}")
async def get_vm(vm_id: str, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get VM status."""
    global vm_manager
    if not vm_manager:
        raise HTTPException(status_code=503, detail="VM provisioning not configured")

    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    vm = await vm_manager.get_vm_status(vm_id, user.user_id)
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")

    return vm


# =============================================================================
# Session Sync API
# =============================================================================

@app.get("/api/sessions")
async def list_sessions(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """List all sessions for a user."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT session_id, title, created_at, updated_at FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC",
            (user.user_id,)
        ).fetchall()

    return [{"id": r["session_id"], "title": r["title"], "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


@app.post("/api/sessions")
async def create_session(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Create a new session."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    data = await request.json()
    session_id = secrets.token_urlsafe(16)
    now = time.time()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (session_id, user_id, title, messages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user.user_id, data.get("title", "New Chat"), json.dumps([]), now, now)
        )
        conn.commit()

    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get a session with messages."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE session_id = ? AND user_id = ?",
            (session_id, user.user_id)
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "id": row["session_id"],
        "title": row["title"],
        "messages": json.loads(row["messages"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"]
    }


@app.put("/api/sessions/{session_id}")
async def update_session(session_id: str, request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Update a session."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    data = await request.json()
    now = time.time()

    with get_db() as conn:
        conn.execute(
            "UPDATE chat_sessions SET title = ?, messages = ?, updated_at = ? WHERE session_id = ? AND user_id = ?",
            (data.get("title"), json.dumps(data.get("messages", [])), now, session_id, user.user_id)
        )
        conn.commit()

    return {"success": True}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Delete a session."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    with get_db() as conn:
        conn.execute("DELETE FROM chat_sessions WHERE session_id = ? AND user_id = ?", (session_id, user.user_id))
        conn.commit()

    return {"success": True}


# =============================================================================
# Usage & Billing
# =============================================================================

@app.get("/api/usage")
async def get_usage(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get usage stats for the current user."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    with get_db() as conn:
        # Total usage
        total = conn.execute(
            """SELECT
                COUNT(*) as total_executions,
                SUM(duration_ms) as total_duration_ms,
                SUM(billable_units) as total_billable_units,
                SUM(CASE WHEN execution_type = 'virtual' THEN 1 ELSE 0 END) as virtual_count,
                SUM(CASE WHEN execution_type = 'local' THEN 1 ELSE 0 END) as local_count
               FROM usage_logs WHERE user_id = ?""",
            (user.user_id,)
        ).fetchone()

        # Unbilled usage
        unbilled = conn.execute(
            """SELECT
                COUNT(*) as executions,
                SUM(billable_units) as billable_units
               FROM usage_logs WHERE user_id = ? AND billed = 0""",
            (user.user_id,)
        ).fetchone()

        # Recent executions
        recent = conn.execute(
            """SELECT log_id, execution_type, code_size, start_time, duration_ms,
                      exit_code, billable_units, vm_id
               FROM usage_logs WHERE user_id = ?
               ORDER BY start_time DESC LIMIT 20""",
            (user.user_id,)
        ).fetchall()

    return {
        "total": {
            "executions": total["total_executions"] or 0,
            "duration_ms": total["total_duration_ms"] or 0,
            "billable_units": total["total_billable_units"] or 0,
            "virtual_count": total["virtual_count"] or 0,
            "local_count": total["local_count"] or 0
        },
        "unbilled": {
            "executions": unbilled["executions"] or 0,
            "billable_units": unbilled["billable_units"] or 0,
            # $0.001 per billable unit (100ms of compute)
            "estimated_cost_usd": (unbilled["billable_units"] or 0) * 0.001
        },
        "recent": [
            {
                "log_id": r["log_id"],
                "type": r["execution_type"],
                "code_size": r["code_size"],
                "timestamp": r["start_time"],
                "duration_ms": r["duration_ms"],
                "exit_code": r["exit_code"],
                "billable_units": r["billable_units"],
                "vm_id": r["vm_id"]
            }
            for r in recent
        ]
    }


@app.post("/api/usage/mark-billed")
async def mark_usage_billed(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Mark usage as billed (for payment processing)."""
    user = get_user_from_api_key(credentials.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    data = await request.json()
    log_ids = data.get("log_ids", [])

    if log_ids:
        with get_db() as conn:
            placeholders = ",".join("?" * len(log_ids))
            conn.execute(
                f"UPDATE usage_logs SET billed = 1 WHERE log_id IN ({placeholders}) AND user_id = ?",
                (*log_ids, user.user_id)
            )
            conn.commit()

    return {"success": True, "marked": len(log_ids)}


# =============================================================================
# Encryption & GitHub Sync
# =============================================================================

GARY_ENCRYPTION_SALT = os.environ.get("GARY_ENCRYPTION_SALT", "vibewithgary2024").encode()


def get_encryption_key(user_id: str) -> bytes:
    """Generate a deterministic encryption key from user_id."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=GARY_ENCRYPTION_SALT,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(user_id.encode()))


def encrypt_data(data: str, user_id: str) -> str:
    """Encrypt data using user-specific key."""
    key = get_encryption_key(user_id)
    f = Fernet(key)
    return f.encrypt(data.encode()).decode()


def decrypt_data(encrypted: str, user_id: str) -> str:
    """Decrypt data using user-specific key."""
    key = get_encryption_key(user_id)
    f = Fernet(key)
    return f.decrypt(encrypted.encode()).decode()


def extract_code_blocks(content: str) -> list:
    """Extract code blocks from message content."""
    import re
    pattern = r'```(\w*)\n([\s\S]*?)```'
    matches = re.findall(pattern, content)
    blocks = []
    for lang, code in matches:
        # Determine file extension
        ext_map = {
            'python': 'py', 'py': 'py',
            'javascript': 'js', 'js': 'js',
            'typescript': 'ts', 'ts': 'ts',
            'html': 'html', 'css': 'css',
            'json': 'json', 'yaml': 'yaml', 'yml': 'yml',
            'bash': 'sh', 'sh': 'sh', 'shell': 'sh',
            'sql': 'sql', 'go': 'go', 'rust': 'rs',
            'java': 'java', 'c': 'c', 'cpp': 'cpp',
            'ruby': 'rb', 'php': 'php', 'swift': 'swift',
            '': 'txt'
        }
        ext = ext_map.get(lang.lower(), 'txt')
        blocks.append({'lang': lang or 'text', 'code': code.strip(), 'ext': ext})
    return blocks


FLY_API_TOKEN = os.environ.get("FLY_API_TOKEN", "")
FLY_APP_NAME = "vibewithgary-runners"  # App for ephemeral runner machines


def detect_language(code: str) -> tuple[str, str, str]:
    """Detect language and return (lang, filename, docker_image)."""
    is_python = any(kw in code for kw in ['def ', 'import ', 'print(', 'class ', 'if __name__'])
    is_node = any(kw in code for kw in ['const ', 'let ', 'var ', 'console.log', 'function ', '=>'])
    is_bash = code.strip().startswith('#!') or any(kw in code for kw in ['echo ', 'ls ', 'cd ', 'mkdir '])

    if is_python or (not is_node and not is_bash):
        return ("python", "code.py", "python:3.11-slim")
    elif is_node:
        return ("node", "code.js", "node:20-slim")
    else:
        return ("bash", "code.sh", "alpine:latest")


async def run_code_virtual(code: str, user_id: str, log_id: str) -> dict:
    """Run code in a Fly.io ephemeral machine."""
    import subprocess
    import tempfile

    lang, filename, image = detect_language(code)

    # First try Fly.io Machines API if token is available
    if FLY_API_TOKEN:
        try:
            result = await run_on_fly_machine(code, lang, filename, image, user_id, log_id)
            if result:
                return result
        except Exception as e:
            print(f"Fly.io machine failed: {e}, falling back to local sandbox")

    # Fallback to local sandboxed execution
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, filename)
            with open(filepath, "w") as f:
                f.write(code)

            if lang == "python":
                cmd = ["python3", filepath]
            elif lang == "node":
                cmd = ["node", filepath]
            else:
                cmd = ["bash", filepath]

            # Run with timeout (30 seconds max)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tmpdir
            )

            output = result.stdout
            if result.stderr:
                output += "\n" + result.stderr if output else result.stderr

            return {
                "output": output[:10000],
                "exit_code": result.returncode,
                "vm_id": "local-sandbox"
            }

    except subprocess.TimeoutExpired:
        return {
            "output": "Execution timed out (30 second limit)",
            "exit_code": 124,
            "vm_id": "local-sandbox"
        }
    except Exception as e:
        return {
            "output": f"Execution error: {str(e)}",
            "exit_code": 1,
            "vm_id": "local-sandbox"
        }


async def run_on_fly_machine(code: str, lang: str, filename: str, image: str, user_id: str, log_id: str) -> dict:
    """Spin up an ephemeral Fly.io machine to run code."""
    import base64

    # Encode code as base64 for safe transmission
    code_b64 = base64.b64encode(code.encode()).decode()

    # Build the run command
    if lang == "python":
        run_cmd = f'echo "{code_b64}" | base64 -d > /code/{filename} && python3 /code/{filename}'
    elif lang == "node":
        run_cmd = f'echo "{code_b64}" | base64 -d > /code/{filename} && node /code/{filename}'
    else:
        run_cmd = f'echo "{code_b64}" | base64 -d > /code/{filename} && bash /code/{filename}'

    machine_config = {
        "name": f"runner-{log_id[:8]}",
        "config": {
            "image": image,
            "guest": {
                "cpu_kind": "shared",
                "cpus": 1,
                "memory_mb": 256
            },
            "auto_destroy": True,
            "restart": {"policy": "no"},
            "init": {
                "cmd": ["sh", "-c", f"mkdir -p /code && {run_cmd}"]
            }
        }
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        headers = {
            "Authorization": f"Bearer {FLY_API_TOKEN}",
            "Content-Type": "application/json"
        }

        # Create and start machine
        create_resp = await client.post(
            f"https://api.machines.dev/v1/apps/{FLY_APP_NAME}/machines",
            headers=headers,
            json=machine_config
        )

        if create_resp.status_code != 200:
            raise Exception(f"Failed to create machine: {create_resp.text}")

        machine = create_resp.json()
        machine_id = machine["id"]

        # Wait for machine to complete (poll status)
        max_wait = 30
        waited = 0
        while waited < max_wait:
            await asyncio.sleep(1)
            waited += 1

            status_resp = await client.get(
                f"https://api.machines.dev/v1/apps/{FLY_APP_NAME}/machines/{machine_id}",
                headers=headers
            )

            if status_resp.status_code != 200:
                continue

            status = status_resp.json()
            state = status.get("state", "")

            if state in ["stopped", "destroyed"]:
                break

        # Get logs
        logs_resp = await client.get(
            f"https://api.machines.dev/v1/apps/{FLY_APP_NAME}/machines/{machine_id}/logs",
            headers=headers
        )

        output = ""
        if logs_resp.status_code == 200:
            logs = logs_resp.json()
            output = "\n".join([l.get("message", "") for l in logs])

        # Clean up machine
        try:
            await client.delete(
                f"https://api.machines.dev/v1/apps/{FLY_APP_NAME}/machines/{machine_id}?force=true",
                headers=headers
            )
        except:
            pass

        return {
            "output": output[:10000],
            "exit_code": 0 if "error" not in output.lower() else 1,
            "vm_id": machine_id
        }


async def sync_chat_to_github(github_username: str, github_token: str, project_id: str, session_id: str, messages: list):
    """Sync chat and code files to GitHub Gary repo."""
    try:
        # Get project name for folder
        project_name = "project"
        with get_db() as conn:
            row = conn.execute("SELECT name FROM projects WHERE project_id = ?", (project_id,)).fetchone()
            if row:
                # Sanitize name for folder
                project_name = "".join(c if c.isalnum() or c in '-_ ' else '' for c in row["name"]).strip().replace(' ', '-')

        async with httpx.AsyncClient() as client:
            headers = {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github.v3+json"
            }

            # 1. Save encrypted chat history
            chat_data = json.dumps({
                "project_id": project_id,
                "session_id": session_id,
                "messages": messages,
                "updated_at": time.time()
            })
            encrypted_content = encrypt_data(chat_data, session_id)
            chat_path = f"projects/{project_name}/chats/{session_id}.enc"

            # Check if file exists
            existing_sha = None
            check_resp = await client.get(
                f"https://api.github.com/repos/{github_username}/Gary/contents/{chat_path}",
                headers=headers
            )
            if check_resp.status_code == 200:
                existing_sha = check_resp.json().get("sha")

            payload = {
                "message": f"Update chat {session_id[:8]}",
                "content": base64.b64encode(encrypted_content.encode()).decode(),
            }
            if existing_sha:
                payload["sha"] = existing_sha

            await client.put(
                f"https://api.github.com/repos/{github_username}/Gary/contents/{chat_path}",
                json=payload,
                headers=headers
            )

            # 2. Extract and save code snippets from the latest assistant message
            code_count = 0
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    code_blocks = extract_code_blocks(msg.get("content", ""))
                    for i, block in enumerate(code_blocks):
                        code_count += 1
                        # Create filename from session and index
                        code_filename = f"code_{session_id[:8]}_{code_count}.{block['ext']}"
                        code_path = f"projects/{project_name}/code/{code_filename}"

                        # Check if exists
                        code_sha = None
                        code_check = await client.get(
                            f"https://api.github.com/repos/{github_username}/Gary/contents/{code_path}",
                            headers=headers
                        )
                        if code_check.status_code == 200:
                            code_sha = code_check.json().get("sha")

                        code_payload = {
                            "message": f"Add code snippet {code_filename}",
                            "content": base64.b64encode(block['code'].encode()).decode(),
                        }
                        if code_sha:
                            code_payload["sha"] = code_sha

                        await client.put(
                            f"https://api.github.com/repos/{github_username}/Gary/contents/{code_path}",
                            json=code_payload,
                            headers=headers
                        )
                    break  # Only process latest assistant message

            print(f"[GitHub] Synced {project_name}: chat + {code_count} code files")

    except Exception as e:
        print(f"[GitHub] Sync error: {e}")


# =============================================================================
# WebSocket Endpoints
# =============================================================================

@app.websocket("/ws/agent")
async def agent_websocket(websocket: WebSocket, api_key: str = None):
    """WebSocket for desktop agents. Accepts either api_key or waits for pairing code registration."""
    await websocket.accept()

    agent = None
    user = None
    pairing_code = None

    # If api_key provided, authenticate immediately
    if api_key:
        user = get_user_from_api_key(api_key)
        if not user:
            await websocket.close(code=4001, reason="Invalid API key")
            return
        agent = DesktopAgent(websocket=websocket, user_id=user.user_id)
        desktop_agents[user.user_id] = agent
        print(f"[Agent] Connected (authenticated): {user.user_id[:8]}...")
    else:
        # Create pending agent, wait for registration message
        agent = DesktopAgent(websocket=websocket)
        print("[Agent] Connected (awaiting registration)...")

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            if "text" not in message:
                continue

            try:
                data = json.loads(message["text"])
            except:
                continue

            # Handle registration from agents with pairing codes
            if data.get("type") == "register" and not agent.user_id:
                pairing_code = data.get("pairing_code", "").upper().strip()
                agent_id = data.get("agent_id")
                system_info = data.get("system_info", {})

                if pairing_code:
                    agent.pairing_code = pairing_code
                    agent.agent_id = agent_id
                    pending_agents[pairing_code] = agent
                    print(f"[Agent] Registered with code: {pairing_code}")

                    await websocket.send_json({
                        "type": "registered",
                        "message": "Waiting for user to enter pairing code on vibewithgary.com"
                    })
                continue

            # Handle pong responses
            if data.get("type") == "pong":
                continue

            # Forward execution results to web client
            if agent.mobile_client and agent.user_id:
                try:
                    await agent.mobile_client.send_text(message["text"])
                except:
                    agent.mobile_client = None

    except WebSocketDisconnect:
        pass
    finally:
        # Clean up
        if agent.user_id and agent.user_id in desktop_agents:
            del desktop_agents[agent.user_id]
            print(f"[Agent] Disconnected: {agent.user_id[:8]}...")
        elif pairing_code and pairing_code in pending_agents:
            del pending_agents[pairing_code]
            print(f"[Agent] Disconnected (unpaired): {pairing_code}")


@app.websocket("/ws/client")
async def client_websocket(websocket: WebSocket, token: str):
    """WebSocket for web clients - uses Gary chat directly or forwards to agent."""
    user = get_user_from_api_key(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()

    # Session state for this client
    current_session_id = None
    current_project_id = None
    session_messages = []

    # Check if desktop agent is connected
    agent = desktop_agents.get(user.user_id)
    use_direct_chat = agent is None

    if agent:
        agent.mobile_client = websocket
        try:
            await agent.websocket.send_text('{"type":"client_connected"}')
        except:
            use_direct_chat = True

    if use_direct_chat:
        print(f"[Client] Connected (direct chat): {user.user_id[:8]}...")
        # Get user's first project for default session
        projects = get_projects(user.user_id)
        if projects:
            current_project_id = projects[0]["id"]

        # Send welcome message for new users
        if gary_chat:
            history = gary_chat.get_history(user.user_id)
            if not history:
                # New user - send initial greeting
                await websocket.send_json({
                    "type": "message",
                    "content": "Yo! I'm Gary, your coding buddy. What do you want to build today, bro? 🤙"
                })
    else:
        print(f"[Client] Connected to agent: {user.user_id[:8]}...")

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            if "text" not in message:
                continue

            data = json.loads(message["text"])

            # Handle project/session switching
            if data.get("type") == "set_project":
                current_project_id = data.get("project_id")
                current_session_id = None
                session_messages = []
                continue

            if data.get("type") == "set_session":
                current_session_id = data.get("session_id")
                # Load existing session messages
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT messages, project_id FROM chat_sessions WHERE session_id = ? AND user_id = ?",
                        (current_session_id, user.user_id)
                    ).fetchone()
                    if row:
                        session_messages = json.loads(row["messages"])
                        current_project_id = row["project_id"]
                continue

            if data.get("type") == "new_session":
                # Start a fresh session
                current_session_id = None
                session_messages = []
                if data.get("project_id"):
                    current_project_id = data.get("project_id")
                continue

            if data.get("type") == "run_code":
                code = data.get("code", "")
                mode = data.get("mode", "virtual")
                project_id = data.get("project_id")
                session_id = data.get("session_id")

                # Create usage log entry
                log_id = secrets.token_urlsafe(16)
                start_time = time.time()

                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO usage_logs
                           (log_id, user_id, project_id, session_id, execution_type, code_size, start_time)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (log_id, user.user_id, project_id, session_id, mode, len(code), start_time)
                    )
                    conn.commit()

                if mode == "local" and agent:
                    # Forward to desktop agent
                    try:
                        await agent.websocket.send_json({
                            "type": "run_code",
                            "code": code,
                            "log_id": log_id
                        })
                        # Agent will send result back, we'll update usage log then
                    except Exception as e:
                        await websocket.send_json({
                            "type": "code_error",
                            "error": f"Failed to send to agent: {e}"
                        })
                        # Update usage log with error
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE usage_logs SET end_time = ?, exit_code = -1 WHERE log_id = ?",
                                (time.time(), log_id)
                            )
                            conn.commit()

                elif mode == "virtual":
                    # Run in Fly.io VM or sandboxed environment
                    result = await run_code_virtual(code, user.user_id, log_id)

                    # Update usage log
                    end_time = time.time()
                    duration_ms = int((end_time - start_time) * 1000)
                    # Calculate billable units (1 unit = 100ms of compute)
                    billable_units = max(1, duration_ms // 100)

                    with get_db() as conn:
                        conn.execute(
                            """UPDATE usage_logs
                               SET end_time = ?, duration_ms = ?, exit_code = ?,
                                   billable_units = ?, vm_id = ?
                               WHERE log_id = ?""",
                            (end_time, duration_ms, result.get("exit_code", 1),
                             billable_units, result.get("vm_id"), log_id)
                        )
                        conn.commit()

                    await websocket.send_json({
                        "type": "code_output",
                        "output": result.get("output", ""),
                        "exit_code": result.get("exit_code", 1),
                        "mode": "virtual",
                        "duration_ms": duration_ms,
                        "billable_units": billable_units
                    })
                else:
                    await websocket.send_json({
                        "type": "code_error",
                        "error": "No desktop agent connected for local execution. Click 'Run Locally' to install the Gary Agent."
                    })
                continue

            if use_direct_chat and gary_chat:
                # Handle chat directly via Claude API
                if data.get("type") == "message":
                    content = data.get("content", "")
                    project_id = data.get("project_id") or current_project_id
                    if content:
                        # Create session if needed
                        if not current_session_id:
                            current_session_id = secrets.token_urlsafe(16)
                            # Generate title from first message (first 50 chars)
                            title = content[:50] + ("..." if len(content) > 50 else "")
                            now = time.time()
                            with get_db() as conn:
                                conn.execute(
                                    "INSERT INTO chat_sessions (session_id, user_id, project_id, title, messages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (current_session_id, user.user_id, project_id, title, json.dumps([]), now, now)
                                )
                                conn.commit()
                            current_project_id = project_id

                        # Add user message to session
                        session_messages.append({"role": "user", "content": content})

                        # Send thinking indicator
                        await websocket.send_json({"type": "thinking"})

                        # Get response from Gary
                        response = await gary_chat.chat(user.user_id, content)

                        # Add assistant message to session
                        session_messages.append({"role": "assistant", "content": response})

                        # Save to database
                        now = time.time()
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE chat_sessions SET messages = ?, updated_at = ? WHERE session_id = ?",
                                (json.dumps(session_messages), now, current_session_id)
                            )
                            conn.commit()

                        # Sync to GitHub (encrypted)
                        if user.github_token and user.github_username:
                            asyncio.create_task(sync_chat_to_github(
                                user.github_username,
                                user.github_token,
                                current_project_id,
                                current_session_id,
                                session_messages
                            ))

                        # Get session title for response
                        session_title = None
                        with get_db() as conn:
                            row = conn.execute(
                                "SELECT title FROM chat_sessions WHERE session_id = ?",
                                (current_session_id,)
                            ).fetchone()
                            if row:
                                session_title = row["title"]

                        # Send response with session info
                        await websocket.send_json({
                            "type": "message",
                            "content": response,
                            "session_id": current_session_id,
                            "session_title": session_title,
                            "project_id": current_project_id
                        })
            elif agent:
                # Forward to desktop agent
                try:
                    await agent.websocket.send_text(message["text"])
                except:
                    break

    except WebSocketDisconnect:
        pass
    finally:
        if agent and agent.mobile_client == websocket:
            agent.mobile_client = None
            try:
                await agent.websocket.send_text('{"type":"client_disconnected"}')
            except:
                pass
        print(f"[Client] Disconnected")


# =============================================================================
# Static Files
# =============================================================================

@app.get("/")
async def root():
    return FileResponse("static/index.html")


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# =============================================================================
# Startup
# =============================================================================

@app.on_event("startup")
async def startup_event():
    global vm_manager, gary_chat
    init_db()

    # Initialize Gary chat (direct Claude API)
    if ANTHROPIC_API_KEY:
        gary_chat = GaryChat(ANTHROPIC_API_KEY)
        print("[Gary] Chat enabled (Claude API)")

    # Initialize VM manager if configured
    if FLY_API_TOKEN:
        vm_manager = VMManager(FLY_API_TOKEN, FLY_ORG)
        print("[Gary] VM provisioning enabled")


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    print("\n🎸 Vibe With Gary - API Server")
    print("=" * 40)
    print("Starting on http://0.0.0.0:8080")
    print("=" * 40 + "\n")
    uvicorn.run(app, host="0.0.0.0", port=8080)
