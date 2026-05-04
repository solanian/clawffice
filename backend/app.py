#!/usr/bin/env python3
"""clawffice - Backend State Service"""

from flask import Flask, jsonify, send_from_directory, make_response, request, session
from datetime import datetime, timedelta
import base64
import json
import os
import random
import math
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from security_utils import is_production_mode, is_strong_secret, is_strong_drawer_pass
from memo_utils import get_yesterday_date_str, sanitize_content, extract_memo_from_file
from store_utils import (
    load_agents_state as _store_load_agents_state,
    save_agents_state as _store_save_agents_state,
    load_asset_positions as _store_load_asset_positions,
    save_asset_positions as _store_save_asset_positions,
    load_asset_defaults as _store_load_asset_defaults,
    save_asset_defaults as _store_save_asset_defaults,
    load_runtime_config as _store_load_runtime_config,
    save_runtime_config as _store_save_runtime_config,
    load_join_keys as _store_load_join_keys,
    save_join_keys as _store_save_join_keys,
)

try:
    from PIL import Image
except Exception:
    Image = None

# Paths (project-relative, no hardcoded absolute paths)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(os.path.dirname(ROOT_DIR), "memory")
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
FRONTEND_INDEX_FILE = os.path.join(FRONTEND_DIR, "index.html")
FRONTEND_ELECTRON_STANDALONE_FILE = os.path.join(FRONTEND_DIR, "electron-standalone.html")
STATE_FILE = os.path.join(ROOT_DIR, "state.json")
AGENTS_STATE_FILE = os.path.join(ROOT_DIR, "agents-state.json")
JOIN_KEYS_FILE = os.path.join(ROOT_DIR, "join-keys.json")
FRONTEND_PATH = Path(FRONTEND_DIR)
ASSET_ALLOWED_EXTS = {".png", ".webp", ".jpg", ".jpeg", ".gif", ".svg", ".avif"}
ASSET_TEMPLATE_ZIP = os.path.join(ROOT_DIR, "assets-replace-template.zip")
WORKSPACE_DIR = os.path.dirname(ROOT_DIR)
OPENCLAW_WORKSPACE = os.environ.get("OPENCLAW_WORKSPACE") or os.path.join(os.path.expanduser("~"), ".openclaw", "workspace")
IDENTITY_FILE = os.path.join(OPENCLAW_WORKSPACE, "IDENTITY.md")
GEMINI_SCRIPT = os.path.join(WORKSPACE_DIR, "skills", "gemini-image-generate", "scripts", "gemini_image_generate.py")
GEMINI_PYTHON = os.path.join(WORKSPACE_DIR, "skills", "gemini-image-generate", ".venv", "bin", "python")
ROOM_REFERENCE_IMAGE = (
    os.path.join(ROOT_DIR, "assets", "room-reference.webp")
    if os.path.exists(os.path.join(ROOT_DIR, "assets", "room-reference.webp"))
    else os.path.join(ROOT_DIR, "assets", "room-reference.png")
)
BG_HISTORY_DIR = os.path.join(ROOT_DIR, "assets", "bg-history")
HOME_FAVORITES_DIR = os.path.join(ROOT_DIR, "assets", "home-favorites")
HOME_FAVORITES_INDEX_FILE = os.path.join(HOME_FAVORITES_DIR, "index.json")
HOME_FAVORITES_MAX = 30
ASSET_POSITIONS_FILE = os.path.join(ROOT_DIR, "asset-positions.json")

# 性能保护：默认关闭“每次打开页面随机换背景”，避免首页首屏被磁盘复制拖慢
AUTO_ROTATE_HOME_ON_PAGE_OPEN = (os.getenv("AUTO_ROTATE_HOME_ON_PAGE_OPEN", "0").strip().lower() in {"1", "true", "yes", "on"})
AUTO_ROTATE_MIN_INTERVAL_SECONDS = int(os.getenv("AUTO_ROTATE_MIN_INTERVAL_SECONDS", "60"))
_last_home_rotate_at = 0
ASSET_DEFAULTS_FILE = os.path.join(ROOT_DIR, "asset-defaults.json")
RUNTIME_CONFIG_FILE = os.path.join(ROOT_DIR, "runtime-config.json")
DATA_DIR = os.environ.get("CLAWFFICE_DATA_DIR") or os.environ.get("STAR_OFFICE_DATA_DIR") or ROOT_DIR
CHAT_DB_FILE = os.environ.get("CHAT_DB_FILE") or os.path.join(DATA_DIR, "chat.db")
OPENCLAW_CLI = os.environ.get("OPENCLAW_CLI", "openclaw").strip() or "openclaw"
OPENCLAW_CHAT_DEFAULT_AGENT = os.environ.get("OPENCLAW_CHAT_AGENT", "clawffice manager").strip() or "clawffice manager"
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "").strip()
try:
    OPENCLAW_CHAT_TIMEOUT_SECONDS = max(10, int(os.environ.get("OPENCLAW_CHAT_TIMEOUT", "1800")))
except ValueError:
    OPENCLAW_CHAT_TIMEOUT_SECONDS = 1800
try:
    OPENCLAW_CHAT_SEND_TIMEOUT_SECONDS = max(30, int(os.environ.get("OPENCLAW_CHAT_SEND_TIMEOUT", "120")))
except ValueError:
    OPENCLAW_CHAT_SEND_TIMEOUT_SECONDS = 120
OPENCLAW_HISTORY_TIMEOUT_MS = min(120000, max(60000, OPENCLAW_CHAT_TIMEOUT_SECONDS * 1000))
CHAT_WAITING_MESSAGE = "응답을 기다리는 중..."

# Canonical agent states: single source of truth for validation and mapping
VALID_AGENT_STATES = frozenset({"idle", "writing", "researching", "executing", "syncing", "error"})
WORKING_STATES = frozenset({"writing", "researching", "executing"})  # subset used for auto-idle TTL
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max")
STATE_TO_AREA_MAP = {
    "idle": "breakroom",
    "writing": "writing",
    "researching": "writing",
    "executing": "writing",
    "syncing": "writing",
    "error": "error",
}


app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="/static")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.getenv("CLAWFFICE_SECRET") or os.getenv("STAR_OFFICE_SECRET") or "clawffice-dev-secret-change-me"

# Session hardening
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_production_mode(),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# Guard join-agent critical section to enforce per-key concurrency under parallel requests
join_lock = threading.Lock()

# Async background task registry for long-running operations (e.g. image generation)
# Avoids Cloudflare 524 timeout (100s limit) by letting frontend poll for completion.
_bg_tasks = {}  # task_id -> {"status": "pending"|"done"|"error", "result": ..., "error": ..., "created_at": ...}
_bg_tasks_lock = threading.Lock()

# Generate a version timestamp once at server startup for cache busting
VERSION_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
ASSET_DRAWER_PASS_DEFAULT = os.getenv("ASSET_DRAWER_PASS", "1234")

if is_production_mode():
    hardening_errors = []
    if not is_strong_secret(str(app.secret_key)):
        hardening_errors.append("FLASK_SECRET_KEY / CLAWFFICE_SECRET is weak (need >=24 chars, non-default)")
    if not is_strong_drawer_pass(ASSET_DRAWER_PASS_DEFAULT):
        hardening_errors.append("ASSET_DRAWER_PASS is weak (do not use default 1234; recommend >=8 chars)")
    if hardening_errors:
        raise RuntimeError("Security hardening check failed in production mode: " + "; ".join(hardening_errors))


def _is_asset_editor_authed() -> bool:
    return bool(session.get("asset_editor_authed"))


def _require_asset_editor_auth():
    if _is_asset_editor_authed():
        return None
    return jsonify({"ok": False, "code": "UNAUTHORIZED", "msg": "Asset editor auth required"}), 401


@app.after_request
def add_no_cache_headers(response):
    """Apply cache policy by path:
    - HTML/API/state: no-cache (always fresh)
    - /static assets (2xx only): long cache (filenames are versioned with ?v=VERSION_TIMESTAMP)
    - /static assets (non-2xx, e.g. 404): no-cache to prevent CDN from caching errors
    """
    path = (request.path or "")
    if path.startswith('/static/') and 200 <= response.status_code < 300:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers.pop("Pragma", None)
        response.headers.pop("Expires", None)
    else:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Default state
DEFAULT_STATE = {
    "state": "idle",
    "detail": "대기 중입니다.",
    "progress": 0,
    "updated_at": datetime.now().isoformat()
}


def load_state():
    """Load state from file.

    Includes a simple auto-idle mechanism:
    - If the last update is older than ttl_seconds (default 25s)
      and the state is a "working" state, we fall back to idle.

    This avoids the UI getting stuck at the desk when no new updates arrive.
    """
    state = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = None

    if not isinstance(state, dict):
        state = dict(DEFAULT_STATE)

    # Auto-idle
    try:
        ttl = int(state.get("ttl_seconds", 300))
        updated_at = state.get("updated_at")
        s = state.get("state", "idle")
        if updated_at and s in WORKING_STATES:
            # tolerate both with/without timezone
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            # Use UTC for aware datetimes; local time for naive.
            if dt.tzinfo:
                from datetime import timezone
                age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
            else:
                age = (datetime.now() - dt).total_seconds()
            if age > ttl:
                state["state"] = "idle"
                state["detail"] = "대기 중입니다. 오래된 작업 상태를 자동으로 정리했습니다."
                state["progress"] = 0
                state["updated_at"] = datetime.now().isoformat()
                # persist the auto-idle so every client sees it consistently
                try:
                    save_state(state)
                except Exception:
                    pass
    except Exception:
        pass

    return state


def get_office_name_from_identity():
    """Read office display name from OpenClaw workspace IDENTITY.md (Name field)."""
    if not os.path.isfile(IDENTITY_FILE):
        return None
    try:
        with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"-\s*\*\*Name:\*\*\s*(.+)", content)
        if m:
            name = m.group(1).strip().replace("\r", "").split("\n")[0].strip()
            return f"{name}의 사무실" if name else None
    except Exception:
        pass
    return None


def save_state(state: dict):
    """Save state to file"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def ensure_electron_standalone_snapshot():
    """Create Electron standalone frontend snapshot once if missing.

    The snapshot is intentionally decoupled from the browser page:
    - browser uses frontend/index.html
    - Electron uses frontend/electron-standalone.html
    """
    if os.path.exists(FRONTEND_ELECTRON_STANDALONE_FILE):
        return
    try:
        shutil.copy2(FRONTEND_INDEX_FILE, FRONTEND_ELECTRON_STANDALONE_FILE)
        print(f"[standalone] created: {FRONTEND_ELECTRON_STANDALONE_FILE}")
    except Exception as e:
        print(f"[standalone] create failed: {e}")


# Initialize state
if not os.path.exists(STATE_FILE):
    save_state(DEFAULT_STATE)
ensure_electron_standalone_snapshot()


_INDEX_HTML_CACHE = None
_INDEX_HTML_CACHE_MTIME = None


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return send_from_directory(FRONTEND_DIR, "favicon.svg", mimetype="image/svg+xml")


@app.route("/", methods=["GET"])
def index():
    """Serve the pixel office UI with built-in version cache busting"""
    # 默认禁用页面打开即换背景，避免首屏慢
    # 如需启用，可配置 AUTO_ROTATE_HOME_ON_PAGE_OPEN=1
    _maybe_apply_random_home_favorite()

    global _INDEX_HTML_CACHE, _INDEX_HTML_CACHE_MTIME
    try:
        index_mtime = os.path.getmtime(FRONTEND_INDEX_FILE)
    except OSError:
        index_mtime = None
    if _INDEX_HTML_CACHE is None or _INDEX_HTML_CACHE_MTIME != index_mtime:
        with open(FRONTEND_INDEX_FILE, "r", encoding="utf-8") as f:
            raw_html = f.read()
        _INDEX_HTML_CACHE = raw_html.replace("{{VERSION_TIMESTAMP}}", VERSION_TIMESTAMP)
        _INDEX_HTML_CACHE_MTIME = index_mtime

    resp = make_response(_INDEX_HTML_CACHE)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/electron-standalone", methods=["GET"])
def electron_standalone_page():
    """Serve Electron-only standalone frontend page."""
    ensure_electron_standalone_snapshot()
    target = FRONTEND_ELECTRON_STANDALONE_FILE
    if not os.path.exists(target):
        target = FRONTEND_INDEX_FILE
    with open(target, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{VERSION_TIMESTAMP}}", VERSION_TIMESTAMP)
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp

    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/join", methods=["GET"])
def join_page():
    """Serve the agent join page"""
    with open(os.path.join(FRONTEND_DIR, "join.html"), "r", encoding="utf-8") as f:
        html = f.read()
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.route("/invite", methods=["GET"])
def invite_page():
    """Serve human-facing invite instruction page"""
    with open(os.path.join(FRONTEND_DIR, "invite.html"), "r", encoding="utf-8") as f:
        html = f.read()
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


DEFAULT_AGENTS = [
    {
        "agentId": "star",
        "name": "Star",
        "isMain": True,
        "state": "idle",
        "detail": "대기 중입니다.",
        "updated_at": datetime.now().isoformat(),
        "area": "breakroom",
        "source": "local",
        "openclawAgentId": OPENCLAW_CHAT_DEFAULT_AGENT,
        "joinKey": None,
        "authStatus": "approved",
        "authExpiresAt": None,
        "lastPushAt": None
    }
]


def load_agents_state():
    return _store_load_agents_state(AGENTS_STATE_FILE, DEFAULT_AGENTS)


def save_agents_state(agents):
    _store_save_agents_state(AGENTS_STATE_FILE, agents)


def load_asset_positions():
    return _store_load_asset_positions(ASSET_POSITIONS_FILE)


def save_asset_positions(data):
    _store_save_asset_positions(ASSET_POSITIONS_FILE, data)


def load_asset_defaults():
    return _store_load_asset_defaults(ASSET_DEFAULTS_FILE)


def save_asset_defaults(data):
    _store_save_asset_defaults(ASSET_DEFAULTS_FILE, data)


def load_runtime_config():
    return _store_load_runtime_config(RUNTIME_CONFIG_FILE)


def save_runtime_config(data):
    _store_save_runtime_config(RUNTIME_CONFIG_FILE, data)


chat_db_lock = threading.Lock()


def _chat_db():
    db_dir = os.path.dirname(os.path.abspath(CHAT_DB_FILE))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(CHAT_DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_chat_db():
    with chat_db_lock:
        with _chat_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL UNIQUE,
                    agent_name TEXT NOT NULL,
                    last_read_message_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            existing_conversation_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
            if "last_read_message_id" not in existing_conversation_cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN last_read_message_id INTEGER NOT NULL DEFAULT 0")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'agent', 'system')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id, id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    user_message_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'finalizing', 'done', 'error')),
                    openclaw_agent_id TEXT,
                    gateway_run_id TEXT,
                    gateway_accepted_at TEXT,
                    reply_message_id INTEGER,
                    error_message_id INTEGER,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE CASCADE,
                    FOREIGN KEY (reply_message_id) REFERENCES messages(id) ON DELETE SET NULL,
                    FOREIGN KEY (error_message_id) REFERENCES messages(id) ON DELETE SET NULL
                )
            """)
            existing_turn_cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_turns)").fetchall()}
            if "gateway_run_id" not in existing_turn_cols:
                conn.execute("ALTER TABLE chat_turns ADD COLUMN gateway_run_id TEXT")
            if "gateway_accepted_at" not in existing_turn_cols:
                conn.execute("ALTER TABLE chat_turns ADD COLUMN gateway_accepted_at TEXT")
            turn_schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_turns'"
            ).fetchone()
            if turn_schema and "finalizing" not in str(turn_schema[0] or ""):
                conn.execute("ALTER TABLE chat_turns RENAME TO chat_turns_old")
                conn.execute("""
                    CREATE TABLE chat_turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id INTEGER NOT NULL,
                        user_message_id INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('pending', 'finalizing', 'done', 'error')),
                        openclaw_agent_id TEXT,
                        gateway_run_id TEXT,
                        gateway_accepted_at TEXT,
                        reply_message_id INTEGER,
                        error_message_id INTEGER,
                        error_text TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                        FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE CASCADE,
                        FOREIGN KEY (reply_message_id) REFERENCES messages(id) ON DELETE SET NULL,
                        FOREIGN KEY (error_message_id) REFERENCES messages(id) ON DELETE SET NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO chat_turns (
                        id, conversation_id, user_message_id, status,
                        openclaw_agent_id, gateway_run_id, gateway_accepted_at,
                        reply_message_id, error_message_id, error_text,
                        created_at, updated_at
                    )
                    SELECT
                        id, conversation_id, user_message_id, status,
                        openclaw_agent_id, gateway_run_id, gateway_accepted_at,
                        reply_message_id, error_message_id, error_text,
                        created_at, updated_at
                    FROM chat_turns_old
                """)
                conn.execute("DROP TABLE chat_turns_old")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_turns_conversation_status ON chat_turns(conversation_id, status, id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_turns_one_pending ON chat_turns(conversation_id) WHERE status = 'pending'")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'sent', 'cancelled')) DEFAULT 'queued',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_message_id INTEGER,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                    FOREIGN KEY (sent_message_id) REFERENCES messages(id) ON DELETE SET NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_queue_conversation_status ON chat_queue(conversation_id, status, id)")


def _normalize_chat_agent_id(agent_id: str) -> str:
    value = (agent_id or "").strip()
    if not value:
        raise ValueError("agentId가 없습니다")
    if len(value) > 128:
        raise ValueError("agentId가 너무 깁니다")
    return value


