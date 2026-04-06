import os
import discord
import asyncio
import logging
import json
import time
import hashlib
from typing import Optional, Set, Dict, Any, Tuple, List
from datetime import datetime, timezone
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web
from collections import deque
from enum import Enum

# ─────────────────────────────────────────────
#  AWS SETUP (Optional)
# ─────────────────────────────────────────────
USE_AWS = os.getenv('USE_AWS', 'false').lower() == 'true'
AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

AWS_AVAILABLE = False
if USE_AWS and AWS_ACCESS_KEY and AWS_SECRET_KEY:
    try:
        import boto3
        from boto3.dynamodb.conditions import Key
        AWS_AVAILABLE = True
        dynamodb = boto3.resource(
            'dynamodb',
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
            region_name=AWS_REGION
        )
        whitelist_table     = dynamodb.Table('rawr_whitelist')
        blacklist_table     = dynamodb.Table('rawr_blacklist')
        ticket_history_table = dynamodb.Table('rawr_ticket_history')
        analytics_table     = dynamodb.Table('rawr_analytics')
        logging.info("✅ AWS DynamoDB connected")
    except ImportError:
        logging.warning("boto3 not installed – using file-based storage")
    except Exception as e:
        logging.warning(f"AWS connection failed: {e} – using file-based storage")

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('RawrBot')

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

GUILD_ID = int(os.getenv('GUILD_ID', '0'))
if not GUILD_ID:
    raise ValueError("GUILD_ID environment variable not set")

PORT                = int(os.getenv('PORT', 8080))
TICKET_CATEGORY_ID  = int(os.getenv('TICKET_CATEGORY_ID', '0'))
REVIEW_CHANNEL_ID   = int(os.getenv('REVIEW_CHANNEL_ID', '1489438620233240596'))
OWNER_ID            = int(os.getenv('OWNER_ID', '0'))
STAFF_ROLE_ID       = int(os.getenv('STAFF_ROLE_ID', '0'))
MANAGER_ROLE_ID     = int(os.getenv('MANAGER_ROLE_ID', '0'))
MODERATOR_ROLE_ID   = int(os.getenv('MODERATOR_ROLE_ID', '0'))

RATE_LIMIT_SECONDS      = 5
MAX_MESSAGES_PER_MINUTE = 12

# How long (in minutes) a ticket can be idle before auto-close warning / close
IDLE_WARN_MINUTES  = int(os.getenv('IDLE_WARN_MINUTES', '30'))
IDLE_CLOSE_MINUTES = int(os.getenv('IDLE_CLOSE_MINUTES', '60'))

# ─────────────────────────────────────────────
#  DATA DIRECTORY
# ─────────────────────────────────────────────
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "transcripts"), exist_ok=True)

WHITELIST_FILE  = os.path.join(DATA_DIR, "whitelist.json")
BLACKLIST_FILE  = os.path.join(DATA_DIR, "blacklist.json")
ANALYTICS_FILE  = os.path.join(DATA_DIR, "analytics.json")

# ─────────────────────────────────────────────
#  ANALYTICS
# ─────────────────────────────────────────────
class Analytics:
    """Lightweight, append-only analytics stored in a local JSON file (or DynamoDB)."""

    _cache: Dict[str, Any] = {}

    @classmethod
    def _load(cls) -> Dict[str, Any]:
        if cls._cache:
            return cls._cache
        try:
            if os.path.exists(ANALYTICS_FILE):
                with open(ANALYTICS_FILE, 'r') as f:
                    cls._cache = json.load(f)
            else:
                cls._cache = {
                    "tickets_created": 0,
                    "tickets_closed": 0,
                    "tickets_resolved": 0,
                    "messages_relayed": 0,
                    "bans": 0,
                    "kicks": 0,
                    "timeouts": 0,
                    "warns": 0,
                    "staff_activity": {},   # staff_name -> count
                    "hourly_volume": {},    # "YYYY-MM-DD-HH" -> count
                    "daily_volume": {},     # "YYYY-MM-DD" -> count
                    "response_times": [],   # seconds from open to first claim
                    "resolution_times": [], # seconds from open to close
                }
        except Exception as e:
            logger.error(f"Analytics load error: {e}")
            cls._cache = {}
        return cls._cache

    @classmethod
    def _save(cls):
        try:
            with open(ANALYTICS_FILE, 'w') as f:
                json.dump(cls._cache, f, indent=2)
        except Exception as e:
            logger.error(f"Analytics save error: {e}")

    @classmethod
    def increment(cls, key: str, amount: int = 1):
        data = cls._load()
        data[key] = data.get(key, 0) + amount
        cls._save()

    @classmethod
    def record_staff(cls, staff_name: str):
        data = cls._load()
        sa = data.setdefault("staff_activity", {})
        sa[staff_name] = sa.get(staff_name, 0) + 1
        cls._save()

    @classmethod
    def record_volume(cls):
        data = cls._load()
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%Y-%m-%d-%H")
        day_key  = now.strftime("%Y-%m-%d")
        data.setdefault("hourly_volume", {})[hour_key] = data["hourly_volume"].get(hour_key, 0) + 1
        data.setdefault("daily_volume",  {})[day_key]  = data["daily_volume"].get(day_key,   0) + 1
        cls._save()

    @classmethod
    def record_response_time(cls, seconds: float):
        data = cls._load()
        rt = data.setdefault("response_times", [])
        rt.append(round(seconds, 1))
        if len(rt) > 500:
            data["response_times"] = rt[-500:]
        cls._save()

    @classmethod
    def record_resolution_time(cls, seconds: float):
        data = cls._load()
        rt = data.setdefault("resolution_times", [])
        rt.append(round(seconds, 1))
        if len(rt) > 500:
            data["resolution_times"] = rt[-500:]
        cls._save()

    @classmethod
    def get_summary(cls) -> Dict[str, Any]:
        data = cls._load()
        rt = data.get("response_times", [])
        rst = data.get("resolution_times", [])
        avg_rt  = round(sum(rt)  / len(rt),  1) if rt  else 0
        avg_rst = round(sum(rst) / len(rst), 1) if rst else 0

        # Top 5 staff by activity
        sa = data.get("staff_activity", {})
        top_staff = sorted(sa.items(), key=lambda x: x[1], reverse=True)[:5]

        # Last 7 days volume
        today = datetime.now(timezone.utc)
        daily = data.get("daily_volume", {})
        last_7 = {}
        for i in range(6, -1, -1):
            from datetime import timedelta
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            last_7[d] = daily.get(d, 0)

        return {
            "tickets_created":   data.get("tickets_created", 0),
            "tickets_closed":    data.get("tickets_closed", 0),
            "tickets_resolved":  data.get("tickets_resolved", 0),
            "messages_relayed":  data.get("messages_relayed", 0),
            "bans":   data.get("bans",    0),
            "kicks":  data.get("kicks",   0),
            "timeouts": data.get("timeouts", 0),
            "warns":  data.get("warns",   0),
            "avg_response_time_s":   avg_rt,
            "avg_resolution_time_s": avg_rst,
            "top_staff": top_staff,
            "last_7_days": last_7,
        }

