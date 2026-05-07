import os
import sys
import asyncio
import logging
import json
import time
import hashlib
import base64
from typing import Optional, Set, Dict, Any, Tuple, List
from datetime import datetime, timezone, timedelta
from collections import deque
from enum import Enum

import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web

# ──────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('RawrBot')

# ──────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION (safe environment parsing)
# ──────────────────────────────────────────────────────────────────────────────
def _env_int(key: str, default: int = 0) -> int:
    """Parse an integer environment variable, with fallback if missing/empty."""
    val = os.getenv(key, '').strip()
    if val == '':
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning(f"Invalid int for {key}: {val!r}, using default {default}")
        return default

TOKEN = os.getenv('BOT_TOKEN', '').strip()
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

GUILD_ID = _env_int('GUILD_ID')
if not GUILD_ID:
    raise ValueError("GUILD_ID environment variable not set")

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

RATE_LIMIT_SECONDS      = 5
MAX_MESSAGES_PER_MINUTE = 12

IDLE_WARN_MINUTES  = _env_int('IDLE_WARN_MINUTES', 30)
IDLE_CLOSE_MINUTES = _env_int('IDLE_CLOSE_MINUTES', 60)

# ──────────────────────────────────────────────────────────────────────────────
#  DATA DIRECTORY (local fallback)
# ──────────────────────────────────────────────────────────────────────────────
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "transcripts"), exist_ok=True)

WHITELIST_FILE  = os.path.join(DATA_DIR, "whitelist.json")
BLACKLIST_FILE  = os.path.join(DATA_DIR, "blacklist.json")
ANALYTICS_FILE  = os.path.join(DATA_DIR, "analytics.json")

# ──────────────────────────────────────────────────────────────────────────────
#  GITHUB STORAGE HELPERS
# ──────────────────────────────────────────────────────────────────────────────
GITHUB_API_BASE = "https://api.github.com"

async def _github_request(method: str, path: str, token: str, data: dict = None):
    """Make an authenticated request to the GitHub REST API."""
    url = f"{GITHUB_API_BASE}/repos/{STORAGE_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with aiohttp.ClientSession() as session:
        if method == "GET":
            async with session.get(url, headers=headers) as resp:
                return resp.status, await resp.json()
        elif method == "PUT":
            headers["Content-Type"] = "application/json"
            async with session.put(url, headers=headers, json=data) as resp:
                return resp.status, await resp.json()

async def _read_github_file(file_path: str) -> Optional[dict]:
    """Read JSON from the Storage repo. Returns None if unavailable."""
    if not STORAGE_TOKEN:
        return None
    status, body = await _github_request("GET", file_path, STORAGE_TOKEN)
    if status == 200:
        content = body.get("content", "")
        if content:
            decoded = base64.b64decode(content).decode("utf-8")
            return json.loads(decoded)
    return None

