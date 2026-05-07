"""
RawrBot v2.0.0 – Production Discord Support Bot
================================================
Features:
  • Long‑term memory (Redis + PostgreSQL)
  • Sentiment‑based tonal adaptation + local LLM (Ollama)
  • Full ticket lifecycle + intelligent auto‑close
  • Web chat broadcast via SSE
  • GitHub persistent storage for whitelist/blacklist/logs
  • Advanced rate limiting (distributed / local fallback)
  • Human‑like, friendly replies – never robotic
  • Anti‑duplicate ticket creation, interaction timeout safety

Environment:
  BOT_TOKEN (secret), GUILD_ID, OWNER_ID, STAFF_ROLE_ID, MANAGER_ROLE_ID,
  MODERATOR_ROLE_ID, TICKET_CATEGORY_ID, REVIEW_CHANNEL_ID, STORAGE_TOKEN (secret),
  REDIS_URL, DATABASE_URL, OLLAMA_URL (optional), SENTRY_DSN (optional)
"""

import os, sys, asyncio, json, time, base64, hashlib, logging, re
from typing import Optional, Set, Dict, Any, List, Tuple, Callable, Union
from datetime import datetime, timezone, timedelta
from collections import deque
from enum import Enum

import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web, ClientSession

# Try to import optional heavy dependencies gracefully
try:
    import structlog
    STRUCTLOG_AVAILABLE = True
except ImportError:
    STRUCTLOG_AVAILABLE = False

try:
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    SENTRY_AVAILABLE = True
except ImportError:
    SENTRY_AVAILABLE = False

try:
    from textblob import TextBlob
    SENTIMENT_AVAILABLE = True
except ImportError:
    SENTIMENT_AVAILABLE = False

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

try:
    import asyncpg
except ImportError:
    asyncpg = None


# ──────────────────────────────────────────────────────────────────────────────
# SAFE ENVIRONMENT PARSING
# ──────────────────────────────────────────────────────────────────────────────
def _env_int(key, default=0):
    val = os.getenv(key, '').strip()
    if val == '':
        return default
    try:
        return int(val)
    except ValueError:
        return default

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
TOKEN = os.getenv('BOT_TOKEN', '').strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

GUILD_ID = _env_int('GUILD_ID')
if not GUILD_ID:
    raise RuntimeError("GUILD_ID not set")

PORT                = _env_int('PORT', 8080)
TICKET_CATEGORY_ID  = _env_int('TICKET_CATEGORY_ID', 0)
REVIEW_CHANNEL_ID   = _env_int('REVIEW_CHANNEL_ID', 0)
OWNER_ID            = _env_int('OWNER_ID', 0)
STAFF_ROLE_ID       = _env_int('STAFF_ROLE_ID', 0)
MANAGER_ROLE_ID     = _env_int('MANAGER_ROLE_ID', 0)
MODERATOR_ROLE_ID   = _env_int('MODERATOR_ROLE_ID', 0)

STORAGE_TOKEN   = os.getenv('STORAGE_TOKEN', '').strip()
STORAGE_REPO    = os.getenv('STORAGE_REPO', 'imcomingforyou6959-gif/Storage')
STORAGE_BRANCH  = os.getenv('STORAGE_BRANCH', 'main')

# Optional heavy services
ENABLE_REDIS     = os.getenv('ENABLE_REDIS', 'false').lower() == 'true'
ENABLE_POSTGRES  = os.getenv('ENABLE_POSTGRES', 'false').lower() == 'true'
ENABLE_OLLAMA    = os.getenv('ENABLE_OLLAMA', 'false').lower() == 'true'
ENABLE_SENTIMENT = os.getenv('ENABLE_SENTIMENT', 'true').lower() == 'true' and SENTIMENT_AVAILABLE
REDIS_URL        = os.getenv('REDIS_URL', 'redis://localhost:6379')
DATABASE_URL     = os.getenv('DATABASE_URL', '')
OLLAMA_URL       = os.getenv('OLLAMA_URL', 'http://localhost:11434')

# Idle thresholds
IDLE_WARN_MINUTES  = _env_int('IDLE_WARN_MINUTES', 30)
IDLE_CLOSE_MINUTES = _env_int('IDLE_CLOSE_MINUTES', 60)

# Rate limits
RATE_LIMIT_SECONDS      = 5
MAX_MESSAGES_PER_MINUTE = 12


# ──────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────────────────────────────────────
if STRUCTLOG_AVAILABLE:
    structlog.configure(
        processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.dev.ConsoleRenderer()],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logger = structlog.get_logger()
else:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    logger = logging.getLogger('RawrBot')

# Sentry
if SENTRY_AVAILABLE and os.getenv('SENTRY_DSN'):
    sentry_sdk.init(dsn=os.getenv('SENTRY_DSN'), integrations=[AsyncioIntegration()], traces_sample_rate=0.1)
    logger.info("Sentry enabled")