# ─────────────────────────────────────────────
#  STORAGE
# ─────────────────────────────────────────────
class Storage:
    @staticmethod
    async def _read_json(path: str, default: Any) -> Any:
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"File read error ({path}): {e}")
        return default

    @staticmethod
    async def _write_json(path: str, data: Any) -> bool:
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            logger.error(f"File write error ({path}): {e}")
            return False

    # ── Whitelist ──────────────────────────────
    @staticmethod
    async def add_whitelist_user(user_id: int, added_by: str) -> bool:
        if AWS_AVAILABLE:
            try:
                whitelist_table.put_item(Item={
                    'type': 'user', 'id': user_id,
                    'added_by': added_by,
                    'added_at': datetime.now(timezone.utc).isoformat()
                })
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        data = await Storage._read_json(WHITELIST_FILE, {'users': [], 'roles': []})
        if user_id not in data['users']:
            data['users'].append(user_id)
        return await Storage._write_json(WHITELIST_FILE, data)

    @staticmethod
    async def add_whitelist_role(role_id: int, added_by: str) -> bool:
        if AWS_AVAILABLE:
            try:
                whitelist_table.put_item(Item={
                    'type': 'role', 'id': role_id,
                    'added_by': added_by,
                    'added_at': datetime.now(timezone.utc).isoformat()
                })
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        data = await Storage._read_json(WHITELIST_FILE, {'users': [], 'roles': []})
        if role_id not in data['roles']:
            data['roles'].append(role_id)
        return await Storage._write_json(WHITELIST_FILE, data)

    @staticmethod
    async def remove_whitelist(item_id: int, item_type: str = 'user') -> bool:
        if AWS_AVAILABLE:
            try:
                whitelist_table.delete_item(Key={'type': item_type, 'id': item_id})
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        data = await Storage._read_json(WHITELIST_FILE, {'users': [], 'roles': []})
        key = 'users' if item_type == 'user' else 'roles'
        if item_id in data[key]:
            data[key].remove(item_id)
        return await Storage._write_json(WHITELIST_FILE, data)

    @staticmethod
    async def get_whitelist() -> Tuple[List[int], List[int]]:
        if AWS_AVAILABLE:
            try:
                response = whitelist_table.scan()
                items = response.get('Items', [])
                users = [item['id'] for item in items if item.get('type') == 'user']
                roles = [item['id'] for item in items if item.get('type') == 'role']
                return users, roles
            except Exception as e:
                logger.error(f"AWS error: {e}")
        data = await Storage._read_json(WHITELIST_FILE, {'users': [], 'roles': []})
        return data.get('users', []), data.get('roles', [])

    # ── Blacklist ──────────────────────────────
    @staticmethod
    async def add_blacklist(user_id: int, reason: str, blacklisted_by: str) -> bool:
        if AWS_AVAILABLE:
            try:
                blacklist_table.put_item(Item={
                    'user_id': user_id, 'reason': reason,
                    'blacklisted_by': blacklisted_by,
                    'blacklisted_at': datetime.now(timezone.utc).isoformat()
                })
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        data = await Storage._read_json(BLACKLIST_FILE, {'users': []})
        if user_id not in data['users']:
            data['users'].append(user_id)
        return await Storage._write_json(BLACKLIST_FILE, data)

    @staticmethod
    async def remove_blacklist(user_id: int) -> bool:
        if AWS_AVAILABLE:
            try:
                blacklist_table.delete_item(Key={'user_id': user_id})
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        data = await Storage._read_json(BLACKLIST_FILE, {'users': []})
        if user_id in data['users']:
            data['users'].remove(user_id)
        return await Storage._write_json(BLACKLIST_FILE, data)

    @staticmethod
    async def get_blacklist() -> List[int]:
        if AWS_AVAILABLE:
            try:
                response = blacklist_table.scan()
                return [item['user_id'] for item in response.get('Items', [])]
            except Exception as e:
                logger.error(f"AWS error: {e}")
        data = await Storage._read_json(BLACKLIST_FILE, {'users': []})
        return data.get('users', [])

    # ── Ticket History ─────────────────────────
    @staticmethod
    async def save_ticket(ticket_data: dict) -> bool:
        if AWS_AVAILABLE:
            try:
                ticket_history_table.put_item(Item=ticket_data)
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        # Local fallback: append to a per-user JSONL file
        path = os.path.join(DATA_DIR, f"history_{ticket_data.get('user_id', 'unknown')}.jsonl")
        try:
            with open(path, 'a') as f:
                f.write(json.dumps(ticket_data) + '\n')
            return True
        except Exception as e:
            logger.error(f"File error: {e}")
        return False

    @staticmethod
    async def get_user_history(user_id: int) -> List[dict]:
        if AWS_AVAILABLE:
            try:
                response = ticket_history_table.query(
                    KeyConditionExpression=Key('user_id').eq(user_id)
                )
                return response.get('Items', [])
            except Exception as e:
                logger.error(f"AWS error: {e}")
        path = os.path.join(DATA_DIR, f"history_{user_id}.jsonl")
        if not os.path.exists(path):
            return []
        try:
            with open(path, 'r') as f:
                return [json.loads(line) for line in f if line.strip()]
        except Exception as e:
            logger.error(f"File error: {e}")
        return []

# ─────────────────────────────────────────────
#  GLOBAL STORAGE CACHE
# ─────────────────────────────────────────────
whitelisted_users: Set[int] = set()
whitelisted_roles: Set[int] = set()
blacklisted_users: Set[int] = set()

async def load_all_data():
    global whitelisted_users, whitelisted_roles, blacklisted_users
    users, roles = await Storage.get_whitelist()
    whitelisted_users  = set(users)
    whitelisted_roles  = set(roles)
    blacklisted_users  = set(await Storage.get_blacklist())
    logger.info(f"Loaded: {len(whitelisted_users)} wl-users, "
                f"{len(whitelisted_roles)} wl-roles, "
                f"{len(blacklisted_users)} blacklisted")

# ─────────────────────────────────────────────
#  TICKET SYSTEM
# ─────────────────────────────────────────────
class TicketStatus(Enum):
    OPEN     = "open"
    CLAIMED  = "claimed"
    RESOLVED = "resolved"
    CLOSED   = "closed"

class TicketType(Enum):
    WEB    = "web"
    DM     = "dm"
    FORCED = "forced"

class Ticket:
    def __init__(
        self,
        user_id: int,
        user_name: str,
        channel_id: int,
        ticket_type: TicketType,
        created_by: Optional[str] = None,
    ):
        self.user_id          = user_id
        self.user_name        = user_name
        self.channel_id       = channel_id
        self.ticket_type      = ticket_type
        self.status           = TicketStatus.OPEN
        self.claimed_by: Optional[int] = None
        self.claimed_by_name: Optional[str] = None
        self.claimed_by_role: Optional[str] = None
        self.created_at       = datetime.now(timezone.utc)
        self.claimed_at: Optional[datetime] = None
        self.resolved_at: Optional[datetime] = None
        self.closed_at: Optional[datetime] = None
        self.last_activity    = datetime.now(timezone.utc)   # ← idle tracking
        self.idle_warned      = False                         # ← idle tracking
        self.message_count    = 0
        self.created_by       = created_by
        self.resolved_by: Optional[str] = None
        self.resolved_reason: Optional[str] = None
        self.resolved_by_role: Optional[str] = None
        self.closed_by: Optional[str] = None
        self.closed_reason: Optional[str] = None
        self.review_sent      = False
        self.history: List[Dict] = []
        self.additional_users: List[int] = []
        # Full message log for transcript generation
        self.transcript_log: List[Dict] = []

    def touch(self):
        """Reset idle timer."""
        self.last_activity = datetime.now(timezone.utc)
        self.idle_warned   = False

    def idle_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.last_activity).total_seconds()


class TicketManager:
    def __init__(self):
        self.tickets: Dict[int, Ticket] = {}
        self.channel_to_user: Dict[int, int] = {}
        self.lock = asyncio.Lock()
        self.handled_count = 0

    # ── create / lookup ────────────────────────
    async def create_ticket(
        self,
        user_id: int,
        user_name: str,
        channel_id: int,
        ticket_type: TicketType,
        created_by: Optional[str] = None,
    ) -> Ticket:
        async with self.lock:
            ticket = Ticket(user_id, user_name, channel_id, ticket_type, created_by)
            self.tickets[user_id]         = ticket
            self.channel_to_user[channel_id] = user_id
            self.handled_count += 1
            Analytics.increment("tickets_created")
            Analytics.record_volume()
            logger.info(f"📫 Ticket #{channel_id} created for {user_name}")
            return ticket

    async def get_ticket_by_user(self, user_id: int) -> Optional[Ticket]:
        async with self.lock:
            return self.tickets.get(user_id)

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[Ticket]:
        async with self.lock:
            uid = self.channel_to_user.get(channel_id)
            return self.tickets.get(uid) if uid else None

    # ── user management ────────────────────────
    async def add_user_to_ticket(self, user_id: int, new_user_id: int) -> bool:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and new_user_id not in ticket.additional_users:
                ticket.additional_users.append(new_user_id)
                return True
            return False

    async def remove_user_from_ticket(self, user_id: int, remove_user_id: int) -> bool:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and remove_user_id in ticket.additional_users:
                ticket.additional_users.remove(remove_user_id)
                return True
            return False

    # ── state transitions ─────────────────────
    async def claim_ticket(
        self,
        user_id: int,
        staff_id: int,
        staff_name: str,
        staff_role: str,
    ) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status == TicketStatus.OPEN:
                # Record response time
                if ticket.created_at:
                    dt = (datetime.now(timezone.utc) - ticket.created_at).total_seconds()
                    Analytics.record_response_time(dt)

                ticket.status         = TicketStatus.CLAIMED
                ticket.claimed_by     = staff_id
                ticket.claimed_by_name = staff_name
                ticket.claimed_by_role = staff_role
                ticket.claimed_at     = datetime.now(timezone.utc)
                ticket.touch()
                ticket.history.append({
                    'action': 'claimed', 'by': staff_name,
                    'role': staff_role,
                    'at': datetime.now(timezone.utc).isoformat()
                })
                Analytics.record_staff(staff_name)
                logger.info(f"✅ Ticket #{ticket.channel_id} claimed by {staff_name}")
                return ticket
            return None

    async def resolve_ticket(
        self,
        user_id: int,
        resolved_by: str,
        resolved_by_role: str,
        reason: str,
    ) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]:
                ticket.status        = TicketStatus.RESOLVED
                ticket.resolved_at   = datetime.now(timezone.utc)
                ticket.resolved_by   = resolved_by
                ticket.resolved_reason = reason
                ticket.resolved_by_role = resolved_by_role
                ticket.touch()
                ticket.history.append({
                    'action': 'resolved', 'by': resolved_by,
                    'role': resolved_by_role, 'reason': reason,
                    'at': datetime.now(timezone.utc).isoformat()
                })
                Analytics.increment("tickets_resolved")
                Analytics.record_staff(resolved_by)
                logger.info(f"✅ Ticket #{ticket.channel_id} resolved by {resolved_by}")
                return ticket
            return None

    async def close_ticket(
        self,
        user_id: int,
        channel_id: int = None,
        closed_by: str = None,
        reason: str = None,
    ) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                if channel_id:
                    self.channel_to_user.pop(channel_id, None)
                ticket.status    = TicketStatus.CLOSED
                ticket.closed_at = datetime.now(timezone.utc)
                ticket.closed_by = closed_by
                ticket.closed_reason = reason
                ticket.history.append({
                    'action': 'closed', 'by': closed_by,
                    'reason': reason,
                    'at': ticket.closed_at.isoformat()
                })

                # Record resolution time
                if ticket.created_at:
                    dt = (ticket.closed_at - ticket.created_at).total_seconds()
                    Analytics.record_resolution_time(dt)

                Analytics.increment("tickets_closed")
                if closed_by:
                    Analytics.record_staff(closed_by)

                await Storage.save_ticket({
                    'user_id':          ticket.user_id,
                    'ticket_id':        ticket.channel_id,
                    'user_name':        ticket.user_name,
                    'ticket_type':      ticket.ticket_type.value,
                    'status':           ticket.status.value,
                    'claimed_by':       ticket.claimed_by,
                    'claimed_by_name':  ticket.claimed_by_name,
                    'claimed_by_role':  ticket.claimed_by_role,
                    'created_at':       ticket.created_at.isoformat(),
                    'claimed_at':       ticket.claimed_at.isoformat() if ticket.claimed_at else None,
                    'resolved_at':      ticket.resolved_at.isoformat() if ticket.resolved_at else None,
                    'closed_at':        ticket.closed_at.isoformat(),
                    'message_count':    ticket.message_count,
                    'created_by':       ticket.created_by,
                    'resolved_by':      ticket.resolved_by,
                    'resolved_reason':  ticket.resolved_reason,
                    'additional_users': ticket.additional_users,
                })
                logger.info(f"🔒 Ticket #{ticket.channel_id} closed")
                return ticket
            return None

    async def force_transfer(
        self,
        user_id: int,
        new_staff_id: int,
        new_staff_name: str,
        new_staff_role: str,
    ) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket:
                old_staff = (f"{ticket.claimed_by_name} ({ticket.claimed_by_role})"
                             if ticket.claimed_by_name else "Unclaimed")
                ticket.claimed_by      = new_staff_id
                ticket.claimed_by_name = new_staff_name
                ticket.claimed_by_role = new_staff_role
                ticket.claimed_at      = datetime.now(timezone.utc)
                if ticket.status == TicketStatus.OPEN:
                    ticket.status = TicketStatus.CLAIMED
                ticket.history.append({
                    'action': 'force_transferred',
                    'from': old_staff,
                    'to': f"{new_staff_name} ({new_staff_role})",
                    'at': datetime.now(timezone.utc).isoformat()
                })
                logger.info(f"🔄 Ticket #{ticket.channel_id} force-transferred to {new_staff_name}")
                return ticket
            return None

    # ── message tracking ──────────────────────
    async def add_message(self, user_id: int, content: str, author: str = "unknown", origin: str = "dm"):
        async with self.lock:
            t = self.tickets.get(user_id)
            if t:
                t.message_count += 1
                t.touch()
                t.transcript_log.append({
                    "ts":      datetime.now(timezone.utc).isoformat(),
                    "author":  author,
                    "content": content,
                    "origin":  origin,
                })
                Analytics.increment("messages_relayed")

    # ── queries ───────────────────────────────
    async def get_all_open_tickets(self) -> List[Ticket]:
        async with self.lock:
            return [t for t in self.tickets.values()
                    if t.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]]

    async def get_idle_tickets(self) -> List[Ticket]:
        """Return tickets idle beyond the warn/close thresholds."""
        async with self.lock:
            result = []
            for t in self.tickets.values():
                if t.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]:
                    result.append(t)
            return result

    async def get_stats(self) -> dict:
        async with self.lock:
            return {
                'total':   len(self.tickets),
                'open':    sum(1 for t in self.tickets.values() if t.status == TicketStatus.OPEN),
                'claimed': sum(1 for t in self.tickets.values() if t.status == TicketStatus.CLAIMED),
                'resolved':sum(1 for t in self.tickets.values() if t.status == TicketStatus.RESOLVED),
                'handled': self.handled_count,
            }

