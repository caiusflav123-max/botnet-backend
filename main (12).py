"""
BOTNET Panel - FastAPI Backend
Handles real Telegram bot process management.
"""

import os
import sys
import time
import uuid
import signal
import asyncio
import logging
import zipfile
import shutil
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI, UploadFile, File, Form, HTTPException,
    Depends, Request, status
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─── Configuration ─────────────────────────────────────────────────
BOTS_DIR       = Path("bots")          # Where bot files live
DB_PATH        = "botnet.db"
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "changeme-super-secret-key-2024")
MAX_BOTS       = 3
LOG_LINES      = 200                   # Max log lines kept per bot
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,https://your-netlify-app.netlify.app"
).split(",")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("botnet")

# ─── In-memory process table ────────────────────────────────────────
#  { bot_id: { "proc": subprocess.Popen, "logs": [...], "start_time": float } }
PROCESSES: Dict[str, dict] = {}

# ─── Database ───────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            token       TEXT NOT NULL,
            entry_file  TEXT NOT NULL DEFAULT 'main.py',
            bot_dir     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'stopped',
            pid         INTEGER,
            created_at  REAL,
            start_time  REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL
        )
    """)
    # Seed default admin (in production, hash the password!)
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
    c.execute(
        "INSERT OR IGNORE INTO users (username, password) VALUES (?, ?)",
        ("admin", admin_pass)
    )
    conn.commit()
    conn.close()

# ─── Auth ────────────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)

def verify_token(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security)
):
    if not creds or creds.credentials != API_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token",
        )
    return True

# ─── Lifespan ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    BOTS_DIR.mkdir(exist_ok=True)
    init_db()
    # Restore running state for bots that were marked running (but aren't)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE bots SET status='stopped', pid=NULL WHERE status='running'")
    conn.commit()
    conn.close()
    logger.info("BOTNET backend started")
    yield
    # Shutdown: kill all running bots
    for bot_id, pdata in list(PROCESSES.items()):
        proc = pdata.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
            logger.info(f"Terminated bot {bot_id} on shutdown")
    logger.info("BOTNET backend stopped")

app = FastAPI(title="BOTNET Panel API", version="3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Schemas ─────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    token: str
    username: str

# ─── Helpers ─────────────────────────────────────────────────────────

def bot_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    bot_id = d["id"]
    pdata = PROCESSES.get(bot_id, {})
    proc  = pdata.get("proc")
    # Refresh live status
    if proc and proc.poll() is None:
        d["status"] = "running"
        d["pid"] = proc.pid
    else:
        if d["status"] == "running":
            d["status"] = "stopped"
            d["pid"] = None
    return d

def append_log(bot_id: str, line: str):
    if bot_id not in PROCESSES:
        PROCESSES[bot_id] = {"proc": None, "logs": [], "start_time": None}
    logs = PROCESSES[bot_id]["logs"]
    ts = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{ts}] {line}")
    if len(logs) > LOG_LINES:
        PROCESSES[bot_id]["logs"] = logs[-LOG_LINES:]

async def drain_output(bot_id: str, stream, tag: str):
    """Read process stdout/stderr line by line asynchronously."""
    try:
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, stream.readline)
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                append_log(bot_id, f"[{tag}] {decoded}")
    except Exception as e:
        append_log(bot_id, f"[system] Output reader error: {e}")

def detect_entry(bot_dir: Path) -> str:
    """Return best entry point file."""
    for name in ("main.py", "bot.py", "run.py", "app.py", "index.py"):
        if (bot_dir / name).exists():
            return name
    py_files = list(bot_dir.glob("*.py"))
    if py_files:
        return py_files[0].name
    return "main.py"

def install_requirements(bot_dir: Path) -> tuple[bool, str]:
    req = bot_dir / "requirements.txt"
    if not req.exists():
        return True, "No requirements.txt found — skipping install"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "--quiet"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return True, "Requirements installed successfully"
        return False, f"pip error: {result.stderr[:300]}"
    except subprocess.TimeoutExpired:
        return False, "pip install timed out (120s)"
    except Exception as e:
        return False, str(e)

# ─── Routes ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "online", "version": "3.0"}

@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, db=Depends(get_db)):
    row = db.execute(
        "SELECT password FROM users WHERE username = ?", (body.username,)
    ).fetchone()
    if not row or row["password"] != body.password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": API_SECRET_KEY, "username": body.username}

# ── Bot CRUD ──────────────────────────────────────────────────────────

@app.get("/bots", dependencies=[Depends(verify_token)])
def list_bots(db=Depends(get_db)):
    rows = db.execute("SELECT * FROM bots ORDER BY created_at DESC").fetchall()
    return [bot_row_to_dict(r) for r in rows]

@app.post("/bots/upload", dependencies=[Depends(verify_token)])
async def upload_bot(
    name:  str = Form(...),
    token: str = Form(...),
    file:  UploadFile = File(...),
    db=Depends(get_db),
):
    # Enforce bot limit
    count = db.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    if count >= MAX_BOTS:
        raise HTTPException(400, f"Max {MAX_BOTS} bots allowed. Delete one first.")

    # Validate file type
    filename = file.filename or ""
    if not (filename.endswith(".zip") or filename.endswith(".py")):
        raise HTTPException(400, "Only .zip or .py files accepted")

    bot_id  = "bot_" + uuid.uuid4().hex[:10]
    bot_dir = BOTS_DIR / bot_id
    bot_dir.mkdir(parents=True, exist_ok=True)

    try:
        data = await file.read()

        if filename.endswith(".zip"):
            # Write zip and extract
            zip_path = bot_dir / "upload.zip"
            zip_path.write_bytes(data)
            with zipfile.ZipFile(zip_path, "r") as zf:
                # Security: strip absolute paths
                for member in zf.infolist():
                    target = (bot_dir / member.filename).resolve()
                    if not str(target).startswith(str(bot_dir.resolve())):
                        raise HTTPException(400, "Unsafe zip path detected")
                zf.extractall(bot_dir)
            zip_path.unlink()
            entry = detect_entry(bot_dir)
        else:
            # Single .py file
            (bot_dir / filename).write_bytes(data)
            entry = filename

        # Install requirements
        ok, msg = install_requirements(bot_dir)
        if not ok:
            logger.warning(f"requirements install failed for {bot_id}: {msg}")

        now = time.time()
        db.execute(
            """INSERT INTO bots (id, name, token, entry_file, bot_dir, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'stopped', ?)""",
            (bot_id, name, token, entry, str(bot_dir), now)
        )
        db.commit()

        PROCESSES[bot_id] = {"proc": None, "logs": [
            f"[{datetime.now():%H:%M:%S}] [deploy] Bot '{name}' deployed",
            f"[{datetime.now():%H:%M:%S}] [system] Entry: {entry}",
            f"[{datetime.now():%H:%M:%S}] [system] {msg}",
            f"[{datetime.now():%H:%M:%S}] [system] Ready — press START to run",
        ], "start_time": None}

        return {
            "id": bot_id, "name": name, "entry_file": entry,
            "status": "stopped", "message": f"Deployed — {msg}"
        }

    except HTTPException:
        shutil.rmtree(bot_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(bot_dir, ignore_errors=True)
        logger.exception("Upload failed")
        raise HTTPException(500, str(e))

@app.delete("/bots/{bot_id}", dependencies=[Depends(verify_token)])
def delete_bot(bot_id: str, db=Depends(get_db)):
    row = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Bot not found")

    # Stop if running
    pdata = PROCESSES.get(bot_id, {})
    proc  = pdata.get("proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    PROCESSES.pop(bot_id, None)
    shutil.rmtree(row["bot_dir"], ignore_errors=True)
    db.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    db.commit()
    return {"message": f"Bot {bot_id} deleted"}

# ── Process Control ───────────────────────────────────────────────────

@app.post("/bots/{bot_id}/start", dependencies=[Depends(verify_token)])
async def start_bot(bot_id: str, db=Depends(get_db)):
    row = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Bot not found")

    pdata = PROCESSES.get(bot_id, {})
    proc  = pdata.get("proc")
    if proc and proc.poll() is None:
        raise HTTPException(400, "Bot is already running")

    bot_dir    = Path(row["bot_dir"])
    entry_file = row["entry_file"]
    entry_path = bot_dir / entry_file

    if not entry_path.exists():
        raise HTTPException(400, f"Entry file '{entry_file}' not found in bot directory")

    # Inject token as environment variable
    env = os.environ.copy()
    env["BOT_TOKEN"] = row["token"]

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(entry_path)],
            cwd=str(bot_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            preexec_fn=os.setsid if sys.platform != "win32" else None,
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to start process: {e}")

    start_time = time.time()
    PROCESSES[bot_id] = {"proc": proc, "logs": [], "start_time": start_time}
    append_log(bot_id, f"[start] Starting {row['name']} (PID {proc.pid})...")
    append_log(bot_id, f"[system] Entry: {entry_file}")
    append_log(bot_id, f"[system] Token: {row['token'][:10]}... configured via env")

    # Async drain stdout + stderr
    asyncio.create_task(drain_output(bot_id, proc.stdout, "stdout"))
    asyncio.create_task(drain_output(bot_id, proc.stderr, "stderr"))

    db.execute(
        "UPDATE bots SET status='running', pid=?, start_time=? WHERE id=?",
        (proc.pid, start_time, bot_id)
    )
    db.commit()

    return {"message": "Bot started", "pid": proc.pid}

@app.post("/bots/{bot_id}/stop", dependencies=[Depends(verify_token)])
def stop_bot(bot_id: str, db=Depends(get_db)):
    pdata = PROCESSES.get(bot_id, {})
    proc  = pdata.get("proc")

    if not proc or proc.poll() is not None:
        db.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bot_id,))
        db.commit()
        raise HTTPException(400, "Bot is not running")

    append_log(bot_id, f"[system] Stop requested — sending SIGTERM to PID {proc.pid}")
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        append_log(bot_id, "[system] SIGTERM timeout — sending SIGKILL")
        if sys.platform != "win32":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception as e:
        append_log(bot_id, f"[error] Stop error: {e}")

    PROCESSES[bot_id]["proc"] = None
    PROCESSES[bot_id]["start_time"] = None
    append_log(bot_id, "[system] Process stopped")

    db.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bot_id,))
    db.commit()
    return {"message": "Bot stopped"}

@app.post("/bots/{bot_id}/restart", dependencies=[Depends(verify_token)])
async def restart_bot(bot_id: str, db=Depends(get_db)):
    # Stop first (ignore error if already stopped)
    try:
        stop_bot(bot_id, db)
    except HTTPException:
        pass
    await asyncio.sleep(1)
    return await start_bot(bot_id, db)

# ── Logs & Status ─────────────────────────────────────────────────────

@app.get("/bots/{bot_id}/logs", dependencies=[Depends(verify_token)])
def get_logs(bot_id: str, limit: int = 100):
    pdata = PROCESSES.get(bot_id, {})
    logs  = pdata.get("logs", [])
    return {"logs": logs[-limit:]}

@app.get("/bots/{bot_id}/status", dependencies=[Depends(verify_token)])
def get_status(bot_id: str, db=Depends(get_db)):
    row = db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Bot not found")
    return bot_row_to_dict(row)