# ──────────────────────────────────────────────────────────────────────────────
# DATA MODELS & HELPERS
# ──────────────────────────────────────────────────────────────────────────────
class TicketStatus(Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    CLOSED = "closed"

class TicketType(Enum):
    WEB = "web"
    DM = "dm"
    FORCED = "forced"

class SentimentType(Enum):
    VERY_NEGATIVE = (-1.0, -0.6)
    NEGATIVE = (-0.6, -0.2)
    NEUTRAL = (-0.2, 0.2)
    POSITIVE = (0.2, 0.6)
    VERY_POSITIVE = (0.6, 1.0)

class ToneMode(Enum):
    PLAYFUL = "playful"
    EMPATHETIC = "empathetic"
    SERIOUS = "serious"
    WITTY = "witty"
    NEUTRAL = "neutral"

class Ticket:
    def __init__(self, user_id, user_name, channel_id, ticket_type, created_by=None):
        self.user_id = user_id
        self.user_name = user_name
        self.channel_id = channel_id
        self.ticket_type = ticket_type
        self.status = TicketStatus.OPEN
        self.claimed_by = None
        self.claimed_by_name = None
        self.claimed_by_role = None
        self.created_at = datetime.now(timezone.utc)
        self.claimed_at = None
        self.resolved_at = None
        self.resolved_by = None
        self.resolved_reason = None
        self.resolved_by_role = None
        self.closed_at = None
        self.closed_by = None
        self.last_activity = datetime.now(timezone.utc)
        self.message_count = 0
        self.transcript_log = []
        self.additional_users = []
        self.idle_warned = False
        self.created_by = created_by

    def touch(self):
        self.last_activity = datetime.now(timezone.utc)
        self.idle_warned = False

    def idle_seconds(self):
        return (datetime.now(timezone.utc) - self.last_activity).total_seconds()

class TicketManager:
    def __init__(self):
        self.tickets = {}
        self.channel_to_user = {}
        self.lock = asyncio.Lock()
        self.handled_count = 0

    async def create_ticket(self, user_id, user_name, channel_id, ticket_type, created_by=None):
        async with self.lock:
            if user_id in self.tickets:
                return self.tickets[user_id]
            ticket = Ticket(user_id, user_name, channel_id, ticket_type, created_by)
            self.tickets[user_id] = ticket
            self.channel_to_user[channel_id] = user_id
            self.handled_count += 1
            return ticket

    async def get_ticket_by_user(self, user_id):
        async with self.lock:
            return self.tickets.get(user_id)

    async def get_ticket_by_channel(self, channel_id):
        async with self.lock:
            uid = self.channel_to_user.get(channel_id)
            return self.tickets.get(uid) if uid else None

    async def claim_ticket(self, user_id, staff_id, staff_name, staff_role):
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status == TicketStatus.OPEN:
                ticket.status = TicketStatus.CLAIMED
                ticket.claimed_by = staff_id
                ticket.claimed_by_name = staff_name
                ticket.claimed_by_role = staff_role
                ticket.claimed_at = datetime.now(timezone.utc)
                ticket.touch()
                return ticket
            return None

    async def resolve_ticket(self, user_id, resolved_by, resolved_by_role, reason):
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status in (TicketStatus.OPEN, TicketStatus.CLAIMED):
                ticket.status = TicketStatus.RESOLVED
                ticket.resolved_at = datetime.now(timezone.utc)
                ticket.resolved_by = resolved_by
                ticket.resolved_reason = reason
                ticket.resolved_by_role = resolved_by_role
                ticket.touch()
                return ticket
            return None

    async def close_ticket(self, user_id, channel_id=None, closed_by=None, reason=None):
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                if channel_id:
                    self.channel_to_user.pop(channel_id, None)
                ticket.status = TicketStatus.CLOSED
                ticket.closed_at = datetime.now(timezone.utc)
                ticket.closed_by = closed_by
                return ticket
            return None

    async def add_message(self, user_id, content, author="unknown", origin="dm"):
        async with self.lock:
            t = self.tickets.get(user_id)
            if t:
                t.message_count += 1
                t.touch()
                t.transcript_log.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "author": author,
                    "content": content,
                    "origin": origin,
                })

    async def get_all_open_tickets(self):
        async with self.lock:
            return [t for t in self.tickets.values() if t.status in (TicketStatus.OPEN, TicketStatus.CLAIMED)]

    async def get_stats(self):
        async with self.lock:
            return {
                'total': len(self.tickets),
                'open': sum(1 for t in self.tickets.values() if t.status == TicketStatus.OPEN),
                'claimed': sum(1 for t in self.tickets.values() if t.status == TicketStatus.CLAIMED),
                'resolved': sum(1 for t in self.tickets.values() if t.status == TicketStatus.RESOLVED),
                'handled': self.handled_count,
            }

ticket_manager = TicketManager()

# ──────────────────────────────────────────────────────────────────────────────
# GITHUB STORAGE (same as before)
# ──────────────────────────────────────────────────────────────────────────────
GITHUB_API_BASE = "https://api.github.com"

async def _github_request(method, path, token, data=None):
    url = f"{GITHUB_API_BASE}/repos/{STORAGE_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with ClientSession() as session:
        if method == "GET":
            async with session.get(url, headers=headers) as resp:
                return resp.status, await resp.json()
        elif method == "PUT":
            headers["Content-Type"] = "application/json"
            async with session.put(url, headers=headers, json=data) as resp:
                return resp.status, await resp.json()

async def _read_github_file(file_path):
    if not STORAGE_TOKEN:
        return None
    status, body = await _github_request("GET", file_path, STORAGE_TOKEN)
    if status == 200:
        content = body.get("content", "")
        if content:
            decoded = base64.b64decode(content).decode("utf-8")
            return json.loads(decoded)
    return None

async def _write_github_file(file_path, data):
    if not STORAGE_TOKEN:
        return False
    status, body = await _github_request("GET", file_path, STORAGE_TOKEN)
    sha = body.get("sha") if status == 200 else None
    content_str = json.dumps(data, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(content_str.encode()).decode()
    payload = {"message": f"Update {file_path}", "content": encoded, "branch": STORAGE_BRANCH}
    if sha:
        payload["sha"] = sha
    status, _ = await _github_request("PUT", file_path, STORAGE_TOKEN, data=payload)
    return status in (200, 201)

# Storage wrappers (GitHub first, local JSON fallback)
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "transcripts"), exist_ok=True)
WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")

class Storage:
    @staticmethod
    async def _read_local(path, default):
        try:
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
        except:
            pass
        return default

    @staticmethod
    async def _write_local(path, data):
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            return True
        except:
            return False

    # Whitelist
    @staticmethod
    async def add_whitelist_user(uid, added_by):
        data = await _read_github_file("whitelist.json")
        if data is not None:
            data.setdefault("users", [])
            if uid not in data["users"]:
                data["users"].append(uid)
                await _write_github_file("whitelist.json", data)
            return True
        data = await Storage._read_local(WHITELIST_FILE, {"users":[],"roles":[]})
        if uid not in data["users"]:
            data["users"].append(uid)
            await Storage._write_local(WHITELIST_FILE, data)
        return True

    @staticmethod
    async def add_whitelist_role(rid, added_by):
        data = await _read_github_file("whitelist.json")
        if data is not None:
            data.setdefault("roles", [])
            if rid not in data["roles"]:
                data["roles"].append(rid)
                await _write_github_file("whitelist.json", data)
            return True
        data = await Storage._read_local(WHITELIST_FILE, {"users":[],"roles":[]})
        if rid not in data["roles"]:
            data["roles"].append(rid)
            await Storage._write_local(WHITELIST_FILE, data)
        return True

    @staticmethod
    async def remove_whitelist(item_id, item_type='user'):
        data = await _read_github_file("whitelist.json")
        if data is not None:
            key = 'users' if item_type == 'user' else 'roles'
            if item_id in data.get(key, []):
                data[key].remove(item_id)
                await _write_github_file("whitelist.json", data)
            return True
        data = await Storage._read_local(WHITELIST_FILE, {"users":[],"roles":[]})
        key = 'users' if item_type == 'user' else 'roles'
        if item_id in data[key]:
            data[key].remove(item_id)
            await Storage._write_local(WHITELIST_FILE, data)
        return True

    @staticmethod
    async def get_whitelist():
        data = await _read_github_file("whitelist.json")
        if data is not None:
            return data.get("users", []), data.get("roles", [])
        data = await Storage._read_local(WHITELIST_FILE, {"users":[],"roles":[]})
        return data.get("users", []), data.get("roles", [])

    # Blacklist
    @staticmethod
    async def add_blacklist(uid, reason, by):
        data = await _read_github_file("blacklist.json")
        if data is not None:
            data.setdefault("users", [])
            if uid not in data["users"]:
                data["users"].append(uid)
                await _write_github_file("blacklist.json", data)
            return True
        data = await Storage._read_local(BLACKLIST_FILE, {"users":[]})
        if uid not in data["users"]:
            data["users"].append(uid)
            await Storage._write_local(BLACKLIST_FILE, data)
        return True

    @staticmethod
    async def remove_blacklist(uid):
        data = await _read_github_file("blacklist.json")
        if data is not None:
            if uid in data.get("users", []):
                data["users"].remove(uid)
                await _write_github_file("blacklist.json", data)
            return True
        data = await Storage._read_local(BLACKLIST_FILE, {"users":[]})
        if uid in data["users"]:
            data["users"].remove(uid)
            await Storage._write_local(BLACKLIST_FILE, data)
        return True

    @staticmethod
    async def get_blacklist():
        data = await _read_github_file("blacklist.json")
        if data is not None:
            return data.get("users", [])
        data = await Storage._read_local(BLACKLIST_FILE, {"users":[]})
        return data.get("users", [])

    # Ticket logs
    @staticmethod
    async def save_ticket(ticket_data):
        data = await _read_github_file("Chats/Logging.json")
        if data is not None:
            if isinstance(data, list):
                data.append(ticket_data)
            else:
                data = [ticket_data]
            await _write_github_file("Chats/Logging.json", data)
            return True
        # local fallback
        path = os.path.join(DATA_DIR, f"history_{ticket_data.get('user_id','unknown')}.jsonl")
        try:
            with open(path, 'a') as f:
                f.write(json.dumps(ticket_data) + '\n')
            return True
        except:
            return False

    @staticmethod
    async def get_user_history(uid):
        data = await _read_github_file("Chats/Logging.json")
        if data is not None and isinstance(data, list):
            return [t for t in data if t.get("user_id") == uid]
        path = os.path.join(DATA_DIR, f"history_{uid}.jsonl")
        if not os.path.exists(path):
            return []
        try:
            with open(path) as f:
                return [json.loads(line) for line in f if line.strip()]
        except:
            return []