ticket_manager = TicketManager()

# ─────────────────────────────────────────────
#  TRANSCRIPT GENERATOR
# ─────────────────────────────────────────────
def generate_transcript_html(ticket: Ticket) -> str:
    """Return a styled HTML string for a closed ticket transcript."""
    rows = ""
    for entry in ticket.transcript_log:
        ts      = entry.get("ts", "")
        author  = entry.get("author", "Unknown")
        content = entry.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
        origin  = entry.get("origin", "")
        badge   = ("🌐 Web" if origin == "web"
                   else "🤖 Bot" if origin == "bot"
                   else "💬 DM")
        rows += f"""
        <div class="message">
          <span class="ts">{ts[:19].replace('T',' ')}</span>
          <span class="badge">{badge}</span>
          <span class="author">{author}</span>
          <span class="content">{content}</span>
        </div>"""

    duration = ""
    if ticket.closed_at and ticket.created_at:
        secs = int((ticket.closed_at - ticket.created_at).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        duration = f"{h}h {m}m {s}s"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Ticket Transcript – {ticket.user_name}</title>
  <style>
    :root {{
      --bg: #0d1117; --surface: #161b22; --border: #30363d;
      --text: #e6edf3; --muted: #8b949e;
      --accent: #ef4444; --green: #3fb950; --blue: #58a6ff;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text);
            font-family: 'Segoe UI', system-ui, sans-serif;
            padding: 2rem; line-height: 1.5; }}
    header {{ border-bottom: 1px solid var(--border);
              padding-bottom: 1rem; margin-bottom: 1.5rem; }}
    header h1 {{ color: var(--accent); font-size: 1.4rem; }}
    .meta {{ display: flex; gap: 2rem; flex-wrap: wrap;
             font-size: 0.85rem; color: var(--muted); margin-top: .5rem; }}
    .meta span strong {{ color: var(--text); }}
    .log {{ display: flex; flex-direction: column; gap: .5rem; }}
    .message {{ background: var(--surface); border: 1px solid var(--border);
                border-radius: 6px; padding: .6rem .9rem;
                display: grid;
                grid-template-columns: 9rem 4rem 10rem 1fr;
                gap: .5rem; align-items: baseline; font-size: .875rem; }}
    .ts     {{ color: var(--muted); font-size: .75rem; }}
    .badge  {{ font-size: .7rem; }}
    .author {{ font-weight: 600; color: var(--blue); }}
    .content {{ word-break: break-word; white-space: pre-wrap; }}
    footer {{ margin-top: 2rem; font-size: .75rem; color: var(--muted);
              border-top: 1px solid var(--border); padding-top: 1rem; }}
  </style>
</head>
<body>
  <header>
    <h1>🎫 Ticket Transcript</h1>
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
  <div class="log">
    {rows if rows else '<p style="color:var(--muted)">No messages recorded.</p>'}
  </div>
  <footer>Generated by RawrBot &bull; {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</footer>
</body>
</html>"""

def save_transcript(ticket: Ticket) -> str:
    """Save transcript HTML to disk. Returns the file path."""
    filename = f"ticket_{ticket.channel_id}_{ticket.user_id}.html"
    path     = os.path.join(DATA_DIR, "transcripts", filename)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(generate_transcript_html(ticket))
        logger.info(f"📄 Transcript saved: {path}")
    except Exception as e:
        logger.error(f"Transcript save error: {e}")
    return path

# ─────────────────────────────────────────────
#  RATE LIMITER
# ─────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self.user_messages: Dict[int, deque] = {}

    def can_send(self, user_id: int, content: str) -> Tuple[bool, str]:
        now = time.time()
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=MAX_MESSAGES_PER_MINUTE)
        dq = self.user_messages[user_id]
        if len(dq) >= MAX_MESSAGES_PER_MINUTE:
            oldest = dq[0]
            if now - oldest < 60:
                return False, f"Rate limited. Try again in {60-(now-oldest):.0f}s"
        if dq:
            since_last = now - dq[-1]
            if since_last < RATE_LIMIT_SECONDS:
                return False, f"Wait {RATE_LIMIT_SECONDS - since_last:.1f}s between messages"
        return True, "OK"

    def record(self, user_id: int):
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=MAX_MESSAGES_PER_MINUTE)
        self.user_messages[user_id].append(time.time())

rate_limiter = RateLimiter()

# ─────────────────────────────────────────────
#  SSE / WEB BROADCAST
# ─────────────────────────────────────────────
message_queue: deque = deque(maxlen=100)
sse_clients: Set = set()

async def broadcast_to_web(message: dict):
    if not sse_clients:
        return
    message['id']        = f"msg_{int(time.time()*1000)}_{hashlib.md5(message.get('text','').encode()).hexdigest()[:6]}"
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

async def forward_to_discord(message: dict):
    if not TICKET_CATEGORY_ID:
        return
    guild    = bot.get_guild(GUILD_ID)
    category = bot.get_channel(TICKET_CATEGORY_ID)
    if not guild or not category:
        return

    channel_name = f"web-{message['user'].lower().replace(' ', '-')}"
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
                reason=f"Web chat from {message['user']}"
            )
            await ticket_manager.create_ticket(
                hash(message['user']), message['user'], channel.id, TicketType.WEB
            )
            await channel.send(f"🚀 **Chat Started:** `{message['user']}`\nUse `/reply` to respond.")
        except Exception as e:
            logger.error(f"Failed to create channel: {e}")
            return

    embed = discord.Embed(
        description=message['text'], color=0xef4444,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(name=f"🌐 Web: {message['user']}")
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send: {e}")

# ─────────────────────────────────────────────
#  HTTP HANDLERS
# ─────────────────────────────────────────────
async def sse_endpoint(request):
    headers = {
        'Content-Type':                'text/event-stream',
        'Cache-Control':               'no-cache',
        'Access-Control-Allow-Origin': '*',
        'Connection':                  'keep-alive',
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
        data        = await request.json()
        user_id_hash = hash(data.get('user', 'unknown'))
        can_send, error = rate_limiter.can_send(user_id_hash, data.get('text', ''))
        if not can_send:
            return web.json_response({'error': error}, status=429)
        rate_limiter.record(user_id_hash)
        message = {
            'type': 'chat',
            'user': data.get('user', 'Guest'),
            'text': data.get('text', ''),
            'origin': 'web',
        }
        await broadcast_to_web(message)
        await forward_to_discord(message)
        return web.json_response({'status': 'ok'})
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)

async def health_check(request):
    stats = await ticket_manager.get_stats()
    return web.json_response({
        'status':    'alive',
        'clients':   len(sse_clients),
        'messages':  len(message_queue),
        'tickets':   stats,
        'storage':   'AWS DynamoDB' if AWS_AVAILABLE else 'Local Files',
        'whitelist': {'users': len(whitelisted_users), 'roles': len(whitelisted_roles)},
        'blacklist': len(blacklisted_users),
    })

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────
class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members         = True
        intents.moderation      = True

        self.boot_time = datetime.now(timezone.utc)
        self.web_app   = None
        self.runner    = None
        self._ready    = False

        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await load_all_data()

        self.web_app = web.Application()
        self.web_app.router.add_get('/events', sse_endpoint)
        self.web_app.router.add_post('/send',  send_message_endpoint)
        self.web_app.router.add_get('/health', health_check)
        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        await web.TCPSite(self.runner, '0.0.0.0', PORT).start()
        logger.info(f"✅ HTTP server on port {PORT}")

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("✅ Bot setup complete")

    async def on_ready(self):
        self._ready = True
        logger.info(f"✅ Logged in as: {self.user.name} ({self.user.id})")
        logger.info(f"✅ Connected to {len(self.guilds)} guilds")
        logger.info(f"✅ Storage: {'AWS DynamoDB' if AWS_AVAILABLE else 'Local Files'}")

        cmds = await self.tree.fetch_commands(guild=discord.Object(id=GUILD_ID))
        logger.info(f"✅ Synced {len(cmds)} slash commands")

        self.status_task.start()
        self.idle_check_task.start()

    async def close(self):
        for task in (self.status_task, self.idle_check_task):
            if task.is_running():
                task.cancel()
        if self.runner:
            await self.runner.cleanup()
        await super().close()

    # ── background tasks ──────────────────────
    @tasks.loop(seconds=30)
    async def status_task(self):
        if not self._ready:
            return
        try:
            stats = await ticket_manager.get_stats()
            await self.change_presence(activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{stats['open']} open | rawrs.zapto.org"
            ))
        except Exception as e:
            logger.error(f"Status task error: {e}")

    @tasks.loop(minutes=5)
    async def idle_check_task(self):
        """Warn then auto-close tickets that have been idle too long."""
        if not self._ready:
            return
        tickets = await ticket_manager.get_idle_tickets()
        for ticket in tickets:
            idle_min = ticket.idle_seconds() / 60
            channel  = self.get_channel(ticket.channel_id)

            # Warn threshold
            if idle_min >= IDLE_WARN_MINUTES and not ticket.idle_warned:
                ticket.idle_warned = True
                if channel:
                    try:
                        embed = discord.Embed(
                            title="⚠️ Idle Ticket Warning",
                            description=(
                                f"This ticket has been idle for **{int(idle_min)} minutes**.\n"
                                f"It will auto-close in **{IDLE_CLOSE_MINUTES - IDLE_WARN_MINUTES} minutes** "
                                f"if there is no activity."
                            ),
                            color=0xeab308,
                            timestamp=datetime.now(timezone.utc)
                        )
                        await channel.send(embed=embed)
                    except Exception as e:
                        logger.warning(f"Idle warn send error: {e}")

            # Auto-close threshold
            if idle_min >= IDLE_CLOSE_MINUTES:
                logger.info(f"⏰ Auto-closing idle ticket #{ticket.channel_id}")
                if channel:
                    try:
                        embed = discord.Embed(
                            title="⏰ Ticket Auto-Closed",
                            description=f"This ticket was automatically closed after **{int(idle_min)} minutes** of inactivity.",
                            color=0xef4444,
                            timestamp=datetime.now(timezone.utc)
                        )
                        await channel.send(embed=embed)
                    except Exception:
                        pass
                await close_ticket_by_user(
                    ticket.user_id, channel,
                    closed_by="Auto-Close", reason="Idle timeout"
                )

bot = RawrBot()

# ─────────────────────────────────────────────
#  PERMISSION HELPERS
# ─────────────────────────────────────────────
def is_whitelisted():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        if interaction.user.id in whitelisted_users:
            return True
        user_roles = {role.id for role in interaction.user.roles}
        if user_roles & whitelisted_roles:
            return True
        await interaction.response.send_message("❌ You are not whitelisted to use this bot.", ephemeral=True)
        return False
    return app_commands.check(predicate)

def is_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        user_roles = {role.id for role in interaction.user.roles}
        return bool(user_roles & {STAFF_ROLE_ID, MANAGER_ROLE_ID, MODERATOR_ROLE_ID})
    return app_commands.check(predicate)

def is_manager():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        return MANAGER_ROLE_ID in {role.id for role in interaction.user.roles}
    return app_commands.check(predicate)

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID
    return app_commands.check(predicate)

def is_moderator():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        user_roles = {role.id for role in interaction.user.roles}
        return bool(user_roles & {MODERATOR_ROLE_ID, MANAGER_ROLE_ID})
    return app_commands.check(predicate)

def get_staff_role_name(member: discord.Member) -> str:
    if member.id == OWNER_ID:
        return "👑 Owner"
    roles = {r.id for r in member.roles}
    if MANAGER_ROLE_ID   in roles: return "⭐ Server Manager"
    if MODERATOR_ROLE_ID in roles: return "🛡️ Moderator"
    if STAFF_ROLE_ID     in roles: return "📞 Support Staff"
    return "👤 Staff Member"

# ─────────────────────────────────────────────
#  AUTOCOMPLETE HELPERS
# ─────────────────────────────────────────────
async def active_ticket_user_ids(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """Autocomplete for user_id fields – searches active tickets by name or ID."""
    tickets = await ticket_manager.get_all_open_tickets()
    choices = []
    for t in tickets:
        label = f"{t.user_name} ({t.user_id})"
        if current.lower() in label.lower() or not current:
            choices.append(app_commands.Choice(name=label, value=str(t.user_id)))
        if len(choices) >= 25:
            break
    return choices

async def ticket_type_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    options = ["web", "dm", "forced"]
    return [app_commands.Choice(name=o, value=o) for o in options if current.lower() in o]

# ─────────────────────────────────────────────
#  UTILITY COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="embed", description="Send a custom embed message")
@is_whitelisted()
@is_staff()
async def embed_command(
    interaction: discord.Interaction,
    title: str, description: str,
    color: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None,
):
    target = channel or interaction.channel
    color_map = {
        'red': 0xef4444, 'green': 0x22c55e, 'blue': 0x3b82f6,
        'yellow': 0xeab308, 'purple': 0xa855f7, 'orange': 0xf97316,
        'pink': 0xec4899,
    }
    embed_color = color_map.get((color or '').lower(), 0xef4444)
    embed = discord.Embed(
        title=title, description=description, color=embed_color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Sent by {interaction.user.display_name}")
    await target.send(embed=embed)
    await interaction.response.send_message(f"✅ Embed sent to {target.mention}", ephemeral=True)

@bot.tree.command(name="message", description="Send a direct message to a user")
@is_whitelisted()
@is_staff()
async def message_command(interaction: discord.Interaction, user: discord.User, message: str):
    try:
        embed = discord.Embed(
            title="📨 Message from Staff", description=message,
            color=0x3b82f6, timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="rawr.xyz Support Team")
        await user.send(embed=embed)
        await interaction.response.send_message(f"✅ Message sent to {user.mention}", ephemeral=True)
        logger.info(f"Staff {interaction.user.name} sent DM to {user.name}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Cannot DM this user (DMs disabled)", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)[:100]}", ephemeral=True)

# ─────────────────────────────────────────────
#  MODERATION COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="ban", description="Ban a user from the server")
@is_whitelisted()
@is_moderator()
async def ban_command(
    interaction: discord.Interaction,
    user: discord.User,
    reason: Optional[str] = None,
    delete_days: int = 7,
):
    if user.id in (OWNER_ID, interaction.user.id):
        await interaction.response.send_message("❌ Cannot ban this user.", ephemeral=True)
        return
    try:
        await interaction.guild.ban(user, reason=reason or "No reason provided", delete_message_days=delete_days)
        embed = discord.Embed(
            title="🔨 User Banned",
            description=f"**User:** {user.mention}\n**ID:** {user.id}\n**Reason:** {reason or 'No reason provided'}\n**Deleted Messages:** {delete_days}d",
            color=0xef4444, timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Banned by {interaction.user.name}")
        await interaction.response.send_message(embed=embed)
        Analytics.increment("bans")
        try: await user.send(f"🔨 You were banned from **{interaction.guild.name}**.\nReason: {reason or 'No reason provided'}")
        except: pass
    except discord.Forbidden:
        await interaction.response.send_message("❌ No permission to ban.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a user from the server")
@is_whitelisted()
@is_moderator()
async def kick_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id in (OWNER_ID, interaction.user.id):
        await interaction.response.send_message("❌ Cannot kick this user.", ephemeral=True)
        return
    try:
        member = interaction.guild.get_member(user.id)
        if member: await member.kick(reason=reason or "No reason provided")
        embed = discord.Embed(
            title="👢 User Kicked",
            description=f"**User:** {user.mention}\n**ID:** {user.id}\n**Reason:** {reason or 'No reason provided'}",
            color=0xf97316, timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"Kicked by {interaction.user.name}")
        await interaction.response.send_message(embed=embed)
        Analytics.increment("kicks")
        try: await user.send(f"👢 You were kicked from **{interaction.guild.name}**.\nReason: {reason or 'No reason provided'}")
        except: pass
    except discord.Forbidden:
        await interaction.response.send_message("❌ No permission to kick.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="timeout", description="Timeout a user (e.g. 1h, 30m, 1d)")
@is_whitelisted()
@is_moderator()
async def timeout_command(
    interaction: discord.Interaction,
    user: discord.User,
    duration: str,
    reason: Optional[str] = None,
):
    from datetime import timedelta
    if user.id in (OWNER_ID, interaction.user.id):
        await interaction.response.send_message("❌ Cannot timeout this user.", ephemeral=True)
        return
    duration_map = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    try:
        amount  = int(duration[:-1])
        unit    = duration[-1].lower()
        seconds = amount * duration_map.get(unit, 60)
        if seconds > 2419200:
            await interaction.response.send_message("❌ Timeout cannot exceed 28 days.", ephemeral=True)
            return
        member = interaction.guild.get_member(user.id)
        if not member:
            await interaction.response.send_message("❌ User not in server.", ephemeral=True)
            return
        await member.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=reason or "No reason provided")
        embed = discord.Embed(
            title="⏰ User Timed Out",
            description=f"**User:** {user.mention}\n**Duration:** {duration}\n**Reason:** {reason or 'No reason provided'}",
            color=0xeab308, timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=f"By {interaction.user.name}")
        await interaction.response.send_message(embed=embed)
        Analytics.increment("timeouts")
    except ValueError:
        await interaction.response.send_message("❌ Invalid format. Use e.g. 1h, 30m, 1d", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="warn", description="Issue a warning to a user")
@is_whitelisted()
@is_staff()
async def warn_command(interaction: discord.Interaction, user: discord.User, reason: str):
    embed = discord.Embed(
        title="⚠️ Warning Issued",
        description=f"**User:** {user.mention}\n**Reason:** {reason}",
        color=0xeab308, timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"By {interaction.user.name}")
    await interaction.response.send_message(embed=embed)
    Analytics.increment("warns")
    try:
        dm = discord.Embed(
            title="⚠️ You Have Been Warned",
            description=f"**Server:** {interaction.guild.name}\n**Reason:** {reason}\n**By:** {interaction.user.name}",
            color=0xeab308, timestamp=datetime.now(timezone.utc)
        )
        await user.send(embed=dm)
    except: pass

# ─────────────────────────────────────────────
#  TICKET USER MANAGEMENT
# ─────────────────────────────────────────────
@bot.tree.command(name="adduser", description="Add another user to the current ticket")
@is_whitelisted()
@is_staff()
async def adduser_command(interaction: discord.Interaction, user: discord.User):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
        return
    await interaction.channel.set_permissions(user, read_messages=True, send_messages=True)
    ok = await ticket_manager.add_user_to_ticket(ticket.user_id, user.id)
    if ok:
        embed = discord.Embed(
            title="👤 User Added", description=f"Added {user.mention} to this ticket.",
            color=0x00ff00, timestamp=datetime.now(timezone.utc)
        )
        await interaction.response.send_message(embed=embed)
        try: await user.send(f"📋 You were added to a support ticket in {interaction.guild.name}.")
        except: pass
    else:
        await interaction.response.send_message("❌ Failed to add user.", ephemeral=True)

@bot.tree.command(name="removeuser", description="Remove a user from the current ticket")
@is_whitelisted()
@is_manager()
async def removeuser_command(interaction: discord.Interaction, user: discord.User):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True)
        return
    if user.id == ticket.user_id:
        await interaction.response.send_message("❌ Cannot remove the ticket owner.", ephemeral=True)
        return
    await interaction.channel.set_permissions(user, overwrite=None)
    ok = await ticket_manager.remove_user_from_ticket(ticket.user_id, user.id)
    if ok:
        embed = discord.Embed(
            title="👤 User Removed", description=f"Removed {user.mention} from this ticket.",
            color=0xffaa00, timestamp=datetime.now(timezone.utc)
        )
        await interaction.response.send_message(embed=embed)
        try: await user.send(f"📋 You were removed from a support ticket in {interaction.guild.name}.")
        except: pass
    else:
        await interaction.response.send_message("❌ Failed to remove user.", ephemeral=True)

# ─────────────────────────────────────────────
#  OWNER COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="giverole", description="Give a role to a user (Owner only)")
@is_owner()
async def giverole_command(interaction: discord.Interaction, user: discord.User, role: discord.Role):
    member = interaction.guild.get_member(user.id)
    if not member:
        await interaction.response.send_message("❌ User not in server.", ephemeral=True)
        return
    try:
        await member.add_roles(role, reason=f"Given by {interaction.user.name}")
        await interaction.response.send_message(f"✅ Gave {role.mention} to {user.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="removerole", description="Remove a role from a user (Owner only)")
@is_owner()
async def removerole_command(interaction: discord.Interaction, user: discord.User, role: discord.Role):
    member = interaction.guild.get_member(user.id)
    if not member:
        await interaction.response.send_message("❌ User not in server.", ephemeral=True)
        return
    try:
        await member.remove_roles(role, reason=f"Removed by {interaction.user.name}")
        await interaction.response.send_message(f"✅ Removed {role.mention} from {user.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="broadcast", description="Broadcast to all staff via DM (Owner only)")
@is_owner()
async def broadcast_command(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)
    staff_members: Set[discord.Member] = set()
    for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID, MODERATOR_ROLE_ID]:
        if rid:
            role = interaction.guild.get_role(rid)
            if role: staff_members.update(role.members)
    embed = discord.Embed(
        title="📢 Staff Announcement", description=message,
        color=0xef4444, timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Sent by {interaction.user.name}")
    count = 0
    for member in staff_members:
        try:
            await member.send(embed=embed)
            count += 1
            await asyncio.sleep(0.5)
        except: pass
    await interaction.followup.send(f"✅ Broadcast sent to {count} staff members.", ephemeral=True)

@bot.tree.command(name="serverinfo", description="Get server information (Owner only)")
@is_owner()
async def serverinfo_command(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=f"📊 Server Info – {guild.name}", color=0x3b82f6)
    if guild.icon: embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner",       value=guild.owner.mention if guild.owner else "Unknown", inline=True)
    embed.add_field(name="Created",     value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Members",     value=guild.member_count, inline=True)
    embed.add_field(name="Channels",    value=len(guild.channels), inline=True)
    embed.add_field(name="Roles",       value=len(guild.roles), inline=True)
    embed.add_field(name="Boost Level", value=guild.premium_tier, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="purge", description="Delete messages in a channel (Owner only)")
@is_owner()
async def purge_command(
    interaction: discord.Interaction,
    amount: int,
    channel: Optional[discord.TextChannel] = None,
):
    if amount > 100:
        await interaction.response.send_message("❌ Max 100 messages at once.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    try:
        deleted = await target.purge(limit=amount)
        await interaction.followup.send(f"✅ Deleted {len(deleted)} messages in {target.mention}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ {str(e)[:100]}", ephemeral=True)

# ─────────────────────────────────────────────
#  WHITELIST COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="whitelist_add", description="Add a user or role to the whitelist")
@is_whitelisted()
async def whitelist_add(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    if user:
        await Storage.add_whitelist_user(user.id, interaction.user.name)
        whitelisted_users.add(user.id)
        await interaction.response.send_message(f"✅ Added {user.mention} to whitelist", ephemeral=True)
    elif role:
        await Storage.add_whitelist_role(role.id, interaction.user.name)
        whitelisted_roles.add(role.id)
        await interaction.response.send_message(f"✅ Added {role.mention} role to whitelist", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Specify a user or role.", ephemeral=True)

@bot.tree.command(name="whitelist_remove", description="Remove a user or role from the whitelist")
@is_whitelisted()
async def whitelist_remove(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    if user:
        await Storage.remove_whitelist(user.id, 'user')
        whitelisted_users.discard(user.id)
        await interaction.response.send_message(f"✅ Removed {user.mention} from whitelist", ephemeral=True)
    elif role:
        await Storage.remove_whitelist(role.id, 'role')
        whitelisted_roles.discard(role.id)
        await interaction.response.send_message(f"✅ Removed {role.mention} from whitelist", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Specify a user or role.", ephemeral=True)

@bot.tree.command(name="whitelist_list", description="List all whitelisted users and roles")
@is_whitelisted()
async def whitelist_list(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 Whitelist", color=0x00ff00)
    users_list = []
    for uid in whitelisted_users:
        try:
            u = await bot.fetch_user(uid)
            users_list.append(f"{u.name} ({uid})")
        except:
            users_list.append(f"Unknown ({uid})")
    roles_list = []
    for rid in whitelisted_roles:
        r = interaction.guild.get_role(rid)
        roles_list.append(f"{r.name} ({rid})" if r else f"Unknown ({rid})")
    embed.add_field(name="👤 Users", value="\n".join(users_list) or "None", inline=False)
    embed.add_field(name="🎭 Roles", value="\n".join(roles_list) or "None", inline=False)
    embed.set_footer(text=f"{len(whitelisted_users)} users, {len(whitelisted_roles)} roles")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─────────────────────────────────────────────
#  BLACKLIST COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="blacklist", description="Blacklist a user from creating tickets")
@is_whitelisted()
@is_staff()
async def blacklist_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id == OWNER_ID:
        await interaction.response.send_message("❌ Cannot blacklist the owner.", ephemeral=True)
        return
    await Storage.add_blacklist(user.id, reason or "No reason provided", interaction.user.name)
    blacklisted_users.add(user.id)
    embed = discord.Embed(
        title="🚫 User Blacklisted",
        description=f"**User:** {user.mention}\n**Reason:** {reason or 'No reason provided'}",
        color=0xef4444, timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"By {interaction.user.name}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unblacklist", description="Remove a user from the blacklist")
@is_whitelisted()
@is_staff()
async def unblacklist_command(interaction: discord.Interaction, user: discord.User):
    if user.id not in blacklisted_users:
        await interaction.response.send_message(f"❌ {user.mention} is not blacklisted.", ephemeral=True)
        return
    await Storage.remove_blacklist(user.id)
    blacklisted_users.discard(user.id)
    embed = discord.Embed(
        title="✅ User Unblacklisted", description=f"**User:** {user.mention}",
        color=0x00ff00, timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="blacklist_list", description="List all blacklisted users")
@is_whitelisted()
@is_staff()
async def blacklist_list(interaction: discord.Interaction):
    if not blacklisted_users:
        await interaction.response.send_message("📭 No blacklisted users.", ephemeral=True)
        return
    embed = discord.Embed(title="🚫 Blacklisted Users", color=0xef4444)
    lines = []
    for uid in blacklisted_users:
        try:
            u = await bot.fetch_user(uid)
            lines.append(f"• {u.name} ({uid})")
        except:
            lines.append(f"• Unknown ({uid})")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Total: {len(blacklisted_users)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─────────────────────────────────────────────
#  TICKET COMMANDS (with autocomplete)
# ─────────────────────────────────────────────
@bot.tree.command(name="force_ticket", description="Force open a ticket for a user")
@is_whitelisted()
@is_staff()
async def force_ticket_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    if user.id in blacklisted_users:
        await interaction.response.send_message(f"❌ {user.mention} is blacklisted.", ephemeral=True)
        return
    if await ticket_manager.get_ticket_by_user(user.id):
        await interaction.response.send_message(f"❌ {user.mention} already has an open ticket.", ephemeral=True)
        return
    await interaction.response.defer()
    guild = interaction.guild
    category = bot.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    if not category:
        category = discord.utils.get(guild.categories, name="SUPPORT TICKETS") \
                   or await guild.create_category("SUPPORT TICKETS")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
        user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
        if rid:
            role = guild.get_role(rid)
            if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    channel = await guild.create_text_channel(
        f"forced-{user.name.lower().replace(' ','-')}", category=category, overwrites=overwrites
    )
    await ticket_manager.create_ticket(user.id, user.name, channel.id, TicketType.FORCED, interaction.user.name)
    embed = discord.Embed(
        title="🎫 Force Ticket Created",
        description=f"**User:** {user.mention}\n**By:** {interaction.user.mention}\n**Reason:** {reason or 'No reason provided'}",
        color=0xffaa00, timestamp=datetime.now(timezone.utc)
    )
    await channel.send(embed=embed)
    try: await user.send("📋 A support ticket has been opened for you by staff.")
    except: pass
    await interaction.followup.send(f"✅ Force ticket for {user.mention} in {channel.mention}")

@bot.tree.command(name="force_transfer", description="Force transfer a ticket to another staff member")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def force_transfer_command(
    interaction: discord.Interaction,
    user_id: str,
    new_staff: discord.Member,
):
    try: uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID", ephemeral=True); return
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        await interaction.response.send_message(f"❌ No active ticket for that user.", ephemeral=True); return
    new_staff_role = get_staff_role_name(new_staff)
    result = await ticket_manager.force_transfer(uid, new_staff.id, new_staff.display_name, new_staff_role)
    if result:
        embed = discord.Embed(
            title="🔄 Ticket Force Transferred",
            description=f"**User:** {ticket.user_name}\n**New Staff:** {new_staff.mention} ({new_staff_role})\n**By:** {interaction.user.mention}",
            color=0xffaa00, timestamp=datetime.now(timezone.utc)
        )
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        try: await new_staff.send(f"📋 You were assigned to ticket for {ticket.user_name}.")
        except: pass
        await interaction.response.send_message(f"✅ Ticket transferred to {new_staff.mention}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Failed to transfer ticket", ephemeral=True)

@bot.tree.command(name="history", description="Show a user's ticket history")
@is_whitelisted()
@is_staff()
async def history_command(interaction: discord.Interaction, user: discord.User):
    history = await Storage.get_user_history(user.id)
    embed = discord.Embed(
        title=f"📜 Ticket History – {user.name}",
        description=(f"**User ID:** {user.id}\n"
                     f"**Blacklisted:** {'Yes' if user.id in blacklisted_users else 'No'}\n"
                     f"**Whitelisted:** {'Yes' if user.id in whitelisted_users else 'No'}"),
        color=0x3b82f6
    )
    if not history:
        embed.add_field(name="No Tickets", value="No history found.", inline=False)
    else:
        for idx, t in enumerate(reversed(history[-10:]), 1):
            created_at = datetime.fromisoformat(t['created_at']).strftime("%Y-%m-%d %H:%M")
            status_emoji = "✅" if t.get('status') == 'closed' else "🟢"
            val = (f"**Type:** {t.get('ticket_type','?').upper()}\n"
                   f"**Status:** {status_emoji} {t.get('status','?')}\n"
                   f"**Created:** {created_at}\n"
                   f"**Messages:** {t.get('message_count',0)}")
            if t.get('claimed_by_name'): val += f"\n**Claimed by:** {t['claimed_by_name']}"
            if t.get('resolved_by'):     val += f"\n**Resolved by:** {t['resolved_by']}"
            embed.add_field(name=f"Ticket #{idx}", value=val, inline=False)
    embed.set_footer(text=f"Total: {len(history)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reply", description="Reply to a ticket")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def reply_command(interaction: discord.Interaction, message: str, user_id: Optional[str] = None):
    await interaction.response.defer()
    ticket          = None
    target_user_id  = None

    if user_id:
        try: target_user_id = int(user_id)
        except ValueError:
            await interaction.followup.send("❌ Invalid user ID", ephemeral=True); return
        ticket = await ticket_manager.get_ticket_by_user(target_user_id)
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: target_user_id = ticket.user_id

    if not ticket or not target_user_id:
        await interaction.followup.send("❌ No active ticket found.", ephemeral=True); return

    staff_role = get_staff_role_name(interaction.user)
    author_str = f"{interaction.user.display_name} ({staff_role})"

    if ticket.ticket_type in [TicketType.DM, TicketType.FORCED]:
        try:
            user = await bot.fetch_user(target_user_id)
            embed = discord.Embed(
                title="💬 Support Staff Response", description=message,
                color=0x00ff00, timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=author_str, icon_url=interaction.user.display_avatar.url)
            embed.set_footer(text="Reply to this DM to continue")
            await user.send(embed=embed)
            log_embed = discord.Embed(
                description=f"**Staff Reply ({staff_role}):**\n{message}",
                color=0x00ff00, timestamp=datetime.now(timezone.utc)
            )
            channel = bot.get_channel(ticket.channel_id)
            if channel: await channel.send(embed=log_embed)
            await ticket_manager.add_message(target_user_id, message, author_str, "staff")
            await interaction.followup.send(f"✅ Reply sent to {user.name}")
        except discord.Forbidden:
            await interaction.followup.send("❌ Cannot DM user – DMs may be disabled")
        except Exception as e:
            await interaction.followup.send(f"❌ {str(e)[:100]}")

    elif ticket.ticket_type == TicketType.WEB:
        reply_msg = {
            'type': 'reply', 'user': author_str,
            'text': message, 'origin': 'discord',
            'target': ticket.user_name,
        }
        await broadcast_to_web(reply_msg)
        embed = discord.Embed(
            description=message, color=0x00ff00,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"💬 Staff Reply ({staff_role}) → {ticket.user_name}")
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        await ticket_manager.add_message(target_user_id, message, author_str, "staff-web")
        await interaction.followup.send(f"✅ Reply sent to web user {ticket.user_name}")

@bot.tree.command(name="claim", description="Claim a ticket")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def claim_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    target_user_id = None
    ticket         = None
    if user_id:
        try: target_user_id = int(user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID", ephemeral=True); return
        ticket = await ticket_manager.get_ticket_by_user(target_user_id)
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: target_user_id = ticket.user_id
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True); return
    if ticket.status == TicketStatus.CLAIMED:
        await interaction.response.send_message(f"❌ Already claimed by {ticket.claimed_by_name}", ephemeral=True); return
    staff_role = get_staff_role_name(interaction.user)
    claimed = await ticket_manager.claim_ticket(target_user_id, interaction.user.id, interaction.user.display_name, staff_role)
    if claimed:
        embed = discord.Embed(
            title="✅ Ticket Claimed",
            description=f"**User:** {ticket.user_name}\n**Claimed by:** {interaction.user.mention} ({staff_role})",
            color=0x00ff00, timestamp=datetime.now(timezone.utc)
        )
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        if ticket.ticket_type in [TicketType.DM, TicketType.FORCED]:
            try:
                u = await bot.fetch_user(ticket.user_id)
                await u.send(f"✅ **Ticket Claimed**\n{interaction.user.display_name} ({staff_role}) is now handling your ticket.")
            except: pass
        await interaction.response.send_message(f"✅ Claimed ticket for {ticket.user_name}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Failed to claim ticket", ephemeral=True)

@bot.tree.command(name="resolve", description="Mark a ticket as resolved")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def resolve_command(
    interaction: discord.Interaction,
    reason: str = "Issue resolved",
    user_id: Optional[str] = None,
):
    target_user_id = None
    ticket         = None
    if user_id:
        try: target_user_id = int(user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID", ephemeral=True); return
        ticket = await ticket_manager.get_ticket_by_user(target_user_id)
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket: target_user_id = ticket.user_id
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True); return
    staff_role = get_staff_role_name(interaction.user)
    resolved = await ticket_manager.resolve_ticket(target_user_id, interaction.user.display_name, staff_role, reason)
    if resolved:
        embed = discord.Embed(
            title="✅ Ticket Resolved",
            description=(f"**User:** {ticket.user_name}\n**Reason:** {reason}\n"
                         f"**By:** {interaction.user.mention} ({staff_role})\n\n"
                         "Channel closes in 30 seconds."),
            color=0xffaa00, timestamp=datetime.now(timezone.utc)
        )
        channel = bot.get_channel(ticket.channel_id)
        if channel: await channel.send(embed=embed)
        if ticket.ticket_type in [TicketType.DM, TicketType.FORCED]:
            try:
                u = await bot.fetch_user(ticket.user_id)
                await u.send(f"✅ **Ticket Resolved**\nReason: {reason}\n\nThank you for contacting support!")
            except: pass
        await interaction.response.send_message(f"✅ Resolved for {ticket.user_name}. Closing in 30s…", ephemeral=True)
        await asyncio.sleep(30)
        await close_ticket_by_user(target_user_id, channel, interaction.user.name, reason)
    else:
        await interaction.response.send_message("❌ Failed to resolve ticket", ephemeral=True)

@bot.tree.command(name="close", description="Close the current ticket")
@is_whitelisted()
@is_staff()
async def close_command(interaction: discord.Interaction, reason: str = "No reason provided"):
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.response.send_message("❌ Not a ticket channel.", ephemeral=True); return
    await interaction.response.send_message(f"🔒 Closing in 5 seconds…\n**Reason:** {reason}")
    await asyncio.sleep(5)
    await close_ticket_by_user(ticket.user_id, interaction.channel, interaction.user.name, reason)

@bot.tree.command(name="transcript", description="Generate and send a ticket transcript")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def transcript_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    """Generate an HTML transcript for a ticket and post it as a file."""
    await interaction.response.defer(ephemeral=True)
    ticket = None
    if user_id:
        try: ticket = await ticket_manager.get_ticket_by_user(int(user_id))
        except ValueError: pass
    if not ticket:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    if not ticket:
        await interaction.followup.send("❌ No active ticket found.", ephemeral=True); return

    path = save_transcript(ticket)
    try:
        await interaction.followup.send(
            content=f"📄 Transcript for **{ticket.user_name}** ({ticket.message_count} messages)",
            file=discord.File(path),
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to send transcript: {str(e)[:100]}", ephemeral=True)

@bot.tree.command(name="tickets", description="List all active tickets")
@is_whitelisted()
@is_staff()
async def tickets_command(interaction: discord.Interaction):
    tickets = await ticket_manager.get_all_open_tickets()
    if not tickets:
        await interaction.response.send_message("📭 No active tickets", ephemeral=True); return
    embed = discord.Embed(title=f"📋 Active Tickets ({len(tickets)})", color=0x3b82f6)
    for ticket in tickets:
        status_emoji = "🟢" if ticket.status == TicketStatus.OPEN else "🟡"
        claimed_by   = f"{ticket.claimed_by_name}" if ticket.claimed_by_name else "Unclaimed"
        type_icon    = "🌐" if ticket.ticket_type == TicketType.WEB else "💬" if ticket.ticket_type == TicketType.DM else "🔨"
        channel      = bot.get_channel(ticket.channel_id)
        idle_min     = int(ticket.idle_seconds() / 60)
        embed.add_field(
            name=f"{status_emoji} {type_icon} {ticket.user_name}",
            value=(f"Claimed: {claimed_by} | Messages: {ticket.message_count} | Idle: {idle_min}m\n"
                   f"Channel: {channel.mention if channel else 'Unknown'}"),
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="info", description="Get info about a specific ticket")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def ticket_info_command(interaction: discord.Interaction, user_id: str):
    try: uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID", ephemeral=True); return
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True); return
    embed = discord.Embed(title=f"📋 Ticket Info – {ticket.user_name}", color=0x3b82f6)
    embed.add_field(name="User ID",  value=str(ticket.user_id), inline=True)
    embed.add_field(name="Type",     value=ticket.ticket_type.value, inline=True)
    embed.add_field(name="Status",   value=ticket.status.value, inline=True)
    embed.add_field(name="Created",  value=ticket.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=True)
    embed.add_field(name="Messages", value=str(ticket.message_count), inline=True)
    idle_min = int(ticket.idle_seconds() / 60)
    embed.add_field(name="Idle",     value=f"{idle_min}m", inline=True)
    if ticket.claimed_by_name:
        embed.add_field(name="Handled By", value=f"{ticket.claimed_by_name} ({ticket.claimed_by_role})", inline=True)
    channel = bot.get_channel(ticket.channel_id)
    if channel: embed.add_field(name="Channel", value=channel.mention, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="transfer", description="Transfer a ticket to another staff member")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def transfer_command(interaction: discord.Interaction, user_id: str, new_staff: discord.Member):
    try: uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID", ephemeral=True); return
    ticket = await ticket_manager.get_ticket_by_user(uid)
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True); return
    if ticket.status != TicketStatus.CLAIMED:
        await interaction.response.send_message("❌ Ticket must be claimed before transferring.", ephemeral=True); return
    new_staff_role = get_staff_role_name(new_staff)
    old_staff      = f"{ticket.claimed_by_name} ({ticket.claimed_by_role})"
    async with ticket_manager.lock:
        ticket.claimed_by      = new_staff.id
        ticket.claimed_by_name = new_staff.display_name
        ticket.claimed_by_role = new_staff_role
        ticket.claimed_at      = datetime.now(timezone.utc)
        ticket.history.append({
            'action': 'transferred',
            'from': old_staff,
            'to': f"{new_staff.display_name} ({new_staff_role})",
            'at': datetime.now(timezone.utc).isoformat()
        })
    embed = discord.Embed(
        title="🔄 Ticket Transferred",
        description=f"**User:** {ticket.user_name}\n**From:** {old_staff}\n**To:** {new_staff.mention} ({new_staff_role})",
        color=0xffaa00, timestamp=datetime.now(timezone.utc)
    )
    channel = bot.get_channel(ticket.channel_id)
    if channel: await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Ticket transferred to {new_staff.display_name}", ephemeral=True)

@bot.tree.command(name="note", description="Add a private note to a ticket")
@is_whitelisted()
@is_staff()
@app_commands.autocomplete(user_id=active_ticket_user_ids)
async def note_command(interaction: discord.Interaction, user_id: str, note: str):
    try: uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID", ephemeral=True); return
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
    await interaction.response.send_message(f"✅ Note added for {ticket.user_name}", ephemeral=True)

# ─────────────────────────────────────────────
#  ANALYTICS COMMAND
# ─────────────────────────────────────────────
@bot.tree.command(name="analytics", description="Show bot analytics and staff performance (Manager only)")
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

    embed.add_field(
        name="🎫 Tickets",
        value=(f"Created: **{summary['tickets_created']}**\n"
               f"Resolved: **{summary['tickets_resolved']}**\n"
               f"Closed: **{summary['tickets_closed']}**"),
        inline=True
    )
    embed.add_field(
        name="⏱️ Response Times",
        value=(f"Avg First Response: **{fmt_time(int(avg_rt))}**\n"
               f"Avg Resolution: **{fmt_time(int(avg_rst))}**"),
        inline=True
    )
    embed.add_field(
        name="🛡️ Moderation",
        value=(f"Bans: **{summary['bans']}**\n"
               f"Kicks: **{summary['kicks']}**\n"
               f"Timeouts: **{summary['timeouts']}**\n"
               f"Warns: **{summary['warns']}**"),
        inline=True
    )

    if summary['top_staff']:
        staff_lines = "\n".join(f"{i+1}. **{name}** – {count} actions" for i, (name, count) in enumerate(summary['top_staff']))
        embed.add_field(name="🏆 Top Staff", value=staff_lines, inline=False)

    if summary['last_7_days']:
        vol_lines = "\n".join(f"`{date}` – {count} ticket(s)" for date, count in summary['last_7_days'].items())
        embed.add_field(name="📅 Last 7 Days Volume", value=vol_lines, inline=False)

    embed.set_footer(text="Data is cumulative since bot start")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─────────────────────────────────────────────
#  USER COMMANDS
# ─────────────────────────────────────────────
@bot.tree.command(name="help", description="Get help and bot information")
@is_whitelisted()
async def help_command(interaction: discord.Interaction):
    try:
        embed = discord.Embed(
            title="🆘 Rawr.xyz Support Bot",
            description="Here's how to get support and use the bot:",
            color=0xef4444
        )
        embed.add_field(name="📬 Getting Support", value="DM this bot with your issue! A ticket will be created automatically.", inline=False)
        embed.add_field(
            name="💬 Ticket Commands",
            value=("`/reply` `/claim` `/resolve` `/close`\n"
                   "`/force_ticket` `/force_transfer`\n"
                   "`/adduser` `/removeuser`\n"
                   "`/history` `/tickets` `/info`\n"
                   "`/transfer` `/note` `/transcript`"),
            inline=False
        )
        embed.add_field(
            name="🛡️ Moderation",
            value="`/ban` `/kick` `/timeout` `/warn`",
            inline=False
        )
        embed.add_field(
            name="📨 Utility",
            value="`/embed` `/message` `/stats` `/ping` `/analytics`",
            inline=False
        )
        embed.add_field(name="🌐 Website", value="Visit **rawrs.zapto.org**", inline=False)
        embed.set_footer(text="Support is available 24/7")
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("✅ Help sent via DM!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Enable DMs to receive help.", ephemeral=True)

@bot.tree.command(name="stats", description="Show bot statistics")
@is_whitelisted()
async def stats_command(interaction: discord.Interaction):
    uptime = datetime.now(timezone.utc) - bot.boot_time
    d, h, m = uptime.days, uptime.seconds // 3600, (uptime.seconds % 3600) // 60
    ticket_stats = await ticket_manager.get_stats()
    embed = discord.Embed(title="📊 Bot Statistics", color=0xef4444)
    embed.add_field(name="⏰ Uptime",          value=f"{d}d {h}h {m}m", inline=True)
    embed.add_field(name="⚡ Latency",          value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="🎫 Active Tickets",   value=str(ticket_stats['total']), inline=True)
    embed.add_field(name="✅ Handled Tickets",  value=str(ticket_stats['handled']), inline=True)
    embed.add_field(name="👥 Whitelisted",      value=f"{len(whitelisted_users)} users, {len(whitelisted_roles)} roles", inline=True)
    embed.add_field(name="🚫 Blacklisted",      value=str(len(blacklisted_users)), inline=True)
    embed.add_field(name="💾 Storage",          value="AWS DynamoDB" if AWS_AVAILABLE else "Local Files", inline=True)
    embed.add_field(name="🌐 SSE Clients",      value=str(len(sse_clients)), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
@is_whitelisted()
async def ping_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)}ms`", ephemeral=True)

# ─────────────────────────────────────────────
#  ERROR HANDLING
# ─────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("⛔ You don't have permission to use this command.", ephemeral=True)
    else:
        logger.error(f"Command error in /{interaction.command.name if interaction.command else '?'}: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ An error occurred: {str(error)[:100]}", ephemeral=True)

# ─────────────────────────────────────────────
#  DISCORD EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    # Blacklist check for DMs
    if isinstance(message.channel, discord.DMChannel):
        if message.author.id in blacklisted_users:
            await message.author.send("🚫 You are blacklisted from using this support system.")
            return
        await handle_dm(message)
        return
    await bot.process_commands(message)

async def handle_dm(message: discord.Message):
    ticket = await ticket_manager.get_ticket_by_user(message.author.id)

    if ticket:
        await ticket_manager.add_message(message.author.id, message.content, message.author.name, "dm")
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            embed = discord.Embed(
                description=message.content, color=0xef4444,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
            await channel.send(embed=embed)
            await message.author.send("✅ Message sent to support!")
        return

    # ── Create new ticket ─────────────────────
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await message.author.send("❌ Support system unavailable.")
        return

    category = bot.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
    if not category:
        category = discord.utils.get(guild.categories, name="SUPPORT TICKETS") \
                   or await guild.create_category("SUPPORT TICKETS")

    safe_name    = message.author.name.lower().replace(' ', '-')
    channel_name = f"ticket-{safe_name}"
    overwrites = {
        guild.default_role:  discord.PermissionOverwrite(read_messages=False),
        guild.me:            discord.PermissionOverwrite(read_messages=True, send_messages=True),
        message.author:      discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for rid in [STAFF_ROLE_ID, MANAGER_ROLE_ID]:
        if rid:
            role = guild.get_role(rid)
            if role: overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
    await ticket_manager.create_ticket(message.author.id, message.author.name, channel.id, TicketType.DM)

    # Log first message in transcript
    t = await ticket_manager.get_ticket_by_user(message.author.id)
    if t:
        await ticket_manager.add_message(message.author.id, message.content, message.author.name, "dm")

    header_embed = discord.Embed(
        title="🎫 New Support Ticket",
        description=f"**User:** {message.author.mention}\n**ID:** `{message.author.id}`",
        color=0xef4444, timestamp=datetime.now(timezone.utc)
    )
    await channel.send(embed=header_embed)

    msg_embed = discord.Embed(
        description=message.content, color=0x3b82f6,
        timestamp=datetime.now(timezone.utc)
    )
    msg_embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
    await channel.send(embed=msg_embed)

    class ClaimButton(discord.ui.View):
        def __init__(self, user_id: int):
            super().__init__(timeout=None)
            self.user_id = user_id
            self.claimed = False

        @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="✋")
        async def claim_button(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
            if self.claimed:
                await btn_interaction.response.send_message("❌ Ticket already claimed", ephemeral=True); return
            staff_role = get_staff_role_name(btn_interaction.user)
            result = await ticket_manager.claim_ticket(self.user_id, btn_interaction.user.id, btn_interaction.user.display_name, staff_role)
            if result:
                self.claimed = True
                button.disabled = True
                await btn_interaction.message.edit(view=self)
                await btn_interaction.response.send_message(f"✅ Claimed ticket for {message.author.name}", ephemeral=True)
                try:
                    await message.author.send(f"✅ **Ticket Claimed**\n{staff_role} ({btn_interaction.user.display_name}) is now assisting you.")
                except: pass
                claim_embed = discord.Embed(
                    title="✅ Ticket Claimed",
                    description=f"Claimed by {btn_interaction.user.mention} ({staff_role})",
                    color=0x00ff00, timestamp=datetime.now(timezone.utc)
                )
                await channel.send(embed=claim_embed)
            else:
                await btn_interaction.response.send_message("❌ Already claimed", ephemeral=True)

    await channel.send(view=ClaimButton(message.author.id))
    await message.author.send("✅ Support ticket created! Staff will be with you shortly.")

# ─────────────────────────────────────────────
#  TICKET CLOSE HELPER
# ─────────────────────────────────────────────
async def close_ticket_by_user(
    user_id: int,
    channel: discord.TextChannel = None,
    closed_by: str = None,
    reason: str = None,
):
    ticket = await ticket_manager.close_ticket(
        user_id, channel.id if channel else None, closed_by, reason
    )
    if not ticket or not channel:
        return

    # Save transcript
    transcript_path = save_transcript(ticket)

    try:
        duration = (ticket.closed_at - ticket.created_at).total_seconds()
        h, m = int(duration // 3600), int((duration % 3600) // 60)

        embed = discord.Embed(
            title="📋 Ticket Closed",
            description=(f"**User:** {ticket.user_name}\n**ID:** {ticket.user_id}\n"
                         f"**Type:** {ticket.ticket_type.value}\n"
                         f"**Messages:** {ticket.message_count}\n"
                         f"**Duration:** {h}h {m}m"),
            color=0xef4444, timestamp=datetime.now(timezone.utc)
        )
        if ticket.claimed_by_name:
            embed.add_field(name="Handled By", value=f"{ticket.claimed_by_name} ({ticket.claimed_by_role})", inline=True)
        if ticket.resolved_reason:
            embed.add_field(name="Resolution", value=ticket.resolved_reason, inline=False)
        if reason:
            embed.add_field(name="Closed Reason", value=reason, inline=False)

        log_channel = discord.utils.get(channel.guild.text_channels, name="ticket-logs")
        if not log_channel:
            overwrites = {
                channel.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                channel.guild.me:           discord.PermissionOverwrite(read_messages=True),
            }
            log_channel = await channel.guild.create_text_channel("ticket-logs", overwrites=overwrites)

        await log_channel.send(embed=embed, file=discord.File(transcript_path))

        # Review notification
        if (ticket.ticket_type in [TicketType.DM, TicketType.FORCED]
                and ticket.resolved_at and not ticket.review_sent):
            ticket.review_sent = True
            review_channel = bot.get_channel(REVIEW_CHANNEL_ID)
            if review_channel:
                review_embed = discord.Embed(
                    title="⭐ Ticket Closed",
                    description=(f"**User:** {ticket.user_name}\n**ID:** {ticket.user_id}\n"
                                 f"**Resolved by:** {ticket.resolved_by}\n"
                                 f"**Resolution:** {ticket.resolved_reason}"),
                    color=0x00ff00, timestamp=datetime.now(timezone.utc)
                )
                await review_channel.send(embed=review_embed)

    except Exception as e:
        logger.error(f"Failed during ticket close housekeeping: {e}")

    try:
        await channel.delete()
    except Exception as e:
        logger.error(f"Failed to delete channel: {e}")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    try:
        logger.info(f"🚀 Starting RawrBot on port {PORT}…")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"❌ Failed to start: {e}")