async def _write_github_file(file_path: str, data: any) -> bool:
    """Write JSON to the Storage repo. Returns True on success."""
    if not STORAGE_TOKEN:
        return False
    # 1. Get current file SHA (needed for update)
    status, body = await _github_request("GET", file_path, STORAGE_TOKEN)
    sha = body.get("sha", None) if status == 200 else None

    # 2. Encode new content
    content_str = json.dumps(data, indent=2, ensure_ascii=False)
    encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

    # 3. PUT the update
    payload = {
        "message": f"Update {file_path}",
        "content": encoded,
        "branch": STORAGE_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    status, _ = await _github_request("PUT", file_path, STORAGE_TOKEN, data=payload)
    ok = status in (200, 201)
    if not ok:
        logger.error(f"Failed to write to GitHub: {file_path} (status {status})")
    return ok


# ──────────────────────────────────────────────────────────────────────────────
#  IN‑MEMORY CACHE FOR WHITELIST / BLACKLIST
# ──────────────────────────────────────────────────────────────────────────────
whitelisted_users: Set[int] = set()
whitelisted_roles: Set[int] = set()
blacklisted_users: Set[int] = set()


# ──────────────────────────────────────────────────────────────────────────────
#  STORAGE CLASS (GitHub primary → local JSON fallback)
# ──────────────────────────────────────────────────────────────────────────────
class Storage:
    @staticmethod
    async def _read_local(path, default):
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return default

    @staticmethod
    async def _write_local(path, data):
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            return False

    # ── Whitelist ──────────────────────────────────────────────────────────
    @staticmethod
    async def add_whitelist_user(user_id, added_by):
        # GitHub first
        data = await _read_github_file("whitelist.json")
        if data is not None:
            data.setdefault("users", [])
            if user_id not in data["users"]:
                data["users"].append(user_id)
                await _write_github_file("whitelist.json", data)
            return True
        # Local fallback
        data = await Storage._read_local(WHITELIST_FILE, {'users': [], 'roles': []})
        if user_id not in data['users']:
            data['users'].append(user_id)
            await Storage._write_local(WHITELIST_FILE, data)
        return True

    @staticmethod
    async def add_whitelist_role(role_id, added_by):
        data = await _read_github_file("whitelist.json")
        if data is not None:
            data.setdefault("roles", [])
            if role_id not in data["roles"]:
                data["roles"].append(role_id)
                await _write_github_file("whitelist.json", data)
            return True
        data = await Storage._read_local(WHITELIST_FILE, {'users': [], 'roles': []})
        if role_id not in data['roles']:
            data['roles'].append(role_id)
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
        data = await Storage._read_local(WHITELIST_FILE, {'users': [], 'roles': []})
        key = 'users' if item_type == 'user' else 'roles'
        if item_id in data[key]:
            data[key].remove(item_id)
            await Storage._write_local(WHITELIST_FILE, data)
        return True

    @staticmethod
    async def get_whitelist():
        data = await _read_github_file("whitelist.json")
        if data is not None:
            return data.get('users', []), data.get('roles', [])
        data = await Storage._read_local(WHITELIST_FILE, {'users': [], 'roles': []})
        return data.get('users', []), data.get('roles', [])

    # ── Blacklist ──────────────────────────────────────────────────────────
    @staticmethod
    async def add_blacklist(user_id, reason, blacklisted_by):
        data = await _read_github_file("blacklist.json")
        if data is not None:
            data.setdefault("users", [])
            if user_id not in data["users"]:
                data["users"].append(user_id)
                await _write_github_file("blacklist.json", data)
            return True
        data = await Storage._read_local(BLACKLIST_FILE, {'users': []})
        if user_id not in data['users']:
            data['users'].append(user_id)
            await Storage._write_local(BLACKLIST_FILE, data)
        return True

    @staticmethod
    async def remove_blacklist(user_id):
        data = await _read_github_file("blacklist.json")
        if data is not None:
            if user_id in data.get("users", []):
                data["users"].remove(user_id)
                await _write_github_file("blacklist.json", data)
            return True
        data = await Storage._read_local(BLACKLIST_FILE, {'users': []})
        if user_id in data['users']:
            data['users'].remove(user_id)
            await Storage._write_local(BLACKLIST_FILE, data)
        return True

    @staticmethod
    async def get_blacklist():
        data = await _read_github_file("blacklist.json")
        if data is not None:
            return data.get('users', [])
        data = await Storage._read_local(BLACKLIST_FILE, {'users': []})
        return data.get('users', [])

    # ── Ticket History ─────────────────────────────────────────────────────
    @staticmethod
    async def save_ticket(ticket_data):
        # Append to chat log array on GitHub
        data = await _read_github_file("Chats/Logging.json")
        if data is not None:
            if isinstance(data, list):
                data.append(ticket_data)
            else:
                data = [ticket_data]
            await _write_github_file("Chats/Logging.json", data)
            return True
        # Local fallback
        path = os.path.join(DATA_DIR, f"history_{ticket_data.get('user_id', 'unknown')}.jsonl")
        try:
            with open(path, 'a') as f:
                f.write(json.dumps(ticket_data) + '\n')
            return True
        except Exception:
            return False

    @staticmethod
    async def get_user_history(user_id):
        data = await _read_github_file("Chats/Logging.json")
        if data is not None and isinstance(data, list):
            return [t for t in data if t.get('user_id') == user_id]
        path = os.path.join(DATA_DIR, f"history_{user_id}.jsonl")
        if not os.path.exists(path):
            return []
        try:
            with open(path, 'r') as f:
                return [json.loads(line) for line in f if line.strip()]
        except Exception:
            return []


async def load_all_data():
    """Load whitelist/blacklist into memory from storage."""
    global whitelisted_users, whitelisted_roles, blacklisted_users
    users, roles = await Storage.get_whitelist()
    whitelisted_users = set(users)
    whitelisted_roles = set(roles)
    blacklisted_users = set(await Storage.get_blacklist())
    logger.info(f"Loaded {len(whitelisted_users)} whitelisted users, "
                f"{len(whitelisted_roles)} roles, {len(blacklisted_users)} blacklisted")


# ──────────────────────────────────────────────────────────────────────────────
#  TICKET SYSTEM
# ──────────────────────────────────────────────────────────────────────────────
class TicketStatus(Enum):
    OPEN    = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    CLOSED  = "closed"

class TicketType(Enum):
    WEB    = "web"
    DM     = "dm"
    FORCED = "forced"

class Ticket:
    def __init__(self, user_id, user_name, channel_id, ticket_type, created_by=None):
        self.user_id        = user_id
        self.user_name      = user_name
        self.channel_id     = channel_id
        self.ticket_type    = ticket_type
        self.status         = TicketStatus.OPEN
        self.claimed_by     = None
        self.claimed_by_name = None
        self.claimed_by_role = None
        self.created_at     = datetime.now(timezone.utc)
        self.claimed_at     = None
        self.resolved_at    = None
        self.closed_at      = None
        self.last_activity  = self.created_at
        self.idle_warned    = False
        self.message_count  = 0
        self.created_by     = created_by
        self.resolved_by    = None
        self.resolved_reason = None
        self.resolved_by_role = None
        self.closed_by      = None
        self.closed_reason  = None
        self.review_sent    = False
        self.history: List[Dict] = []
        self.additional_users: List[int] = []
        self.transcript_log: List[Dict] = []

    def touch(self):
        self.last_activity = datetime.now(timezone.utc)
        self.idle_warned   = False

    def idle_seconds(self):
        return (datetime.now(timezone.utc) - self.last_activity).total_seconds()


class TicketManager:
    def __init__(self):
        self.tickets: Dict[int, Ticket] = {}               # user_id -> Ticket
        self.channel_to_user: Dict[int, int] = {}          # channel_id -> user_id
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
            logger.info(f"Ticket created: #{channel_id} for {user_name}")
            return ticket

    async def get_ticket_by_user(self, user_id):
        async with self.lock:
            return self.tickets.get(user_id)

    async def get_ticket_by_channel(self, channel_id):
        async with self.lock:
            uid = self.channel_to_user.get(channel_id)
            return self.tickets.get(uid) if uid else None

    async def add_user_to_ticket(self, user_id, new_user_id):
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and new_user_id not in ticket.additional_users:
                ticket.additional_users.append(new_user_id)
                return True
            return False

    async def remove_user_from_ticket(self, user_id, remove_user_id):
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and remove_user_id in ticket.additional_users:
                ticket.additional_users.remove(remove_user_id)
                return True
            return False

    async def claim_ticket(self, user_id, staff_id, staff_name, staff_role):
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status == TicketStatus.OPEN:
                ticket.status          = TicketStatus.CLAIMED
                ticket.claimed_by      = staff_id
                ticket.claimed_by_name = staff_name
                ticket.claimed_by_role = staff_role
                ticket.claimed_at      = datetime.now(timezone.utc)
                ticket.touch()
                ticket.history.append({
                    'action': 'claimed', 'by': staff_name, 'role': staff_role,
                    'at': ticket.claimed_at.isoformat()
                })
                logger.info(f"Ticket #{ticket.channel_id} claimed by {staff_name}")
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
                ticket.history.append({
                    'action': 'resolved', 'by': resolved_by,
                    'role': resolved_by_role, 'reason': reason,
                    'at': ticket.resolved_at.isoformat()
                })
                logger.info(f"Ticket #{ticket.channel_id} resolved by {resolved_by}")
                return ticket
            return None

    async def close_ticket(self, user_id, channel_id=None, closed_by=None, reason=None):
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                if channel_id:
                    self.channel_to_user.pop(channel_id, None)
                ticket.status      = TicketStatus.CLOSED
                ticket.closed_at   = datetime.now(timezone.utc)
                ticket.closed_by   = closed_by
                ticket.closed_reason = reason
                ticket.history.append({
                    'action': 'closed', 'by': closed_by, 'reason': reason,
                    'at': ticket.closed_at.isoformat()
                })
                logger.info(f"Ticket #{ticket.channel_id} closed")
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
            return [t for t in self.tickets.values()
                    if t.status in (TicketStatus.OPEN, TicketStatus.CLAIMED)]

    async def get_idle_tickets(self):
        async with self.lock:
            return [t for t in self.tickets.values()
                    if t.status in (TicketStatus.OPEN, TicketStatus.CLAIMED)]

    async def get_stats(self):
        async with self.lock:
            return {
                'total':    len(self.tickets),
                'open':     sum(1 for t in self.tickets.values() if t.status == TicketStatus.OPEN),
                'claimed':  sum(1 for t in self.tickets.values() if t.status == TicketStatus.CLAIMED),
                'resolved': sum(1 for t in self.tickets.values() if t.status == TicketStatus.RESOLVED),
                'handled':  self.handled_count,
            }

ticket_manager = TicketManager()


# ──────────────────────────────────────────────────────────────────────────────
#  TRANSCRIPT GENERATOR
# ──────────────────────────────────────────────────────────────────────────────
def generate_transcript_html(ticket):
    rows = ""
    for entry in ticket.transcript_log:
        ts = entry.get("ts", "")
        author = entry.get("author", "Unknown")
        content = entry.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
        origin = entry.get("origin", "")
        badge = "🌐 Web" if origin == "web" else ("🤖 Bot" if origin == "bot" else "💬 DM")
        rows += f"""<div class="message">
          <span class="ts">{ts[:19].replace('T',' ')}</span>
          <span class="badge">{badge}</span>
          <span class="author">{author}</span>
          <span class="content">{content}</span>
        </div>"""
    duration = ""
    if ticket.closed_at and ticket.created_at:
        secs = int((ticket.closed_at - ticket.created_at).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        duration = f"{h}h {m}m {s}s"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Transcript – {ticket.user_name}</title>
<style>
  :root {{ --bg:#0d1117; --surface:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#ef4444; --green:#3fb950; --blue:#58a6ff; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:system-ui, sans-serif; padding:2rem; line-height:1.5; }}
  header {{ border-bottom:1px solid var(--border); padding-bottom:1rem; margin-bottom:1.5rem; }}
  header h1 {{ color:var(--accent); font-size:1.4rem; }}
  .meta {{ display:flex; gap:2rem; flex-wrap:wrap; font-size:.85rem; color:var(--muted); margin-top:.5rem; }}
  .meta span strong {{ color:var(--text); }}
  .log {{ display:flex; flex-direction:column; gap:.5rem; }}
  .message {{ background:var(--surface); border:1px solid var(--border); border-radius:6px; padding:.6rem .9rem; display:grid; grid-template-columns:9rem 4rem 10rem 1fr; gap:.5rem; align-items:baseline; font-size:.875rem; }}
  .ts {{ color:var(--muted); font-size:.75rem; }}
  .badge {{ font-size:.7rem; }}
  .author {{ font-weight:600; color:var(--blue); }}
  .content {{ word-break:break-word; white-space:pre-wrap; }}
  footer {{ margin-top:2rem; font-size:.75rem; color:var(--muted); border-top:1px solid var(--border); padding-top:1rem; }}
</style></head>
<body>
  <header><h1>🎫 Ticket Transcript</h1>
    <div class="meta">
      <span><strong>User:</strong> {ticket.user_name} ({ticket.user_id})</span>
      <span><strong>Type:</strong> {ticket.ticket_type.value}</span>
      <span><strong>Created:</strong> {ticket.created_at.strftime('%Y-%m-%d %H:%M UTC')}</span>
      <span><strong>Closed:</strong> {ticket.closed_at.strftime('%Y-%m-%d %H:%M UTC') if ticket.closed_at else 'N/A'}</span>
      <span><strong>Duration:</strong> {duration}</span>
      <span><strong>Messages:</strong> {ticket.message_count}</span>
      <span><strong>Handled by:</strong> {ticket.claimed_by_name or 'Unclaimed'}</span>
      <span><strong>Resolution:</strong> {ticket.resolved_reason or 'N/A'}</span>
    </div>
  </header>
  <div class="log">{rows if rows else '<p style="color:var(--muted)">No messages recorded.</p>'}</div>
  <footer>Generated by RawrBot &bull; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</footer>
</body></html>"""

def save_transcript(ticket):
    filename = f"ticket_{ticket.channel_id}_{ticket.user_id}.html"
    path = os.path.join(DATA_DIR, "transcripts", filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(generate_transcript_html(ticket))
    return path


# ──────────────────────────────────────────────────────────────────────────────
#  RATE LIMITER
# ──────────────────────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.user_messages: Dict[int, deque] = {}
    def can_send(self, user_id, content):
        now = time.time()
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=MAX_MESSAGES_PER_MINUTE)
        dq = self.user_messages[user_id]
        if len(dq) >= MAX_MESSAGES_PER_MINUTE:
            if now - dq[0] < 60:
                return False, "You're sending messages too quickly. Please wait a minute."
        if dq:
            if now - dq[-1] < RATE_LIMIT_SECONDS:
                return False, f"Slow down a little – wait {RATE_LIMIT_SECONDS - (now - dq[-1]):.0f} seconds."
        return True, "OK"
    def record(self, user_id):
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=MAX_MESSAGES_PER_MINUTE)
        self.user_messages[user_id].append(time.time())

rate_limiter = RateLimiter()


# ──────────────────────────────────────────────────────────────────────────────
#  WEB CHAT / SSE
# ──────────────────────────────────────────────────────────────────────────────
message_queue: deque = deque(maxlen=100)
sse_clients: Set = set()
web_user_channels: Dict[int, int] = {}   # web_user_id → channel_id

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
        except Exception:
            dead.add(client)
    for client in dead:
        sse_clients.discard(client)

async def forward_to_discord(message):
    if not TICKET_CATEGORY_ID or not bot._ready:
        return
    guild = bot.get_guild(GUILD_ID)
    category = bot.get_channel(TICKET_CATEGORY_ID)
    if not guild or not category:
        return

    user_name = message.get('user', 'Guest')
    web_user_id = hash("web_" + user_name)  # deterministic

    channel = None
    channel_id = web_user_channels.get(web_user_id)
    if channel_id:
        channel = bot.get_channel(channel_id)

    if not channel:
        channel_name = f"web-{user_name.lower().replace(' ', '-')}"
        channel = discord.utils.get(category.text_channels, name=channel_name)
        if not channel:
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
                }
                for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
                    if rid:
                        role = guild.get_role(rid)
                        if role:
                            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                channel = await guild.create_text_channel(
                    channel_name, category=category, overwrites=overwrites,
                    reason=f"Web chat from {user_name}"
                )
                await ticket_manager.create_ticket(web_user_id, user_name, channel.id, TicketType.WEB)
                await channel.send(f"📬 **Web chat started** – `{user_name}`\nUse `/reply` to respond.")
                web_user_channels[web_user_id] = channel.id
            except Exception as e:
                logger.error(f"Failed to create web channel: {e}")
                return
        else:
            web_user_channels[web_user_id] = channel.id
            if not await ticket_manager.get_ticket_by_user(web_user_id):
                await ticket_manager.create_ticket(web_user_id, user_name, channel.id, TicketType.WEB)

    embed = discord.Embed(description=message['text'], color=0x3b82f6, timestamp=datetime.now(timezone.utc))
    embed.set_author(name=f"🌐 Web: {user_name}")
    await channel.send(embed=embed)


# ──────────────────────────────────────────────────────────────────────────────
#  HTTP SERVER
# ──────────────────────────────────────────────────────────────────────────────
async def sse_endpoint(request):
    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Access-Control-Allow-Origin': '*',
        'Connection': 'keep-alive',
    }
    response = web.StreamResponse(headers=headers)
    await response.prepare(request)
    sse_clients.add(response)
    for msg in list(message_queue)[-10:]:
        await response.write(f"data: {json.dumps(msg)}\n\n".encode())
    try:
        while True:
            await asyncio.sleep(30)
            await response.write(b': heartbeat\n\n')
    except Exception:
        pass
    finally:
        sse_clients.discard(response)
    return response

async def send_message_endpoint(request):
    try:
        data = await request.json()
        user_name = data.get('user', 'unknown')
        can_send, error = rate_limiter.can_send(hash(user_name), data.get('text', ''))
        if not can_send:
            return web.json_response({'error': error}, status=429)
        rate_limiter.record(hash(user_name))
        message = {'type': 'chat', 'user': user_name, 'text': data.get('text', ''), 'origin': 'web'}
        await broadcast_to_web(message)
        await forward_to_discord(message)
        return web.json_response({'status': 'ok'})
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def health_check(request):
    stats = await ticket_manager.get_stats()
    return web.json_response({
        'status': 'alive',
        'clients': len(sse_clients),
        'messages': len(message_queue),
        'tickets': stats,
    })


# ──────────────────────────────────────────────────────────────────────────────
#  BOT CLASS
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
        self._ready = False
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await load_all_data()

        self.web_app = web.Application()
        self.web_app.router.add_get('/events', sse_endpoint)
        self.web_app.router.add_post('/send', send_message_endpoint)
        self.web_app.router.add_get('/health', health_check)
        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', PORT).start()
        logger.info(f"HTTP server started on port {PORT}")

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("Bot setup complete – commands synced.")

    async def on_ready(self):
        self._ready = True
        logger.info(f"Logged in as {self.user.name} ({self.user.id})")
        self.status_task.start()
        self.idle_check_task.start()

    async def close(self):
        for task in [self.status_task, self.idle_check_task]:
            if task.is_running():
                task.cancel()
        if self.runner:
            await self.runner.cleanup()
        await super().close()

    @tasks.loop(seconds=30)
    async def status_task(self):
        if not self._ready:
            return
        try:
            stats = await ticket_manager.get_stats()
            await self.change_presence(activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{stats['open']} open tickets | rawrs.zapto.org"
            ))
        except Exception:
            pass

    @tasks.loop(minutes=5)
    async def idle_check_task(self):
        if not self._ready:
            return
        tickets = await ticket_manager.get_idle_tickets()
        for ticket in tickets:
            idle_min = ticket.idle_seconds() / 60
            channel = self.get_channel(ticket.channel_id)
            if not channel:
                continue
            if idle_min >= IDLE_WARN_MINUTES and not ticket.idle_warned:
                ticket.idle_warned = True
                try:
                    await channel.send(embed=discord.Embed(
                        title="⏳ Still need help?",
                        description=f"This ticket has been quiet for **{int(idle_min)} minutes**. "
                                    f"If you don't reply in the next **{IDLE_CLOSE_MINUTES - IDLE_WARN_MINUTES} minutes** it will be closed automatically.",
                        color=0xeab308
                    ))
                except Exception:
                    pass
            if idle_min >= IDLE_CLOSE_MINUTES:
                logger.info(f"Auto-closing idle ticket #{ticket.channel_id}")
                await close_ticket_by_user(ticket.user_id, channel, "Auto-Close", "Idle timeout")

bot = RawrBot()


# ──────────────────────────────────────────────────────────────────────────────
#  PERMISSION HELPERS (no crash on expired interaction)
# ──────────────────────────────────────────────────────────────────────────────
def is_whitelisted():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        if interaction.user.id in whitelisted_users:
            return True
        if {r.id for r in interaction.user.roles} & whitelisted_roles:
            return True
        # Silently deny – interaction may have expired
        return False
    return app_commands.check(predicate)

def is_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        return bool({r.id for r in interaction.user.roles} & {STAFF_ROLE_ID, MANAGER_ROLE_ID, MODERATOR_ROLE_ID})
    return app_commands.check(predicate)

def is_manager():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        return MANAGER_ROLE_ID in {r.id for r in interaction.user.roles}
    return app_commands.check(predicate)

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID
    return app_commands.check(predicate)

def is_moderator():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        return bool({r.id for r in interaction.user.roles} & {MODERATOR_ROLE_ID, MANAGER_ROLE_ID})
    return app_commands.check(predicate)

def get_staff_role_name(member):
    if member.id == OWNER_ID:
        return "👑 Owner"
    roles = {r.id for r in member.roles}
    if MANAGER_ROLE_ID in roles:   return "⭐ Server Manager"
    if MODERATOR_ROLE_ID in roles: return "🛡️ Moderator"
    if STAFF_ROLE_ID in roles:     return "📞 Support Staff"
    return "👤 Staff Member"


# ──────────────────────────────────────────────────────────────────────────────
#  AUTOCOMPLETE HELPERS
# ──────────────────────────────────────────────────────────────────────────────
async def active_ticket_user_ids(interaction, current):
    tickets = await ticket_manager.get_all_open_tickets()
    choices = []
    for t in tickets:
        label = f"{t.user_name} ({t.user_id})"
        if current.lower() in label.lower() or not current:
            choices.append(app_commands.Choice(name=label, value=str(t.user_id)))
        if len(choices) >= 25:
            break
    return choices

async def ticket_type_autocomplete(interaction, current):
    return [app_commands.Choice(name=o, value=o) for o in ["web", "dm", "forced"] if current.lower() in o]


# ──────────────────────────────────────────────────────────────────────────────
#  COMMANDS (all original, rewritten for better human tone)
# ──────────────────────────────────────────────────────────────────────────────
@bot.tree.command(name="embed")
@is_whitelisted()
@is_staff()
async def embed_command(interaction, title: str, description: str, color: Optional[str] = None, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    color_map = {'red': 0xef4444, 'green': 0x22c55e, 'blue': 0x3b82f6, 'yellow': 0xeab308, 'purple': 0xa855f7, 'orange': 0xf97316, 'pink': 0xec4899}
    embed = discord.Embed(title=title, description=description, color=color_map.get((color or '').lower(), 0xef4444), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Sent by {interaction.user.display_name}")
    await target.send(embed=embed)
    await interaction.response.send_message(f"✅ Embed sent to {target.mention}", ephemeral=True)

@bot.tree.command(name="message")
@is_whitelisted()
@is_staff()
async def message_command(interaction, user: discord.User, message: str):
    try:
        embed = discord.Embed(title="📨 Message from Staff", description=message, color=0x3b82f6, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="rawr.xyz Support Team")
        await user.send(embed=embed)
        await interaction.response.send_message(f"✅ Message sent to {user.mention}", ephemeral=True)
        logger.info(f"Staff {interaction.user.name} sent DM to {user.name}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I can't send them a DM – they may have DMs disabled.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="ban")
@is_whitelisted()
@is_moderator()
async def ban_command(interaction, user: discord.User, reason: Optional[str] = None, delete_days: int = 7):
    if user.id in (OWNER_ID, interaction.user.id):
        await interaction.response.send_message("❌ I can't ban that user.", ephemeral=True)
        return
    try:
        await interaction.guild.ban(user, reason=reason or "No reason provided", delete_message_days=delete_days)
        embed = discord.Embed(title="🔨 User Banned", description=f"**User:** {user.mention}\n**ID:** {user.id}\n**Reason:** {reason or 'No reason provided'}\n**Deleted Messages:** {delete_days}d", color=0xef4444, timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"Banned by {interaction.user.name}")
        await interaction.response.send_message(embed=embed)
        try: await user.send(f"🔨 You've been banned from **{interaction.guild.name}**.\nReason: {reason or 'No reason provided'}")
        except: pass
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to ban that user.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="kick")
@is_whitelisted()
@is_moderator()
async def kick_command(interaction, user: discord.User, reason: Optional[str] = None):
    if user.id in (OWNER_ID, interaction.user.id):
        await interaction.response.send_message("❌ I can't kick that user.", ephemeral=True)
        return
    try:
        member = interaction.guild.get_member(user.id)
        if member: await member.kick(reason=reason or "No reason provided")
        embed = discord.Embed(title="👢 User Kicked", description=f"**User:** {user.mention}\n**ID:** {user.id}\n**Reason:** {reason or 'No reason provided'}", color=0xf97316, timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"Kicked by {interaction.user.name}")
        await interaction.response.send_message(embed=embed)
        try: await user.send(f"👢 You've been kicked from **{interaction.guild.name}**.\nReason: {reason or 'No reason provided'}")
        except: pass
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to kick that user.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="timeout")
@is_whitelisted()
@is_moderator()
async def timeout_command(interaction, user: discord.User, duration: str, reason: Optional[str] = None):
    if user.id in (OWNER_ID, interaction.user.id):
        await interaction.response.send_message("❌ I can't timeout that user.", ephemeral=True)
        return
    duration_map = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    try:
        amount = int(duration[:-1])
        unit = duration[-1].lower()
        seconds = amount * duration_map[unit]
        if seconds > 2419200:
            await interaction.response.send_message("❌ Timeout cannot exceed 28 days.", ephemeral=True)
            return
        member = interaction.guild.get_member(user.id)
        if not member:
            await interaction.response.send_message("❌ User not found in this server.", ephemeral=True)
            return
        await member.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=reason or "No reason provided")
        embed = discord.Embed(title="⏰ User Timed Out", description=f"**User:** {user.mention}\n**Duration:** {duration}\n**Reason:** {reason or 'No reason provided'}", color=0xeab308, timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=f"By {interaction.user.name}")
        await interaction.response.send_message(embed=embed)
    except (ValueError, KeyError):
        await interaction.response.send_message("❌ Incorrect format. Try something like `1h`, `30m`, `1d`", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="warn")
@is_whitelisted()
@is_staff()
async def warn_command(interaction, user: discord.User, reason: str):
    embed = discord.Embed(title="⚠️ Warning Issued", description=f"**User:** {user.mention}\n**Reason:** {reason}", color=0xeab308, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"By {interaction.user.name}")
    await interaction.response.send_message(embed=embed)
    try:
        dm = discord.Embed(title="⚠️ You've been warned", description=f"**Server:** {interaction.guild.name}\n**Reason:** {reason}\n**By:** {interaction.user.name}", color=0xeab308, timestamp=datetime.now(timezone.utc))
        await user.send(embed=dm)
    except: pass

@bot.tree.command(name="adduser")
@is_whitelisted()
@is_staff()
async def adduser_command(interaction: discord.Interaction, user: discord.User):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.response.send_message("❌ This doesn't seem to be a ticket channel.", ephemeral=True)
        return
    await interaction.channel.set_permissions(user, read_messages=True, send_messages=True)
    ok = await ticket_manager.add_user_to_ticket(ticket.user_id, user.id)
    if ok:
        embed = discord.Embed(title="👤 User Added", description=f"{user.mention} has been added to this ticket.", color=0x00ff00, timestamp=datetime.now(timezone.utc))
        await interaction.response.send_message(embed=embed)
        try: await user.send(f"📋 You've been invited to a support ticket in {interaction.guild.name}.")
        except: pass
    else:
        await interaction.response.send_message("❌ Couldn't add that user – maybe they're already in the ticket.", ephemeral=True)

@bot.tree.command(name="removeuser")
@is_whitelisted()
@is_manager()
async def removeuser_command(interaction: discord.Interaction, user: discord.User):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.response.send_message("❌ This isn't a ticket channel.", ephemeral=True)
        return
    if user.id == ticket.user_id:
        await interaction.response.send_message("❌ You can't remove the ticket owner.", ephemeral=True)
        return
    await interaction.channel.set_permissions(user, overwrite=None)
    ok = await ticket_manager.remove_user_from_ticket(ticket.user_id, user.id)
    if ok:
        embed = discord.Embed(title="👤 User Removed", description=f"{user.mention} has been removed from this ticket.", color=0xffaa00, timestamp=datetime.now(timezone.utc))
        await interaction.response.send_message(embed=embed)
        try: await user.send(f"📋 You've been removed from a support ticket in {interaction.guild.name}.")
        except: pass
    else:
        await interaction.response.send_message("❌ Couldn't remove that user.", ephemeral=True)

@bot.tree.command(name="giverole")
@is_owner()
async def giverole_command(interaction: discord.Interaction, user: discord.User, role: discord.Role):
    member = interaction.guild.get_member(user.id)
    if not member:
        await interaction.response.send_message("❌ That user isn't in this server.", ephemeral=True)
        return
    try:
        await member.add_roles(role, reason=f"Given by {interaction.user.name}")
        await interaction.response.send_message(f"✅ Gave {role.mention} to {user.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="removerole")
@is_owner()
async def removerole_command(interaction: discord.Interaction, user: discord.User, role: discord.Role):
    member = interaction.guild.get_member(user.id)
    if not member:
        await interaction.response.send_message("❌ That user isn't in this server.", ephemeral=True)
        return
    try:
        await member.remove_roles(role, reason=f"Removed by {interaction.user.name}")
        await interaction.response.send_message(f"✅ Removed {role.mention} from {user.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="broadcast")
@is_owner()
async def broadcast_command(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)
    staff_members = set()
    for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID, MODERATOR_ROLE_ID]:
        if rid:
            role = interaction.guild.get_role(rid)
            if role: staff_members.update(role.members)
    embed = discord.Embed(title="📢 Staff Announcement", description=message, color=0xef4444, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Sent by {interaction.user.name}")
    count = 0
    for member in staff_members:
        try:
            await member.send(embed=embed)
            count += 1
            await asyncio.sleep(0.5)
        except: pass
    await interaction.followup.send(f"✅ Broadcast sent to {count} staff members.", ephemeral=True)

@bot.tree.command(name="serverinfo")
@is_owner()
async def serverinfo_command(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=f"📊 {guild.name} – Server Info", color=0x3b82f6)
    if guild.icon: embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner",       value=guild.owner.mention if guild.owner else "Unknown", inline=True)
    embed.add_field(name="Created",     value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Members",     value=guild.member_count, inline=True)
    embed.add_field(name="Channels",    value=len(guild.channels), inline=True)
    embed.add_field(name="Roles",       value=len(guild.roles), inline=True)
    embed.add_field(name="Boost Level", value=guild.premium_tier, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="purge")
@is_owner()
async def purge_command(interaction: discord.Interaction, amount: int, channel: Optional[discord.TextChannel] = None):
    if amount > 100:
        await interaction.response.send_message("❌ I can only delete up to 100 messages at once.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    try:
        deleted = await target.purge(limit=amount)
        await interaction.followup.send(f"✅ Deleted {len(deleted)} messages in {target.mention}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ {str(e)[:100]}", ephemeral=True)

# Whitelist / Blacklist commands (unchanged internally, just nicer messages)
@bot.tree.command(name="whitelist_add")
@is_whitelisted()
async def whitelist_add(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    if user:
        await Storage.add_whitelist_user(user.id, interaction.user.name)
        whitelisted_users.add(user.id)
        await interaction.response.send_message(f"✅ {user.mention} has been added to the whitelist.", ephemeral=True)
    elif role:
        await Storage.add_whitelist_role(role.id, interaction.user.name)
        whitelisted_roles.add(role.id)
        await interaction.response.send_message(f"✅ The {role.mention} role has been added to the whitelist.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Please specify a user or a role.", ephemeral=True)

@bot.tree.command(name="whitelist_remove")
@is_whitelisted()
async def whitelist_remove(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    if user:
        await Storage.remove_whitelist(user.id, 'user')
        whitelisted_users.discard(user.id)
        await interaction.response.send_message(f"✅ {user.mention} has been removed from the whitelist.", ephemeral=True)
    elif role:
        await Storage.remove_whitelist(role.id, 'role')
        whitelisted_roles.discard(role.id)
        await interaction.response.send_message(f"✅ The {role.mention} role has been removed from the whitelist.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Please specify a user or a role.", ephemeral=True)

@bot.tree.command(name="whitelist_list")
@is_whitelisted()
async def whitelist_list(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 Whitelist", color=0x00ff00)
    users_list = []
    for uid in whitelisted_users:
        try:
            u = await bot.fetch_user(uid)
            users_list.append(f"{u.name} ({uid})")
        except: users_list.append(f"Unknown ({uid})")
    roles_list = []
    for rid in whitelisted_roles:
        r = interaction.guild.get_role(rid)
        roles_list.append(f"{r.name} ({rid})" if r else f"Unknown ({rid})")
    embed.add_field(name="👤 Users", value="\n".join(users_list) or "None", inline=False)
    embed.add_field(name="🎭 Roles", value="\n".join(roles_list) or "None", inline=False)
    embed.set_footer(text=f"{len(whitelisted_users)} users, {len(whitelisted_roles)} roles")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="blacklist")
@is_whitelisted()
@is_staff()
async def blacklist_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id == OWNER_ID:
        await interaction.response.send_message("❌ I can't blacklist the owner.", ephemeral=True)
        return
    await Storage.add_blacklist(user.id, reason or "No reason provided", interaction.user.name)
    blacklisted_users.add(user.id)
    embed = discord.Embed(title="🚫 User Blacklisted", description=f"**User:** {user.mention}\n**Reason:** {reason or 'No reason provided'}", color=0xef4444, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"By {interaction.user.name}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unblacklist")
@is_whitelisted()
@is_staff()
async def unblacklist_command(interaction: discord.Interaction, user: discord.User):
    if user.id not in blacklisted_users:
        await interaction.response.send_message(f"❌ {user.mention} isn't blacklisted.", ephemeral=True)
        return
    await Storage.remove_blacklist(user.id)
    blacklisted_users.discard(user.id)
    embed = discord.Embed(title="✅ User Unblacklisted", description=f"**User:** {user.mention}", color=0x00ff00, timestamp=datetime.now(timezone.utc))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="blacklist_list")
@is_whitelisted()
@is_staff()
async def blacklist_list(interaction: discord.Interaction):
    if not blacklisted_users:
        await interaction.response.send_message("📭 No one is currently blacklisted.", ephemeral=True)
        return
    embed = discord.Embed(title="🚫 Blacklisted Users", color=0xef4444)
    lines = []
    for uid in blacklisted_users:
        try:
            u = await bot.fetch_user(uid)
            lines.append(f"• {u.name} ({uid})")
        except: lines.append(f"• Unknown ({uid})")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Total: {len(blacklisted_users)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="force_ticket")
@is_whitelisted()
@is_staff()
async def force_ticket_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id in blacklisted_users:
        await interaction.response.send_message(f"❌ {user.mention} is blacklisted and can't receive support.", ephemeral=True)
        return
    if await ticket_manager.get_ticket_by_user(user.id):
        await interaction.response.send_message(f"❌ {user.mention} already has an open ticket.", ephemeral=True)
        return
    await interaction.response.defer()
    guild = interaction.guild
    category = bot.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    if not category:
        category = discord.utils.get(guild.categories, name="SUPPORT TICKETS") or await guild.create_category("SUPPORT TICKETS")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
        user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
        if rid:
            role = guild.get_role(rid)
            if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    channel = await guild.create_text_channel(f"forced-{user.name.lower().replace(' ','-')}", category=category, overwrites=overwrites)
    await ticket_manager.create_ticket(user.id, user.name, channel.id, TicketType.FORCED, interaction.user.name)
    embed = discord.Embed(title="🎫 Force Ticket Created", description=f"**User:** {user.mention}\n**By:** {interaction.user.mention}\n**Reason:** {reason or 'No reason provided'}", color=0xffaa00, timestamp=datetime.now(timezone.utc))
    await channel.send(embed=embed)
    try: await user.send("👋 A staff member has opened a support ticket for you. You can reply here or in the server.")
    except: pass
    await interaction.followup.send(f"✅ Force ticket created for {user.mention} in {channel.mention}")

@bot.tree.command(name="force_transfer")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def force_transfer_command(interaction: discord.Interaction, user_id: str, new_staff: discord.Member):
    try: uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True); return
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found for that user.", ephemeral=True); return
    new_role = get_staff_role_name(new_staff)
    result = await ticket_manager.force_transfer(uid, new_staff.id, new_staff.display_name, new_role)
    if result:
        embed = discord.Embed(title="🔄 Ticket Force Transferred", description=f"**User:** {ticket.user_name}\n**New Staff:** {new_staff.mention} ({new_role})\n**By:** {interaction.user.mention}", color=0xffaa00, timestamp=datetime.now(timezone.utc))
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        try: await new_staff.send(f"📋 You've been assigned to ticket for {ticket.user_name}.")
        except: pass
        await interaction.response.send_message(f"✅ Ticket transferred to {new_staff.mention}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Transfer failed.", ephemeral=True)

@bot.tree.command(name="history")
@is_whitelisted()
@is_staff()
async def history_command(interaction: discord.Interaction, user: discord.User):
    history = await Storage.get_user_history(user.id)
    embed = discord.Embed(title=f"📜 History for {user.name}", description=f"**ID:** {user.id}\n**Blacklisted:** {'Yes' if user.id in blacklisted_users else 'No'}", color=0x3b82f6)
    if not history:
        embed.add_field(name="No tickets", value="This user hasn't opened any tickets yet.", inline=False)
    else:
        for idx, t in enumerate(reversed(history[-10:]), 1):
            created = datetime.fromisoformat(t['created_at']).strftime("%Y-%m-%d %H:%M")
            val = f"**Type:** {t.get('ticket_type','?').upper()}\n**Status:** {t.get('status','?')}\n**Created:** {created}\n**Messages:** {t.get('message_count',0)}"
            if t.get('claimed_by_name'): val += f"\n**Handled by:** {t['claimed_by_name']}"
            if t.get('resolved_by'):     val += f"\n**Resolved by:** {t['resolved_by']}"
            embed.add_field(name=f"Ticket #{idx}", value=val, inline=False)
    embed.set_footer(text=f"Total tickets: {len(history)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reply")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def reply_command(interaction: discord.Interaction, message: str, user_id: Optional[str] = None):
    await interaction.response.defer()
    ticket = None
    target = None
    if user_id:
        try: target = int(user_id)
        except ValueError: pass
    if target:
        ticket = await ticket_manager.get_ticket_by_user(target)
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: target = ticket.user_id
    if not ticket or not target:
        await interaction.followup.send("❌ Couldn't find that ticket. Make sure it's open and active.", ephemeral=True)
        return
    staff_role = get_staff_role_name(interaction.user)
    author_str = f"{interaction.user.display_name} ({staff_role})"
    if ticket.ticket_type in (TicketType.DM, TicketType.FORCED):
        try:
            user = await bot.fetch_user(target)
            embed = discord.Embed(title="💬 Support Response", description=message, color=0x00ff00, timestamp=datetime.now(timezone.utc))
            embed.set_author(name=author_str, icon_url=interaction.user.display_avatar.url)
            embed.set_footer(text="Reply here to continue the conversation")
            await user.send(embed=embed)
            log_embed = discord.Embed(description=f"**Staff Reply ({staff_role}):**\n{message}", color=0x00ff00, timestamp=datetime.now(timezone.utc))
            channel = bot.get_channel(ticket.channel_id)
            if channel: await channel.send(embed=log_embed)
            await ticket_manager.add_message(target, message, author_str, "staff")
            await interaction.followup.send(f"✅ Reply sent to {user.name}")
        except discord.Forbidden:
            await interaction.followup.send("❌ I can't DM that user – they may have DMs disabled.")
        except Exception as e:
            await interaction.followup.send(f"❌ {str(e)[:100]}")
    elif ticket.ticket_type == TicketType.WEB:
        reply_msg = {'type': 'reply', 'user': author_str, 'text': message, 'origin': 'discord', 'target': ticket.user_name}
        await broadcast_to_web(reply_msg)
        embed = discord.Embed(description=message, color=0x00ff00, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=f"💬 Staff Reply ({staff_role}) → {ticket.user_name}")
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        await ticket_manager.add_message(target, message, author_str, "staff-web")
        await interaction.followup.send(f"✅ Reply sent to web user {ticket.user_name}")

@bot.tree.command(name="claim")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def claim_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    target = None
    ticket = None
    if user_id:
        try: target = int(user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True); return
        ticket = await ticket_manager.get_ticket_by_user(target)
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: target = ticket.user_id
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found here.", ephemeral=True); return
    if ticket.status == TicketStatus.CLAIMED:
        await interaction.response.send_message(f"❌ This ticket is already claimed by {ticket.claimed_by_name}.", ephemeral=True); return
    staff_role = get_staff_role_name(interaction.user)
    claimed = await ticket_manager.claim_ticket(target, interaction.user.id, interaction.user.display_name, staff_role)
    if claimed:
        embed = discord.Embed(title="✅ Ticket Claimed", description=f"**User:** {ticket.user_name}\n**Claimed by:** {interaction.user.mention} ({staff_role})", color=0x00ff00, timestamp=datetime.now(timezone.utc))
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        if ticket.ticket_type in (TicketType.DM, TicketType.FORCED):
            try: await (await bot.fetch_user(target)).send(f"👋 {interaction.user.display_name} ({staff_role}) is now handling your ticket.")
            except: pass
        await interaction.response.send_message(f"✅ You've claimed the ticket for {ticket.user_name}.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Couldn't claim that ticket.", ephemeral=True)

@bot.tree.command(name="resolve")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def resolve_command(interaction: discord.Interaction, reason: str = "Issue resolved", user_id: Optional[str] = None):
    target = None
    ticket = None
    if user_id:
        try: target = int(user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True); return
        ticket = await ticket_manager.get_ticket_by_user(target)
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: target = ticket.user_id
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True); return
    staff_role = get_staff_role_name(interaction.user)
    resolved = await ticket_manager.resolve_ticket(target, interaction.user.display_name, staff_role, reason)
    if resolved:
        embed = discord.Embed(title="✅ Ticket Resolved", description=f"**User:** {ticket.user_name}\n**Reason:** {reason}\n**By:** {interaction.user.mention} ({staff_role})\n\nThis channel will close in 30 seconds.", color=0xffaa00, timestamp=datetime.now(timezone.utc))
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        if ticket.ticket_type in (TicketType.DM, TicketType.FORCED):
            try: await (await bot.fetch_user(target)).send(f"✅ Your ticket has been resolved!\nReason: {reason}\nThank you for reaching out!")
            except: pass
        await interaction.response.send_message(f"✅ Resolved ticket for {ticket.user_name}. Closing soon…", ephemeral=True)
        await asyncio.sleep(30)
        await close_ticket_by_user(target, channel, interaction.user.name, reason)
    else:
        await interaction.response.send_message("❌ Couldn't resolve that ticket.", ephemeral=True)

@bot.tree.command(name="close")
@is_whitelisted()
@is_staff()
async def close_command(interaction: discord.Interaction, reason: str = "No reason provided"):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.response.send_message("❌ This isn't a ticket channel.", ephemeral=True); return
    await interaction.response.send_message(f"🔒 Closing this ticket in 5 seconds…\n**Reason:** {reason}")
    await asyncio.sleep(5)
    await close_ticket_by_user(ticket.user_id, interaction.channel, interaction.user.name, reason)

@bot.tree.command(name="transcript")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def transcript_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    ticket = None
    if user_id:
        try: ticket = await ticket_manager.get_ticket_by_user(int(user_id))
        except: pass
    if not ticket:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.followup.send("❌ No active ticket found.", ephemeral=True); return
    path = save_transcript(ticket)
    await interaction.followup.send(content=f"📄 Transcript for **{ticket.user_name}**", file=discord.File(path), ephemeral=True)

@bot.tree.command(name="tickets")
@is_whitelisted()
@is_staff()
async def tickets_command(interaction: discord.Interaction):
    tickets = await ticket_manager.get_all_open_tickets()
    if not tickets:
        await interaction.response.send_message("📭 No active tickets right now.", ephemeral=True); return
    embed = discord.Embed(title=f"📋 Active Tickets ({len(tickets)})", color=0x3b82f6)
    for t in tickets:
        status_emoji = "🟢" if t.status == TicketStatus.OPEN else "🟡"
        claimed = t.claimed_by_name or "Unclaimed"
        type_icon = "🌐" if t.ticket_type == TicketType.WEB else "💬" if t.ticket_type == TicketType.DM else "🔨"
        channel = bot.get_channel(t.channel_id)
        idle_min = int(t.idle_seconds() / 60)
        embed.add_field(name=f"{status_emoji} {type_icon} {t.user_name}", value=f"Claimed: {claimed} | Messages: {t.message_count} | Idle: {idle_min}m\nChannel: {channel.mention if channel else 'Unknown'}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="info")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def ticket_info_command(interaction: discord.Interaction, user_id: str):
    try: uid = int(user_id)
    except: await interaction.response.send_message("❌ Invalid ID.", ephemeral=True); return
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        await interaction.response.send_message("❌ No active ticket for that user.", ephemeral=True); return
    embed = discord.Embed(title=f"📋 Ticket Info – {ticket.user_name}", color=0x3b82f6)
    embed.add_field(name="User ID", value=str(ticket.user_id), inline=True)
    embed.add_field(name="Type",    value=ticket.ticket_type.value, inline=True)
    embed.add_field(name="Status",  value=ticket.status.value, inline=True)
    embed.add_field(name="Created", value=ticket.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=True)
    embed.add_field(name="Messages", value=str(ticket.message_count), inline=True)
    embed.add_field(name="Idle",     value=f"{int(ticket.idle_seconds()/60)}m", inline=True)
    if ticket.claimed_by_name:
        embed.add_field(name="Handled By", value=f"{ticket.claimed_by_name} ({ticket.claimed_by_role})", inline=True)
    ch = bot.get_channel(ticket.channel_id)
    if ch: embed.add_field(name="Channel", value=ch.mention, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="transfer")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def transfer_command(interaction: discord.Interaction, user_id: str, new_staff: discord.Member):
    try: uid = int(user_id)
    except: await interaction.response.send_message("❌ Invalid ID.", ephemeral=True); return
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True); return
    if ticket.status != TicketStatus.CLAIMED:
        await interaction.response.send_message("❌ That ticket hasn't been claimed yet.", ephemeral=True); return
    new_role = get_staff_role_name(new_staff)
    async with ticket_manager.lock:
        ticket.claimed_by = new_staff.id
        ticket.claimed_by_name = new_staff.display_name
        ticket.claimed_by_role = new_role
        ticket.claimed_at = datetime.now(timezone.utc)
        ticket.history.append({'action': 'transferred', 'from': f"{ticket.claimed_by_name} ({ticket.claimed_by_role})", 'to': f"{new_staff.display_name} ({new_role})", 'at': datetime.now(timezone.utc).isoformat()})
    embed = discord.Embed(title="🔄 Ticket Transferred", description=f"**User:** {ticket.user_name}\n**To:** {new_staff.mention} ({new_role})", color=0xffaa00, timestamp=datetime.now(timezone.utc))
    channel = bot.get_channel(ticket.channel_id)
    if channel: await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Ticket transferred to {new_staff.display_name}.", ephemeral=True)

@bot.tree.command(name="note")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def note_command(interaction: discord.Interaction, user_id: str, note: str):
    try: uid = int(user_id)
    except: await interaction.response.send_message("❌ Invalid ID.", ephemeral=True); return
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True); return
    staff_role = get_staff_role_name(interaction.user)
    embed = discord.Embed(title="📝 Staff Note", description=note, color=0x3b82f6, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"By {interaction.user.display_name} ({staff_role}) for {ticket.user_name}")
    notes_channel = discord.utils.get(interaction.guild.text_channels, name="staff-notes")
    if not notes_channel:
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.guild.me:           discord.PermissionOverwrite(read_messages=True),
        }
        if STAFF_ROLE_ID:
            sr = interaction.guild.get_role(STAFF_ROLE_ID)
            if sr: overwrites[sr] = discord.PermissionOverwrite(read_messages=True)
        notes_channel = await interaction.guild.create_text_channel("staff-notes", overwrites=overwrites)
    await notes_channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Note added for {ticket.user_name}.", ephemeral=True)

@bot.tree.command(name="analytics")
@is_whitelisted()
@is_manager()
async def analytics_command(interaction: discord.Interaction):
    summary = Analytics.get_summary()
    avg_rt  = summary['avg_response_time_s']
    avg_rst = summary['avg_resolution_time_s']
    def fmt_time(s):
        if s < 60:   return f"{s}s"
        if s < 3600: return f"{s//60}m {s%60}s"
        return f"{s//3600}h {(s%3600)//60}m"
    embed = discord.Embed(title="📊 Bot Analytics", color=0x3b82f6, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="🎫 Tickets", value=f"Created: {summary['tickets_created']}\nResolved: {summary['tickets_resolved']}\nClosed: {summary['tickets_closed']}", inline=True)
    embed.add_field(name="⏱️ Response Times", value=f"First response: {fmt_time(int(avg_rt))}\nResolution: {fmt_time(int(avg_rst))}", inline=True)
    embed.add_field(name="🛡️ Moderation", value=f"Bans: {summary['bans']}\nKicks: {summary['kicks']}\nTimeouts: {summary['timeouts']}\nWarns: {summary['warns']}", inline=True)
    if summary['top_staff']:
        staff_lines = "\n".join(f"{i+1}. **{name}** – {count} actions" for i,(name,count) in enumerate(summary['top_staff']))
        embed.add_field(name="🏆 Top Staff", value=staff_lines, inline=False)
    if summary['last_7_days']:
        vol_lines = "\n".join(f"`{d}` – {c} tickets" for d,c in summary['last_7_days'].items())
        embed.add_field(name="📅 Last 7 Days", value=vol_lines, inline=False)
    embed.set_footer(text="Data since bot startup")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="help")
@is_whitelisted()
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="🆘 Rawr.xyz Support Bot", description="Here's how I can help you today:", color=0xef4444)
    embed.add_field(name="📬 Getting Support", value="Send me a private message and I'll create a ticket for you.", inline=False)
    embed.add_field(name="💬 Ticket Commands", value="`/reply` `/claim` `/resolve` `/close`\n`/force_ticket` `/force_transfer`\n`/adduser` `/removeuser`\n`/history` `/tickets` `/info` `/transfer` `/note` `/transcript`", inline=False)
    embed.add_field(name="🛡️ Moderation", value="`/ban` `/kick` `/timeout` `/warn`", inline=False)
    embed.add_field(name="📨 Utility", value="`/embed` `/message` `/stats` `/ping` `/analytics`", inline=False)
    embed.add_field(name="🌐 Web Chat", value="Visit **rawrs.zapto.org** to chat with us live!", inline=False)
    embed.set_footer(text="We're here 24/7 – just ask!")
    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("✅ I've sent you some help information in DMs!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I can't send you a DM. Please enable DMs and try again.", ephemeral=True)

@bot.tree.command(name="stats")
@is_whitelisted()
async def stats_command(interaction: discord.Interaction):
    uptime = datetime.now(timezone.utc) - bot.boot_time
    d, h, m = uptime.days, uptime.seconds // 3600, (uptime.seconds % 3600) // 60
    ts = await ticket_manager.get_stats()
    embed = discord.Embed(title="📊 Bot Statistics", color=0xef4444)
    embed.add_field(name="⏰ Uptime",       value=f"{d}d {h}h {m}m", inline=True)
    embed.add_field(name="⚡ Latency",      value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="🎫 Active Tickets", value=str(ts['total']), inline=True)
    embed.add_field(name="✅ Handled Tickets", value=str(ts['handled']), inline=True)
    embed.add_field(name="👥 Whitelisted",  value=f"{len(whitelisted_users)} users, {len(whitelisted_roles)} roles", inline=True)
    embed.add_field(name="🚫 Blacklisted",  value=str(len(blacklisted_users)), inline=True)
    embed.add_field(name="🌐 SSE Clients",  value=str(len(sse_clients)), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping")
@is_whitelisted()
async def ping_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)}ms`", ephemeral=True)


# ──────────────────────────────────────────────────────────────────────────────
#  EVENT HANDLERS (with anti‑duplicate and ready‑check)
# ──────────────────────────────────────────────────────────────────────────────
_dm_creation_locks: Dict[int, asyncio.Lock] = {}

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm(message)
        return
    await bot.process_commands(message)

async def handle_dm(message: discord.Message):
    if not bot._ready:
        return   # bot not fully started yet

    user_id = message.author.id
    if user_id in blacklisted_users:
        await message.author.send("⛔ You are currently blacklisted and cannot open tickets.")
        return

    # If the user already has an open ticket, just relay the message
    existing = await ticket_manager.get_ticket_by_user(user_id)
    if existing:
        await ticket_manager.add_message(user_id, message.content, message.author.name, "dm")
        channel = bot.get_channel(existing.channel_id)
        if channel:
            embed = discord.Embed(description=message.content, color=0xef4444, timestamp=datetime.now(timezone.utc))
            embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
            await channel.send(embed=embed)
            await message.author.send("✅ I've forwarded your message to the support team!")
        return

    # Use a lock to prevent multiple simultaneous creations for the same user
    if user_id not in _dm_creation_locks:
        _dm_creation_locks[user_id] = asyncio.Lock()

    lock = _dm_creation_locks[user_id]
    async with lock:
        # Double‑check after acquiring the lock
        if await ticket_manager.get_ticket_by_user(user_id):
            return
        await _create_dm_ticket(message)

    if not lock.locked():
        _dm_creation_locks.pop(user_id, None)

async def _create_dm_ticket(message: discord.Message):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await message.author.send("❌ I couldn't find the support server. Please try again later.")
        return

    category = bot.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    if not category:
        category = discord.utils.get(guild.categories, name="SUPPORT TICKETS") or await guild.create_category("SUPPORT TICKETS")

    # Sanitize channel name
    safe_name = ''.join(c for c in message.author.name.lower().replace(' ', '-') if c.isalnum() or c == '-')
    channel_name = f"ticket-{safe_name}"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
        message.author:     discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
        if rid:
            role = guild.get_role(rid)
            if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    try:
        channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
    except Exception as e:
        logger.error(f"Failed to create ticket channel: {e}")
        await message.author.send("❌ Something went wrong while creating your ticket. Please try again.")
        return

    ticket = await ticket_manager.create_ticket(message.author.id, message.author.name, channel.id, TicketType.DM)
    if ticket:
        await ticket_manager.add_message(message.author.id, message.content, message.author.name, "dm")

    header = discord.Embed(title="🎫 New Support Ticket", description=f"**User:** {message.author.mention}\n**ID:** `{message.author.id}`", color=0xef4444, timestamp=datetime.now(timezone.utc))
    await channel.send(embed=header)
    msg_embed = discord.Embed(description=message.content, color=0x3b82f6, timestamp=datetime.now(timezone.utc))
    msg_embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
    await channel.send(embed=msg_embed)

    class ClaimButton(discord.ui.View):
        def __init__(self, uid):
            super().__init__(timeout=None)
            self.uid = uid
            self.claimed = False
        @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="✋")
        async def claim_button(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
            if self.claimed:
                await btn_interaction.response.send_message("❌ This ticket has already been claimed.", ephemeral=True)
                return
            staff_role = get_staff_role_name(btn_interaction.user)
            result = await ticket_manager.claim_ticket(self.uid, btn_interaction.user.id, btn_interaction.user.display_name, staff_role)
            if result:
                self.claimed = True
                button.disabled = True
                await btn_interaction.message.edit(view=self)
                await btn_interaction.response.send_message(f"✅ You've claimed the ticket for {message.author.name}.", ephemeral=True)
                try: await message.author.send(f"👋 {staff_role} ({btn_interaction.user.display_name}) is now handling your ticket.")
                except: pass
                await channel.send(embed=discord.Embed(title="✅ Ticket Claimed", description=f"Claimed by {btn_interaction.user.mention} ({staff_role})", color=0x00ff00, timestamp=datetime.now(timezone.utc)))
            else:
                await btn_interaction.response.send_message("❌ Couldn't claim the ticket.", ephemeral=True)
    await channel.send(view=ClaimButton(message.author.id))
    await message.author.send("👋 Thanks for reaching out! I've created a ticket for you. Our support team will assist you shortly.")


# ──────────────────────────────────────────────────────────────────────────────
#  TICKET CLOSE HELPER (stores transcript, deletes channel)
# ──────────────────────────────────────────────────────────────────────────────
async def close_ticket_by_user(user_id, channel, closed_by=None, reason=None):
    ticket = await ticket_manager.close_ticket(user_id, channel.id if channel else None, closed_by, reason)
    if not ticket or not channel:
        return
    transcript_path = save_transcript(ticket)
    try:
        # Send to ticket-logs channel
        log_channel = discord.utils.get(channel.guild.text_channels, name="ticket-logs")
        if not log_channel:
            overwrites = {
                channel.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                channel.guild.me:           discord.PermissionOverwrite(read_messages=True),
            }
            log_channel = await channel.guild.create_text_channel("ticket-logs", overwrites=overwrites)
        embed = discord.Embed(title="📋 Ticket Closed", color=0xef4444, timestamp=datetime.now(timezone.utc))
        embed.description = f"**User:** {ticket.user_name}\n**Type:** {ticket.ticket_type.value}\n**Messages:** {ticket.message_count}"
        embed.add_field(name="Duration", value=f"{(ticket.closed_at - ticket.created_at).total_seconds()/60:.0f} minutes", inline=True)
        if ticket.claimed_by_name:
            embed.add_field(name="Handled by", value=f"{ticket.claimed_by_name} ({ticket.claimed_by_role})", inline=True)
        if reason:
            embed.add_field(name="Closed Reason", value=reason, inline=False)
        await log_channel.send(embed=embed, file=discord.File(transcript_path))
        # Review notification
        if REVIEW_CHANNEL_ID and ticket.ticket_type in (TicketType.DM, TicketType.FORCED) and ticket.resolved_by:
            review_ch = bot.get_channel(REVIEW_CHANNEL_ID)
            if review_ch:
                rev_embed = discord.Embed(title="⭐ Ticket History – Review", color=0x00ff00)
                rev_embed.add_field(name="User", value=ticket.user_name, inline=True)
                rev_embed.add_field(name="Resolved by", value=ticket.resolved_by, inline=True)
                rev_embed.add_field(name="Resolution", value=ticket.resolved_reason or 'N/A', inline=False)
                await review_ch.send(embed=rev_embed, file=discord.File(transcript_path))
        # Save ticket to storage
        await Storage.save_ticket({
            'user_id': ticket.user_id,
            'ticket_id': ticket.channel_id,
            'user_name': ticket.user_name,
            'ticket_type': ticket.ticket_type.value,
            'status': ticket.status.value,
            'claimed_by': ticket.claimed_by,
            'claimed_by_name': ticket.claimed_by_name,
            'claimed_by_role': ticket.claimed_by_role,
            'created_at': ticket.created_at.isoformat(),
            'closed_at': ticket.closed_at.isoformat(),
            'message_count': ticket.message_count,
            'resolved_by': ticket.resolved_by,
            'resolved_reason': ticket.resolved_reason,
        })
    except Exception as e:
        logger.error(f"Error during ticket close: {e}")
    finally:
        try:
            await channel.delete()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
#  RUN
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        logger.info(f"Starting RawrBot on port {PORT}…")
        bot.run(TOKEN, log_handler=None)
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