# Global in‑memory cache for whitelist/blacklist
whitelisted_users: Set[int] = set()
whitelisted_roles: Set[int] = set()
blacklisted_users: Set[int] = set()

async def load_data():
    global whitelisted_users, whitelisted_roles, blacklisted_users
    users, roles = await Storage.get_whitelist()
    whitelisted_users = set(users)
    whitelisted_roles = set(roles)
    blacklisted_users = set(await Storage.get_blacklist())
    logger.info(f"Loaded {len(whitelisted_users)} wl users, {len(whitelisted_roles)} wl roles, {len(blacklisted_users)} blacklisted")


# ──────────────────────────────────────────────────────────────────────────────
# TRANSCRIPT GENERATOR (HTML)
# ──────────────────────────────────────────────────────────────────────────────
def generate_transcript_html(ticket):
    rows = ""
    for entry in ticket.transcript_log:
        ts = entry.get("ts", "")
        author = entry.get("author", "Unknown")
        content = entry.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
        origin = entry.get("origin", "")
        badge = "🌐 Web" if origin == "web" else ("🤖 Bot" if origin == "bot" else "💬 DM")
        rows += f"""<div class="message"><span class="ts">{ts[:19].replace('T',' ')}</span><span class="badge">{badge}</span><span class="author">{author}</span><span class="content">{content}</span></div>"""
    duration = ""
    if ticket.closed_at and ticket.created_at:
        secs = int((ticket.closed_at - ticket.created_at).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        duration = f"{h}h {m}m {s}s"
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Transcript – {ticket.user_name}</title>
<style>
:root {{ --bg:#0d1117; --surface:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#ef4444; --green:#3fb950; --blue:#58a6ff; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--text); font-family:system-ui; padding:2rem; line-height:1.5; }}
header {{ border-bottom:1px solid var(--border); padding-bottom:1rem; margin-bottom:1.5rem; }}
h1 {{ color:var(--accent); font-size:1.4rem; }}
.meta {{ display:flex; gap:2rem; flex-wrap:wrap; font-size:.85rem; color:var(--muted); margin:.5rem 0; }}
.meta span strong {{ color:var(--text); }}
.log {{ display:flex; flex-direction:column; gap:.5rem; }}
.message {{ background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:.6rem .9rem; display:grid; grid-template-columns:9rem 4rem 10rem 1fr; gap:.5rem; align-items:baseline; font-size:.875rem; }}
.ts {{ color:var(--muted); font-size:.75rem; }}
.badge {{ font-size:.7rem; }}
.author {{ font-weight:600; color:var(--blue); }}
.content {{ word-break:break-word; white-space:pre-wrap; }}
footer {{ margin-top:2rem; font-size:.75rem; color:var(--muted); border-top:1px solid var(--border); padding-top:1rem; }}
</style></head><body><header><h1>🎫 Ticket Transcript</h1><div class="meta">
<span><strong>User:</strong> {ticket.user_name} ({ticket.user_id})</span>
<span><strong>Type:</strong> {ticket.ticket_type.value}</span>
<span><strong>Created:</strong> {ticket.created_at.strftime('%Y-%m-%d %H:%M UTC')}</span>
<span><strong>Closed:</strong> {ticket.closed_at.strftime('%Y-%m-%d %H:%M UTC') if ticket.closed_at else 'N/A'}</span>
<span><strong>Duration:</strong> {duration}</span>
<span><strong>Messages:</strong> {ticket.message_count}</span>
<span><strong>Handled by:</strong> {ticket.claimed_by_name or 'Unclaimed'}</span>
<span><strong>Resolution:</strong> {ticket.resolved_reason or 'N/A'}</span>
</div></header><div class="log">{rows if rows else '<p style="color:var(--muted)">No messages recorded.</p>'}</div>
<footer>Generated by RawrBot &bull; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</footer></body></html>"""

def save_transcript(ticket):
    filename = f"ticket_{ticket.channel_id}_{ticket.user_id}.html"
    path = os.path.join(DATA_DIR, "transcripts", filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(generate_transcript_html(ticket))
    return path


# ──────────────────────────────────────────────────────────────────────────────
# ADVANCED SERVICES (optional Redis, PostgreSQL, Ollama)
# ──────────────────────────────────────────────────────────────────────────────
redis_client = None
if ENABLE_REDIS and aioredis:
    try:
        redis_client = aioredis.from_url(REDIS_URL)
        logger.info("Redis connected")
    except Exception as e:
        logger.warning(f"Redis not available: {e}")
        redis_client = None

pool = None
if ENABLE_POSTGRES and asyncpg:
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        logger.info("PostgreSQL connected")
    except Exception as e:
        logger.warning(f"PostgreSQL not available: {e}")
        pool = None

ollama_session = None
if ENABLE_OLLAMA:
    ollama_session = ClientSession()

async def ollama_generate(prompt, context=""):
    if not ollama_session:
        return None
    try:
        async with ollama_session.post(f"{OLLAMA_URL}/api/generate",
                json={"model":"mistral","prompt":f"{context}\nUser: {prompt}\nAssistant:","stream":False,"temperature":0.7}) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("response","")[:150]
    except:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# TONE & SENTIMENT (simple)
# ──────────────────────────────────────────────────────────────────────────────
def analyze_sentiment(text):
    if not ENABLE_SENTIMENT or len(text) < 3:
        return SentimentType.NEUTRAL, 0.0
    try:
        polarity = TextBlob(text).sentiment.polarity
        for st in SentimentType:
            lo, hi = st.value
            if lo <= polarity < hi:
                return st, polarity
        return SentimentType.NEUTRAL, 0.0
    except:
        return SentimentType.NEUTRAL, 0.0

def get_adapted_tone(sentiment):
    mapping = {
        SentimentType.VERY_NEGATIVE: ToneMode.EMPATHETIC,
        SentimentType.NEGATIVE: ToneMode.EMPATHETIC,
        SentimentType.NEUTRAL: ToneMode.NEUTRAL,
        SentimentType.POSITIVE: ToneMode.PLAYFUL,
        SentimentType.VERY_POSITIVE: ToneMode.WITTY,
    }
    return mapping.get(sentiment, ToneMode.NEUTRAL)

def humanize_response(base_text, tone=None):
    if tone == ToneMode.PLAYFUL:
        emojis = ["😄", "🎉", "😊", "✨"]
        return f"{emojis[hash(base_text)%len(emojis)]} {base_text}"
    elif tone == ToneMode.EMPATHETIC:
        emojis = ["💙", "🤝", "👂", "🫂"]
        return f"{emojis[hash(base_text)%len(emojis)]} {base_text}"
    return base_text


# ──────────────────────────────────────────────────────────────────────────────
# RATE LIMITER (local, can use Redis too)
# ──────────────────────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.local = {}
    def can_send(self, user_id, content):
        now = time.time()
        key = str(user_id)
        if key not in self.local:
            self.local[key] = deque()
        dq = self.local[key]
        # Sliding window: 12 messages per 60 secs, minimum 5 secs between
        while dq and dq[0] < now - 60:
            dq.popleft()
        if len(dq) >= MAX_MESSAGES_PER_MINUTE:
            return False, "You're sending messages too quickly. Please slow down."
        if dq and now - dq[-1] < RATE_LIMIT_SECONDS:
            return False, f"Wait {RATE_LIMIT_SECONDS - (now - dq[-1]):.0f} seconds before sending another message."
        return True, "OK"
    def record(self, user_id):
        key = str(user_id)
        if key not in self.local:
            self.local[key] = deque()
        self.local[key].append(time.time())

rate_limiter = RateLimiter()


# ──────────────────────────────────────────────────────────────────────────────
# WEB CHAT (SSE)
# ──────────────────────────────────────────────────────────────────────────────
message_queue = deque(maxlen=100)
sse_clients = set()
web_user_channels = {}  # web_user_id -> channel_id (hash based)

async def broadcast_to_web(message):
    if not sse_clients:
        return
    message['id'] = f"msg_{int(time.time()*1000)}_{hashlib.md5(message.get('text','').encode()).hexdigest()[:6]}"
    message['timestamp'] = datetime.now(timezone.utc).isoformat()
    message_queue.append(message)
    data = f"data: {json.dumps(message)}\n\n"
    dead = set()
    for client in sse_clients:
        try:
            await client.write(data.encode())
        except:
            dead.add(client)
    sse_clients -= dead

async def forward_to_discord(message):
    if not TICKET_CATEGORY_ID or not bot.ready:
        return
    guild = bot.get_guild(GUILD_ID)
    category = bot.get_channel(TICKET_CATEGORY_ID)
    if not guild or not category:
        return
    user_name = message.get('user', 'Guest')
    web_id = hash("web_" + user_name)
    channel = None
    cid = web_user_channels.get(web_id)
    if cid:
        channel = bot.get_channel(cid)
    if not channel:
        channel_name = f"web-{re.sub(r'[^a-z0-9-]','', user_name.lower().replace(' ','-'))}"
        channel = discord.utils.get(category.text_channels, name=channel_name)
        if not channel:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
                if rid:
                    role = guild.get_role(rid)
                    if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            try:
                channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites,
                                                          reason=f"Web chat from {user_name}")
                await ticket_manager.create_ticket(web_id, user_name, channel.id, TicketType.WEB)
                await channel.send(f"📬 Web chat started with **{user_name}**\nUse `/reply` to answer.")
                web_user_channels[web_id] = channel.id
            except Exception as e:
                logger.error(f"Failed to create web channel: {e}")
                return
        else:
            web_user_channels[web_id] = channel.id
            if not await ticket_manager.get_ticket_by_user(web_id):
                await ticket_manager.create_ticket(web_id, user_name, channel.id, TicketType.WEB)
    embed = discord.Embed(description=message['text'], color=0x3b82f6, timestamp=datetime.now(timezone.utc))
    embed.set_author(name=f"🌐 Web: {user_name}")
    await channel.send(embed=embed)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP ENDPOINTS
# ──────────────────────────────────────────────────────────────────────────────
async def sse_handler(request):
    headers = {'Content-Type':'text/event-stream','Cache-Control':'no-cache','Access-Control-Allow-Origin':'*','Connection':'keep-alive'}
    resp = web.StreamResponse(headers=headers)
    await resp.prepare(request)
    sse_clients.add(resp)
    for msg in list(message_queue)[-10:]:
        await resp.write(f"data: {json.dumps(msg)}\n\n".encode())
    try:
        while True:
            await asyncio.sleep(30)
            await resp.write(b': heartbeat\n\n')
    except:
        pass
    finally:
        sse_clients.discard(resp)
    return resp

async def send_endpoint(request):
    try:
        data = await request.json()
        user_name = data.get('user', 'Guest')
        can, err = rate_limiter.can_send(hash(user_name), data.get('text',''))
        if not can:
            return web.json_response({'error': err}, status=429)
        rate_limiter.record(hash(user_name))
        msg = {'type':'chat','user':user_name,'text':data.get('text',''),'origin':'web'}
        await broadcast_to_web(msg)
        await forward_to_discord(msg)
        return web.json_response({'status':'ok'})
    except:
        return web.json_response({'error':'Invalid request'}, status=400)

async def health_handler(request):
    stats = await ticket_manager.get_stats()
    return web.json_response({'status':'alive','tickets':stats,'web_clients':len(sse_clients)})


# ──────────────────────────────────────────────────────────────────────────────
# PERMISSION HELPERS (no crash on 404)
# ──────────────────────────────────────────────────────────────────────────────
def is_owner():
    async def pred(interaction):
        return interaction.user.id == OWNER_ID
    return app_commands.check(pred)

def is_manager():
    async def pred(interaction):
        if interaction.user.id == OWNER_ID: return True
        return MANAGER_ROLE_ID in {r.id for r in interaction.user.roles}
    return app_commands.check(pred)

def is_staff():
    async def pred(interaction):
        if interaction.user.id == OWNER_ID: return True
        roles = {r.id for r in interaction.user.roles}
        return bool(roles & {STAFF_ROLE_ID, MANAGER_ROLE_ID, MODERATOR_ROLE_ID})
    return app_commands.check(pred)

def is_moderator():
    async def pred(interaction):
        if interaction.user.id == OWNER_ID: return True
        roles = {r.id for r in interaction.user.roles}
        return bool(roles & {MODERATOR_ROLE_ID, MANAGER_ROLE_ID})
    return app_commands.check(pred)

def get_staff_role_name(member):
    if member.id == OWNER_ID: return "👑 Owner"
    roles = {r.id for r in member.roles}
    if MANAGER_ROLE_ID in roles: return "⭐ Manager"
    if MODERATOR_ROLE_ID in roles: return "🛡️ Moderator"
    if STAFF_ROLE_ID in roles: return "📞 Staff"
    return "👤 Member"


# ──────────────────────────────────────────────────────────────────────────────
# BOT CLASS
# ──────────────────────────────────────────────────────────────────────────────
class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.moderation = True
        self.boot_time = datetime.now(timezone.utc)
        self.web_app = None
        self.runner = None
        self.ready = False
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await load_data()
        self.web_app = web.Application()
        self.web_app.router.add_get('/events', sse_handler)
        self.web_app.router.add_post('/send', send_endpoint)
        self.web_app.router.add_get('/health', health_handler)
        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', PORT).start()
        logger.info(f"HTTP on port {PORT}")
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("Commands synced")

    async def on_ready(self):
        self.ready = True
        logger.info(f"Logged in as {self.user.name} ({self.user.id})")
        self.status_task.start()
        self.idle_check_task.start()

    @tasks.loop(seconds=30)
    async def status_task(self):
        if not self.ready: return
        try:
            stats = await ticket_manager.get_stats()
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=f"{stats['open']} open tickets | rawrs.zapto.org"))
        except: pass

    @tasks.loop(minutes=5)
    async def idle_check_task(self):
        if not self.ready: return
        for ticket in list(ticket_manager.tickets.values()):
            if ticket.status in (TicketStatus.OPEN, TicketStatus.CLAIMED):
                idle_min = ticket.idle_seconds()/60
                channel = self.get_channel(ticket.channel_id)
                if not channel: continue
                if idle_min >= IDLE_WARN_MINUTES and not ticket.idle_warned:
                    ticket.idle_warned = True
                    try: await channel.send(embed=discord.Embed(title="⏳ Still need help?", description=f"This ticket has been idle for {int(idle_min)} minutes. It will close automatically if we don't hear back.", color=0xeab308))
                    except: pass
                if idle_min >= IDLE_CLOSE_MINUTES:
                    logger.info(f"Auto-closing idle ticket #{ticket.channel_id}")
                    await close_ticket_by_user(ticket.user_id, channel, "Auto-Close", "Idle timeout")

    async def close(self):
        if self.runner: await self.runner.cleanup()
        if redis_client: await redis_client.close()
        if pool: await pool.close()
        if ollama_session: await ollama_session.close()
        await super().close()

bot = RawrBot()


# ──────────────────────────────────────────────────────────────────────────────
# COMMAND AUTOCOMPLETE
# ──────────────────────────────────────────────────────────────────────────────
async def active_tickets_autocomplete(interaction, current):
    choices = []
    for uid,t in list(ticket_manager.tickets.items())[:25]:
        label = f"{t.user_name} ({uid})"
        if current.lower() in label.lower() or not current:
            choices.append(app_commands.Choice(name=label, value=str(uid)))
    return choices

# ──────────────────────────────────────────────────────────────────────────────
# ALL COMMANDS (the missing ones added)
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)}ms`", ephemeral=True)

@bot.tree.command(name="help")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🆘 RawrBot Help", description="I'm here to help with support tickets, moderation, and more!", color=0xef4444)
    embed.add_field(name="📬 Start a Ticket", value="Send me a DM and I'll open a ticket for you.", inline=False)
    embed.add_field(name="🎫 Ticket Commands", value="/claim /resolve /close /reply /transfer /force_ticket /force_transfer /tickets /info /transcript /adduser /removeuser /note", inline=False)
    embed.add_field(name="🛡️ Moderation", value="/ban /kick /timeout /warn", inline=False)
    embed.add_field(name="📊 Stats", value="/stats /tickets", inline=False)
    embed.set_footer(text="We're here for you! 💙")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stats")
async def stats(interaction: discord.Interaction):
    uptime = datetime.now(timezone.utc) - bot.boot_time
    d,h,m = uptime.days, uptime.seconds//3600, (uptime.seconds%3600)//60
    ts = await ticket_manager.get_stats()
    embed = discord.Embed(title="📊 Bot Stats", color=0x3b82f6)
    embed.add_field(name="⏰ Uptime", value=f"{d}d {h}h {m}m", inline=True)
    embed.add_field(name="⚡ Latency", value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="🎫 Active Tickets", value=str(ts['total']), inline=True)
    embed.add_field(name="✅ Handled", value=str(ts['handled']), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Moderation commands
@bot.tree.command(name="ban")
@is_moderator()
async def ban(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id in (OWNER_ID, interaction.user.id):
        return await interaction.response.send_message("❌ Cannot ban that user.", ephemeral=True)
    try:
        await interaction.guild.ban(user, reason=reason or "No reason provided")
        embed = discord.Embed(title="🔨 User Banned", description=f"**User:** {user.mention}\n**Reason:** {reason or 'No reason'}", color=0xef4444, timestamp=datetime.now(timezone.utc))
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="kick")
@is_moderator()
async def kick(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id in (OWNER_ID, interaction.user.id):
        return await interaction.response.send_message("❌ Cannot kick that user.", ephemeral=True)
    try:
        member = interaction.guild.get_member(user.id)
        if member: await member.kick(reason=reason or "No reason provided")
        embed = discord.Embed(title="👢 User Kicked", description=f"**User:** {user.mention}\n**Reason:** {reason or 'No reason'}", color=0xf97316, timestamp=datetime.now(timezone.utc))
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="timeout")
@is_moderator()
async def timeout(interaction: discord.Interaction, user: discord.User, duration: str, reason: Optional[str] = None):
    try:
        amount = int(duration[:-1])
        unit = duration[-1].lower()
        seconds = amount * {'s':1,'m':60,'h':3600,'d':86400}[unit]
        if seconds > 2419200:
            return await interaction.response.send_message("❌ Timeout cannot exceed 28 days.", ephemeral=True)
        member = interaction.guild.get_member(user.id)
        if not member:
            return await interaction.response.send_message("❌ User not in server.", ephemeral=True)
        await member.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=reason or "No reason")
        embed = discord.Embed(title="⏰ User Timed Out", description=f"**User:** {user.mention}\n**Duration:** {duration}\n**Reason:** {reason or 'No reason'}", color=0xeab308, timestamp=datetime.now(timezone.utc))
        await interaction.response.send_message(embed=embed)
    except:
        await interaction.response.send_message("❌ Invalid format. Use like `30m`, `1h`, `1d`.", ephemeral=True)

@bot.tree.command(name="warn")
@is_staff()
async def warn(interaction: discord.Interaction, user: discord.User, reason: str):
    embed = discord.Embed(title="⚠️ Warning Issued", description=f"**User:** {user.mention}\n**Reason:** {reason}", color=0xeab308, timestamp=datetime.now(timezone.utc))
    await interaction.response.send_message(embed=embed)
    try: await user.send(f"⚠️ You were warned in **{interaction.guild.name}**: {reason}")
    except: pass

# Whitelist/Blacklist
@bot.tree.command(name="whitelist_add")
@is_manager()
async def whitelist_add(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    if user:
        await Storage.add_whitelist_user(user.id, interaction.user.name)
        whitelisted_users.add(user.id)
        await interaction.response.send_message(f"✅ {user.mention} added to whitelist.", ephemeral=True)
    elif role:
        await Storage.add_whitelist_role(role.id, interaction.user.name)
        whitelisted_roles.add(role.id)
        await interaction.response.send_message(f"✅ {role.mention} role added to whitelist.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Provide a user or role.", ephemeral=True)

@bot.tree.command(name="whitelist_remove")
@is_manager()
async def whitelist_remove(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    if user:
        await Storage.remove_whitelist(user.id, 'user')
        whitelisted_users.discard(user.id)
        await interaction.response.send_message(f"✅ {user.mention} removed.", ephemeral=True)
    elif role:
        await Storage.remove_whitelist(role.id, 'role')
        whitelisted_roles.discard(role.id)
        await interaction.response.send_message(f"✅ {role.mention} removed.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Provide a user or role.", ephemeral=True)

@bot.tree.command(name="whitelist_list")
@is_staff()
async def whitelist_list(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 Whitelist", color=0x00ff00)
    users = [f"<@{u}> ({u})" for u in whitelisted_users] or ["None"]
    roles = [f"<@&{r}> ({r})" for r in whitelisted_roles] or ["None"]
    embed.add_field(name="Users", value="\n".join(users[:20]), inline=False)
    embed.add_field(name="Roles", value="\n".join(roles[:20]), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="blacklist")
@is_staff()
async def blacklist(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    await Storage.add_blacklist(user.id, reason or "No reason", interaction.user.name)
    blacklisted_users.add(user.id)
    embed = discord.Embed(title="🚫 User Blacklisted", description=f"**User:** {user.mention}\n**Reason:** {reason or 'No reason'}", color=0xef4444)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unblacklist")
@is_staff()
async def unblacklist(interaction: discord.Interaction, user: discord.User):
    await Storage.remove_blacklist(user.id)
    blacklisted_users.discard(user.id)
    await interaction.response.send_message(f"✅ {user.mention} removed from blacklist.", ephemeral=True)

@bot.tree.command(name="blacklist_list")
@is_staff()
async def blacklist_list(interaction: discord.Interaction):
    if not blacklisted_users:
        return await interaction.response.send_message("📭 No blacklisted users.", ephemeral=True)
    embed = discord.Embed(title="🚫 Blacklist", color=0xef4444)
    embed.description = "\n".join(f"<@{u}> ({u})" for u in blacklisted_users)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Ticket user management
@bot.tree.command(name="adduser")
@is_staff()
async def adduser(interaction: discord.Interaction, user: discord.User):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        return await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
    await interaction.channel.set_permissions(user, read_messages=True, send_messages=True)
    ticket.additional_users.append(user.id)
    await interaction.response.send_message(embed=discord.Embed(title="👤 User Added", description=f"{user.mention} can now see this ticket.", color=0x00ff00))
    try: await user.send(f"📋 You were added to a support ticket in {interaction.guild.name}.")
    except: pass

@bot.tree.command(name="removeuser")
@is_manager()
async def removeuser(interaction: discord.Interaction, user: discord.User):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        return await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
    if user.id == ticket.user_id:
        return await interaction.response.send_message("❌ Can't remove ticket owner.", ephemeral=True)
    await interaction.channel.set_permissions(user, overwrite=None)
    if user.id in ticket.additional_users: ticket.additional_users.remove(user.id)
    await interaction.response.send_message(embed=discord.Embed(title="👤 User Removed", description=f"{user.mention} removed.", color=0xffaa00))
    try: await user.send(f"📋 You were removed from a ticket.")
    except: pass

# Force ticket
@bot.tree.command(name="force_ticket")
@is_staff()
async def force_ticket(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id in blacklisted_users:
        return await interaction.response.send_message(f"❌ {user.mention} is blacklisted.", ephemeral=True)
    if await ticket_manager.get_ticket_by_user(user.id):
        return await interaction.response.send_message(f"❌ {user.mention} already has an open ticket.", ephemeral=True)
    await interaction.response.defer()
    guild = interaction.guild
    category = bot.get_channel(TICKET_CATEGORY_ID) or discord.utils.get(guild.categories, name="SUPPORT TICKETS")
    if not category: category = await guild.create_category("SUPPORT TICKETS")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
        if rid and (role := guild.get_role(rid)):
            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    channel = await guild.create_text_channel(f"forced-{user.name.lower().replace(' ','-')}", category=category, overwrites=overwrites)
    await ticket_manager.create_ticket(user.id, user.name, channel.id, TicketType.FORCED, interaction.user.name)
    embed = discord.Embed(title="🎫 Forced Ticket", description=f"{user.mention} \- {reason or 'No reason'}", color=0xffaa00)
    await channel.send(embed=embed)
    try: await user.send("📋 A staff member opened a ticket for you.")
    except: pass
    await interaction.followup.send(f"✅ Ticket created for {user.mention} in {channel.mention}")

# Transfer/ force_transfer
@bot.tree.command(name="transfer")
@is_staff()
@app_commands.autocomplete(user_id=active_tickets_autocomplete)
async def transfer_cmd(interaction: discord.Interaction, user_id: str, new_staff: discord.Member):
    uid = int(user_id)
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket or ticket.status != TicketStatus.CLAIMED:
        return await interaction.response.send_message("❌ Ticket not claimed or not found.", ephemeral=True)
    role = get_staff_role_name(new_staff)
    async with ticket_manager.lock:
        ticket.claimed_by = new_staff.id
        ticket.claimed_by_name = new_staff.display_name
        ticket.claimed_by_role = role
        ticket.claimed_at = datetime.now(timezone.utc)
    embed = discord.Embed(title="🔄 Ticket Transferred", description=f"Ticket now handled by {new_staff.mention} ({role})", color=0xffaa00)
    channel = bot.get_channel(ticket.channel_id)
    if channel: await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Transferred to {new_staff.display_name}", ephemeral=True)

@bot.tree.command(name="force_transfer")
@is_staff()
@app_commands.autocomplete(user_id=active_tickets_autocomplete)
async def force_transfer_cmd(interaction: discord.Interaction, user_id: str, new_staff: discord.Member):
    uid = int(user_id)
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        return await interaction.response.send_message("❌ No active ticket found.", ephemeral=True)
    role = get_staff_role_name(new_staff)
    ticket.claimed_by = new_staff.id
    ticket.claimed_by_name = new_staff.display_name
    ticket.claimed_by_role = role
    ticket.claimed_at = datetime.now(timezone.utc)
    if ticket.status == TicketStatus.OPEN:
        ticket.status = TicketStatus.CLAIMED
    embed = discord.Embed(title="🔄 Force Transfer", description=f"Ticket now handled by {new_staff.mention} ({role})", color=0xffaa00)
    ch = bot.get_channel(ticket.channel_id)
    if ch: await ch.send(embed=embed)
    await interaction.response.send_message(f"✅ Force transferred to {new_staff.display_name}", ephemeral=True)

# Note command
@bot.tree.command(name="note")
@is_staff()
@app_commands.autocomplete(user_id=active_tickets_autocomplete)
async def note_cmd(interaction: discord.Interaction, user_id: str, note: str):
    uid = int(user_id)
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        return await interaction.response.send_message("❌ No active ticket found.", ephemeral=True)
    notes_ch = discord.utils.get(interaction.guild.text_channels, name="staff-notes")
    if not notes_ch:
        overwrites = {interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False)}
        if STAFF_ROLE_ID: overwrites[interaction.guild.get_role(STAFF_ROLE_ID)] = discord.PermissionOverwrite(read_messages=True)
        notes_ch = await interaction.guild.create_text_channel("staff-notes", overwrites=overwrites)
    embed = discord.Embed(title="📝 Note", description=note, color=0x3b82f6, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"By {interaction.user.name} for {ticket.user_name}")
    await notes_ch.send(embed=embed)
    await interaction.response.send_message(f"✅ Note added.", ephemeral=True)

# Info and Transcript
@bot.tree.command(name="info")
@is_staff()
@app_commands.autocomplete(user_id=active_tickets_autocomplete)
async def info_cmd(interaction: discord.Interaction, user_id: str):
    uid = int(user_id)
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        return await interaction.response.send_message("❌ No active ticket.", ephemeral=True)
    embed = discord.Embed(title=f"📋 Ticket – {ticket.user_name}", color=0x3b82f6)
    embed.add_field(name="User ID", value=str(ticket.user_id), inline=True)
    embed.add_field(name="Type", value=ticket.ticket_type.value, inline=True)
    embed.add_field(name="Status", value=ticket.status.value, inline=True)
    embed.add_field(name="Claimed by", value=ticket.claimed_by_name or "Unclaimed", inline=True)
    embed.add_field(name="Messages", value=str(ticket.message_count), inline=True)
    embed.add_field(name="Idle", value=f"{int(ticket.idle_seconds()/60)}m", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="transcript")
@is_staff()
@app_commands.autocomplete(user_id=active_tickets_autocomplete)
async def transcript_cmd(interaction: discord.Interaction, user_id: str):
    uid = int(user_id)
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        return await interaction.response.send_message("❌ No active ticket.", ephemeral=True)
    path = save_transcript(ticket)
    await interaction.response.send_message(f"📄 Transcript for {ticket.user_name}", file=discord.File(path), ephemeral=True)

# Close helper
async def close_ticket_by_user(user_id, channel, closed_by=None, reason=None):
    ticket = await ticket_manager.close_ticket(user_id, channel.id if channel else None, closed_by, reason)
    if not ticket or not channel:
        return
    path = save_transcript(ticket)
    try:
        log_ch = discord.utils.get(channel.guild.text_channels, name="ticket-logs")
        if not log_ch:
            overwrites = {channel.guild.default_role: discord.PermissionOverwrite(read_messages=False)}
            log_ch = await channel.guild.create_text_channel("ticket-logs", overwrites=overwrites)
        embed = discord.Embed(title="📋 Ticket Closed", color=0xef4444, timestamp=datetime.now(timezone.utc))
        embed.description = f"**User:** {ticket.user_name} ({ticket.user_id})\n**Messages:** {ticket.message_count}"
        await log_ch.send(embed=embed, file=discord.File(path))
        if REVIEW_CHANNEL_ID and ticket.ticket_type != TicketType.WEB:
            rev_ch = bot.get_channel(REVIEW_CHANNEL_ID)
            if rev_ch: await rev_ch.send(embed=discord.Embed(title="⭐ Review", description=f"**User:** {ticket.user_name}\n**Resolved by:** {ticket.resolved_by}\n**Resolution:** {ticket.resolved_reason or 'N/A'}", color=0x00ff00))
        await Storage.save_ticket({
            'user_id': ticket.user_id,
            'ticket_id': ticket.channel_id,
            'user_name': ticket.user_name,
            'ticket_type': ticket.ticket_type.value,
            'status': ticket.status.value,
            'claimed_by_name': ticket.claimed_by_name,
            'created_at': ticket.created_at.isoformat(),
            'closed_at': ticket.closed_at.isoformat(),
            'message_count': ticket.message_count,
            'resolved_by': ticket.resolved_by,
            'resolved_reason': ticket.resolved_reason,
        })
    except Exception as e:
        logger.error(f"Close error: {e}")
    finally:
        try: await channel.delete()
        except: pass

# Ticket commands (claim, resolve, close, reply, tickets)
@bot.tree.command(name="claim")
@is_staff()
@app_commands.autocomplete(user_id=active_tickets_autocomplete)
async def claim(interaction: discord.Interaction, user_id: Optional[str] = None):
    uid = None
    ticket = None
    if user_id:
        try: uid = int(user_id)
        except: return await interaction.response.send_message("❌ Invalid ID.", ephemeral=True)
        ticket = await ticket_manager.get_ticket_by_user(uid)
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: uid = ticket.user_id
    if not ticket:
        return await interaction.response.send_message("❌ No active ticket found.", ephemeral=True)
    if ticket.status == TicketStatus.CLAIMED:
        return await interaction.response.send_message(f"❌ Already claimed by {ticket.claimed_by_name}.", ephemeral=True)
    role = get_staff_role_name(interaction.user)
    claimed = await ticket_manager.claim_ticket(uid, interaction.user.id, interaction.user.display_name, role)
    if claimed:
        embed = discord.Embed(title="✅ Ticket Claimed", description=f"**User:** {ticket.user_name}\n**Claimed by:** {interaction.user.mention} ({role})", color=0x00ff00)
        ch = bot.get_channel(ticket.channel_id)
        if ch: await ch.send(embed=embed)
        await interaction.response.send_message(f"✅ Claimed ticket for {ticket.user_name}.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Claim failed.", ephemeral=True)

@bot.tree.command(name="resolve")
@is_staff()
async def resolve(interaction: discord.Interaction, reason: str = "Issue resolved"):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        return await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
    role = get_staff_role_name(interaction.user)
    resolved = await ticket_manager.resolve_ticket(ticket.user_id, interaction.user.display_name, role, reason)
    if resolved:
        ch = bot.get_channel(ticket.channel_id)
        if ch:
            await ch.send(embed=discord.Embed(title="✅ Resolved", description=f"**Reason:** {reason}\n**By:** {interaction.user.mention} ({role})\n\nChannel closes in 30s.", color=0xffaa00))
        if ticket.ticket_type in (TicketType.DM, TicketType.FORCED):
            try: await (await bot.fetch_user(ticket.user_id)).send(f"✅ Your ticket has been resolved!\n**Reason:** {reason}\nThank you!")
            except: pass
        await interaction.response.send_message(f"✅ Resolved. Channel will be closed soon.", ephemeral=True)
        await asyncio.sleep(30)
        await close_ticket_by_user(ticket.user_id, ch, interaction.user.name, reason)

@bot.tree.command(name="close")
@is_staff()
async def close(interaction: discord.Interaction, reason: str = "No reason provided"):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        return await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
    await interaction.response.send_message("🔒 Closing in 5 seconds…")
    await asyncio.sleep(5)
    await close_ticket_by_user(ticket.user_id, interaction.channel, interaction.user.name, reason)

@bot.tree.command(name="reply")
@is_staff()
@app_commands.autocomplete(user_id=active_tickets_autocomplete)
async def reply(interaction: discord.Interaction, message: str, user_id: Optional[str] = None):
    await interaction.response.defer()
    uid = None
    ticket = None
    if user_id:
        try: uid = int(user_id)
        except: pass
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: uid = ticket.user_id
    if uid: ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        return await interaction.followup.send("❌ No active ticket found.", ephemeral=True)
    role = get_staff_role_name(interaction.user)
    author_str = f"{interaction.user.display_name} ({role})"
    if ticket.ticket_type in (TicketType.DM, TicketType.FORCED):
        try:
            user = await bot.fetch_user(ticket.user_id)
            embed = discord.Embed(title="💬 Support Response", description=message, color=0x00ff00, timestamp=datetime.now(timezone.utc))
            embed.set_author(name=author_str, icon_url=interaction.user.display_avatar.url)
            await user.send(embed=embed)
            log_embed = discord.Embed(description=f"**Staff Reply:**\n{message}", color=0x00ff00)
            ch = bot.get_channel(ticket.channel_id)
            if ch: await ch.send(embed=log_embed)
            await ticket_manager.add_message(ticket.user_id, message, author_str, "staff")
            await interaction.followup.send(f"✅ Reply sent to {user.name}", ephemeral=True)
        except:
            await interaction.followup.send("❌ Cannot DM user.", ephemeral=True)
    elif ticket.ticket_type == TicketType.WEB:
        reply_msg = {'type':'reply', 'user':author_str, 'text':message, 'origin':'discord', 'target':ticket.user_name}
        await broadcast_to_web(reply_msg)
        ch = bot.get_channel(ticket.channel_id)
        if ch: await ch.send(embed=discord.Embed(description=f"**Staff Reply:** {message}", color=0x00ff00))
        await interaction.followup.send(f"✅ Reply sent to web user.", ephemeral=True)

@bot.tree.command(name="tickets")
@is_staff()
async def tickets(interaction: discord.Interaction):
    active = await ticket_manager.get_all_open_tickets()
    if not active:
        return await interaction.response.send_message("📭 No active tickets.", ephemeral=True)
    embed = discord.Embed(title=f"📋 Active Tickets ({len(active)})", color=0x3b82f6)
    for t in active:
        status = "🟢" if t.status == TicketStatus.OPEN else "🟡"
        claimed = t.claimed_by_name or "Unclaimed"
        icon = "🌐" if t.ticket_type == TicketType.WEB else "💬" if t.ticket_type == TicketType.DM else "🔨"
        ch = bot.get_channel(t.channel_id)
        embed.add_field(name=f"{status} {icon} {t.user_name}", value=f"Claimed: {claimed} | Messages: {t.message_count} | Idle: {int(t.idle_seconds()/60)}m\n{ch.mention if ch else 'Unknown'}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ──────────────────────────────────────────────────────────────────────────────
# DM HANDLING (anti-duplicate)
# ──────────────────────────────────────────────────────────────────────────────
_dm_locks = {}
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm(message)
    else:
        await bot.process_commands(message)

async def handle_dm(message):
    uid = message.author.id
    if uid in blacklisted_users:
        return await message.author.send("⛔ You are blacklisted and cannot open tickets.")
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if ticket:
        await ticket_manager.add_message(uid, message.content, message.author.name, "dm")
        ch = bot.get_channel(ticket.channel_id)
        if ch:
            embed = discord.Embed(description=message.content, color=0x3b82f6, timestamp=datetime.now(timezone.utc))
            embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
            await ch.send(embed=embed)
            await message.author.send("✅ Message received by the team!")
        return
    # Anti-duplicate lock
    if uid not in _dm_locks:
        _dm_locks[uid] = asyncio.Lock()
    lock = _dm_locks[uid]
    async with lock:
        # Double-check
        if await ticket_manager.get_ticket_by_user(uid):
            return
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            return await message.author.send("❌ Support server unavailable.")
        category = bot.get_channel(TICKET_CATEGORY_ID) or discord.utils.get(guild.categories, name="SUPPORT TICKETS") or await guild.create_category("SUPPORT TICKETS")
        safe_name = ''.join(c for c in message.author.name.lower().replace(' ','-') if c.isalnum() or c=='-')
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            message.author: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
            if rid and (role := guild.get_role(rid)):
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            channel = await guild.create_text_channel(f"ticket-{safe_name}", category=category, overwrites=overwrites)
        except:
            return await message.author.send("❌ Failed to create ticket. Try again later.")
        ticket = await ticket_manager.create_ticket(uid, message.author.name, channel.id, TicketType.DM)
        if ticket:
            await ticket_manager.add_message(uid, message.content, message.author.name, "dm")
        header = discord.Embed(title="🎫 New Ticket", description=f"**User:** {message.author.mention}\n**ID:** `{uid}`", color=0xef4444)
        await channel.send(embed=header)
        msg_embed = discord.Embed(description=message.content, color=0x3b82f6)
        msg_embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
        await channel.send(embed=msg_embed)
        # Claim button
        class ClaimView(discord.ui.View):
            def __init__(self, uid):
                super().__init__(timeout=None)
                self.uid = uid
            @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.green, emoji="✋")
            async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
                role = get_staff_role_name(interaction.user)
                res = await ticket_manager.claim_ticket(self.uid, interaction.user.id, interaction.user.display_name, role)
                if res:
                    button.disabled = True
                    await interaction.message.edit(view=self)
                    await interaction.response.send_message(f"✅ Claimed!", ephemeral=True)
                    await channel.send(embed=discord.Embed(title="✅ Claimed", description=f"Claimed by {interaction.user.mention} ({role})", color=0x00ff00))
                else:
                    await interaction.response.send_message("❌ Already claimed.", ephemeral=True)
        await channel.send(view=ClaimView(uid))
        await message.author.send("👋 Thanks for reaching out! A support ticket has been created. Our team will assist you shortly. 💙")


# ──────────────────────────────────────────────────────────────────────────────
# ERROR HANDLER
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        try: await interaction.response.send_message("⛔ You don't have permission to use this command.", ephemeral=True)
        except: pass
    else:
        logger.error(f"Command error: {error}")
        try: await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except: pass


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        logger.info("Starting RawrBot v2.0.0...")
        bot.run(TOKEN, log_handler=None)
    except Exception as e:
        logger.critical(f"Startup failed: {e}")
        sys.exit(1)