def _normalize_chat_agent_name(agent_name: str, agent_id: str) -> str:
    value = (agent_name or "").strip()
    if not value:
        agents = load_agents_state()
        target = next((a for a in agents if a.get("agentId") == agent_id), None)
        value = (target or {}).get("name") or agent_id
    return value[:120]


def _chat_openclaw_agent_map():
    mapping = {"star": OPENCLAW_CHAT_DEFAULT_AGENT}
    raw = os.environ.get("OPENCLAW_CHAT_AGENT_MAP", "").strip()
    if not raw:
        return mapping
    try:
        data = json.loads(raw)
    except Exception:
        return mapping
    if not isinstance(data, dict):
        return mapping
    for key, value in data.items():
        key = str(key or "").strip()
        value = str(value or "").strip()
        if key and value:
            mapping[key] = value
    return mapping


def _find_agent_record(agent_id: str):
    try:
        return next((a for a in load_agents_state() if a.get("agentId") == agent_id), None)
    except Exception:
        return None


def _resolve_openclaw_agent_id(agent_id: str) -> str:
    record = _find_agent_record(agent_id) or {}
    explicit = (
        record.get("openclawAgentId")
        or record.get("openclaw_agent_id")
        or record.get("openclawAgent")
    )
    if explicit:
        return str(explicit).strip()
    mapped = _chat_openclaw_agent_map().get(agent_id)
    if mapped:
        return mapped
    return agent_id


def _openclaw_session_key(agent_id: str, openclaw_agent_id: str) -> str:
    safe_agent = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", openclaw_agent_id or agent_id).strip("-")
    safe_character = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", agent_id).strip("-")
    return f"agent:{safe_agent or 'main'}:webchat:{safe_character or 'agent'}"[:512]


def _openclaw_gateway_env():
    env = os.environ.copy()
    path_parts = [
        "/home/opc/.npm-global/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    existing_path = env.get("PATH", "")
    env["PATH"] = ":".join([p for p in path_parts if p] + ([existing_path] if existing_path else []))
    if not env.get("HOME") and os.path.exists("/home/opc/.openclaw"):
        env["HOME"] = "/home/opc"
    return env


def _openclaw_gateway_call(method: str, params: dict | None = None, timeout_ms: int | None = None) -> dict:
    timeout_ms = int(timeout_ms or max(10000, OPENCLAW_CHAT_TIMEOUT_SECONDS * 1000))
    cmd = [
        OPENCLAW_CLI,
        "gateway",
        "call",
        method,
        "--json",
        "--timeout",
        str(timeout_ms),
        "--params",
        json.dumps(params or {}, ensure_ascii=False),
    ]
    if OPENCLAW_GATEWAY_URL:
        cmd.extend(["--url", OPENCLAW_GATEWAY_URL])

    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT_DIR,
            env=_openclaw_gateway_env(),
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000) + 15,
        )
    except FileNotFoundError:
        raise RuntimeError(f"OpenClaw CLI를 찾지 못했습니다: {OPENCLAW_CLI}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"OpenClaw Gateway 호출 시간이 초과되었습니다({timeout_ms}ms)")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        detail = stderr or stdout or "unknown error"
        raise RuntimeError(f"OpenClaw Gateway 호출 실패({result.returncode}): {detail[-1000:]}")

    if not stdout:
        return {}
    try:
        parsed = json.loads(stdout)
    except Exception:
        parsed = None
        for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
            if not line.startswith(("{", "[")):
                continue
            try:
                parsed = json.loads(line)
                break
            except Exception:
                continue
    if not isinstance(parsed, dict):
        raise RuntimeError(f"OpenClaw Gateway 응답을 해석하지 못했습니다: {(stdout or stderr)[-1000:]}")
    return parsed


def _openclaw_cli_run(args: list[str], timeout_seconds: int = 8) -> str:
    cmd = [OPENCLAW_CLI, *args]
    try:
        result = subprocess.run(
            cmd,
            cwd=ROOT_DIR,
            env=_openclaw_gateway_env(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        raise RuntimeError(f"OpenClaw CLI를 찾지 못했습니다: {OPENCLAW_CLI}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("OpenClaw CLI 응답 시간이 초과되었습니다")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError((stderr or stdout or "OpenClaw CLI 호출 실패")[-1000:])
    return stdout


def _openclaw_cli_json(args: list[str], timeout_seconds: int = 8) -> dict | list:
    stdout = _openclaw_cli_run(args, timeout_seconds=timeout_seconds)
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except Exception:
        for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
            if not line.startswith(("{", "[")):
                continue
            try:
                return json.loads(line)
            except Exception:
                continue
    raise RuntimeError(f"OpenClaw CLI JSON 응답을 해석하지 못했습니다: {stdout[-1000:]}")


def _load_openclaw_config() -> dict:
    config_path = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _openclaw_agent_config_entry(openclaw_agent_id: str, cfg: dict | None = None) -> tuple[dict, int | None]:
    cfg = cfg if isinstance(cfg, dict) else _load_openclaw_config()
    items = ((cfg.get("agents") or {}).get("list") or [])
    for idx, item in enumerate(items):
        if isinstance(item, dict) and item.get("id") == openclaw_agent_id:
            return item, idx
    return {}, None


def _openclaw_model_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("primary") or value.get("model")
    return None


def _openclaw_model_config(openclaw_agent_id: str, latest_session: dict | None = None, openclaw_agent: dict | None = None) -> dict:
    cfg = _load_openclaw_config()
    entry, idx = _openclaw_agent_config_entry(openclaw_agent_id, cfg)
    default_value = _openclaw_model_value(((cfg.get("agents") or {}).get("defaults") or {}).get("model"))
    agent_value = _openclaw_model_value(entry.get("model") if isinstance(entry, dict) else None)
    current_value = agent_value or (openclaw_agent or {}).get("model") or default_value or (latest_session or {}).get("model")
    models = []
    try:
        data = _openclaw_cli_json(["models", "list", "--json"], timeout_seconds=8)
        models = [m.get("key") for m in (data.get("models") or []) if isinstance(m, dict) and m.get("key")]
    except Exception:
        models = []
    if current_value and current_value not in models:
        models.insert(0, current_value)
    return {
        "configured": agent_value,
        "default": default_value,
        "effective": current_value,
        "models": models,
        "configPath": f"agents.list[{idx}].model" if idx is not None else None,
    }


def _openclaw_thinking_config(openclaw_agent_id: str, latest_session: dict | None = None) -> dict:
    cfg = _load_openclaw_config()
    entry, idx = _openclaw_agent_config_entry(openclaw_agent_id, cfg)
    default_value = (((cfg.get("agents") or {}).get("defaults") or {}).get("thinkingDefault"))
    agent_value = entry.get("thinkingDefault") if isinstance(entry, dict) else None
    session_value = None
    if isinstance(latest_session, dict):
        session_value = (
            latest_session.get("thinkingLevel")
            or latest_session.get("thinking")
            or latest_session.get("thinkingDefault")
            or latest_session.get("reasoningEffort")
        )
    return {
        "configured": agent_value,
        "default": default_value,
        "effective": agent_value or default_value,
        "lastRun": session_value,
        "levels": list(THINKING_LEVELS),
        "configPath": f"agents.list[{idx}].thinkingDefault" if idx is not None else None,
    }


def _set_openclaw_agent_thinking(openclaw_agent_id: str, thinking: str) -> dict:
    thinking = str(thinking or "").strip()
    if thinking not in THINKING_LEVELS:
        raise ValueError("thinking 값이 유효하지 않습니다")
    _entry, idx = _openclaw_agent_config_entry(openclaw_agent_id)
    if idx is None:
        raise ValueError("OpenClaw agent 설정을 찾지 못했습니다")
    path = f"agents.list[{idx}].thinkingDefault"
    _openclaw_cli_run(["config", "set", path, json.dumps(thinking), "--strict-json"], timeout_seconds=8)
    return {"thinkingDefault": thinking, "configPath": path}


def _set_openclaw_agent_model(openclaw_agent_id: str, model: str) -> dict:
    model = str(model or "").strip()
    if not model:
        raise ValueError("model 값이 없습니다")
    _entry, idx = _openclaw_agent_config_entry(openclaw_agent_id)
    if idx is None:
        raise ValueError("OpenClaw agent 설정을 찾지 못했습니다")
    available = set(_openclaw_model_config(openclaw_agent_id).get("models") or [])
    if available and model not in available:
        raise ValueError("선택할 수 없는 model입니다")
    path = f"agents.list[{idx}].model"
    payload = {"primary": model}
    _openclaw_cli_run(["config", "set", path, json.dumps(payload), "--strict-json"], timeout_seconds=8)
    return {"model": model, "configPath": path}


def _openclaw_auth_summary() -> str:
    config_path = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
    try:
        with config_path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        profiles = ((cfg.get("auth") or {}).get("profiles") or {})
        for profile_id, profile in profiles.items():
            provider = profile.get("provider") or str(profile_id).split(":", 1)[0]
            mode = profile.get("mode") or "configured"
            safe_profile = re.sub(r"(:)[^:@/]+@", r"\1***@", str(profile_id))
            return f"{mode} ({provider}:{safe_profile.split(':', 1)[-1]})"
    except Exception:
        pass
    return "-"


def _find_latest_openclaw_session(openclaw_agent_id: str, status_data: dict | None = None, sessions_data: dict | None = None) -> dict:
    candidates = []
    for item in (((status_data or {}).get("sessions") or {}).get("recent") or []):
        if item.get("agentId") == openclaw_agent_id:
            candidates.append(item)
    for item in ((sessions_data or {}).get("sessions") or []):
        if item.get("agentId") == openclaw_agent_id:
            candidates.append(item)
    if not candidates:
        return {}
    return max(candidates, key=lambda x: int(x.get("updatedAt") or 0))


def _agent_openclaw_info(agent_id: str) -> dict:
    agent_id = str(agent_id or "").strip() or "star"
    record = _find_agent_record(agent_id) or {}
    if not record and agent_id != "star":
        raise ValueError("agent를 찾지 못했습니다")
    if not record:
        record = dict(DEFAULT_AGENTS[0])
    openclaw_agent_id = _resolve_openclaw_agent_id(agent_id)

    errors = []
    status_data = {}
    sessions_data = {}
    agents_list = []
    try:
        status_data = _openclaw_cli_json(["status", "--json"], timeout_seconds=8)
    except Exception as e:
        errors.append(f"status: {e}")
    try:
        sessions_data = _openclaw_cli_json(["sessions", "--all-agents", "--json"], timeout_seconds=6)
    except Exception as e:
        errors.append(f"sessions: {e}")
    try:
        agents_list = _openclaw_cli_json(["agents", "list", "--json"], timeout_seconds=6)
    except Exception as e:
        errors.append(f"agents: {e}")

    openclaw_agent = next((a for a in agents_list if a.get("id") == openclaw_agent_id), {}) if isinstance(agents_list, list) else {}
    status_agent = next((a for a in (((status_data.get("agents") or {}).get("agents")) or []) if a.get("id") == openclaw_agent_id), {}) if isinstance(status_data, dict) else {}
    latest_session = _find_latest_openclaw_session(openclaw_agent_id, status_data, sessions_data)
    tasks = (status_data.get("tasks") or {}) if isinstance(status_data, dict) else {}
    gateway = (status_data.get("gateway") or {}) if isinstance(status_data, dict) else {}
    runtime = latest_session.get("agentRuntime") or {}
    think_level = (
        latest_session.get("thinking")
        or latest_session.get("thinkingLevel")
        or latest_session.get("reasoningEffort")
        or latest_session.get("reasoning")
        or runtime.get("thinking")
        or runtime.get("thinkingLevel")
    )

    return {
        "ok": True,
        "agent": {
            "agentId": record.get("agentId") or agent_id,
            "name": record.get("name") or openclaw_agent.get("identityName") or openclaw_agent_id,
            "isMain": bool(record.get("isMain")),
            "avatar": record.get("avatar") or "guest_role_1",
            "state": record.get("state") or "idle",
            "detail": record.get("detail") or "",
            "authStatus": record.get("authStatus") or "approved",
            "lastPushAt": record.get("lastPushAt") or record.get("updated_at"),
            "openclawAgentId": openclaw_agent_id,
        },
        "openclaw": {
            "version": status_data.get("runtimeVersion") if isinstance(status_data, dict) else None,
            "model": openclaw_agent.get("model") or latest_session.get("model"),
            "modelProvider": latest_session.get("modelProvider"),
            "modelConfig": _openclaw_model_config(openclaw_agent_id, latest_session, openclaw_agent),
            "auth": _openclaw_auth_summary(),
            "thinkLevel": think_level,
            "thinking": _openclaw_thinking_config(openclaw_agent_id, latest_session),
            "identityName": openclaw_agent.get("identityName"),
            "identityEmoji": openclaw_agent.get("identityEmoji"),
            "workspace": openclaw_agent.get("workspace") or status_agent.get("workspaceDir"),
            "agentDir": openclaw_agent.get("agentDir"),
            "isDefault": openclaw_agent.get("isDefault"),
            "bindings": openclaw_agent.get("bindings"),
            "gateway": {
                "url": gateway.get("url"),
                "reachable": gateway.get("reachable"),
                "latencyMs": gateway.get("connectLatencyMs"),
            },
            "server": {
                "host": record.get("remoteHost"),
                "sshUser": record.get("remoteSshUser"),
                "sshPort": record.get("remoteSshPort"),
                "stateFile": record.get("remoteStateFile"),
                "installDir": record.get("remoteInstallDir"),
            },
            "tokens": {
                "input": latest_session.get("inputTokens"),
                "output": latest_session.get("outputTokens"),
                "total": latest_session.get("totalTokens"),
                "cacheRead": latest_session.get("cacheRead"),
                "cacheWrite": latest_session.get("cacheWrite"),
            },
            "context": {
                "used": latest_session.get("totalTokens"),
                "limit": latest_session.get("contextTokens"),
                "percent": latest_session.get("percentUsed"),
                "remaining": latest_session.get("remainingTokens"),
            },
            "session": {
                "key": latest_session.get("key"),
                "kind": latest_session.get("kind"),
                "sessionId": latest_session.get("sessionId"),
                "updatedAt": latest_session.get("updatedAt"),
                "ageMs": latest_session.get("ageMs") or latest_session.get("age"),
            },
            "runtime": {
                "id": runtime.get("id"),
                "fallback": runtime.get("fallback"),
                "source": runtime.get("source"),
            },
            "queue": {
                "active": tasks.get("active"),
                "queued": (tasks.get("byStatus") or {}).get("queued"),
                "running": (tasks.get("byStatus") or {}).get("running"),
                "failures": tasks.get("failures"),
            },
            "errors": errors,
        },
    }


def _openclaw_sessions_dir(openclaw_agent_id: str) -> Path:
    safe_agent = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", openclaw_agent_id or "").strip("-")
    if not safe_agent:
        raise RuntimeError("OpenClaw agent id가 없습니다")
    openclaw_home = os.environ.get("OPENCLAW_HOME") or os.path.expanduser("~")
    return Path(openclaw_home) / ".openclaw" / "agents" / safe_agent / "sessions"


def _load_openclaw_session_entry(session_key: str, openclaw_agent_id: str) -> dict:
    sessions_file = _openclaw_sessions_dir(openclaw_agent_id) / "sessions.json"
    try:
        with sessions_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    entry = data.get(session_key)
    return entry if isinstance(entry, dict) else {}


def _resolve_openclaw_session_file(session_key: str, openclaw_agent_id: str) -> Path | None:
    entry = _load_openclaw_session_entry(session_key, openclaw_agent_id)
    raw_file = str(entry.get("sessionFile") or "").strip()
    if raw_file:
        path = Path(raw_file)
    else:
        session_id = str(entry.get("sessionId") or "").strip()
        if not session_id:
            return None
        path = _openclaw_sessions_dir(openclaw_agent_id) / f"{session_id}.jsonl"

    sessions_dir = _openclaw_sessions_dir(openclaw_agent_id).resolve()
    try:
        resolved = path.resolve()
        if sessions_dir not in resolved.parents and resolved != sessions_dir:
            return None
        return resolved
    except Exception:
        return None


def _extract_openclaw_content_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_extract_openclaw_content_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"].strip()
        if isinstance(value.get("content"), (str, list, dict)):
            text = _extract_openclaw_content_text(value.get("content"))
            if text:
                return text
        for key in ("transcriptText", "message", "value", "output"):
            if isinstance(value.get(key), (str, list, dict)):
                text = _extract_openclaw_content_text(value.get(key))
                if text:
                    return text
    return ""


def _latest_openclaw_assistant_snapshot(session_key: str, openclaw_agent_id: str) -> tuple[str, str]:
    session_file = _resolve_openclaw_session_file(session_key, openclaw_agent_id)
    if not session_file or not session_file.exists():
        return "", ""

    try:
        with session_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return "", ""

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = _extract_openclaw_content_text(message.get("content")) or _extract_openclaw_content_text(message.get("text"))
        if not text:
            continue
        try:
            fingerprint = json.dumps(
                {
                    "id": record.get("id"),
                    "timestamp": record.get("timestamp"),
                    "message": message,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception:
            fingerprint = text
        return text, fingerprint
    return "", ""


def _openclaw_message_records(session_key: str, openclaw_agent_id: str) -> list[dict]:
    session_file = _resolve_openclaw_session_file(session_key, openclaw_agent_id)
    if not session_file or not session_file.exists():
        return []

    records = []
    try:
        with session_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("type") != "message":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _extract_openclaw_content_text(message.get("content")) or _extract_openclaw_content_text(message.get("text"))
        if not text:
            continue
        records.append({
            "role": role,
            "text": text,
            "timestamp": record.get("timestamp") or message.get("timestamp") or datetime.now().isoformat(),
        })
    return records


def _iso_to_timestamp(value) -> float | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _timestamp_in_turn_window(timestamp, after_iso: str | None = None, before_iso: str | None = None) -> bool:
    ts = _iso_to_timestamp(timestamp)
    if ts is None:
        return True
    after_ts = _iso_to_timestamp(after_iso)
    if after_ts is not None and ts < after_ts - 5:
        return False
    before_ts = _iso_to_timestamp(before_iso)
    if before_ts is not None and ts >= before_ts:
        return False
    return True


def _find_openclaw_user_submission_for_content(session_key: str, openclaw_agent_id: str, user_content: str, after_iso: str | None = None, before_iso: str | None = None) -> str | None:
    needle = (user_content or "").strip()
    if not needle:
        return None

    records = _openclaw_message_records(session_key, openclaw_agent_id)
    for record in reversed(records):
        if (
            record.get("role") == "user"
            and needle in str(record.get("text") or "")
            and _timestamp_in_turn_window(record.get("timestamp"), after_iso, before_iso)
        ):
            return str(record.get("timestamp") or datetime.now().isoformat())
    return None


def _find_openclaw_reply_for_user_content(session_key: str, openclaw_agent_id: str, user_content: str, after_iso: str | None = None, before_iso: str | None = None) -> tuple[str, str] | None:
    needle = (user_content or "").strip()
    if not needle:
        return None

    records = _openclaw_message_records(session_key, openclaw_agent_id)
    matching_indexes = [
        index for index, record in enumerate(records)
        if (
            record.get("role") == "user"
            and needle in str(record.get("text") or "")
            and _timestamp_in_turn_window(record.get("timestamp"), after_iso, before_iso)
        )
    ]
    for index in reversed(matching_indexes):
        for record in records[index + 1:]:
            role = record.get("role")
            if role == "user":
                break
            if role == "assistant" and record.get("text"):
                return str(record["text"]).strip(), str(record.get("timestamp") or datetime.now().isoformat())
    return None


def _wait_for_openclaw_assistant_reply(session_key: str, openclaw_agent_id: str, baseline_fingerprint: str, timeout_ms: int) -> str:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        reply, fingerprint = _latest_openclaw_assistant_snapshot(session_key, openclaw_agent_id)
        if reply and fingerprint != baseline_fingerprint:
            return reply
        time.sleep(0.5)
    raise RuntimeError(f"OpenClaw 응답 시간이 초과되었습니다({max(1, timeout_ms // 1000)}초)")


def _start_openclaw_agent_turn(agent_id: str, content: str) -> tuple[str, str, str, str]:
    openclaw_agent_id = _resolve_openclaw_agent_id(agent_id)
    if not openclaw_agent_id:
        raise RuntimeError("이 캐릭터에 연결된 OpenClaw agent id가 없습니다")

    session_key = _openclaw_session_key(agent_id, openclaw_agent_id)
    _, baseline_fingerprint = _latest_openclaw_assistant_snapshot(session_key, openclaw_agent_id)
    timeout_ms = OPENCLAW_CHAT_TIMEOUT_SECONDS * 1000
    send_result = _openclaw_gateway_call(
        "chat.send",
        {
            "sessionKey": session_key,
            "message": content,
            "deliver": False,
            "timeoutMs": timeout_ms,
            "idempotencyKey": str(uuid.uuid4()),
        },
        timeout_ms=OPENCLAW_CHAT_SEND_TIMEOUT_SECONDS * 1000,
    )
    run_id = str(send_result.get("runId") or "").strip()
    if not run_id:
        raise RuntimeError(f"OpenClaw Gateway가 runId를 반환하지 않았습니다: {send_result}")
    return session_key, openclaw_agent_id, baseline_fingerprint, run_id


def _run_openclaw_agent_turn(agent_id: str, content: str) -> tuple[str, str]:
    session_key, openclaw_agent_id, baseline_fingerprint, _run_id = _start_openclaw_agent_turn(agent_id, content)

    return _wait_for_openclaw_assistant_reply(
        session_key,
        openclaw_agent_id,
        baseline_fingerprint,
        OPENCLAW_CHAT_TIMEOUT_SECONDS * 1000,
    ), openclaw_agent_id


def _run_openclaw_context_action(agent_id: str, action: str) -> dict:
    agent_id = str(agent_id or "").strip() or "star"
    action = str(action or "").strip().lower()
    openclaw_agent_id = _resolve_openclaw_agent_id(agent_id)
    if not openclaw_agent_id:
        raise RuntimeError("이 캐릭터에 연결된 OpenClaw agent id가 없습니다")
    session_key = _openclaw_session_key(agent_id, openclaw_agent_id)

    if action == "compact":
        reply, _ = _run_openclaw_agent_turn(agent_id, "/compact")
        return {
            "action": action,
            "sessionKey": session_key,
            "message": reply,
        }

    if action == "reset":
        result = _openclaw_gateway_call(
            "sessions.reset",
            {"key": session_key},
            timeout_ms=30000,
        )
        return {
            "action": action,
            "sessionKey": session_key,
            "result": result,
        }

    raise ValueError("지원하지 않는 context action입니다")


def _public_office_url() -> str:
    configured = (
        os.environ.get("PUBLIC_OFFICE_URL")
        or os.environ.get("OFFICE_PUBLIC_URL")
        or os.environ.get("OFFICE_URL")
        or ""
    ).strip().rstrip("/")
    if configured:
        return configured
    return request.host_url.rstrip("/")


def _remote_install_ssh_command(host: str, ssh_user: str, ssh_port: int, ssh_key_path: str, ssh_password: str) -> list[str]:
    target = f"{ssh_user}@{host}"
    cmd = [
        "ssh",
        "-p",
        str(ssh_port),
        "-o",
        "BatchMode=yes" if not ssh_password else "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if ssh_key_path:
        cmd.extend(["-i", ssh_key_path])
    cmd.append(target)
    if ssh_password:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            raise RuntimeError("SSH password를 쓰려면 서버에 sshpass가 설치되어 있어야 합니다. SSH key path를 쓰는 방식을 권장합니다.")
        cmd = [sshpass, "-p", ssh_password] + cmd
    return cmd


def _normalize_guest_avatar(value: str | None) -> str:
    avatar = str(value or "").strip()
    return avatar if re.match(r"^guest_role_[1-6]$", avatar) else "guest_role_1"


def _safe_agent_path_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return safe or "agent"


def _remote_agent_default_paths(openclaw_agent_id: str) -> tuple[str, str, str]:
    safe_id = _safe_agent_path_id(openclaw_agent_id)
    base_dir = f"~/.clawffice/{safe_id}"
    return safe_id, base_dir, f"{base_dir}/state.json"


def _remote_install_payload(data: dict) -> dict:
    host = str(data.get("host") or "").strip()
    ssh_user = str(data.get("sshUser") or data.get("ssh_user") or "").strip()
    agent_name = str(data.get("agentName") or data.get("name") or "").strip()
    join_key = str(data.get("joinKey") or "").strip()
    openclaw_agent_id = str(data.get("openclawAgentId") or data.get("openclaw_agent_id") or "").strip()
    office_url = str(data.get("officeUrl") or "").strip().rstrip("/") or _public_office_url()
    ssh_key_path = str(data.get("sshKeyPath") or "").strip()
    ssh_password = str(data.get("sshPassword") or "").strip()
    safe_agent_id, remote_install_dir, remote_state_file = _remote_agent_default_paths(openclaw_agent_id)
    avatar = _normalize_guest_avatar(data.get("avatar"))
    try:
        ssh_port = int(str(data.get("sshPort") or "22").strip())
    except ValueError:
        raise ValueError("SSH 포트는 숫자여야 합니다")

    if not host or not ssh_user or not agent_name or not join_key or not openclaw_agent_id:
        raise ValueError("서버, SSH 사용자, agent 이름, OpenClaw agent id, join key는 필수입니다")
    if ssh_port < 1 or ssh_port > 65535:
        raise ValueError("SSH 포트 범위가 올바르지 않습니다")
    if host.startswith("-") or ssh_user.startswith("-"):
        raise ValueError("서버와 SSH 사용자는 '-'로 시작할 수 없습니다")
    if not re.match(r"^[a-zA-Z0-9_.@:-]+$", host):
        raise ValueError("서버 값에는 영문, 숫자, 점, 하이픈, 콜론만 사용할 수 있습니다")
    if not re.match(r"^[a-zA-Z0-9_.-]+$", ssh_user):
        raise ValueError("SSH 사용자 값에는 영문, 숫자, 점, 하이픈, 밑줄만 사용할 수 있습니다")
    for label, value in {
        "서버": host,
        "SSH 사용자": ssh_user,
        "agent id": safe_agent_id,
    }.items():
        if any(ch in value for ch in ("\n", "\r", "\0")):
            raise ValueError(f"{label} 값에 줄바꿈을 넣을 수 없습니다")
    if not office_url.startswith(("http://", "https://")):
        raise ValueError("office URL은 http:// 또는 https:// 로 시작해야 합니다")

    return {
        "host": host,
        "sshUser": ssh_user,
        "sshPort": ssh_port,
        "agentName": agent_name,
        "joinKey": join_key,
        "openclawAgentId": openclaw_agent_id,
        "officeUrl": office_url,
        "sshKeyPath": ssh_key_path,
        "sshPassword": ssh_password,
        "remoteStateFile": remote_state_file,
        "remoteInstallDir": remote_install_dir,
        "safeAgentId": safe_agent_id,
        "avatar": avatar,
    }


def _build_remote_agent_install_script(config: dict) -> str:
    with open(os.path.join(ROOT_DIR, "office-agent-push.py"), "rb") as f:
        script_b64 = base64.b64encode(f.read()).decode("ascii")

    env_lines = {
        "OFFICE_URL": config["officeUrl"],
        "OFFICE_JOIN_KEY": config["joinKey"],
        "OFFICE_AGENT_NAME": config["agentName"],
        "OFFICE_OPENCLAW_AGENT_ID": config["openclawAgentId"],
        "OFFICE_AGENT_AVATAR": config["avatar"],
    }

    exports = "\n".join(
        f"export {key}={shlex.quote(str(value))}"
        for key, value in env_lines.items()
    )
    safe_agent_id = shlex.quote(config["safeAgentId"])
    return f"""set -e
CLAWFFICE_AGENT_ID={safe_agent_id}
INSTALL_DIR="$HOME/.clawffice/$CLAWFFICE_AGENT_ID"
STATE_FILE="$INSTALL_DIR/state.json"
mkdir -p "$INSTALL_DIR"
if [ ! -f "$STATE_FILE" ]; then
  NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
  cat > "$STATE_FILE" <<__CLAWFFICE_STATE__
{{"state":"idle","detail":"대기 중","progress":0,"updated_at":"$NOW"}}
__CLAWFFICE_STATE__
fi
base64 -d > "$INSTALL_DIR/office-agent-push.py" <<'__CLAWFFICE_SCRIPT__'
{script_b64}
__CLAWFFICE_SCRIPT__
cat > "$INSTALL_DIR/office-agent.env" <<'__CLAWFFICE_ENV__'
{exports}
__CLAWFFICE_ENV__
cat > "$INSTALL_DIR/run-office-agent.sh" <<'__CLAWFFICE_RUN__'
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
set -a
. ./office-agent.env
set +a
export OFFICE_LOCAL_STATE_FILE="$(pwd)/state.json"
exec python3 ./office-agent-push.py
__CLAWFFICE_RUN__
chmod 700 "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/office-agent.env"
chmod 700 "$INSTALL_DIR/run-office-agent.sh" "$INSTALL_DIR/office-agent-push.py"
PID_FILE="$INSTALL_DIR/office-agent.pid"
if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
  echo "already-running"
else
  nohup "$INSTALL_DIR/run-office-agent.sh" > "$INSTALL_DIR/office-agent.log" 2>&1 &
  echo $! > "$PID_FILE"
  echo "started"
fi
"""


def _get_or_create_conversation(conn, agent_id: str, agent_name: str):
    now_iso = datetime.now().isoformat()
    row = conn.execute(
        "SELECT id, agent_id, agent_name, last_read_message_id, created_at, updated_at FROM conversations WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if row:
        if agent_name and row["agent_name"] != agent_name:
            conn.execute(
                "UPDATE conversations SET agent_name = ?, updated_at = ? WHERE id = ?",
                (agent_name, now_iso, row["id"]),
            )
            row = conn.execute(
                "SELECT id, agent_id, agent_name, last_read_message_id, created_at, updated_at FROM conversations WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return row

    cur = conn.execute(
        "INSERT INTO conversations (agent_id, agent_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (agent_id, agent_name, now_iso, now_iso),
    )
    return conn.execute(
        "SELECT id, agent_id, agent_name, last_read_message_id, created_at, updated_at FROM conversations WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()


def _insert_chat_message(conn, conversation_id: int, role: str, content: str, created_at: str | None = None):
    now_iso = created_at or datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, now_iso),
    )
    return conn.execute(
        "SELECT id, role, content, created_at FROM messages WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()


def _chat_message_exists(conn, conversation_id: int, role: str, content: str, created_at: str):
    row = conn.execute(
        """
        SELECT id
        FROM messages
        WHERE conversation_id = ?
          AND role = ?
          AND content = ?
          AND created_at = ?
        LIMIT 1
        """,
        (conversation_id, role, content, created_at),
    ).fetchone()
    return bool(row)


def _is_imported_openclaw_user_message(content: str | None) -> bool:
    text = (content or "").lstrip()
    return text.startswith("Sender (untrusted metadata):")


def _sync_openclaw_messages_for_conversation(conn, conversation_row):
    agent_id = conversation_row["agent_id"]
    openclaw_agent_id = _resolve_openclaw_agent_id(agent_id)
    if not openclaw_agent_id:
        return 0

    session_key = _openclaw_session_key(agent_id, openclaw_agent_id)
    records = _openclaw_message_records(session_key, openclaw_agent_id)
    if not records:
        return 0

    latest_message = conn.execute(
        """
        SELECT created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (conversation_row["id"],),
    ).fetchone()
    latest_ts = _iso_to_timestamp(latest_message["created_at"]) if latest_message else None
    imported = 0
    latest_imported_at = None
    for record in records:
        role = "agent" if record.get("role") == "assistant" else "user"
        if role != "agent":
            continue
        content = str(record.get("text") or "").strip()
        created_at = str(record.get("timestamp") or datetime.now().isoformat())
        if not content:
            continue
        record_ts = _iso_to_timestamp(created_at)
        if latest_ts is not None and record_ts is not None and record_ts <= latest_ts:
            continue
        if _chat_message_exists(conn, conversation_row["id"], role, content, created_at):
            continue
        _insert_chat_message(conn, conversation_row["id"], role, content, created_at)
        imported += 1
        latest_imported_at = created_at

    if latest_imported_at:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (latest_imported_at, conversation_row["id"]),
        )
    return imported


def _find_existing_agent_reply_for_user_message(conn, conversation_id: int, user_message_id: int):
    rows = conn.execute(
        """
        SELECT id, role, content
        FROM messages
        WHERE conversation_id = ?
          AND id > ?
        ORDER BY id ASC
        """,
        (conversation_id, user_message_id),
    ).fetchall()
    for row in rows:
        role = str(row["role"] or "").lower()
        if role == "user" and not _is_imported_openclaw_user_message(row["content"]):
            return None
        if role == "agent":
            return row
    return None


def _expire_stale_chat_turns(conn):
    now_iso = datetime.now().isoformat()
    cutoff = (datetime.now() - timedelta(seconds=OPENCLAW_CHAT_TIMEOUT_SECONDS + 60)).isoformat()
    conn.execute(
        """
        UPDATE chat_turns
        SET status = 'error',
            error_text = COALESCE(error_text, '응답 생성이 중단되었습니다. 다시 메시지를 보내주세요.'),
            updated_at = ?
        WHERE status IN ('pending', 'finalizing') AND created_at < ?
        """,
        (now_iso, cutoff),
    )


def _get_pending_chat_turn(conn, conversation_id: int):
    _expire_stale_chat_turns(conn)
    return conn.execute(
        """
        SELECT id, conversation_id, user_message_id, status, created_at, updated_at
        FROM chat_turns
        WHERE conversation_id = ? AND status IN ('pending', 'finalizing')
        ORDER BY id DESC
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()


def _insert_chat_queue_item(conn, conversation_id: int, content: str, created_at: str | None = None):
    now_iso = created_at or datetime.now().isoformat()
    cur = conn.execute(
        """
        INSERT INTO chat_queue (conversation_id, content, status, created_at, updated_at)
        VALUES (?, ?, 'queued', ?, ?)
        """,
        (conversation_id, content, now_iso, now_iso),
    )
    return conn.execute(
        """
        SELECT id, conversation_id, content, status, created_at, updated_at
        FROM chat_queue
        WHERE id = ?
        """,
        (cur.lastrowid,),
    ).fetchone()


def _chat_queue_items(conn, conversation_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, content, status, created_at, updated_at
        FROM chat_queue
        WHERE conversation_id = ? AND status = 'queued'
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "content": row["content"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        for row in rows
    ]


def _insert_chat_turn(conn, conversation_id: int, user_message_id: int, created_at: str | None = None):
    now_iso = created_at or datetime.now().isoformat()
    cur = conn.execute(
        """
        INSERT INTO chat_turns (conversation_id, user_message_id, status, created_at, updated_at)
        VALUES (?, ?, 'pending', ?, ?)
        """,
        (conversation_id, user_message_id, now_iso, now_iso),
    )
    return conn.execute(
        """
        SELECT id, conversation_id, user_message_id, status, created_at, updated_at
        FROM chat_turns
        WHERE id = ?
        """,
        (cur.lastrowid,),
    ).fetchone()


def _finish_chat_turn(conn, turn_id: int, status: str, openclaw_agent_id: str | None = None, reply_message_id: int | None = None, error_message_id: int | None = None, error_text: str | None = None):
    now_iso = datetime.now().isoformat()
    conn.execute(
        """
        UPDATE chat_turns
        SET status = ?,
            openclaw_agent_id = COALESCE(?, openclaw_agent_id),
            reply_message_id = COALESCE(?, reply_message_id),
            error_message_id = COALESCE(?, error_message_id),
            error_text = COALESCE(?, error_text),
            updated_at = ?
        WHERE id = ?
        """,
        (status, openclaw_agent_id, reply_message_id, error_message_id, error_text, now_iso, turn_id),
    )


def _claim_chat_turn_for_completion(conn, turn_id: int) -> bool:
    cur = conn.execute(
        """
        UPDATE chat_turns
        SET status = 'finalizing',
            updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (datetime.now().isoformat(), turn_id),
    )
    return cur.rowcount == 1


def _mark_chat_turn_gateway_accepted(conn, turn_id: int, openclaw_agent_id: str | None = None, run_id: str | None = None, accepted_at: str | None = None):
    now_iso = accepted_at or datetime.now().isoformat()
    conn.execute(
        """
        UPDATE chat_turns
        SET openclaw_agent_id = COALESCE(?, openclaw_agent_id),
            gateway_run_id = COALESCE(?, gateway_run_id),
            gateway_accepted_at = COALESCE(gateway_accepted_at, ?),
            updated_at = ?
        WHERE id = ?
        """,
        (openclaw_agent_id, run_id, now_iso, now_iso, turn_id),
    )


def _recover_pending_chat_turns(conn, conversation_row):
    agent_id = conversation_row["agent_id"]
    agent_name = conversation_row["agent_name"]
    openclaw_agent_id = _resolve_openclaw_agent_id(agent_id)
    session_key = _openclaw_session_key(agent_id, openclaw_agent_id)
    pending_turns = conn.execute(
        """
        SELECT id, conversation_id, user_message_id, status, error_message_id, created_at, gateway_accepted_at
        FROM chat_turns
        WHERE conversation_id = ?
          AND (
            status = 'pending'
            OR (
              status = 'error'
              AND COALESCE(error_text, '') LIKE '%GatewayTransportError: gateway timeout%'
            )
          )
        ORDER BY id ASC
        """,
        (conversation_row["id"],),
    ).fetchall()

    recovered = 0
    for turn in pending_turns:
        user_message = conn.execute(
            "SELECT id, content, created_at FROM messages WHERE id = ?",
            (turn["user_message_id"],),
        ).fetchone()
        if not user_message:
            _finish_chat_turn(conn, turn["id"], "error", error_text="사용자 메시지를 찾지 못했습니다.")
            continue

        next_user_message = conn.execute(
            """
            SELECT created_at
            FROM messages
            WHERE conversation_id = ?
              AND id > ?
              AND lower(role) = 'user'
              AND content NOT LIKE 'Sender (untrusted metadata):%'
            ORDER BY id ASC
            LIMIT 1
            """,
            (conversation_row["id"], user_message["id"]),
        ).fetchone()
        before_iso = next_user_message["created_at"] if next_user_message else None

        submitted_at = _find_openclaw_user_submission_for_content(
            session_key,
            openclaw_agent_id,
            user_message["content"],
            user_message["created_at"],
            before_iso,
        )
        if submitted_at:
            _mark_chat_turn_gateway_accepted(
                conn,
                turn["id"],
                openclaw_agent_id=openclaw_agent_id,
                accepted_at=submitted_at,
            )
            if turn["status"] == "error":
                if turn["error_message_id"]:
                    conn.execute("DELETE FROM messages WHERE id = ?", (turn["error_message_id"],))
                conn.execute(
                    """
                    UPDATE chat_turns
                    SET status = 'pending',
                        error_message_id = NULL,
                        error_text = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (datetime.now().isoformat(), turn["id"]),
                )

        existing_reply = _find_existing_agent_reply_for_user_message(conn, conversation_row["id"], user_message["id"])
        if existing_reply:
            if not _claim_chat_turn_for_completion(conn, turn["id"]):
                continue
            _finish_chat_turn(
                conn,
                turn["id"],
                "done",
                openclaw_agent_id=openclaw_agent_id,
                reply_message_id=existing_reply["id"],
            )
            recovered += 1
            continue

        found = _find_openclaw_reply_for_user_content(
            session_key,
            openclaw_agent_id,
            user_message["content"],
            user_message["created_at"],
            before_iso,
        )
        if not found:
            continue
        reply_text, reply_created_at = found
        if not _claim_chat_turn_for_completion(conn, turn["id"]):
            continue
        if turn["error_message_id"]:
            conn.execute("DELETE FROM messages WHERE id = ?", (turn["error_message_id"],))
        reply_message = _insert_chat_message(conn, conversation_row["id"], "agent", reply_text, reply_created_at)
        _finish_chat_turn(
            conn,
            turn["id"],
            "done",
            openclaw_agent_id=openclaw_agent_id,
            reply_message_id=reply_message["id"],
        )
        conn.execute(
            "UPDATE conversations SET agent_name = ?, updated_at = ? WHERE id = ?",
            (agent_name, reply_message["created_at"], conversation_row["id"]),
        )
        recovered += 1
    return recovered


def _conversation_payload(row):
    return {
        "id": row["id"],
        "agentId": row["agent_id"],
        "agentName": row["agent_name"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "lastReadMessageId": row["last_read_message_id"],
    }


def _conversation_unread_map(conn) -> dict[str, bool]:
    rows = conn.execute(
        """
        SELECT c.agent_id,
               EXISTS(
                   SELECT 1
                   FROM messages m
                   WHERE m.conversation_id = c.id
                     AND m.role = 'agent'
                     AND m.id > COALESCE(c.last_read_message_id, 0)
               ) AS has_unread
        FROM conversations c
        """
    ).fetchall()
    return {row["agent_id"]: bool(row["has_unread"]) for row in rows}


def _mark_conversation_read(conn, conversation_id: int):
    latest_agent_message = conn.execute(
        """
        SELECT COALESCE(MAX(id), 0) AS latest_id
        FROM messages
        WHERE conversation_id = ? AND role = 'agent'
        """,
        (conversation_id,),
    ).fetchone()
    latest_id = int((latest_agent_message or {})["latest_id"] or 0)
    conn.execute(
        """
        UPDATE conversations
        SET last_read_message_id = MAX(COALESCE(last_read_message_id, 0), ?)
        WHERE id = ?
        """,
        (latest_id, conversation_id),
    )
    return latest_id


def _message_payload(row):
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "createdAt": row["created_at"],
    }


def _pending_message_payload(turn):
    return {
        "id": f"pending-{turn['id']}",
        "role": "system",
        "content": CHAT_WAITING_MESSAGE,
        "createdAt": turn["created_at"],
        "pending": True,
        "turnId": turn["id"],
    }


def _dequeue_next_chat_turn(conn, conversation_id: int):
    if _get_pending_chat_turn(conn, conversation_id):
        return None
    item = conn.execute(
        """
        SELECT id, content, created_at
        FROM chat_queue
        WHERE conversation_id = ? AND status = 'queued'
        ORDER BY id ASC
        LIMIT 1
        """,
        (conversation_id,),
    ).fetchone()
    if not item:
        return None
    now_iso = datetime.now().isoformat()
    message = _insert_chat_message(conn, conversation_id, "user", item["content"], now_iso)
    turn = _insert_chat_turn(conn, conversation_id, message["id"], now_iso)
    conn.execute(
        """
        UPDATE chat_queue
        SET status = 'sent', sent_message_id = ?, updated_at = ?
        WHERE id = ? AND status = 'queued'
        """,
        (message["id"], now_iso, item["id"]),
    )
    return turn, item["content"]


def _conversation_messages_with_pending(conn, conversation_id: int, limit: int):
    suppressed_error_rows = conn.execute(
        """
        SELECT error_message_id
        FROM chat_turns
        WHERE conversation_id = ?
          AND error_message_id IS NOT NULL
          AND COALESCE(error_text, '') LIKE '%GatewayTransportError: gateway timeout%'
        """,
        (conversation_id,),
    ).fetchall()
    suppressed_error_message_ids = {row["error_message_id"] for row in suppressed_error_rows}
    rows = conn.execute(
        """
        SELECT id, role, content, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (conversation_id, limit),
    ).fetchall()
    messages = [
        _message_payload(row)
        for row in reversed(rows)
        if row["id"] not in suppressed_error_message_ids
        and not (
            str(row["role"] or "").lower() == "user"
            and _is_imported_openclaw_user_message(row["content"])
        )
    ]
    turn_rows = conn.execute(
        """
        SELECT user_message_id, status, gateway_accepted_at, reply_message_id
        FROM chat_turns
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchall()
    turns_by_user_id = {row["user_message_id"]: row for row in turn_rows}
    for index, message in enumerate(messages):
        if str(message.get("role") or "").lower() != "user":
            continue
        turn = turns_by_user_id.get(message.get("id"))
        if turn:
            message["read"] = bool(turn["gateway_accepted_at"] or turn["reply_message_id"] or turn["status"] == "done")
            continue
        for next_message in messages[index + 1:]:
            next_role = str(next_message.get("role") or "").lower()
            if next_role == "agent":
                message["read"] = True
                break
            if next_role == "user":
                break
    pending_turns = conn.execute(
        """
        SELECT id, user_message_id, created_at
        FROM chat_turns
        WHERE conversation_id = ? AND status IN ('pending', 'finalizing')
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()
    if not pending_turns:
        return messages, False

    pending_by_user_id = {turn["user_message_id"]: turn for turn in pending_turns}
    inserted = set()
    with_pending = []
    for message in messages:
        with_pending.append(message)
        turn = pending_by_user_id.get(message.get("id"))
        if turn:
            with_pending.append(_pending_message_payload(turn))
            inserted.add(turn["id"])

    for turn in pending_turns:
        if turn["id"] not in inserted:
            with_pending.append(_pending_message_payload(turn))

    return with_pending, True


def _ensure_home_favorites_index():
    os.makedirs(HOME_FAVORITES_DIR, exist_ok=True)
    if not os.path.exists(HOME_FAVORITES_INDEX_FILE):
        with open(HOME_FAVORITES_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, ensure_ascii=False, indent=2)


def _load_home_favorites_index():
    _ensure_home_favorites_index()
    try:
        with open(HOME_FAVORITES_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
    except Exception:
        pass
    return {"items": []}


def _save_home_favorites_index(data):
    _ensure_home_favorites_index()
    with open(HOME_FAVORITES_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _maybe_apply_random_home_favorite():
    """On page open, randomly apply one saved home favorite if available."""
    global _last_home_rotate_at

    if not AUTO_ROTATE_HOME_ON_PAGE_OPEN:
        return False, "disabled"

    try:
        now_ts = datetime.now().timestamp()
        if _last_home_rotate_at and (now_ts - _last_home_rotate_at) < AUTO_ROTATE_MIN_INTERVAL_SECONDS:
            return False, "throttled"

        idx = _load_home_favorites_index()
        items = idx.get("items") or []
        candidates = []
        for it in items:
            rel = (it.get("path") or "").strip()
            if not rel:
                continue
            abs_path = os.path.join(ROOT_DIR, rel)
            if os.path.exists(abs_path):
                candidates.append((rel, abs_path))

        if not candidates:
            return False, "no-favorites"

        rel, src = random.choice(candidates)
        target = FRONTEND_PATH / "office_bg_small.webp"
        if not target.exists():
            return False, "missing-office-bg"

        shutil.copy2(src, str(target))
        _last_home_rotate_at = now_ts
        return True, rel
    except Exception as e:
        return False, str(e)


def load_join_keys():
    return _store_load_join_keys(JOIN_KEYS_FILE)


def save_join_keys(data):
    _store_save_join_keys(JOIN_KEYS_FILE, data)


def _ensure_magick_or_ffmpeg_available():
    if shutil.which("magick"):
        return "magick"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    return None


def _probe_animated_frame_size(upload_path: str):
    """Return (w,h) from first frame if possible."""
    if Image is not None:
        try:
            with Image.open(upload_path) as im:
                w, h = im.size
                return int(w), int(h)
        except Exception:
            pass
    # ffprobe fallback
    if shutil.which("ffprobe"):
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x",
                upload_path,
            ]
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=5).decode().strip()
            if "x" in out:
                w, h = out.split("x", 1)
                return int(w), int(h)
        except Exception:
            pass
    return None, None


def _animated_to_spritesheet(
    upload_path: str,
    frame_w: int,
    frame_h: int,
    out_ext: str = ".webp",
    preserve_original: bool = True,
    pixel_art: bool = True,
    cols: int | None = None,
    rows: int | None = None,
):
    """Convert animated GIF/WEBP to spritesheet, return (out_path, columns, rows, frames, out_frame_w, out_frame_h)."""
    backend = _ensure_magick_or_ffmpeg_available()
    if not backend:
        raise RuntimeError("ImageMagick/ffmpeg을 찾지 못해 애니메이션 이미지를 자동 변환할 수 없습니다")

    ext = (out_ext or ".webp").lower()
    if ext not in {".webp", ".png"}:
        ext = ".webp"

    out_fd, out_path = tempfile.mkstemp(suffix=ext)
    os.close(out_fd)

    with tempfile.TemporaryDirectory() as td:
        frames = 0
        out_fw, out_fh = int(frame_w), int(frame_h)
        if Image is not None:
            try:
                with Image.open(upload_path) as im:
                    n = getattr(im, "n_frames", 1)
                    # 默认保留用户原始帧尺寸（避免先压缩再放大导致像素糊）
                    if preserve_original:
                        out_fw, out_fh = im.size
                    for i in range(n):
                        im.seek(i)
                        fr = im.convert("RGBA")
                        if not preserve_original and (fr.size != (out_fw, out_fh)):
                            resample = Image.Resampling.NEAREST if pixel_art else Image.Resampling.LANCZOS
                            fr = fr.resize((out_fw, out_fh), resample)
                        fr.save(os.path.join(td, f"f_{i:04d}.png"), "PNG")
                    frames = n
            except Exception:
                frames = 0

        if frames <= 0:
            cmd1 = f"ffmpeg -y -i '{upload_path}' '{td}/f_%04d.png' >/dev/null 2>&1"
            if os.system(cmd1) != 0:
                raise RuntimeError("애니메이션 이미지 프레임 추출에 실패했습니다(Pillow/ffmpeg 모두 실패)")
            files = sorted([x for x in os.listdir(td) if x.startswith("f_") and x.endswith(".png")])
            frames = len(files)
            if frames <= 0:
                raise RuntimeError("애니메이션 이미지에 유효한 프레임이 없습니다")

        if backend == "magick":
            # 像素风动图转精灵表默认无损，避免颜色/边缘被压缩糊掉
            quality_flag = "-define webp:lossless=true -define webp:method=6 -quality 100" if ext == ".webp" else ""
            # 允许按 cols/rows 排布；默认单行
            if cols is None or cols <= 0:
                cols_eff = frames
            else:
                cols_eff = max(1, int(cols))
            rows_eff = max(1, int(rows)) if (rows is not None and rows > 0) else max(1, math.ceil(frames / cols_eff))

            # 先规范单帧尺寸
            prep = ""
            if not preserve_original:
                magick_filter = "-filter point" if pixel_art else ""
                prep = f" {magick_filter} -resize {out_fw}x{out_fh}^ -gravity center -background none -extent {out_fw}x{out_fh}"

            cmd = (
                f"magick '{td}/f_*.png'{prep} "
                f"-tile {cols_eff}x{rows_eff} -background none -geometry +0+0 {quality_flag} '{out_path}'"
            )
            rc = os.system(cmd)
            if rc != 0:
                raise RuntimeError("ImageMagick 스프라이트 시트 생성에 실패했습니다")
            return out_path, cols_eff, rows_eff, frames, out_fw, out_fh

        ffmpeg_quality = "-lossless 1 -compression_level 6 -q:v 100" if ext == ".webp" else ""
        cols_eff = max(1, int(cols)) if (cols is not None and cols > 0) else frames
        rows_eff = max(1, int(rows)) if (rows is not None and rows > 0) else max(1, math.ceil(frames / cols_eff))
        if preserve_original:
            vf = f"tile={cols_eff}x{rows_eff}"
        else:
            scale_algo = "neighbor" if pixel_art else "lanczos"
            vf = (
                f"scale={out_fw}:{out_fh}:force_original_aspect_ratio=decrease:flags={scale_algo},"
                f"pad={out_fw}:{out_fh}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
                f"tile={cols_eff}x{rows_eff}"
            )
        cmd2 = (
            f"ffmpeg -y -pattern_type glob -i '{td}/f_*.png' "
            f"-vf '{vf}' "
            f"{ffmpeg_quality} '{out_path}' >/dev/null 2>&1"
        )
        if os.system(cmd2) != 0:
            raise RuntimeError("ffmpeg 스프라이트 시트 생성에 실패했습니다")
        return out_path, frames, 1, frames, out_fw, out_fh


def normalize_agent_state(s):
    """Normalize agent state for compatibility.
    Maps synonyms (e.g. working/busy -> writing, run/running -> executing) into VALID_AGENT_STATES.
    Returns 'idle' for unknown values.
    """
    if not s:
        return 'idle'
    s_lower = s.lower().strip()
    if s_lower in {'working', 'busy', 'write'}:
        return 'writing'
    if s_lower in {'run', 'running', 'execute', 'exec'}:
        return 'executing'
    if s_lower in {'sync'}:
        return 'syncing'
    if s_lower in {'research', 'search'}:
        return 'researching'
    if s_lower in VALID_AGENT_STATES:
        return s_lower
    return 'idle'


# User-facing model aliases -> provider model ids
USER_MODEL_TO_PROVIDER_MODELS = {
    # 严格按用户要求：仅两种官方模型映射
    "nanobanana-pro": [
        "nano-banana-pro-preview",
    ],
    "nanobanana-2": [
        "gemini-2.5-flash-image",
    ],
}

PROVIDER_MODEL_TO_USER_MODEL = {
    provider: user
    for user, providers in USER_MODEL_TO_PROVIDER_MODELS.items()
    for provider in providers
}


def _normalize_user_model(model_name: str) -> str:
    m = (model_name or "").strip()
    if not m:
        return "nanobanana-pro"
    low = m.lower()
    if low in USER_MODEL_TO_PROVIDER_MODELS:
        return low
    if low in PROVIDER_MODEL_TO_USER_MODEL:
        return PROVIDER_MODEL_TO_USER_MODEL[low]
    return "nanobanana-pro"


def _provider_model_candidates(user_model: str):
    normalized = _normalize_user_model(user_model)
    return list(USER_MODEL_TO_PROVIDER_MODELS.get(normalized, USER_MODEL_TO_PROVIDER_MODELS["nanobanana-pro"]))


def _generate_rpg_background_to_webp(out_webp_path: str, width: int = 1280, height: int = 720, custom_prompt: str = "", speed_mode: str = "fast"):
    """Generate RPG-style room background and save as webp.

    speed_mode:
      - fast: use nanobanana-2 + 1024x576 intermediate + downscaled reference (faster)
      - quality: use configured model (fallback nanobanana-pro) + full 1280x720 path
    """
    runtime_cfg = load_runtime_config()
    api_key = (runtime_cfg.get("gemini_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("MISSING_API_KEY")
    themes = [
        "8-bit dungeon guild room",
        "8-bit stardew-valley inspired cozy farm tavern",
        "8-bit nordic fantasy tavern",
        "8-bit magitech workshop",
        "8-bit elven forest inn",
        "8-bit pixel cyber tavern",
        "8-bit desert caravan inn",
        "8-bit snow mountain lodge",
    ]
    theme = random.choice(themes)

    if not (os.path.exists(GEMINI_PYTHON) and os.path.exists(GEMINI_SCRIPT)):
        raise RuntimeError("이미지 생성 환경이 없습니다: gemini-image-generate가 설치되지 않았습니다")

    style_hint = (custom_prompt or "").strip()
    if not style_hint:
        style_hint = theme

    # 默认使用更稳妥的 quality 档，避免 fast 模型在部分 API 通道不可用
    mode = (speed_mode or "quality").strip().lower()
    if mode not in {"fast", "quality"}:
        mode = "quality"

    configured_user_model = _normalize_user_model(runtime_cfg.get("gemini_model") or "nanobanana-pro")
    if mode == "fast":
        preferred_user_model = "nanobanana-2"
        # fast 也提高基础清晰度：从 1024x576 提升到 1152x648（牺牲少量速度）
        gen_width, gen_height = 1152, 648
        ref_width, ref_height = 1152, 648
    else:
        preferred_user_model = configured_user_model
        gen_width, gen_height = width, height
        ref_width, ref_height = width, height

    # 同时规避可能触发 400 的特殊能力参数：
    # 仅 nanobanana-2 走 aspect-ratio，nanobanana-pro 交给模型默认比例（后续再标准化到 1280x720）
    allow_aspect_ratio = (preferred_user_model == "nanobanana-2")

    prompt = (
        "Use a top-down pixel room composition compatible with an office game scene. "
        "STRICTLY preserve the same room geometry, camera angle, wall/floor boundaries and major object placement as the provided reference image. "
        "Keep region layout stable (left work area, center lounge, right error area). "
        "Only change visual style/theme/material/lighting according to: " + style_hint + ". "
        "Do not add text or watermark. Retro 8-bit RPG style."
    )

    tmp_dir = tempfile.mkdtemp(prefix="rpg-bg-")
    cmd = [
        GEMINI_PYTHON,
        GEMINI_SCRIPT,
        "--prompt", prompt,
        "--model", configured_user_model,
        "--out-dir", tmp_dir,
        "--cleanup",
    ]
    if allow_aspect_ratio:
        cmd.extend(["--aspect-ratio", "16:9"])

    # 强约束：每次都带固定参考图，保持房间区域布局不漂移
    ref_for_call = None
    if os.path.exists(ROOM_REFERENCE_IMAGE):
        ref_for_call = ROOM_REFERENCE_IMAGE
        if mode == "fast" and Image is not None:
            try:
                ref_fast = os.path.join(tmp_dir, "room-reference-fast.webp")
                with Image.open(ROOM_REFERENCE_IMAGE) as rim:
                    rim = rim.convert("RGBA").resize((ref_width, ref_height), Image.Resampling.LANCZOS)
                    rim.save(ref_fast, "WEBP", quality=85, method=4)
                ref_for_call = ref_fast
            except Exception:
                ref_for_call = ROOM_REFERENCE_IMAGE

    if ref_for_call:
        cmd.extend(["--reference-image", ref_for_call])

    env = os.environ.copy()
    # 运行时配置优先：只保留 GEMINI_API_KEY，避免脚本因双 key 报错
    env.pop("GOOGLE_API_KEY", None)
    env["GEMINI_API_KEY"] = api_key

    def _run_cmd(cmd_args):
        return subprocess.run(cmd_args, capture_output=True, text=True, env=env, timeout=240)

    def _is_model_unavailable_error(text: str) -> bool:
        low = (text or "").strip().lower()
        return (
            ("not found" in low and "models/" in low)
            or ("model_not_available" in low)
            or ("model is not available" in low)
            or ("configured model is not available" in low)
            or ("this model is not available" in low)
            or ("not supported for generatecontent" in low)
        )

    def _with_model(cmd_args, model_name: str):
        m = cmd_args[:]
        if "--model" in m:
            idx = m.index("--model")
            if idx + 1 < len(m):
                m[idx + 1] = model_name
        else:
            m.extend(["--model", model_name])
        return m

    # 模型多级回退（仅允许两类用户模型：nanobanana-pro / nanobanana-2）
    # 每个用户模型映射到若干 provider 真实模型。
    user_model_order = [preferred_user_model, configured_user_model]
    user_model_order = [m for i, m in enumerate(user_model_order) if m and m not in user_model_order[:i]]

    model_candidates = []
    for um in user_model_order:
        model_candidates.extend(_provider_model_candidates(um))
    # 去重并清理空项
    model_candidates = [m for i, m in enumerate(model_candidates) if m and m not in model_candidates[:i]]

    proc = None
    last_err_text = ""
    model_unavailable_count = 0

    for mname in model_candidates:
        env["GEMINI_MODEL"] = mname
        try_cmd = _with_model(cmd, mname)
        proc = _run_cmd(try_cmd)
        if proc.returncode == 0:
            break

        err_text = (proc.stderr or proc.stdout or "").strip()
        last_err_text = err_text

        # key 失效/泄漏：立即终止，不继续尝试
        low = err_text.lower()
        if "your api key was reported as leaked" in low or "permission_denied" in low:
            raise RuntimeError("API_KEY_REVOKED_OR_LEAKED")

        if _is_model_unavailable_error(err_text):
            model_unavailable_count += 1
            continue

        # 非模型不可用错误，直接返回真实错误
        raise RuntimeError(f"이미지 생성 실패: {err_text}")

    if proc is None or proc.returncode != 0:
        err_text = (last_err_text or "").strip()
        if model_unavailable_count >= len(model_candidates) or _is_model_unavailable_error(err_text):
            brief = (err_text or "").replace("\n", " ")[:240]
            raise RuntimeError(f"MODEL_NOT_AVAILABLE::{brief}")
        raise RuntimeError(f"이미지 생성 실패: {err_text}")

    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        raise RuntimeError("이미지 생성 결과를 해석하지 못했습니다")

    files = result.get("files") or []
    if not files:
        raise RuntimeError("이미지 생성 결과 파일이 반환되지 않았습니다")

    gen_path = files[0]
    if not os.path.exists(gen_path):
        raise RuntimeError("이미지 생성 파일이 존재하지 않습니다")

    if Image is None:
        raise RuntimeError("Pillow를 사용할 수 없어 이미지 크기를 표준화할 수 없습니다")

    with Image.open(gen_path) as im:
        im = im.convert("RGBA")
        # 质量模式优先保细节；快速模式优先速度
        if mode == "fast":
            im = im.resize((gen_width, gen_height), Image.Resampling.LANCZOS)
            if (gen_width, gen_height) != (width, height):
                # fast 的放大改为 LANCZOS，牺牲少量速度换更高细节
                im = im.resize((width, height), Image.Resampling.LANCZOS)
            im.save(out_webp_path, "WEBP", quality=96, method=6)
        else:
            # quality：确保输出标准尺寸，同时使用无损 webp，减少压缩损失
            if im.size != (width, height):
                im = im.resize((width, height), Image.Resampling.LANCZOS)
            im.save(out_webp_path, "WEBP", lossless=True, quality=100, method=6)


def state_to_area(state):
    """Map agent state to office area (breakroom / writing / error)."""
    return STATE_TO_AREA_MAP.get(state, "breakroom")


# Ensure files exist
if not os.path.exists(AGENTS_STATE_FILE):
    save_agents_state(DEFAULT_AGENTS)
if not os.path.exists(JOIN_KEYS_FILE):
    if os.path.exists(os.path.join(ROOT_DIR, "join-keys.sample.json")):
        try:
            with open(os.path.join(ROOT_DIR, "join-keys.sample.json"), "r", encoding="utf-8") as sf:
                sample = json.load(sf)
            save_join_keys(sample if isinstance(sample, dict) else {"keys": []})
        except Exception:
            save_join_keys({"keys": []})
    else:
        save_join_keys({"keys": []})

# Tighten runtime-config file perms if exists
if os.path.exists(RUNTIME_CONFIG_FILE):
    try:
        os.chmod(RUNTIME_CONFIG_FILE, 0o600)
    except Exception:
        pass

init_chat_db()


def _start_next_queued_chat_turn(agent_id: str, agent_name: str):
    next_turn = None
    with chat_db_lock:
        with _chat_db() as conn:
            conversation = _get_or_create_conversation(conn, agent_id, agent_name)
            dequeued = _dequeue_next_chat_turn(conn, conversation["id"])
            if dequeued:
                next_turn, content = dequeued
                conn.execute(
                    "UPDATE conversations SET agent_name = ?, updated_at = ? WHERE id = ?",
                    (agent_name, datetime.now().isoformat(), conversation["id"]),
                )
    if not next_turn:
        return
    threading.Thread(
        target=_complete_chat_turn_background,
        args=(next_turn["id"], agent_id, agent_name, content),
        daemon=True,
        name=f"chat-turn-{next_turn['id']}",
    ).start()


def _complete_chat_turn_background(turn_id: int, agent_id: str, agent_name: str, content: str):
    try:
        try:
            session_key, openclaw_agent_id, baseline_fingerprint, run_id = _start_openclaw_agent_turn(agent_id, content)
            with chat_db_lock:
                with _chat_db() as conn:
                    turn = conn.execute(
                        "SELECT id, status FROM chat_turns WHERE id = ?",
                        (turn_id,),
                    ).fetchone()
                    if not turn or turn["status"] != "pending":
                        return
                    _mark_chat_turn_gateway_accepted(
                        conn,
                        turn_id,
                        openclaw_agent_id=openclaw_agent_id,
                        run_id=run_id,
                    )
            reply_content = _wait_for_openclaw_assistant_reply(
                session_key,
                openclaw_agent_id,
                baseline_fingerprint,
                OPENCLAW_CHAT_TIMEOUT_SECONDS * 1000,
            )
        except Exception as e:
            if "GatewayTransportError: gateway timeout" in str(e) or "gateway timeout" in str(e):
                with chat_db_lock:
                    with _chat_db() as conn:
                        conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                        _recover_pending_chat_turns(conn, conversation)
                return
            error_text = f"OpenClaw 대화 연결 실패: {e}"
            with chat_db_lock:
                with _chat_db() as conn:
                    turn = conn.execute(
                        "SELECT id, conversation_id, user_message_id, status FROM chat_turns WHERE id = ?",
                        (turn_id,),
                    ).fetchone()
                    if not turn or turn["status"] != "pending":
                        return
                    conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                    error_message = _insert_chat_message(conn, conversation["id"], "system", error_text)
                    _finish_chat_turn(
                        conn,
                        turn_id,
                        "error",
                        error_message_id=error_message["id"],
                        error_text=error_text,
                    )
                    conn.execute(
                        "UPDATE conversations SET agent_name = ?, updated_at = ? WHERE id = ?",
                        (agent_name, error_message["created_at"], conversation["id"]),
                    )
            return

        with chat_db_lock:
            with _chat_db() as conn:
                turn = conn.execute(
                    "SELECT id, conversation_id, user_message_id, status FROM chat_turns WHERE id = ?",
                    (turn_id,),
                ).fetchone()
                if not turn or turn["status"] != "pending":
                    return
                conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                if not _claim_chat_turn_for_completion(conn, turn_id):
                    return
                existing_reply = _find_existing_agent_reply_for_user_message(conn, conversation["id"], turn["user_message_id"])
                if existing_reply:
                    _finish_chat_turn(
                        conn,
                        turn_id,
                        "done",
                        openclaw_agent_id=openclaw_agent_id,
                        reply_message_id=existing_reply["id"],
                    )
                else:
                    reply_message = _insert_chat_message(conn, conversation["id"], "agent", reply_content)
                    _finish_chat_turn(
                        conn,
                        turn_id,
                        "done",
                        openclaw_agent_id=openclaw_agent_id,
                        reply_message_id=reply_message["id"],
                    )
                    conn.execute(
                        "UPDATE conversations SET agent_name = ?, updated_at = ? WHERE id = ?",
                        (agent_name, reply_message["created_at"], conversation["id"]),
                    )
    finally:
        _start_next_queued_chat_turn(agent_id, agent_name)


@app.route("/chat/conversation", methods=["GET"])
def get_chat_conversation():
    """Get or create an agent conversation and return recent messages."""
    try:
        agent_id = _normalize_chat_agent_id(request.args.get("agentId", ""))
        agent_name = _normalize_chat_agent_name(request.args.get("agentName", ""), agent_id)
        limit = min(500, max(1, int(request.args.get("limit", "200"))))
        recovered_turns = 0

        with chat_db_lock:
            with _chat_db() as conn:
                conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                _sync_openclaw_messages_for_conversation(conn, conversation)
                recovered_turns = _recover_pending_chat_turns(conn, conversation)
                _expire_stale_chat_turns(conn)
                conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                messages, pending = _conversation_messages_with_pending(conn, conversation["id"], limit)
                queue_items = _chat_queue_items(conn, conversation["id"])

        if recovered_turns or (queue_items and not pending):
            _start_next_queued_chat_turn(agent_id, agent_name)

        return jsonify({
            "ok": True,
            "conversation": _conversation_payload(conversation),
            "messages": messages,
            "pending": pending,
            "queue": queue_items,
        })
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/chat/queue/<int:item_id>", methods=["PATCH", "DELETE"])
def update_chat_queue_item(item_id: int):
    try:
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400
        agent_id = _normalize_chat_agent_id(data.get("agentId", ""))
        agent_name = _normalize_chat_agent_name(data.get("agentName", ""), agent_id)
        with chat_db_lock:
            with _chat_db() as conn:
                conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                item = conn.execute(
                    "SELECT id, conversation_id, status FROM chat_queue WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if not item or item["conversation_id"] != conversation["id"] or item["status"] != "queued":
                    return jsonify({"ok": False, "msg": "queue 항목을 찾지 못했습니다"}), 404
                now_iso = datetime.now().isoformat()
                if request.method == "DELETE":
                    conn.execute(
                        "UPDATE chat_queue SET status = 'cancelled', updated_at = ? WHERE id = ? AND status = 'queued'",
                        (now_iso, item_id),
                    )
                else:
                    content = str(data.get("content") or "").strip()
                    if not content:
                        return jsonify({"ok": False, "msg": "메시지를 입력해주세요"}), 400
                    if len(content) > 8000:
                        return jsonify({"ok": False, "msg": "메시지가 너무 깁니다"}), 400
                    conn.execute(
                        "UPDATE chat_queue SET content = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                        (content, now_iso, item_id),
                    )
                queue_items = _chat_queue_items(conn, conversation["id"])
        return jsonify({"ok": True, "queue": queue_items})
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/chat/read", methods=["POST"])
def mark_chat_read():
    """Mark an agent conversation as read by the user."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400

        agent_id = _normalize_chat_agent_id(data.get("agentId", ""))
        agent_name = _normalize_chat_agent_name(data.get("agentName", ""), agent_id)
        with chat_db_lock:
            with _chat_db() as conn:
                conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                latest_id = _mark_conversation_read(conn, conversation["id"])

        return jsonify({"ok": True, "agentId": agent_id, "lastReadMessageId": latest_id})
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/chat/messages", methods=["POST"])
def create_chat_message():
    """Append a user message, call the character-linked OpenClaw agent, and store the reply."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400

        agent_id = _normalize_chat_agent_id(data.get("agentId", ""))
        agent_name = _normalize_chat_agent_name(data.get("agentName", ""), agent_id)
        role = (data.get("role") or "user").strip().lower()
        content = (data.get("content") or "").strip()

        if role not in {"user", "agent", "system"}:
            return jsonify({"ok": False, "msg": "role은 user/agent/system 중 하나여야 합니다"}), 400
        if not content:
            return jsonify({"ok": False, "msg": "메시지를 입력해주세요"}), 400
        if len(content) > 8000:
            return jsonify({"ok": False, "msg": "메시지가 너무 깁니다"}), 400

        now_iso = datetime.now().isoformat()
        turn_id = None
        queued_payload = None
        start_queued_after_response = False
        with chat_db_lock:
            with _chat_db() as conn:
                conversation = _get_or_create_conversation(conn, agent_id, agent_name)
                if role == "user":
                    _recover_pending_chat_turns(conn, conversation)
                if role == "user":
                    pending_turn = _get_pending_chat_turn(conn, conversation["id"])
                    existing_queue = _chat_queue_items(conn, conversation["id"])
                    if pending_turn or existing_queue:
                        queue_item = _insert_chat_queue_item(conn, conversation["id"], content, now_iso)
                        conn.execute(
                            "UPDATE conversations SET agent_name = ?, updated_at = ? WHERE id = ?",
                            (agent_name, now_iso, conversation["id"]),
                        )
                        messages, pending = _conversation_messages_with_pending(conn, conversation["id"], 200)
                        queued_payload = {
                            "ok": True,
                            "queued": True,
                            "pending": True,
                            "queueItem": {
                                "id": queue_item["id"],
                                "content": queue_item["content"],
                                "status": queue_item["status"],
                                "createdAt": queue_item["created_at"],
                                "updatedAt": queue_item["updated_at"],
                            },
                            "queue": _chat_queue_items(conn, conversation["id"]),
                            "messages": messages,
                            "conversation": {
                                **_conversation_payload(conversation),
                                "agentName": agent_name,
                            },
                        }
                        start_queued_after_response = not pending
                if queued_payload is not None:
                    message = None
                else:
                    message = _insert_chat_message(conn, conversation["id"], role, content, now_iso)
                if queued_payload is None:
                    if role == "user":
                        turn = _insert_chat_turn(conn, conversation["id"], message["id"], now_iso)
                        turn_id = turn["id"]
                    conn.execute(
                        "UPDATE conversations SET agent_name = ?, updated_at = ? WHERE id = ?",
                        (agent_name, now_iso, conversation["id"]),
                    )

        if queued_payload is not None:
            if start_queued_after_response:
                _start_next_queued_chat_turn(agent_id, agent_name)
            return jsonify(queued_payload)

        if role != "user":
            return jsonify({
                "ok": True,
                "message": _message_payload(message),
                "conversation": {
                    **_conversation_payload(conversation),
                    "agentName": agent_name,
                    "updatedAt": now_iso,
                },
            })

        threading.Thread(
            target=_complete_chat_turn_background,
            args=(turn_id, agent_id, agent_name, content),
            daemon=True,
            name=f"chat-turn-{turn_id}",
        ).start()

        return jsonify({
            "ok": True,
            "message": _message_payload(message),
            "pending": True,
            "messages": [_message_payload(message), _pending_message_payload({"id": turn_id, "created_at": now_iso})],
            "queue": [],
            "conversation": {
                **_conversation_payload(conversation),
                "agentName": agent_name,
                "updatedAt": now_iso,
            },
        })
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/agent-info", methods=["GET"])
def get_agent_info():
    try:
        return jsonify(_agent_openclaw_info(request.args.get("agentId") or "star"))
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/agent-info/thinking", methods=["POST"])
def set_agent_thinking():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400
        agent_id = str(data.get("agentId") or "star").strip() or "star"
        openclaw_agent_id = _resolve_openclaw_agent_id(agent_id)
        result = _set_openclaw_agent_thinking(openclaw_agent_id, data.get("thinking"))
        return jsonify({"ok": True, "openclawAgentId": openclaw_agent_id, **result})
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/agent-info/model", methods=["POST"])
def set_agent_model():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400
        agent_id = str(data.get("agentId") or "star").strip() or "star"
        openclaw_agent_id = _resolve_openclaw_agent_id(agent_id)
        result = _set_openclaw_agent_model(openclaw_agent_id, data.get("model"))
        return jsonify({"ok": True, "openclawAgentId": openclaw_agent_id, **result})
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/agent-info/context", methods=["POST"])
def run_agent_context_action():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400
        agent_id = str(data.get("agentId") or "star").strip() or "star"
        result = _run_openclaw_context_action(agent_id, data.get("action"))
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/agents", methods=["GET"])
def get_agents():
    """Get full agents list (for multi-agent UI), with auto-cleanup on access"""
    agents = load_agents_state()
    now = datetime.now()

    cleaned_agents = []
    keys_data = load_join_keys()

    for a in agents:
        if a.get("isMain"):
            cleaned_agents.append(a)
            continue

        auth_expires_at_str = a.get("authExpiresAt")
        auth_status = a.get("authStatus", "pending")

        # 1) 超时未批准自动 leave
        if auth_status == "pending" and auth_expires_at_str:
            try:
                auth_expires_at = datetime.fromisoformat(auth_expires_at_str)
                if now > auth_expires_at:
                    key = a.get("joinKey")
                    if key:
                        key_item = next((k for k in keys_data.get("keys", []) if k.get("key") == key), None)
                        if key_item:
                            key_item["used"] = False
                            key_item["usedBy"] = None
                            key_item["usedByAgentId"] = None
                            key_item["usedAt"] = None
                    continue
            except Exception:
                pass

        # 2) 超时未推送自动离线（超过5分钟）
        last_push_at_str = a.get("lastPushAt")
        if auth_status == "approved" and last_push_at_str:
            try:
                last_push_at = datetime.fromisoformat(last_push_at_str)
                age = (now - last_push_at).total_seconds()
                if age > 300:  # 5分钟无推送自动离线
                    a["authStatus"] = "offline"
            except Exception:
                pass

        cleaned_agents.append(a)

    save_agents_state(cleaned_agents)
    save_join_keys(keys_data)

    with chat_db_lock:
        with _chat_db() as conn:
            for agent in cleaned_agents:
                if not agent.get("agentId"):
                    continue
                conversation = _get_or_create_conversation(conn, agent.get("agentId"), agent.get("name") or agent.get("agentId"))
                _sync_openclaw_messages_for_conversation(conn, conversation)
            unread_by_agent = _conversation_unread_map(conn)
    response_agents = []
    for agent in cleaned_agents:
        item = dict(agent)
        item["hasUnread"] = bool(unread_by_agent.get(item.get("agentId")))
        response_agents.append(item)

    return jsonify(response_agents)


@app.route("/agent-approve", methods=["POST"])
def agent_approve():
    """Approve an agent (set authStatus to approved)"""
    try:
        data = request.get_json()
        agent_id = (data.get("agentId") or "").strip()
        if not agent_id:
            return jsonify({"ok": False, "msg": "agentId가 없습니다"}), 400

        agents = load_agents_state()
        target = next((a for a in agents if a.get("agentId") == agent_id and not a.get("isMain")), None)
        if not target:
            return jsonify({"ok": False, "msg": "agent를 찾지 못했습니다"}), 404

        target["authStatus"] = "approved"
        target["authApprovedAt"] = datetime.now().isoformat()
        target["authExpiresAt"] = (datetime.now() + timedelta(hours=24)).isoformat()  # 默认授权24h

        save_agents_state(agents)
        return jsonify({"ok": True, "agentId": agent_id, "authStatus": "approved"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/agent-reject", methods=["POST"])
def agent_reject():
    """Reject an agent (set authStatus to rejected and optionally revoke key)"""
    try:
        data = request.get_json()
        agent_id = (data.get("agentId") or "").strip()
        if not agent_id:
            return jsonify({"ok": False, "msg": "agentId가 없습니다"}), 400

        agents = load_agents_state()
        target = next((a for a in agents if a.get("agentId") == agent_id and not a.get("isMain")), None)
        if not target:
            return jsonify({"ok": False, "msg": "agent를 찾지 못했습니다"}), 404

        target["authStatus"] = "rejected"
        target["authRejectedAt"] = datetime.now().isoformat()

        # Optionally free join key back to unused
        join_key = target.get("joinKey")
        keys_data = load_join_keys()
        if join_key:
            key_item = next((k for k in keys_data.get("keys", []) if k.get("key") == join_key), None)
            if key_item:
                key_item["used"] = False
                key_item["usedBy"] = None
                key_item["usedByAgentId"] = None
                key_item["usedAt"] = None

        # Remove from agents list
        agents = [a for a in agents if a.get("agentId") != agent_id or a.get("isMain")]

        save_agents_state(agents)
        save_join_keys(keys_data)
        return jsonify({"ok": True, "agentId": agent_id, "authStatus": "rejected"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/remote-agent/hosts", methods=["GET"])
def remote_agent_hosts():
    """Return previously used remote SSH hosts for the add-agent UI."""
    try:
        hosts = []
        seen = set()
        for agent in load_agents_state():
            host = str(agent.get("remoteHost") or "").strip()
            if not host or host in seen:
                continue
            seen.add(host)
            hosts.append({
                "host": host,
                "sshUser": agent.get("remoteSshUser") or "",
                "sshPort": agent.get("remoteSshPort") or 22,
                "remoteStateFile": agent.get("remoteStateFile") or "",
                "remoteInstallDir": agent.get("remoteInstallDir") or "",
                "lastUsedAt": agent.get("updated_at") or agent.get("lastPushAt") or "",
            })
        hosts.sort(key=lambda item: item.get("lastUsedAt") or "", reverse=True)
        return jsonify({"ok": True, "hosts": hosts})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/remote-agent/install", methods=["POST"])
def install_remote_agent():
    """Install and start office-agent-push.py on a remote OpenClaw host over SSH."""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400
        config = _remote_install_payload(data)
        remote_script = _build_remote_agent_install_script(config)
        cmd = _remote_install_ssh_command(
            config["host"],
            config["sshUser"],
            config["sshPort"],
            config["sshKeyPath"],
            config["sshPassword"],
        )
        result = subprocess.run(
            cmd,
            input=remote_script,
            capture_output=True,
            text=True,
            timeout=45,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            detail = stderr or stdout or "unknown error"
            return jsonify({"ok": False, "msg": f"원격 설치 실패({result.returncode}): {detail[-1200:]}"}), 502
        status = "already-running" if "already-running" in stdout else "started"
        now_iso = datetime.now().isoformat()
        agents = load_agents_state()
        existing = next((a for a in agents if not a.get("isMain") and a.get("name") == config["agentName"]), None)
        metadata_target = existing
        if not metadata_target:
            import random
            import string
            metadata_target = {
                "agentId": "agent_pending_" + str(int(datetime.now().timestamp() * 1000)) + "_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4)),
                "name": config["agentName"],
                "isMain": False,
                "state": "idle",
                "detail": "원격 설치를 시작했습니다",
                "updated_at": now_iso,
                "area": "breakroom",
                "source": "remote-openclaw",
                "joinKey": config["joinKey"],
                "authStatus": "offline",
                "authExpiresAt": None,
                "lastPushAt": None,
            }
            agents.append(metadata_target)
        metadata_target["remoteHost"] = config["host"]
        metadata_target["remoteSshUser"] = config["sshUser"]
        metadata_target["remoteSshPort"] = config["sshPort"]
        metadata_target["remoteStateFile"] = config["remoteStateFile"]
        metadata_target["remoteInstallDir"] = config["remoteInstallDir"]
        metadata_target["openclawAgentId"] = config["openclawAgentId"]
        metadata_target["avatar"] = config["avatar"]
        metadata_target["updated_at"] = now_iso
        save_agents_state(agents)
        return jsonify({
            "ok": True,
            "status": status,
            "host": config["host"],
            "installDir": config["remoteInstallDir"],
            "logFile": f"{config['remoteInstallDir'].rstrip('/')}/office-agent.log",
            "msg": "이미 실행 중입니다" if status == "already-running" else "원격 agent 푸시를 시작했습니다",
        })
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "msg": "SSH 원격 설치 시간이 초과되었습니다"}), 504
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/join-agent", methods=["POST"])
def join_agent():
    """Add a new agent with one-time join key validation and pending auth"""
    try:
        data = request.get_json()
        if not isinstance(data, dict) or not data.get("name"):
            return jsonify({"ok": False, "msg": "이름을 입력해주세요"}), 400

        name = data["name"].strip()
        state = data.get("state", "idle")
        detail = data.get("detail", "")
        join_key = data.get("joinKey", "").strip()
        openclaw_agent_id = (
            data.get("openclawAgentId")
            or data.get("openclaw_agent_id")
            or data.get("openclawAgent")
            or ""
        ).strip()
        avatar = _normalize_guest_avatar(data.get("avatar"))
        remote_host = str(data.get("remoteHost") or "").strip()
        remote_ssh_user = str(data.get("remoteSshUser") or "").strip()
        remote_ssh_port = data.get("remoteSshPort") or None
        remote_state_file = str(data.get("remoteStateFile") or "").strip()
        remote_install_dir = str(data.get("remoteInstallDir") or "").strip()

        # Normalize state early for compatibility
        state = normalize_agent_state(state)

        if not join_key:
            return jsonify({"ok": False, "msg": "접속 키를 입력해주세요"}), 400

        keys_data = load_join_keys()
        key_item = next((k for k in keys_data.get("keys", []) if k.get("key") == join_key), None)
        if not key_item:
            return jsonify({"ok": False, "msg": "접속 키가 유효하지 않습니다"}), 403
        # key 可复用：不再因为 used=true 拒绝

        with join_lock:
            # 在锁内重新读取，避免并发请求都基于同一旧快照通过校验
            keys_data = load_join_keys()
            key_item = next((k for k in keys_data.get("keys", []) if k.get("key") == join_key), None)
            if not key_item:
                return jsonify({"ok": False, "msg": "접속 키가 유효하지 않습니다"}), 403

            # Key-level expiration check
            key_expires_at_str = key_item.get("expiresAt")
            if key_expires_at_str:
                try:
                    key_expires_at = datetime.fromisoformat(key_expires_at_str)
                    if datetime.now() > key_expires_at:
                        return jsonify({"ok": False, "msg": "이 접속 키는 만료되었습니다. 이벤트가 종료되었습니다."}), 403
                except Exception:
                    pass

            agents = load_agents_state()

            # 并发上限：同一个 key “同时在线”最多 3 个。
            # 在线判定：lastPushAt/updated_at 在 5 分钟内；否则视为 offline，不计入并发。
            now = datetime.now()
            existing = next((a for a in agents if a.get("name") == name and not a.get("isMain")), None)

            def _age_seconds(dt_str):
                if not dt_str:
                    return None
                try:
                    dt = datetime.fromisoformat(dt_str)
                    return (now - dt).total_seconds()
                except Exception:
                    return None

            # opportunistic offline marking
            for a in agents:
                if a.get("isMain"):
                    continue
                if a.get("authStatus") != "approved":
                    continue
                age = _age_seconds(a.get("lastPushAt"))
                if age is None:
                    age = _age_seconds(a.get("updated_at"))
                if age is not None and age > 300:
                    a["authStatus"] = "offline"

            if existing:
                existing["state"] = state
                existing["detail"] = detail
                existing["updated_at"] = datetime.now().isoformat()
                existing["area"] = state_to_area(state)
                existing["source"] = "remote-openclaw"
                existing["joinKey"] = join_key
                if openclaw_agent_id:
                    existing["openclawAgentId"] = openclaw_agent_id
                existing["avatar"] = avatar
                if remote_host:
                    existing["remoteHost"] = remote_host
                if remote_ssh_user:
                    existing["remoteSshUser"] = remote_ssh_user
                if remote_ssh_port:
                    existing["remoteSshPort"] = remote_ssh_port
                if remote_state_file:
                    existing["remoteStateFile"] = remote_state_file
                if remote_install_dir:
                    existing["remoteInstallDir"] = remote_install_dir
                existing["authStatus"] = "approved"
                existing["authApprovedAt"] = datetime.now().isoformat()
                existing["authExpiresAt"] = (datetime.now() + timedelta(hours=24)).isoformat()
                existing["lastPushAt"] = datetime.now().isoformat()  # join 视为上线，纳入并发/离线判定
                existing_id = str(existing.get("agentId") or "")
                if existing_id.startswith("agent_pending_"):
                    import random
                    import string
                    existing["agentId"] = "agent_" + str(int(datetime.now().timestamp() * 1000)) + "_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
                agent_id = existing.get("agentId")
            else:
                # Use ms + random suffix to avoid collisions under concurrent joins
                import random
                import string
                agent_id = "agent_" + str(int(datetime.now().timestamp() * 1000)) + "_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
                new_agent = {
                    "agentId": agent_id,
                    "name": name,
                    "isMain": False,
                    "state": state,
                    "detail": detail,
                    "updated_at": datetime.now().isoformat(),
                    "area": state_to_area(state),
                    "source": "remote-openclaw",
                    "joinKey": join_key,
                    "authStatus": "approved",
                    "authApprovedAt": datetime.now().isoformat(),
                    "authExpiresAt": (datetime.now() + timedelta(hours=24)).isoformat(),
                    "lastPushAt": datetime.now().isoformat(),
                    "avatar": avatar
                }
                if openclaw_agent_id:
                    new_agent["openclawAgentId"] = openclaw_agent_id
                if remote_host:
                    new_agent["remoteHost"] = remote_host
                if remote_ssh_user:
                    new_agent["remoteSshUser"] = remote_ssh_user
                if remote_ssh_port:
                    new_agent["remoteSshPort"] = remote_ssh_port
                if remote_state_file:
                    new_agent["remoteStateFile"] = remote_state_file
                if remote_install_dir:
                    new_agent["remoteInstallDir"] = remote_install_dir
                agents.append(new_agent)

            key_item["used"] = True
            key_item["usedBy"] = name
            key_item["usedByAgentId"] = agent_id
            key_item["usedAt"] = datetime.now().isoformat()
            key_item["reusable"] = True

            # 拿到有效 key 直接批准，不再等待主人手动点击
            # （状态已在上面 existing/new 分支写入）
            save_agents_state(agents)
            save_join_keys(keys_data)

        return jsonify({"ok": True, "agentId": agent_id, "authStatus": "approved", "nextStep": "자동 승인되었습니다. 바로 상태 푸시를 시작하세요"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/leave-agent", methods=["POST"])
def leave_agent():
    """Remove an agent and free its one-time join key for reuse (optional)

    Prefer agentId (stable). Name is accepted for backward compatibility.
    """
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400

        agent_id = (data.get("agentId") or "").strip()
        name = (data.get("name") or "").strip()
        if not agent_id and not name:
            return jsonify({"ok": False, "msg": "agentId 또는 이름을 입력해주세요"}), 400

        agents = load_agents_state()

        target = None
        if agent_id:
            target = next((a for a in agents if a.get("agentId") == agent_id and not a.get("isMain")), None)
        if (not target) and name:
            # fallback: remove by name only if agentId not provided
            target = next((a for a in agents if a.get("name") == name and not a.get("isMain")), None)

        if not target:
            return jsonify({"ok": False, "msg": "나갈 agent를 찾지 못했습니다"}), 404

        join_key = target.get("joinKey")
        new_agents = [a for a in agents if a.get("isMain") or a.get("agentId") != target.get("agentId")]

        # Optional: free key back to unused after leave
        keys_data = load_join_keys()
        if join_key:
            key_item = next((k for k in keys_data.get("keys", []) if k.get("key") == join_key), None)
            if key_item:
                key_item["used"] = False
                key_item["usedBy"] = None
                key_item["usedByAgentId"] = None
                key_item["usedAt"] = None

        save_agents_state(new_agents)
        save_join_keys(keys_data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/status", methods=["GET"])
def get_status():
    """Get current main state (backward compatibility). Optionally include officeName from IDENTITY.md."""
    state = load_state()
    try:
        main_agent = next((a for a in load_agents_state() if a.get("isMain")), None)
        state.setdefault("agentId", (main_agent or {}).get("agentId") or "star")
        state.setdefault("agentName", (main_agent or {}).get("name") or "Star")
    except Exception:
        state.setdefault("agentId", "star")
        state.setdefault("agentName", "Star")
    office_name = get_office_name_from_identity()
    if office_name:
        state["officeName"] = office_name
    return jsonify(state)


@app.route("/agent-push", methods=["POST"])
def agent_push():
    """Remote openclaw actively pushes status to office.

    Required fields:
    - agentId
    - joinKey
    - state
    Optional:
    - detail
    - name
    """
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"ok": False, "msg": "invalid json"}), 400

        agent_id = (data.get("agentId") or "").strip()
        join_key = (data.get("joinKey") or "").strip()
        state = (data.get("state") or "").strip()
        detail = (data.get("detail") or "").strip()
        name = (data.get("name") or "").strip()
        openclaw_agent_id = (
            data.get("openclawAgentId")
            or data.get("openclaw_agent_id")
            or data.get("openclawAgent")
            or ""
        ).strip()
        avatar = _normalize_guest_avatar(data.get("avatar")) if data.get("avatar") else ""

        if not agent_id or not join_key or not state:
            return jsonify({"ok": False, "msg": "agentId/joinKey/state가 없습니다"}), 400

        state = normalize_agent_state(state)

        keys_data = load_join_keys()
        key_item = next((k for k in keys_data.get("keys", []) if k.get("key") == join_key), None)
        if not key_item:
            return jsonify({"ok": False, "msg": "joinKey가 유효하지 않습니다"}), 403

        # Key-level expiration check
        key_expires_at_str = key_item.get("expiresAt")
        if key_expires_at_str:
            try:
                key_expires_at = datetime.fromisoformat(key_expires_at_str)
                if datetime.now() > key_expires_at:
                    return jsonify({"ok": False, "msg": "이 접속 키는 만료되었습니다. 이벤트가 종료되었습니다."}), 403
            except Exception:
                pass

        agents = load_agents_state()
        if agent_id == "star":
            now_iso = datetime.now().isoformat()
            target = next((a for a in agents if a.get("agentId") == "star" and a.get("isMain")), None)
            if not target:
                target = dict(DEFAULT_AGENTS[0])
                agents.insert(0, target)

            target["name"] = "Star"
            target["isMain"] = True
            target["state"] = state
            target["detail"] = detail
            target["updated_at"] = now_iso
            target["area"] = state_to_area(state)
            target["source"] = "remote-openclaw"
            target["joinKey"] = join_key
            target["openclawAgentId"] = openclaw_agent_id or _resolve_openclaw_agent_id("star")
            target["authStatus"] = "approved"
            target["authApprovedAt"] = target.get("authApprovedAt") or now_iso
            target["authExpiresAt"] = None
            target["lastPushAt"] = now_iso

            save_state({
                "state": state,
                "detail": detail,
                "progress": 0,
                "updated_at": now_iso,
            })

            key_item["used"] = True
            key_item["usedBy"] = "Star"
            key_item["usedByAgentId"] = "star"
            key_item["usedAt"] = now_iso
            key_item["reusable"] = True

            save_agents_state(agents)
            save_join_keys(keys_data)
            return jsonify({"ok": True, "agentId": "star", "area": target.get("area")})

        target = next((a for a in agents if a.get("agentId") == agent_id and not a.get("isMain")), None)
        if not target:
            return jsonify({"ok": False, "msg": "agent가 등록되지 않았습니다. 먼저 join 해주세요"}), 404

        # Auth check: only approved agents can push.
        # Note: "offline" is a presence state (stale), not a revoked authorization.
        # Allow offline agents to resume pushing and auto-promote them back to approved.
        auth_status = target.get("authStatus", "pending")
        if auth_status not in {"approved", "offline"}:
            return jsonify({"ok": False, "msg": "agent가 아직 승인되지 않았습니다. 관리자 승인을 기다려주세요"}), 403
        if auth_status == "offline":
            target["authStatus"] = "approved"
            target["authApprovedAt"] = datetime.now().isoformat()
            target["authExpiresAt"] = (datetime.now() + timedelta(hours=24)).isoformat()

        if target.get("joinKey") != join_key:
            return jsonify({"ok": False, "msg": "joinKey가 일치하지 않습니다"}), 403

        target["state"] = state
        target["detail"] = detail
        if name:
            target["name"] = name
        target["updated_at"] = datetime.now().isoformat()
        target["area"] = state_to_area(state)
        target["source"] = "remote-openclaw"
        if openclaw_agent_id:
            target["openclawAgentId"] = openclaw_agent_id
        if avatar:
            target["avatar"] = avatar
        target["lastPushAt"] = datetime.now().isoformat()

        save_agents_state(agents)
        return jsonify({"ok": True, "agentId": agent_id, "area": target.get("area")})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """Health check"""
    return jsonify({
        "status": "ok",
        "service": "clawffice",
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/yesterday-memo", methods=["GET"])
def get_yesterday_memo():
    """获取昨日小日记"""
    try:
        # 先尝试找昨天的文件
        yesterday_str = get_yesterday_date_str()
        yesterday_file = os.path.join(MEMORY_DIR, f"{yesterday_str}.md")
        
        target_file = None
        target_date = yesterday_str
        
        if os.path.exists(yesterday_file):
            target_file = yesterday_file
        else:
            # 如果昨天没有，找最近的一天
            if os.path.exists(MEMORY_DIR):
                files = [f for f in os.listdir(MEMORY_DIR) if f.endswith(".md") and re.match(r"\d{4}-\d{2}-\d{2}\.md", f)]
                if files:
                    files.sort(reverse=True)
                    # 跳过今天的（如果存在）
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    for f in files:
                        if f != f"{today_str}.md":
                            target_file = os.path.join(MEMORY_DIR, f)
                            target_date = f.replace(".md", "")
                            break
        
        if target_file and os.path.exists(target_file):
            memo_content = extract_memo_from_file(target_file)
            return jsonify({
                "success": True,
                "date": target_date,
                "memo": memo_content
            })
        else:
            return jsonify({
                "success": False,
                "msg": "어제의 메모를 찾지 못했습니다"
            })
    except Exception as e:
        return jsonify({
            "success": False,
            "msg": str(e)
        }), 500


@app.route("/set_state", methods=["POST"])
def set_state_endpoint():
    """Set state via POST (for UI control panel)"""
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"status": "error", "msg": "invalid json"}), 400
        state = load_state()
        if "state" in data:
            s = data["state"]
            if s in VALID_AGENT_STATES:
                state["state"] = s
        if "detail" in data:
            state["detail"] = data["detail"]
        state["updated_at"] = datetime.now().isoformat()
        save_state(state)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


@app.route("/assets/template.zip", methods=["GET"])
def assets_template_download():
    if not os.path.exists(ASSET_TEMPLATE_ZIP):
        return jsonify({"ok": False, "msg": "템플릿 패키지가 없습니다. 먼저 생성해주세요"}), 404
    return send_from_directory(ROOT_DIR, "assets-replace-template.zip", as_attachment=True)


@app.route("/assets/list", methods=["GET"])
def assets_list():
    items = []
    for p in FRONTEND_PATH.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(FRONTEND_PATH).as_posix()
        if rel.startswith("fonts/"):
            continue
        if p.suffix.lower() not in ASSET_ALLOWED_EXTS:
            continue
        st = p.stat()
        width = None
        height = None
        if Image is not None:
            try:
                with Image.open(p) as im:
                    width, height = im.size
            except Exception:
                pass
        items.append({
            "path": rel,
            "size": st.st_size,
            "ext": p.suffix.lower(),
            "width": width,
            "height": height,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    items.sort(key=lambda x: x["path"])
    return jsonify({"ok": True, "count": len(items), "items": items})


def _bg_generate_worker(task_id: str, custom_prompt: str, speed_mode: str):
    """Background worker for RPG background generation."""
    try:
        target = FRONTEND_PATH / "office_bg_small.webp"

        # 覆盖前保留最近一次备份
        bak = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, bak)

        _generate_rpg_background_to_webp(
            str(target),
            width=1280,
            height=720,
            custom_prompt=custom_prompt,
            speed_mode=speed_mode,
        )

        # 每次生成都归档一份历史底图（可回溯风格演化）
        os.makedirs(BG_HISTORY_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        hist_file = os.path.join(BG_HISTORY_DIR, f"office_bg_small-{ts}.webp")
        shutil.copy2(target, hist_file)

        st = target.stat()
        with _bg_tasks_lock:
            _bg_tasks[task_id] = {
                "status": "done",
                "result": {
                    "ok": True,
                    "path": "office_bg_small.webp",
                    "size": st.st_size,
                    "history": os.path.relpath(hist_file, ROOT_DIR),
                    "speed_mode": speed_mode,
                    "msg": "RPG 방 배경을 생성해 교체했습니다. 자동으로 보관했습니다.",
                },
            }
    except Exception as e:
        msg = str(e)
        error_result = {"ok": False, "msg": msg}
        if msg == "MISSING_API_KEY":
            error_result["code"] = "MISSING_API_KEY"
            error_result["msg"] = "Missing GEMINI_API_KEY or GOOGLE_API_KEY"
        elif msg == "API_KEY_REVOKED_OR_LEAKED":
            error_result["code"] = "API_KEY_REVOKED_OR_LEAKED"
            error_result["msg"] = "API key is revoked or flagged as leaked. Please rotate to a new key."
        elif msg.startswith("MODEL_NOT_AVAILABLE"):
            error_result["code"] = "MODEL_NOT_AVAILABLE"
            error_result["msg"] = "Configured model is not available for this API key/channel."
            if "::" in msg:
                error_result["detail"] = msg.split("::", 1)[1]
        with _bg_tasks_lock:
            _bg_tasks[task_id] = {"status": "error", "result": error_result}


@app.route("/assets/generate-rpg-background", methods=["POST"])
def assets_generate_rpg_background():
    """Start async RPG background generation. Returns a task_id for polling."""
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        req = request.get_json(silent=True) or {}
        custom_prompt = (req.get("prompt") or "").strip() if isinstance(req, dict) else ""
        speed_mode = (req.get("speed_mode") or "quality").strip().lower() if isinstance(req, dict) else "quality"
        if speed_mode not in {"fast", "quality"}:
            speed_mode = "fast"

        target = FRONTEND_PATH / "office_bg_small.webp"
        if not target.exists():
            return jsonify({"ok": False, "msg": "office_bg_small.webp가 없습니다"}), 404

        # Pre-flight checks that can fail fast (before spawning thread)
        runtime_cfg = load_runtime_config()
        api_key = (runtime_cfg.get("gemini_api_key") or "").strip()
        if not api_key:
            return jsonify({"ok": False, "code": "MISSING_API_KEY", "msg": "Missing GEMINI_API_KEY or GOOGLE_API_KEY"}), 400
        if not (os.path.exists(GEMINI_PYTHON) and os.path.exists(GEMINI_SCRIPT)):
            return jsonify({"ok": False, "msg": "이미지 생성 환경이 없습니다: gemini-image-generate가 설치되지 않았습니다"}), 500

        # Check if another generation is already running
        with _bg_tasks_lock:
            for tid, task in _bg_tasks.items():
                if task.get("status") == "pending":
                    return jsonify({"ok": True, "async": True, "task_id": tid, "msg": "이미지 생성 작업이 이미 진행 중입니다. 완료될 때까지 기다려주세요"}), 200

        # Create async task
        import string as _string
        task_id = "gen_" + str(int(datetime.now().timestamp() * 1000)) + "_" + "".join(random.choices(_string.ascii_lowercase + _string.digits, k=4))
        with _bg_tasks_lock:
            _bg_tasks[task_id] = {"status": "pending", "created_at": datetime.now().isoformat()}

        t = threading.Thread(target=_bg_generate_worker, args=(task_id, custom_prompt, speed_mode), daemon=True)
        t.start()

        return jsonify({"ok": True, "async": True, "task_id": task_id, "msg": "이미지 생성 작업을 시작했습니다. task_id로 결과를 확인해주세요"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/generate-rpg-background/poll", methods=["GET"])
def assets_generate_rpg_background_poll():
    """Poll async generation task status."""
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    task_id = (request.args.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"ok": False, "msg": "task_id가 없습니다"}), 400
    with _bg_tasks_lock:
        task = _bg_tasks.get(task_id)
    if not task:
        return jsonify({"ok": False, "msg": "작업을 찾지 못했습니다"}), 404
    status = task.get("status", "pending")
    if status == "pending":
        return jsonify({"ok": True, "status": "pending", "msg": "이미지 생성 중입니다..."})
    elif status == "done":
        # Clean up task after delivering result
        with _bg_tasks_lock:
            _bg_tasks.pop(task_id, None)
        return jsonify({"ok": True, "status": "done", **task.get("result", {})})
    else:
        with _bg_tasks_lock:
            _bg_tasks.pop(task_id, None)
        result = task.get("result", {})
        code = 400 if result.get("code") else 500
        return jsonify({"ok": False, "status": "error", **result}), code


@app.route("/assets/restore-reference-background", methods=["POST"])
def assets_restore_reference_background():
    """Restore office_bg_small.webp from fixed reference image."""
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        target = FRONTEND_PATH / "office_bg_small.webp"
        if not target.exists():
            return jsonify({"ok": False, "msg": "office_bg_small.webp가 없습니다"}), 404
        if not os.path.exists(ROOM_REFERENCE_IMAGE):
            return jsonify({"ok": False, "msg": "참조 이미지가 없습니다"}), 404

        # 备份当前底图
        bak = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, bak)

        # 快速路径：若参考图已是 1280x720 的 webp，直接拷贝（秒级）
        ref_ext = os.path.splitext(ROOM_REFERENCE_IMAGE)[1].lower()
        fast_copied = False
        if ref_ext == '.webp':
            try:
                with Image.open(ROOM_REFERENCE_IMAGE) as rim:
                    if rim.size == (1280, 720):
                        shutil.copy2(ROOM_REFERENCE_IMAGE, target)
                        fast_copied = True
            except Exception:
                fast_copied = False

        # 慢路径：仅在必要时重编码
        if not fast_copied:
            if Image is None:
                return jsonify({"ok": False, "msg": "Pillow를 사용할 수 없습니다"}), 500
            with Image.open(ROOM_REFERENCE_IMAGE) as im:
                im = im.convert("RGBA").resize((1280, 720), Image.Resampling.LANCZOS)
                im.save(target, "WEBP", quality=92, method=6)

        st = target.stat()
        return jsonify({
            "ok": True,
            "path": "office_bg_small.webp",
            "size": st.st_size,
            "msg": "초기 배경으로 복원했습니다",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/restore-last-generated-background", methods=["POST"])
def assets_restore_last_generated_background():
    """Restore office_bg_small.webp from latest bg-history snapshot."""
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        target = FRONTEND_PATH / "office_bg_small.webp"
        if not target.exists():
            return jsonify({"ok": False, "msg": "office_bg_small.webp가 없습니다"}), 404

        if not os.path.isdir(BG_HISTORY_DIR):
            return jsonify({"ok": False, "msg": "저장된 이전 배경이 없습니다"}), 404

        files = [
            os.path.join(BG_HISTORY_DIR, x)
            for x in os.listdir(BG_HISTORY_DIR)
            if x.startswith("office_bg_small-") and x.endswith(".webp")
        ]
        if not files:
            return jsonify({"ok": False, "msg": "저장된 이전 배경이 없습니다"}), 404

        latest = max(files, key=lambda p: os.path.getmtime(p))

        bak = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, bak)
        shutil.copy2(latest, target)

        st = target.stat()
        return jsonify({
            "ok": True,
            "path": "office_bg_small.webp",
            "size": st.st_size,
            "from": os.path.relpath(latest, ROOT_DIR),
            "msg": "가장 최근에 생성한 배경으로 되돌렸습니다",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/home-favorites/list", methods=["GET"])
def assets_home_favorites_list():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = _load_home_favorites_index()
        items = data.get("items") or []
        out = []
        for it in items:
            rel = (it.get("path") or "").strip()
            if not rel:
                continue
            abs_path = os.path.join(ROOT_DIR, rel)
            if not os.path.exists(abs_path):
                continue
            fn = os.path.basename(rel)
            out.append({
                "id": it.get("id"),
                "path": rel,
                "url": f"/assets/home-favorites/file/{fn}",
                "thumb_url": f"/assets/home-favorites/file/{fn}",
                "created_at": it.get("created_at") or "",
            })
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return jsonify({"ok": True, "items": out})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/home-favorites/file/<path:filename>", methods=["GET"])
def assets_home_favorites_file(filename):
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    return send_from_directory(HOME_FAVORITES_DIR, filename)


@app.route("/assets/home-favorites/save-current", methods=["POST"])
def assets_home_favorites_save_current():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        src = FRONTEND_PATH / "office_bg_small.webp"
        if not src.exists():
            return jsonify({"ok": False, "msg": "office_bg_small.webp가 없습니다"}), 404

        _ensure_home_favorites_index()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        item_id = f"home-{ts}"
        fn = f"{item_id}.webp"
        dst = os.path.join(HOME_FAVORITES_DIR, fn)
        shutil.copy2(str(src), dst)

        idx = _load_home_favorites_index()
        items = idx.get("items") or []
        items.insert(0, {
            "id": item_id,
            "path": os.path.relpath(dst, ROOT_DIR),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })

        # 控制收藏数量上限，清理最旧项
        if len(items) > HOME_FAVORITES_MAX:
            extra = items[HOME_FAVORITES_MAX:]
            items = items[:HOME_FAVORITES_MAX]
            for it in extra:
                try:
                    p = os.path.join(ROOT_DIR, it.get("path") or "")
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

        idx["items"] = items
        _save_home_favorites_index(idx)
        return jsonify({"ok": True, "id": item_id, "path": os.path.relpath(dst, ROOT_DIR), "msg": "현재 맵을 저장했습니다"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/home-favorites/delete", methods=["POST"])
def assets_home_favorites_delete():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = request.get_json(silent=True) or {}
        item_id = (data.get("id") or "").strip()
        if not item_id:
            return jsonify({"ok": False, "msg": "id가 없습니다"}), 400

        idx = _load_home_favorites_index()
        items = idx.get("items") or []
        hit = next((x for x in items if (x.get("id") or "") == item_id), None)
        if not hit:
            return jsonify({"ok": False, "msg": "저장 항목을 찾지 못했습니다"}), 404

        rel = hit.get("path") or ""
        abs_path = os.path.join(ROOT_DIR, rel)
        if os.path.exists(abs_path):
            try:
                os.remove(abs_path)
            except Exception:
                pass

        idx["items"] = [x for x in items if (x.get("id") or "") != item_id]
        _save_home_favorites_index(idx)
        return jsonify({"ok": True, "id": item_id, "msg": "저장 항목을 삭제했습니다"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/home-favorites/apply", methods=["POST"])
def assets_home_favorites_apply():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = request.get_json(silent=True) or {}
        item_id = (data.get("id") or "").strip()
        if not item_id:
            return jsonify({"ok": False, "msg": "id가 없습니다"}), 400

        idx = _load_home_favorites_index()
        items = idx.get("items") or []
        hit = next((x for x in items if (x.get("id") or "") == item_id), None)
        if not hit:
            return jsonify({"ok": False, "msg": "저장 항목을 찾지 못했습니다"}), 404

        src = os.path.join(ROOT_DIR, hit.get("path") or "")
        if not os.path.exists(src):
            return jsonify({"ok": False, "msg": "저장 파일을 찾지 못했습니다"}), 404

        target = FRONTEND_PATH / "office_bg_small.webp"
        if not target.exists():
            return jsonify({"ok": False, "msg": "office_bg_small.webp가 없습니다"}), 404

        bak = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(str(target), str(bak))
        shutil.copy2(src, str(target))

        st = target.stat()
        return jsonify({"ok": True, "path": "office_bg_small.webp", "size": st.st_size, "from": hit.get("path"), "msg": "저장한 맵을 적용했습니다"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/auth", methods=["POST"])
def assets_auth():
    try:
        data = request.get_json(silent=True) or {}
        pwd = (data.get("password") or "").strip()
        if pwd and pwd == ASSET_DRAWER_PASS_DEFAULT:
            session["asset_editor_authed"] = True
            return jsonify({"ok": True, "msg": "인증되었습니다"})
        return jsonify({"ok": False, "msg": "인증코드가 올바르지 않습니다"}), 401
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/auth/status", methods=["GET"])
def assets_auth_status():
    return jsonify({
        "ok": True,
        "authed": _is_asset_editor_authed(),
        "drawer_default_pass": ASSET_DRAWER_PASS_DEFAULT == "1234",
    })


@app.route("/assets/positions", methods=["GET"])
def assets_positions_get():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        return jsonify({"ok": True, "items": load_asset_positions()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/positions", methods=["POST"])
def assets_positions_set():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = request.get_json(silent=True) or {}
        key = (data.get("key") or "").strip()
        x = data.get("x")
        y = data.get("y")
        scale = data.get("scale")
        if not key:
            return jsonify({"ok": False, "msg": "key가 없습니다"}), 400
        if x is None or y is None:
            return jsonify({"ok": False, "msg": "x/y가 없습니다"}), 400
        x = float(x)
        y = float(y)
        if scale is None:
            scale = 1.0
        scale = float(scale)

        all_pos = load_asset_positions()
        all_pos[key] = {"x": x, "y": y, "scale": scale, "updated_at": datetime.now().isoformat()}
        save_asset_positions(all_pos)
        return jsonify({"ok": True, "key": key, "x": x, "y": y, "scale": scale})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/defaults", methods=["GET"])
def assets_defaults_get():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        return jsonify({"ok": True, "items": load_asset_defaults()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/defaults", methods=["POST"])
def assets_defaults_set():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = request.get_json(silent=True) or {}
        key = (data.get("key") or "").strip()
        x = data.get("x")
        y = data.get("y")
        scale = data.get("scale")
        if not key:
            return jsonify({"ok": False, "msg": "key가 없습니다"}), 400
        if x is None or y is None:
            return jsonify({"ok": False, "msg": "x/y가 없습니다"}), 400
        x = float(x)
        y = float(y)
        if scale is None:
            scale = 1.0
        scale = float(scale)

        all_defaults = load_asset_defaults()
        all_defaults[key] = {"x": x, "y": y, "scale": scale, "updated_at": datetime.now().isoformat()}
        save_asset_defaults(all_defaults)
        return jsonify({"ok": True, "key": key, "x": x, "y": y, "scale": scale})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/config/gemini", methods=["GET"])
def gemini_config_get():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        cfg = load_runtime_config()
        key = (cfg.get("gemini_api_key") or "").strip()
        masked = ("*" * max(0, len(key) - 4)) + key[-4:] if key else ""
        return jsonify({
            "ok": True,
            "has_api_key": bool(key),
            "api_key_masked": masked,
            "gemini_model": _normalize_user_model(cfg.get("gemini_model") or "nanobanana-pro"),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/config/gemini", methods=["POST"])
def gemini_config_set():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = request.get_json(silent=True) or {}
        api_key = (data.get("api_key") or "").strip()
        model = _normalize_user_model((data.get("model") or "").strip() or "nanobanana-pro")
        payload = {"gemini_model": model}
        if api_key:
            payload["gemini_api_key"] = api_key
        save_runtime_config(payload)
        return jsonify({"ok": True, "msg": "Gemini 설정을 저장했습니다"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/restore-default", methods=["POST"])
def assets_restore_default():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = request.get_json(silent=True) or {}
        rel_path = (data.get("path") or "").strip().lstrip("/")
        if not rel_path:
            return jsonify({"ok": False, "msg": "path가 없습니다"}), 400

        target = (FRONTEND_PATH / rel_path).resolve()
        try:
            target.relative_to(FRONTEND_PATH.resolve())
        except Exception:
            return jsonify({"ok": False, "msg": "허용되지 않는 path입니다"}), 400

        if not target.exists():
            return jsonify({"ok": False, "msg": "대상 파일이 없습니다"}), 404

        root, ext = os.path.splitext(str(target))
        default_path = root + ext + ".default"
        if not os.path.exists(default_path):
            return jsonify({"ok": False, "msg": "기본 에셋 스냅샷을 찾지 못했습니다"}), 404

        # 回滚前保留上一版
        bak = str(target) + ".bak"
        if os.path.exists(str(target)):
            shutil.copy2(str(target), bak)

        shutil.copy2(default_path, str(target))
        st = os.stat(str(target))
        return jsonify({"ok": True, "path": rel_path, "size": st.st_size, "msg": "기본 에셋으로 복원했습니다"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/restore-prev", methods=["POST"])
def assets_restore_prev():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        data = request.get_json(silent=True) or {}
        rel_path = (data.get("path") or "").strip().lstrip("/")
        if not rel_path:
            return jsonify({"ok": False, "msg": "path가 없습니다"}), 400

        target = (FRONTEND_PATH / rel_path).resolve()
        try:
            target.relative_to(FRONTEND_PATH.resolve())
        except Exception:
            return jsonify({"ok": False, "msg": "허용되지 않는 path입니다"}), 400

        bak = str(target) + ".bak"
        if not os.path.exists(bak):
            return jsonify({"ok": False, "msg": "이전 버전 백업을 찾지 못했습니다"}), 404

        shutil.copy2(str(target), bak + ".tmp") if os.path.exists(str(target)) else None
        shutil.copy2(bak, str(target))
        st = os.stat(str(target))
        return jsonify({"ok": True, "path": rel_path, "size": st.st_size, "msg": "이전 버전으로 되돌렸습니다"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/assets/upload", methods=["POST"])
def assets_upload():
    guard = _require_asset_editor_auth()
    if guard:
        return guard
    try:
        rel_path = (request.form.get("path") or "").strip().lstrip("/")
        backup = (request.form.get("backup") or "1").strip() != "0"
        f = request.files.get("file")

        if not rel_path or f is None:
            return jsonify({"ok": False, "msg": "path 또는 file이 없습니다"}), 400

        target = (FRONTEND_PATH / rel_path).resolve()
        try:
            target.relative_to(FRONTEND_PATH.resolve())
        except Exception:
            return jsonify({"ok": False, "msg": "허용되지 않는 path입니다"}), 400

        if target.suffix.lower() not in ASSET_ALLOWED_EXTS:
            return jsonify({"ok": False, "msg": "이미지/아트 리소스 파일만 업로드할 수 있습니다"}), 400

        if not target.exists():
            return jsonify({"ok": False, "msg": "대상 파일이 없습니다. 먼저 /assets/list에서 path를 선택해주세요"}), 404

        target.parent.mkdir(parents=True, exist_ok=True)

        # 首次上传前固化默认资产快照，供“重置为默认资产”使用
        default_snap = Path(str(target) + ".default")
        if not default_snap.exists():
            try:
                shutil.copy2(target, default_snap)
            except Exception:
                pass

        if backup:
            bak = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, bak)

        auto_sheet = (request.form.get("auto_spritesheet") or "0").strip() == "1"
        ext_name = (f.filename or "").lower()

        if auto_sheet and target.suffix.lower() in {".webp", ".png"}:
            with tempfile.NamedTemporaryFile(suffix=os.path.splitext(ext_name)[1] or ".gif", delete=False) as tf:
                src_path = tf.name
                f.save(src_path)
            try:
                in_w, in_h = _probe_animated_frame_size(src_path)
                frame_w = int(request.form.get("frame_w") or (in_w or 64))
                frame_h = int(request.form.get("frame_h") or (in_h or 64))

                # 如果是静态图上传到精灵表目标，按网格切片而不是整图覆盖
                if not (ext_name.endswith(".gif") or ext_name.endswith(".webp")) and Image is not None:
                    try:
                        with Image.open(src_path) as sim:
                            sim = sim.convert("RGBA")
                            sw, sh = sim.size
                            if frame_w <= 0 or frame_h <= 0:
                                frame_w, frame_h = sw, sh
                            cols = max(1, sw // frame_w)
                            rows = max(1, sh // frame_h)
                            sheet_w = cols * frame_w
                            sheet_h = rows * frame_h
                            if sheet_w <= 0 or sheet_h <= 0:
                                raise RuntimeError("정적 이미지 크기가 프레임 규격과 일치하지 않습니다")

                            cropped = sim.crop((0, 0, sheet_w, sheet_h))
                            # 目标是 webp 仍按无损保存，避免像素损失
                            if target.suffix.lower() == ".webp":
                                cropped.save(str(target), "WEBP", lossless=True, quality=100, method=6)
                            else:
                                cropped.save(str(target), "PNG")

                            st = target.stat()
                            return jsonify({
                                "ok": True,
                                "path": rel_path,
                                "size": st.st_size,
                                "backup": backup,
                                "converted": {
                                    "from": ext_name.split(".")[-1] if "." in ext_name else "image",
                                    "to": "webp_spritesheet" if target.suffix.lower() == ".webp" else "png_spritesheet",
                                    "frame_w": frame_w,
                                    "frame_h": frame_h,
                                    "columns": cols,
                                    "rows": rows,
                                    "frames": cols * rows,
                                    "preserve_original": False,
                                    "pixel_art": True,
                                }
                            })
                    finally:
                        pass

                # 默认：优先保留输入帧尺寸；若前端传了强制值则按前端。
                preserve_original_val = request.form.get("preserve_original")
                if preserve_original_val is None:
                    preserve_original = True
                else:
                    preserve_original = preserve_original_val.strip() == "1"

                pixel_art = (request.form.get("pixel_art") or "1").strip() == "1"
                req_cols = int(request.form.get("cols") or 0)
                req_rows = int(request.form.get("rows") or 0)
                sheet_path, cols, rows, frames, out_fw, out_fh = _animated_to_spritesheet(
                    src_path,
                    frame_w,
                    frame_h,
                    out_ext=target.suffix.lower(),
                    preserve_original=preserve_original,
                    pixel_art=pixel_art,
                    cols=(req_cols if req_cols > 0 else None),
                    rows=(req_rows if req_rows > 0 else None),
                )
                shutil.move(sheet_path, str(target))
                st = target.stat()
                from_type = "gif" if ext_name.endswith(".gif") else "webp"
                to_type = "webp_spritesheet" if target.suffix.lower() == ".webp" else "png_spritesheet"
                return jsonify({
                    "ok": True,
                    "path": rel_path,
                    "size": st.st_size,
                    "backup": backup,
                    "converted": {
                        "from": from_type,
                        "to": to_type,
                        "frame_w": out_fw,
                        "frame_h": out_fh,
                        "columns": cols,
                        "rows": rows,
                        "frames": frames,
                        "preserve_original": preserve_original,
                        "pixel_art": pixel_art,
                    }
                })
            finally:
                try:
                    os.remove(src_path)
                except Exception:
                    pass

        f.save(str(target))
        st = target.stat()
        return jsonify({"ok": True, "path": rel_path, "size": st.st_size, "backup": backup})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    raw_port = os.environ.get("CLAWFFICE_BACKEND_PORT") or os.environ.get("STAR_BACKEND_PORT", "19000")
    try:
        backend_port = int(raw_port)
    except ValueError:
        backend_port = 19000
    if backend_port <= 0:
        backend_port = 19000

    print("=" * 50)
    print("clawffice - Backend State Service")
    print("=" * 50)
    print(f"State file: {STATE_FILE}")
    print(f"Listening on: http://0.0.0.0:{backend_port}")
    if backend_port != 19000:
        print(f"(Port override: set CLAWFFICE_BACKEND_PORT to change; current: {raw_port})")
    else:
        print("(Set CLAWFFICE_BACKEND_PORT to use a different port, e.g. 3009)")
    mode = "production" if is_production_mode() else "development"
    print(f"Mode: {mode}")
    if is_production_mode():
        print("Security hardening: ENABLED (strict checks)")
    else:
        weak_flags = []
        if not is_strong_secret(str(app.secret_key)):
            weak_flags.append("weak FLASK_SECRET_KEY/CLAWFFICE_SECRET")
        if not is_strong_drawer_pass(ASSET_DRAWER_PASS_DEFAULT):
            weak_flags.append("weak ASSET_DRAWER_PASS")
        if weak_flags:
            print("Security hardening: WARNING (dev mode) -> " + ", ".join(weak_flags))
        else:
            print("Security hardening: OK")
    print("=" * 50)

    app.run(host="0.0.0.0", port=backend_port, debug=False)
