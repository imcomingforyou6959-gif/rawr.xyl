import os
import discord
import asyncio
import logging
import json
import time
import hashlib
from typing import Optional, Set, Dict, Any, Tuple, List
from datetime import datetime
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web
from collections import deque
from enum import Enum

# --- AWS SETUP ---
USE_AWS = os.getenv('USE_AWS', 'false').lower() == 'true'
AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

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
        
        # Reference your tables
        whitelist_table = dynamodb.Table('rawr_whitelist')
        blacklist_table = dynamodb.Table('rawr_blacklist')
        ticket_history_table = dynamodb.Table('rawr_ticket_history')
        
        logging.info("✅ AWS DynamoDB connected")
    except Exception as e:
        AWS_AVAILABLE = False
        logging.error(f"AWS connection failed: {e}")
else:
    AWS_AVAILABLE = False
    logging.info("Using file-based storage")

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('RawrBot')

# --- CONFIGURATION ---
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

GUILD_ID = int(os.getenv('GUILD_ID', '0'))
if not GUILD_ID:
    raise ValueError("GUILD_ID environment variable not set")

PORT = int(os.getenv('PORT', 8080))
TICKET_CATEGORY_ID = int(os.getenv('TICKET_CATEGORY_ID', '0'))
REVIEW_CHANNEL_ID = 1489438620233240596
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID', '0'))
MANAGER_ROLE_ID = int(os.getenv('MANAGER_ROLE_ID', '0'))

RATE_LIMIT_SECONDS = 5
MAX_MESSAGES_PER_MINUTE = 12

# --- DATA DIRECTORY (Fallback for file storage) ---
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")

# --- STORAGE CLASSES ---
class Storage:
    """Handles all data storage (AWS or local)"""
    
    @staticmethod
    async def add_whitelist_user(user_id: int, added_by: str):
        if AWS_AVAILABLE:
            try:
                whitelist_table.put_item(Item={
                    'type': 'user',
                    'id': user_id,
                    'added_by': added_by,
                    'added_at': datetime.utcnow().isoformat()
                })
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        else:
            try:
                data = {}
                if os.path.exists(WHITELIST_FILE):
                    with open(WHITELIST_FILE, 'r') as f:
                        data = json.load(f)
                else:
                    data = {'users': [], 'roles': []}
                
                if user_id not in data['users']:
                    data['users'].append(user_id)
                
                with open(WHITELIST_FILE, 'w') as f:
                    json.dump(data, f, indent=2)
                return True
            except Exception as e:
                logger.error(f"File error: {e}")
        return False
    
    @staticmethod
    async def add_whitelist_role(role_id: int, added_by: str):
        if AWS_AVAILABLE:
            try:
                whitelist_table.put_item(Item={
                    'type': 'role',
                    'id': role_id,
                    'added_by': added_by,
                    'added_at': datetime.utcnow().isoformat()
                })
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        else:
            try:
                data = {}
                if os.path.exists(WHITELIST_FILE):
                    with open(WHITELIST_FILE, 'r') as f:
                        data = json.load(f)
                else:
                    data = {'users': [], 'roles': []}
                
                if role_id not in data['roles']:
                    data['roles'].append(role_id)
                
                with open(WHITELIST_FILE, 'w') as f:
                    json.dump(data, f, indent=2)
                return True
            except Exception as e:
                logger.error(f"File error: {e}")
        return False
    
    @staticmethod
    async def remove_whitelist(item_id: int, item_type: str = 'user'):
        if AWS_AVAILABLE:
            try:
                whitelist_table.delete_item(Key={'type': item_type, 'id': item_id})
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        else:
            try:
                if os.path.exists(WHITELIST_FILE):
                    with open(WHITELIST_FILE, 'r') as f:
                        data = json.load(f)
                    
                    if item_type == 'user' and item_id in data['users']:
                        data['users'].remove(item_id)
                    elif item_type == 'role' and item_id in data['roles']:
                        data['roles'].remove(item_id)
                    
                    with open(WHITELIST_FILE, 'w') as f:
                        json.dump(data, f, indent=2)
                    return True
            except Exception as e:
                logger.error(f"File error: {e}")
        return False
    
    @staticmethod
    async def get_whitelist():
        if AWS_AVAILABLE:
            try:
                response = whitelist_table.scan()
                users = [item['id'] for item in response.get('Items', []) if item.get('type') == 'user']
                roles = [item['id'] for item in response.get('Items', []) if item.get('type') == 'role']
                return users, roles
            except Exception as e:
                logger.error(f"AWS error: {e}")
        else:
            try:
                if os.path.exists(WHITELIST_FILE):
                    with open(WHITELIST_FILE, 'r') as f:
                        data = json.load(f)
                    return data.get('users', []), data.get('roles', [])
            except Exception as e:
                logger.error(f"File error: {e}")
        return [], []
    
    @staticmethod
    async def add_blacklist(user_id: int, reason: str, blacklisted_by: str):
        if AWS_AVAILABLE:
            try:
                blacklist_table.put_item(Item={
                    'user_id': user_id,
                    'reason': reason,
                    'blacklisted_by': blacklisted_by,
                    'blacklisted_at': datetime.utcnow().isoformat()
                })
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        else:
            try:
                data = {}
                if os.path.exists(BLACKLIST_FILE):
                    with open(BLACKLIST_FILE, 'r') as f:
                        data = json.load(f)
                else:
                    data = {'users': []}
                
                if user_id not in data['users']:
                    data['users'].append(user_id)
                
                with open(BLACKLIST_FILE, 'w') as f:
                    json.dump(data, f, indent=2)
                return True
            except Exception as e:
                logger.error(f"File error: {e}")
        return False
    
    @staticmethod
    async def remove_blacklist(user_id: int):
        if AWS_AVAILABLE:
            try:
                blacklist_table.delete_item(Key={'user_id': user_id})
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        else:
            try:
                if os.path.exists(BLACKLIST_FILE):
                    with open(BLACKLIST_FILE, 'r') as f:
                        data = json.load(f)
                    
                    if user_id in data['users']:
                        data['users'].remove(user_id)
                    
                    with open(BLACKLIST_FILE, 'w') as f:
                        json.dump(data, f, indent=2)
                    return True
            except Exception as e:
                logger.error(f"File error: {e}")
        return False
    
    @staticmethod
    async def get_blacklist():
        if AWS_AVAILABLE:
            try:
                response = blacklist_table.scan()
                return [item['user_id'] for item in response.get('Items', [])]
            except Exception as e:
                logger.error(f"AWS error: {e}")
        else:
            try:
                if os.path.exists(BLACKLIST_FILE):
                    with open(BLACKLIST_FILE, 'r') as f:
                        data = json.load(f)
                    return data.get('users', [])
            except Exception as e:
                logger.error(f"File error: {e}")
        return []
    
    @staticmethod
    async def save_ticket(ticket_data: dict):
        if AWS_AVAILABLE:
            try:
                ticket_history_table.put_item(Item=ticket_data)
                return True
            except Exception as e:
                logger.error(f"AWS error: {e}")
        return False
    
    @staticmethod
    async def get_user_history(user_id: int):
        if AWS_AVAILABLE:
            try:
                response = ticket_history_table.query(
                    KeyConditionExpression=Key('user_id').eq(user_id)
                )
                return response.get('Items', [])
            except Exception as e:
                logger.error(f"AWS error: {e}")
        return []

# --- GLOBAL STORAGE CACHE ---
whitelisted_users: Set[int] = set()
whitelisted_roles: Set[int] = set()
blacklisted_users: Set[int] = set()

async def load_all_data():
    """Load all data from storage"""
    global whitelisted_users, whitelisted_roles, blacklisted_users
    users, roles = await Storage.get_whitelist()
    whitelisted_users = set(users)
    whitelisted_roles = set(roles)
    blacklisted_users = set(await Storage.get_blacklist())
    logger.info(f"Loaded: {len(whitelisted_users)} users, {len(whitelisted_roles)} roles, {len(blacklisted_users)} blacklisted")

# --- TICKET SYSTEM ---
class TicketStatus(Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    CLOSED = "closed"

class TicketType(Enum):
    WEB = "web"
    DM = "dm"
    FORCED = "forced"

class Ticket:
    def __init__(self, user_id: int, user_name: str, channel_id: int, ticket_type: TicketType, created_by: Optional[str] = None):
        self.user_id = user_id
        self.user_name = user_name
        self.channel_id = channel_id
        self.ticket_type = ticket_type
        self.status = TicketStatus.OPEN
        self.claimed_by: Optional[int] = None
        self.claimed_by_name: Optional[str] = None
        self.claimed_by_role: Optional[str] = None
        self.created_at = datetime.utcnow()
        self.claimed_at: Optional[datetime] = None
        self.resolved_at: Optional[datetime] = None
        self.closed_at: Optional[datetime] = None
        self.message_count = 0
        self.created_by = created_by
        self.resolved_by: Optional[str] = None
        self.resolved_reason: Optional[str] = None
        self.resolved_by_role: Optional[str] = None
        self.closed_by: Optional[str] = None
        self.closed_reason: Optional[str] = None
        self.review_sent = False
        self.history: List[Dict] = []

class TicketManager:
    def __init__(self):
        self.tickets: Dict[int, Ticket] = {}
        self.channel_to_user: Dict[int, int] = {}
        self.lock = asyncio.Lock()
        self.handled_count = 0
    
    async def create_ticket(self, user_id: int, user_name: str, channel_id: int, ticket_type: TicketType, created_by: Optional[str] = None) -> Ticket:
        async with self.lock:
            ticket = Ticket(user_id, user_name, channel_id, ticket_type, created_by)
            self.tickets[user_id] = ticket
            self.channel_to_user[channel_id] = user_id
            self.handled_count += 1
            logger.info(f"📫 Ticket #{channel_id} created for {user_name}")
            return ticket
    
    async def get_ticket_by_user(self, user_id: int) -> Optional[Ticket]:
        async with self.lock:
            return self.tickets.get(user_id)
    
    async def get_ticket_by_channel(self, channel_id: int) -> Optional[Ticket]:
        async with self.lock:
            user_id = self.channel_to_user.get(channel_id)
            return self.tickets.get(user_id) if user_id else None
    
    async def get_ticket_by_id(self, ticket_id: int) -> Optional[Ticket]:
        async with self.lock:
            for ticket in self.tickets.values():
                if ticket.channel_id == ticket_id:
                    return ticket
            return None
    
    async def claim_ticket(self, user_id: int, staff_id: int, staff_name: str, staff_role: str) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status == TicketStatus.OPEN:
                ticket.status = TicketStatus.CLAIMED
                ticket.claimed_by = staff_id
                ticket.claimed_by_name = staff_name
                ticket.claimed_by_role = staff_role
                ticket.claimed_at = datetime.utcnow()
                ticket.history.append({
                    'action': 'claimed',
                    'by': staff_name,
                    'role': staff_role,
                    'at': datetime.utcnow().isoformat()
                })
                logger.info(f"✅ Ticket #{ticket.channel_id} claimed by {staff_name}")
                return ticket
            return None
    
    async def resolve_ticket(self, user_id: int, resolved_by: str, resolved_by_role: str, reason: str) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]:
                ticket.status = TicketStatus.RESOLVED
                ticket.resolved_at = datetime.utcnow()
                ticket.resolved_by = resolved_by
                ticket.resolved_reason = reason
                ticket.resolved_by_role = resolved_by_role
                ticket.history.append({
                    'action': 'resolved',
                    'by': resolved_by,
                    'role': resolved_by_role,
                    'reason': reason,
                    'at': datetime.utcnow().isoformat()
                })
                logger.info(f"✅ Ticket #{ticket.channel_id} resolved by {resolved_by}")
                return ticket
            return None
    
    async def close_ticket(self, user_id: int, channel_id: int = None, closed_by: str = None, reason: str = None) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                if channel_id:
                    self.channel_to_user.pop(channel_id, None)
                ticket.status = TicketStatus.CLOSED
                ticket.closed_at = datetime.utcnow()
                ticket.closed_by = closed_by
                ticket.closed_reason = reason
                ticket.history.append({
                    'action': 'closed',
                    'by': closed_by,
                    'reason': reason,
                    'at': datetime.utcnow().isoformat()
                })
                # Save to AWS
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
                    'claimed_at': ticket.claimed_at.isoformat() if ticket.claimed_at else None,
                    'resolved_at': ticket.resolved_at.isoformat() if ticket.resolved_at else None,
                    'closed_at': ticket.closed_at.isoformat(),
                    'message_count': ticket.message_count,
                    'created_by': ticket.created_by,
                    'resolved_by': ticket.resolved_by,
                    'resolved_reason': ticket.resolved_reason,
                    'resolved_by_role': ticket.resolved_by_role,
                    'closed_by': ticket.closed_by,
                    'closed_reason': ticket.closed_reason,
                    'review_sent': ticket.review_sent
                })
                logger.info(f"🔒 Ticket #{ticket.channel_id} closed")
                return ticket
            return None
    
    async def force_transfer(self, user_id: int, new_staff_id: int, new_staff_name: str, new_staff_role: str) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket:
                old_staff = f"{ticket.claimed_by_name} ({ticket.claimed_by_role})" if ticket.claimed_by_name else "Unclaimed"
                ticket.claimed_by = new_staff_id
                ticket.claimed_by_name = new_staff_name
                ticket.claimed_by_role = new_staff_role
                ticket.claimed_at = datetime.utcnow()
                if ticket.status == TicketStatus.OPEN:
                    ticket.status = TicketStatus.CLAIMED
                ticket.history.append({
                    'action': 'force_transferred',
                    'from': old_staff,
                    'to': f"{new_staff_name} ({new_staff_role})",
                    'at': datetime.utcnow().isoformat()
                })
                logger.info(f"🔄 Ticket #{ticket.channel_id} force transferred to {new_staff_name}")
                return ticket
            return None
    
    async def add_message(self, user_id: int, message: str):
        async with self.lock:
            if user_id in self.tickets:
                self.tickets[user_id].message_count += 1
    
    async def get_all_open_tickets(self) -> list[Ticket]:
        async with self.lock:
            return [t for t in self.tickets.values() if t.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]]
    
    async def get_stats(self) -> dict:
        async with self.lock:
            return {
                'total': len(self.tickets),
                'open': sum(1 for t in self.tickets.values() if t.status == TicketStatus.OPEN),
                'claimed': sum(1 for t in self.tickets.values() if t.status == TicketStatus.CLAIMED),
                'resolved': sum(1 for t in self.tickets.values() if t.status == TicketStatus.RESOLVED),
                'handled': self.handled_count
            }

ticket_manager = TicketManager()

# --- RATE LIMITER ---
class RateLimiter:
    def __init__(self):
        self.user_messages: Dict[int, deque] = {}
    
    def can_send(self, user_id: int, content: str) -> Tuple[bool, str]:
        current_time = time.time()
        
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=MAX_MESSAGES_PER_MINUTE)
        
        user_deque = self.user_messages[user_id]
        
        if len(user_deque) >= MAX_MESSAGES_PER_MINUTE:
            oldest = user_deque[0]
            if current_time - oldest < 60:
                return False, f"Rate limited. Try again in {60 - (current_time - oldest):.0f}s"
        
        if user_deque:
            time_since_last = current_time - user_deque[-1]
            if time_since_last < RATE_LIMIT_SECONDS:
                wait = RATE_LIMIT_SECONDS - time_since_last
                return False, f"Wait {wait:.1f}s between messages"
        
        return True, "OK"
    
    def record(self, user_id: int):
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=MAX_MESSAGES_PER_MINUTE)
        self.user_messages[user_id].append(time.time())

rate_limiter = RateLimiter()

# --- SSE BROADCAST FUNCTIONS ---
message_queue = deque(maxlen=100)
sse_clients = set()

async def broadcast_to_web(message: dict):
    if not sse_clients:
        return
    
    message['id'] = f"msg_{int(time.time() * 1000)}_{hashlib.md5(message.get('text', '').encode()).hexdigest()[:6]}"
    message['timestamp'] = datetime.utcnow().isoformat()
    
    message_queue.append(message)
    
    data = f"data: {json.dumps(message)}\n\n"
    dead_clients = set()
    
    for client in sse_clients:
        try:
            await client.write(data.encode())
        except Exception:
            dead_clients.add(client)
    
    for client in dead_clients:
        sse_clients.discard(client)

async def forward_to_discord(message: dict):
    """Forward web message to Discord channel"""
    if not TICKET_CATEGORY_ID:
        return
    
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    
    category = bot.get_channel(TICKET_CATEGORY_ID)
    if not category:
        return
    
    user_name = message['user'].lower().replace(' ', '-')
    channel_name = f"web-{user_name}"
    
    channel = discord.utils.get(category.text_channels, name=channel_name)
    
    if not channel:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            if STAFF_ROLE_ID:
                staff_role = guild.get_role(STAFF_ROLE_ID)
                if staff_role:
                    overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            if MANAGER_ROLE_ID:
                manager_role = guild.get_role(MANAGER_ROLE_ID)
                if manager_role:
                    overwrites[manager_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Web chat from {message['user']}"
            )
            await ticket_manager.create_ticket(hash(message['user']), message['user'], channel.id, TicketType.WEB)
            await channel.send(f"🚀 **Chat Started:** `{message['user']}`\nUse `/reply` to respond.")
        except Exception as e:
            logger.error(f"Failed to create channel: {e}")
            return
    
    embed = discord.Embed(
        description=message['text'],
        color=0xef4444,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=f"🌐 Web: {message['user']}")
    
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send: {e}")

# --- SSE HTTP HANDLERS ---
async def sse_endpoint(request):
    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Access-Control-Allow-Origin': '*',
        'Connection': 'keep-alive'
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
        user_id_hash = hash(data.get('user', 'unknown'))
        
        can_send, error = rate_limiter.can_send(user_id_hash, data.get('text', ''))
        if not can_send:
            return web.json_response({'error': error}, status=429)
        
        rate_limiter.record(user_id_hash)
        
        message = {
            'type': 'chat',
            'user': data.get('user', 'Guest'),
            'text': data.get('text', ''),
            'origin': 'web'
        }
        
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
        'storage': 'AWS DynamoDB' if AWS_AVAILABLE else 'Local Files',
        'whitelist': {'users': len(whitelisted_users), 'roles': len(whitelisted_roles)},
        'blacklist': len(blacklisted_users)
    })

# --- DISCORD BOT ---
class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        self.boot_time = datetime.utcnow()
        self.web_app = None
        self.runner = None
        
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        await load_all_data()
        
        # Start HTTP server for SSE
        self.web_app = web.Application()
        self.web_app.router.add_get('/events', sse_endpoint)
        self.web_app.router.add_post('/send', send_message_endpoint)
        self.web_app.router.add_get('/health', health_check)
        
        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"✅ HTTP server on port {PORT}")
        
        # Sync slash commands
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        
        self.status_task.start()
        logger.info("✅ Bot setup complete")
    
    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        await super().close()
    
    @tasks.loop(seconds=30)
    async def status_task(self):
        stats = await ticket_manager.get_stats()
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{stats['open']} open | rawrs.zapto.org"
            )
        )

bot = RawrBot()

# --- PERMISSION CHECKS ---
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
        return STAFF_ROLE_ID in user_roles or MANAGER_ROLE_ID in user_roles
    return app_commands.check(predicate)

def is_manager():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        user_roles = {role.id for role in interaction.user.roles}
        return MANAGER_ROLE_ID in user_roles
    return app_commands.check(predicate)

def get_staff_role_name(member: discord.Member) -> str:
    """Get the staff role name for display"""
    if member.id == OWNER_ID:
        return "👑 Owner"
    
    roles = member.roles
    if MANAGER_ROLE_ID and any(r.id == MANAGER_ROLE_ID for r in roles):
        return "⭐ Server Manager"
    elif STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in roles):
        return "🛡️ Rawr Staff"
    else:
        return "👤 Staff Member"

# --- WHITELIST COMMANDS ---
@bot.tree.command(name="whitelist_add", description="Add a user or role to the whitelist")
@is_whitelisted()
async def whitelist_add(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    """Add a user or role to the whitelist"""
    if user:
        await Storage.add_whitelist_user(user.id, interaction.user.name)
        whitelisted_users.add(user.id)
        await interaction.response.send_message(f"✅ Added {user.mention} to whitelist", ephemeral=True)
        logger.info(f"Added {user.name} to whitelist")
    elif role:
        await Storage.add_whitelist_role(role.id, interaction.user.name)
        whitelisted_roles.add(role.id)
        await interaction.response.send_message(f"✅ Added {role.mention} role to whitelist", ephemeral=True)
        logger.info(f"Added {role.name} role to whitelist")
    else:
        await interaction.response.send_message("❌ Please specify either a user or a role", ephemeral=True)

@bot.tree.command(name="whitelist_remove", description="Remove a user or role from the whitelist")
@is_whitelisted()
async def whitelist_remove(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    """Remove a user or role from the whitelist"""
    if user:
        await Storage.remove_whitelist(user.id, 'user')
        whitelisted_users.discard(user.id)
        await interaction.response.send_message(f"✅ Removed {user.mention} from whitelist", ephemeral=True)
        logger.info(f"Removed {user.name} from whitelist")
    elif role:
        await Storage.remove_whitelist(role.id, 'role')
        whitelisted_roles.discard(role.id)
        await interaction.response.send_message(f"✅ Removed {role.mention} role from whitelist", ephemeral=True)
        logger.info(f"Removed {role.name} role from whitelist")
    else:
        await interaction.response.send_message("❌ Please specify either a user or a role", ephemeral=True)

@bot.tree.command(name="whitelist_list", description="List all whitelisted users and roles")
@is_whitelisted()
async def whitelist_list(interaction: discord.Interaction):
    """List all whitelisted users and roles"""
    embed = discord.Embed(title="📋 Whitelist", color=0x00ff00)
    
    users_list = []
    for uid in whitelisted_users:
        try:
            user = await bot.fetch_user(uid)
            users_list.append(f"{user.name} ({uid})")
        except:
            users_list.append(f"Unknown User ({uid})")
    
    roles_list = []
    for rid in whitelisted_roles:
        role = interaction.guild.get_role(rid)
        if role:
            roles_list.append(f"{role.name} ({rid})")
        else:
            roles_list.append(f"Unknown Role ({rid})")
    
    embed.add_field(name="👤 Whitelisted Users", value="\n".join(users_list) if users_list else "None", inline=False)
    embed.add_field(name="🎭 Whitelisted Roles", value="\n".join(roles_list) if roles_list else "None", inline=False)
    embed.set_footer(text=f"Total: {len(whitelisted_users)} users, {len(whitelisted_roles)} roles")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- BLACKLIST COMMANDS ---
@bot.tree.command(name="blacklist", description="Blacklist a user from creating tickets")
@is_whitelisted()
@is_staff()
async def blacklist_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    """Blacklist a user from creating tickets"""
    if user.id == OWNER_ID:
        await interaction.response.send_message("❌ Cannot blacklist the bot owner.", ephemeral=True)
        return
    
    await Storage.add_blacklist(user.id, reason or "No reason provided", interaction.user.name)
    blacklisted_users.add(user.id)
    
    embed = discord.Embed(
        title="🚫 User Blacklisted",
        description=f"**User:** {user.mention}\n**User ID:** {user.id}\n**Reason:** {reason or 'No reason provided'}",
        color=0xef4444,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Blacklisted by {interaction.user.name}")
    
    await interaction.response.send_message(embed=embed)
    logger.info(f"User {user.name} blacklisted by {interaction.user.name}")

@bot.tree.command(name="unblacklist", description="Remove a user from the blacklist")
@is_whitelisted()
@is_staff()
async def unblacklist_command(interaction: discord.Interaction, user: discord.User):
    """Remove a user from the blacklist"""
    if user.id not in blacklisted_users:
        await interaction.response.send_message(f"❌ {user.mention} is not blacklisted.", ephemeral=True)
        return
    
    await Storage.remove_blacklist(user.id)
    blacklisted_users.discard(user.id)
    
    embed = discord.Embed(
        title="✅ User Unblacklisted",
        description=f"**User:** {user.mention}\n**User ID:** {user.id}",
        color=0x00ff00,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Unblacklisted by {interaction.user.name}")
    
    await interaction.response.send_message(embed=embed)
    logger.info(f"User {user.name} unblacklisted by {interaction.user.name}")

@bot.tree.command(name="blacklist_list", description="List all blacklisted users")
@is_whitelisted()
@is_staff()
async def blacklist_list(interaction: discord.Interaction):
    """List all blacklisted users"""
    if not blacklisted_users:
        await interaction.response.send_message("📭 No blacklisted users.", ephemeral=True)
        return
    
    embed = discord.Embed(title="🚫 Blacklisted Users", color=0xef4444)
    
    users_list = []
    for uid in blacklisted_users:
        try:
            user = await bot.fetch_user(uid)
            users_list.append(f"• {user.name} ({uid})")
        except:
            users_list.append(f"• Unknown User ({uid})")
    
    embed.description = "\n".join(users_list)
    embed.set_footer(text=f"Total: {len(blacklisted_users)} users")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- TICKET COMMANDS ---
@bot.tree.command(name="force_ticket", description="Force open a ticket for a user")
@is_whitelisted()
@is_staff()
async def force_ticket_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    """Force open a ticket for a user"""
    if user.id in blacklisted_users:
        await interaction.response.send_message(f"❌ {user.mention} is blacklisted.", ephemeral=True)
        return
    
    existing_ticket = await ticket_manager.get_ticket_by_user(user.id)
    if existing_ticket:
        await interaction.response.send_message(f"❌ {user.mention} already has an open ticket.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    guild = interaction.guild
    category = None
    if TICKET_CATEGORY_ID:
        category = bot.get_channel(TICKET_CATEGORY_ID)
    
    if not category:
        category = discord.utils.get(guild.categories, name="SUPPORT TICKETS")
        if not category:
            category = await guild.create_category("SUPPORT TICKETS")
    
    channel_name = f"forced-{user.name.lower().replace(' ', '-')}"
    
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    
    if STAFF_ROLE_ID:
        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    
    if MANAGER_ROLE_ID:
        manager_role = guild.get_role(MANAGER_ROLE_ID)
        if manager_role:
            overwrites[manager_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    
    channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
    
    await ticket_manager.create_ticket(user.id, user.name, channel.id, TicketType.FORCED, interaction.user.name)
    
    embed = discord.Embed(
        title="🎫 Force Ticket Created",
        description=f"**User:** {user.mention}\n**Created by:** {interaction.user.mention}\n**Reason:** {reason or 'No reason provided'}",
        color=0xffaa00,
        timestamp=datetime.utcnow()
    )
    await channel.send(embed=embed)
    
    try:
        user_embed = discord.Embed(
            title="📋 Support Ticket Opened",
            description=f"A support ticket has been opened for you by staff.\nYou will be assisted shortly.",
            color=0x00ff00
        )
        await user.send(embed=user_embed)
    except:
        pass
    
    await interaction.followup.send(f"✅ Force ticket created for {user.mention} in {channel.mention}")
    logger.info(f"Force ticket created for {user.name} by {interaction.user.name}")

@bot.tree.command(name="force_transfer", description="Force transfer a ticket to another staff member")
@is_whitelisted()
@is_staff()
async def force_transfer_command(interaction: discord.Interaction, user: discord.User, new_staff: discord.Member):
    """Force transfer a ticket to another staff member"""
    ticket = await ticket_manager.get_ticket_by_user(user.id)
    
    if not ticket:
        await interaction.response.send_message(f"❌ No active ticket found for {user.mention}", ephemeral=True)
        return
    
    new_staff_role = get_staff_role_name(new_staff)
    
    result = await ticket_manager.force_transfer(user.id, new_staff.id, new_staff.display_name, new_staff_role)
    
    if result:
        embed = discord.Embed(
            title="🔄 Ticket Force Transferred",
            description=f"**User:** {user.mention}\n**New Staff:** {new_staff.mention} ({new_staff_role})\n**Transferred by:** {interaction.user.mention}",
            color=0xffaa00,
            timestamp=datetime.utcnow()
        )
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        try:
            await new_staff.send(f"📋 You have been force assigned to ticket for {user.name}.")
        except:
            pass
        
        await interaction.response.send_message(f"✅ Ticket force transferred to {new_staff.mention}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Failed to transfer ticket", ephemeral=True)

@bot.tree.command(name="history", description="Show a user's ticket history")
@is_whitelisted()
@is_staff()
async def history_command(interaction: discord.Interaction, user: discord.User):
    """Show a user's ticket history"""
    history = await Storage.get_user_history(user.id)
    is_blacklisted = user.id in blacklisted_users
    is_whitelisted_flag = user.id in whitelisted_users
    
    embed = discord.Embed(
        title=f"📜 Ticket History - {user.name}",
        description=f"**User ID:** {user.id}\n**Blacklisted:** {'Yes' if is_blacklisted else 'No'}\n**Whitelisted:** {'Yes' if is_whitelisted_flag else 'No'}",
        color=0x3b82f6
    )
    
    if not history:
        embed.add_field(name="No Tickets Found", value="This user has no ticket history.", inline=False)
    else:
        for idx, ticket in enumerate(reversed(history[-10:]), 1):
            created_at = datetime.fromisoformat(ticket['created_at']).strftime("%Y-%m-%d %H:%M")
            status_emoji = "✅" if ticket.get('status') == 'closed' else "🟢" if ticket.get('status') == 'open' else "🟡"
            
            value = f"**Type:** {ticket.get('ticket_type', 'unknown').upper()}\n**Status:** {status_emoji} {ticket.get('status', 'unknown')}\n**Created:** {created_at}\n**Messages:** {ticket.get('message_count', 0)}"
            
            if ticket.get('created_by'):
                value += f"\n**Created by:** {ticket['created_by']}"
            if ticket.get('claimed_by_name'):
                value += f"\n**Claimed by:** {ticket['claimed_by_name']}"
            if ticket.get('resolved_by'):
                value += f"\n**Resolved by:** {ticket['resolved_by']}"
            if ticket.get('closed_at'):
                closed_at = datetime.fromisoformat(ticket['closed_at']).strftime("%Y-%m-%d %H:%M")
                value += f"\n**Closed:** {closed_at}"
            
            embed.add_field(name=f"Ticket #{idx}", value=value, inline=False)
    
    embed.set_footer(text=f"Total tickets: {len(history)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reply", description="Reply to a ticket (web or DM)")
@is_whitelisted()
@is_staff()
async def reply_command(interaction: discord.Interaction, message: str, user_id: Optional[str] = None):
    """Reply to a ticket"""
    await interaction.response.defer()
    
    target_user_id = None
    ticket = None
    
    if user_id:
        try:
            target_user_id = int(user_id)
            ticket = await ticket_manager.get_ticket_by_user(target_user_id)
        except ValueError:
            await interaction.followup.send("❌ Invalid user ID", ephemeral=True)
            return
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket:
            target_user_id = ticket.user_id
    
    if not ticket or not target_user_id:
        await interaction.followup.send("❌ No active ticket found.", ephemeral=True)
        return
    
    staff_role = get_staff_role_name(interaction.user)
    
    if ticket.ticket_type in [TicketType.DM, TicketType.FORCED]:
        try:
            user = await bot.fetch_user(target_user_id)
            embed = discord.Embed(
                title="💬 Support Staff Response",
                description=message,
                color=0x00ff00,
                timestamp=datetime.utcnow()
            )
            embed.set_author(name=f"{interaction.user.display_name} ({staff_role})", icon_url=interaction.user.display_avatar.url)
            embed.set_footer(text="Reply to this DM to continue")
            
            await user.send(embed=embed)
            
            log_embed = discord.Embed(
                description=f"**Staff Reply to {user.name} ({staff_role}):**\n{message}",
                color=0x00ff00,
                timestamp=datetime.utcnow()
            )
            
            channel = bot.get_channel(ticket.channel_id)
            if channel:
                await channel.send(embed=log_embed)
            
            await ticket_manager.add_message(target_user_id, message)
            await interaction.followup.send(f"✅ Reply sent to {user.name}")
            
        except discord.Forbidden:
            await interaction.followup.send("❌ Cannot DM user - they may have DMs disabled")
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)[:100]}")
    
    elif ticket.ticket_type == TicketType.WEB:
        reply_msg = {
            'type': 'reply',
            'user': f"{interaction.user.display_name} ({staff_role})",
            'text': message,
            'origin': 'discord',
            'target': ticket.user_name
        }
        
        await broadcast_to_web(reply_msg)
        
        embed = discord.Embed(
            description=message,
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"💬 Staff Reply ({staff_role}) to {ticket.user_name}")
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        await ticket_manager.add_message(target_user_id, message)
        await interaction.followup.send(f"✅ Reply sent to web user {ticket.user_name}")

@bot.tree.command(name="claim", description="Claim a ticket")
@is_whitelisted()
@is_staff()
async def claim_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    """Claim a ticket"""
    target_user_id = None
    ticket = None
    
    if user_id:
        try:
            target_user_id = int(user_id)
            ticket = await ticket_manager.get_ticket_by_user(target_user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID", ephemeral=True)
            return
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket:
            target_user_id = ticket.user_id
    
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True)
        return
    
    if ticket.status == TicketStatus.CLAIMED:
        await interaction.response.send_message(f"❌ Ticket already claimed by {ticket.claimed_by_name}", ephemeral=True)
        return
    
    staff_role = get_staff_role_name(interaction.user)
    claimed = await ticket_manager.claim_ticket(target_user_id, interaction.user.id, interaction.user.display_name, staff_role)
    
    if claimed:
        embed = discord.Embed(
            title="✅ Ticket Claimed",
            description=f"**User:** {ticket.user_name}\n**Claimed by:** {interaction.user.mention} ({staff_role})",
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        if ticket.ticket_type in [TicketType.DM, TicketType.FORCED]:
            try:
                user = await bot.fetch_user(ticket.user_id)
                await user.send(f"✅ **Ticket Claimed**\n{staff_role} ({interaction.user.display_name}) is now handling your ticket.")
            except:
                pass
        
        await interaction.response.send_message(f"✅ Claimed ticket for {ticket.user_name}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Failed to claim ticket", ephemeral=True)

@bot.tree.command(name="resolve", description="Mark a ticket as resolved")
@is_whitelisted()
@is_staff()
async def resolve_command(interaction: discord.Interaction, reason: str = "No reason provided", user_id: Optional[str] = None):
    """Mark ticket as resolved with a reason"""
    target_user_id = None
    ticket = None
    
    if user_id:
        try:
            target_user_id = int(user_id)
            ticket = await ticket_manager.get_ticket_by_user(target_user_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID", ephemeral=True)
            return
    else:
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket:
            target_user_id = ticket.user_id
    
    if not ticket:
        await interaction.response.send_message("❌ No active ticket found.", ephemeral=True)
        return
    
    staff_role = get_staff_role_name(interaction.user)
    resolved = await ticket_manager.resolve_ticket(target_user_id, interaction.user.display_name, staff_role, reason)
    
    if resolved:
        embed = discord.Embed(
            title="✅ Ticket Resolved",
            description=f"**User:** {ticket.user_name}\n**Reason:** {reason}\n**Resolved by:** {interaction.user.mention} ({staff_role})\n\nChannel will close in 30 seconds.",
            color=0xffaa00,
            timestamp=datetime.utcnow()
        )
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        if ticket.ticket_type in [TicketType.DM, TicketType.FORCED]:
            try:
                user = await bot.fetch_user(ticket.user_id)
                await user.send(f"✅ **Ticket Resolved**\nYour ticket has been resolved.\n**Reason:** {reason}\n\nThank you for contacting support!")
            except:
                pass
        
        await interaction.response.send_message(f"✅ Ticket resolved for {ticket.user_name}. Closing in 30s...", ephemeral=True)
        
        await asyncio.sleep(30)
        await close_ticket_by_user(target_user_id, interaction.channel if channel else None, interaction.user.name, reason)
    else:
        await interaction.response.send_message("❌ Failed to resolve ticket", ephemeral=True)

@bot.tree.command(name="close", description="Close the current ticket")
@is_whitelisted()
@is_staff()
async def close_command(interaction: discord.Interaction, reason: str = "No reason provided"):
    """Close the current ticket with a reason"""
    if not interaction.channel:
        await interaction.response.send_message("❌ Not a valid channel", ephemeral=True)
        return
    
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    
    if not ticket:
        await interaction.response.send_message("❌ Not a ticket channel", ephemeral=True)
        return
    
    await interaction.response.send_message(f"🔒 Closing channel in 5 seconds...\n**Reason:** {reason}")
    await asyncio.sleep(5)
    
    await close_ticket_by_user(ticket.user_id, interaction.channel, interaction.user.name, reason)

async def close_ticket_by_user(user_id: int, channel: discord.TextChannel = None, closed_by: str = None, reason: str = None):
    """Close a ticket and send review request"""
    ticket = await ticket_manager.close_ticket(user_id, channel.id if channel else None, closed_by, reason)
    
    if ticket and channel:
        try:
            duration = (ticket.closed_at - ticket.created_at).total_seconds()
            hours = int(duration // 3600)
            minutes = int((duration % 3600) // 60)
            
            embed = discord.Embed(
                title="📋 Ticket Closed",
                description=f"**User:** {ticket.user_name}\n**User ID:** {ticket.user_id}\n**Type:** {ticket.ticket_type.value}\n**Messages:** {ticket.message_count}\n**Duration:** {hours}h {minutes}m",
                color=0xef4444,
                timestamp=datetime.utcnow()
            )
            if ticket.claimed_by_name:
                embed.add_field(name="Handled By", value=f"{ticket.claimed_by_name} ({ticket.claimed_by_role})", inline=True)
            if ticket.resolved_reason:
                embed.add_field(name="Resolution Reason", value=ticket.resolved_reason, inline=False)
            if reason:
                embed.add_field(name="Closed Reason", value=reason, inline=False)
            if ticket.created_by:
                embed.add_field(name="Created By", value=ticket.created_by, inline=True)
            
            # Send to logs channel
            log_channel = discord.utils.get(channel.guild.text_channels, name="ticket-logs")
            if not log_channel:
                overwrites = {
                    channel.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    channel.guild.me: discord.PermissionOverwrite(read_messages=True)
                }
                log_channel = await channel.guild.create_text_channel("ticket-logs", overwrites=overwrites)
            
            await log_channel.send(embed=embed)
            
            # Send review request ONLY when ticket is resolved
            if ticket.ticket_type in [TicketType.DM, TicketType.FORCED] and ticket.resolved_at and not ticket.review_sent:
                ticket.review_sent = True
                
                try:
                    user = await bot.fetch_user(user_id)
                    review_channel = bot.get_channel(REVIEW_CHANNEL_ID)
                    
                    if review_channel:
                        review_embed = discord.Embed(
                            title="⭐ Support Ticket Closed",
                            description=f"**User:** {ticket.user_name}\n**User ID:** {ticket.user_id}\n**Resolved by:** {ticket.resolved_by}\n**Resolution:** {ticket.resolved_reason}",
                            color=0x00ff00,
                            timestamp=datetime.utcnow()
                        )
                        await review_channel.send(embed=review_embed)
                    
                    # Send feedback request to user
                    feedback_embed = discord.Embed(
                        title="⭐ How was your support experience?",
                        description="Your ticket has been resolved. We'd love your feedback!\n\nYou can leave a review in our support channel or just reply to this message.",
                        color=0xef4444
                    )
                    await user.send(embed=feedback_embed)
                except Exception as e:
                    logger.error(f"Failed to send review: {e}")
            
        except Exception as e:
            logger.error(f"Failed to close ticket: {e}")
        
        try:
            await channel.delete()
        except Exception as e:
            logger.error(f"Failed to delete channel: {e}")

@bot.tree.command(name="tickets", description="List all active tickets")
@is_whitelisted()
@is_staff()
async def tickets_command(interaction: discord.Interaction):
    """Show all active tickets"""
    tickets = await ticket_manager.get_all_open_tickets()
    
    if not tickets:
        await interaction.response.send_message("📭 No active tickets", ephemeral=True)
        return
    
    embed = discord.Embed(title=f"📋 Active Tickets ({len(tickets)})", color=0x3b82f6)
    
    for ticket in tickets:
        status_emoji = "🟢" if ticket.status == TicketStatus.OPEN else "🟡"
        claimed_by = f"{ticket.claimed_by_name} ({ticket.claimed_by_role})" if ticket.claimed_by_name else "Unclaimed"
        type_icon = "🌐" if ticket.ticket_type == TicketType.WEB else "💬" if ticket.ticket_type == TicketType.DM else "🔨"
        
        channel = bot.get_channel(ticket.channel_id)
        channel_mention = channel.mention if channel else "Unknown"
        
        embed.add_field(
            name=f"{status_emoji} {type_icon} {ticket.user_name}",
            value=f"Type: {ticket.ticket_type.value} | Claimed: {claimed_by} | Messages: {ticket.message_count}\nChannel: {channel_mention}",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="info", description="Get info about a ticket")
@is_whitelisted()
@is_staff()
async def ticket_info_command(interaction: discord.Interaction, user_id: str):
    """Get detailed ticket information"""
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID", ephemeral=True)
        return
    
    ticket = await ticket_manager.get_ticket_by_user(uid)
    
    if not ticket:
        await interaction.response.send_message(f"❌ No active ticket found", ephemeral=True)
        return
    
    embed = discord.Embed(title=f"📋 Ticket Info - {ticket.user_name}", color=0x3b82f6)
    embed.add_field(name="User ID", value=str(ticket.user_id), inline=True)
    embed.add_field(name="Type", value=ticket.ticket_type.value, inline=True)
    embed.add_field(name="Status", value=ticket.status.value, inline=True)
    embed.add_field(name="Created", value=ticket.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.add_field(name="Messages", value=str(ticket.message_count), inline=True)
    
    if ticket.created_by:
        embed.add_field(name="Created By", value=ticket.created_by, inline=True)
    
    if ticket.claimed_by_name:
        embed.add_field(name="Handled By", value=f"{ticket.claimed_by_name} ({ticket.claimed_by_role})", inline=True)
        embed.add_field(name="Claimed At", value=ticket.claimed_at.strftime("%Y-%m-%d %H:%M:%S") if ticket.claimed_at else "N/A", inline=True)
    
    channel = bot.get_channel(ticket.channel_id)
    if channel:
        embed.add_field(name="Channel", value=channel.mention, inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="transfer", description="Transfer ticket to another staff member")
@is_whitelisted()
@is_staff()
async def transfer_command(interaction: discord.Interaction, user_id: str, new_staff: discord.Member):
    """Transfer a ticket to another staff member"""
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID", ephemeral=True)
        return
    
    ticket = await ticket_manager.get_ticket_by_user(uid)
    
    if not ticket:
        await interaction.response.send_message(f"❌ No active ticket found", ephemeral=True)
        return
    
    if ticket.status != TicketStatus.CLAIMED:
        await interaction.response.send_message("❌ Ticket must be claimed before transferring", ephemeral=True)
        return
    
    new_staff_role = get_staff_role_name(new_staff)
    old_staff = f"{ticket.claimed_by_name} ({ticket.claimed_by_role})"
    
    async with ticket_manager.lock:
        ticket.claimed_by = new_staff.id
        ticket.claimed_by_name = new_staff.display_name
        ticket.claimed_by_role = new_staff_role
        ticket.claimed_at = datetime.utcnow()
        ticket.history.append({
            'action': 'transferred',
            'from': old_staff,
            'to': f"{new_staff.display_name} ({new_staff_role})",
            'at': datetime.utcnow().isoformat()
        })
    
    embed = discord.Embed(
        title="🔄 Ticket Transferred",
        description=f"**User:** {ticket.user_name}\n**From:** {old_staff}\n**To:** {new_staff.mention} ({new_staff_role})",
        color=0xffaa00,
        timestamp=datetime.utcnow()
    )
    
    channel = bot.get_channel(ticket.channel_id)
    if channel:
        await channel.send(embed=embed)
    
    await interaction.response.send_message(f"✅ Ticket transferred to {new_staff.display_name}", ephemeral=True)

@bot.tree.command(name="note", description="Add a private note to a ticket")
@is_whitelisted()
@is_staff()
async def note_command(interaction: discord.Interaction, user_id: str, note: str):
    """Add a private note to a ticket"""
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID", ephemeral=True)
        return
    
    ticket = await ticket_manager.get_ticket_by_user(uid)
    
    if not ticket:
        await interaction.response.send_message(f"❌ No active ticket found", ephemeral=True)
        return
    
    staff_role = get_staff_role_name(interaction.user)
    
    embed = discord.Embed(
        title="📝 Staff Note",
        description=note,
        color=0x3b82f6,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Added by {interaction.user.display_name} ({staff_role}) for {ticket.user_name}")
    
    log_channel = discord.utils.get(interaction.guild.text_channels, name="staff-notes")
    if not log_channel:
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True)
        }
        if STAFF_ROLE_ID:
            staff_role_obj = interaction.guild.get_role(STAFF_ROLE_ID)
            if staff_role_obj:
                overwrites[staff_role_obj] = discord.PermissionOverwrite(read_messages=True)
        log_channel = await interaction.guild.create_text_channel("staff-notes", overwrites=overwrites)
    
    await log_channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Note added to ticket for {ticket.user_name}", ephemeral=True)

# --- USER COMMANDS ---
@bot.tree.command(name="help", description="Get help and bot information")
@is_whitelisted()
async def help_command(interaction: discord.Interaction):
    """Send help information via DM"""
    try:
        embed = discord.Embed(
            title="🆘 Rawr.xyz Support Bot - Help",
            description="Here's how to get support and use the bot:",
            color=0xef4444
        )
        embed.add_field(
            name="📬 Getting Support",
            value="Simply DM this bot with your issue! A support ticket will be created automatically.",
            inline=False
        )
        embed.add_field(
            name="💬 Staff Commands",
            value="`/reply [user_id] <message>` - Reply to a ticket\n`/claim [user_id]` - Claim a ticket\n`/resolve [user_id] <reason>` - Resolve ticket\n`/close <reason>` - Close ticket\n`/force_ticket @user <reason>` - Force open ticket\n`/force_transfer @user @staff` - Force transfer\n`/history @user` - View user history\n`/blacklist @user <reason>` - Blacklist user\n`/unblacklist @user` - Remove blacklist\n`/tickets` - List all tickets\n`/info <user_id>` - Get ticket info\n`/transfer <user_id> @staff` - Transfer ticket\n`/note <user_id> <note>` - Add private note",
            inline=False
        )
        embed.add_field(
            name="📊 Stats Commands",
            value="`/stats` - Bot statistics\n`/ping` - Check latency\n`/whitelist_list` - View whitelist\n`/blacklist_list` - View blacklist",
            inline=False
        )
        embed.add_field(
            name="👑 Admin Commands",
            value="`/whitelist_add <user/role>` - Add to whitelist\n`/whitelist_remove <user/role>` - Remove from whitelist",
            inline=False
        )
        embed.add_field(
            name="🌐 Website",
            value="Visit **rawrs.zapto.org** for scripts and updates",
            inline=False
        )
        embed.set_footer(text="Support is available 24/7")
        
        await interaction.user.send(embed=embed)
        await interaction.response.send_message("✅ Help information sent via DM!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Please enable DMs to receive help information.", ephemeral=True)

@bot.tree.command(name="stats", description="Show bot statistics")
@is_whitelisted()
async def stats_command(interaction: discord.Interaction):
    """Show bot stats"""
    uptime = datetime.utcnow() - bot.boot_time
    days = uptime.days
    hours = uptime.seconds // 3600
    minutes = (uptime.seconds % 3600) // 60
    
    ticket_stats = await ticket_manager.get_stats()
    
    embed = discord.Embed(title="📊 Bot Statistics", color=0xef4444)
    embed.add_field(name="⏰ Uptime", value=f"{days}d {hours}h {minutes}m", inline=True)
    embed.add_field(name="⚡ Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🌐 Web Clients", value=str(len(sse_clients)), inline=True)
    embed.add_field(name="🎫 Active Tickets", value=str(ticket_stats['total']), inline=True)
    embed.add_field(name="🟢 Open Tickets", value=str(ticket_stats['open']), inline=True)
    embed.add_field(name="🟡 Claimed Tickets", value=str(ticket_stats['claimed']), inline=True)
    embed.add_field(name="✅ Handled Tickets", value=str(ticket_stats['handled']), inline=True)
    embed.add_field(name="👥 Whitelisted", value=f"{len(whitelisted_users)} users, {len(whitelisted_roles)} roles", inline=True)
    embed.add_field(name="🚫 Blacklisted", value=str(len(blacklisted_users)), inline=True)
    embed.add_field(name="💾 Storage", value="AWS DynamoDB" if AWS_AVAILABLE else "Local Files", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
@is_whitelisted()
async def ping_command(interaction: discord.Interaction):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! `{latency}ms`", ephemeral=True)

# --- ERROR HANDLING ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle command errors"""
    if isinstance(error, app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("⛔ You don't have permission to use this command.", ephemeral=True)
    else:
        logger.error(f"Command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Error: {str(error)[:100]}", ephemeral=True)

# --- DISCORD EVENTS ---
@bot.event
async def on_ready():
    """Bot is ready"""
    logger.info(f"✅ Logged in as: {bot.user.name} ({bot.user.id})")
    logger.info(f"✅ Connected to {len(bot.guilds)} guilds")
    logger.info(f"✅ Storage: {'AWS DynamoDB' if AWS_AVAILABLE else 'Local Files'}")
    
    guild = discord.Object(id=GUILD_ID)
    commands = await bot.tree.fetch_commands(guild=guild)
    logger.info(f"✅ Synced {len(commands)} slash commands")

@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages"""
    if message.author == bot.user:
        return
    
    # Check blacklist for DM messages
    if isinstance(message.channel, discord.DMChannel) and message.author.id in blacklisted_users:
        await message.author.send("🚫 **You are blacklisted from using this support system.**\nIf you believe this is an error, please contact an administrator.")
        return
    
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm(message)
        return
    
    await bot.process_commands(message)

async def handle_dm(message: discord.Message):
    """Handle DM messages and create/update tickets"""
    ticket = await ticket_manager.get_ticket_by_user(message.author.id)
    
    if ticket:
        await ticket_manager.add_message(message.author.id, message.content)
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            embed = discord.Embed(
                description=message.content,
                color=0xef4444,
                timestamp=datetime.utcnow()
            )
            embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
            embed.set_footer(text=f"User ID: {message.author.id}")
            
            await channel.send(embed=embed)
            await message.author.send("✅ **Message sent to support!** Staff will respond shortly.")
        return
    
    # Create new ticket
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await message.author.send("❌ Support system unavailable.")
        return
    
    category = None
    if TICKET_CATEGORY_ID:
        category = bot.get_channel(TICKET_CATEGORY_ID)
    
    if not category:
        category = discord.utils.get(guild.categories, name="SUPPORT TICKETS")
        if not category:
            category = await guild.create_category("SUPPORT TICKETS")
    
    channel_name = f"ticket-{message.author.name.lower().replace(' ', '-')}"
    
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        message.author: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    
    if STAFF_ROLE_ID:
        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    
    if MANAGER_ROLE_ID:
        manager_role = guild.get_role(MANAGER_ROLE_ID)
        if manager_role:
            overwrites[manager_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    
    channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
    
    await ticket_manager.create_ticket(message.author.id, message.author.name, channel.id, TicketType.DM)
    
    embed = discord.Embed(
        title="🎫 New Support Ticket",
        description=f"**User:** {message.author.mention}\n**Name:** {message.author.name}\n**ID:** `{message.author.id}`",
        color=0xef4444,
        timestamp=datetime.utcnow()
    )
    await channel.send(embed=embed)
    
    msg_embed = discord.Embed(
        description=message.content,
        color=0x3b82f6,
        timestamp=datetime.utcnow()
    )
    msg_embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
    await channel.send(embed=msg_embed)
    
    # Claim button
    class ClaimButton(discord.ui.View):
        def __init__(self, user_id: int):
            super().__init__(timeout=None)
            self.user_id = user_id
            self.claimed = False
        
        @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="✋")
        async def claim_button(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if self.claimed:
                await button_interaction.response.send_message("❌ Ticket already claimed", ephemeral=True)
                return
            
            staff_role = get_staff_role_name(button_interaction.user)
            result = await ticket_manager.claim_ticket(self.user_id, button_interaction.user.id, button_interaction.user.display_name, staff_role)
            
            if result:
                self.claimed = True
                button.disabled = True
                await button_interaction.message.edit(view=self)
                
                await button_interaction.response.send_message(f"✅ Claimed ticket for {message.author.name}", ephemeral=True)
                
                try:
                    await message.author.send(f"✅ **Ticket Claimed**\n{staff_role} ({button_interaction.user.display_name}) is now handling your ticket.")
                except:
                    pass
                
                claim_embed = discord.Embed(
                    title="✅ Ticket Claimed",
                    description=f"Claimed by {button_interaction.user.mention} ({staff_role})",
                    color=0x00ff00,
                    timestamp=datetime.utcnow()
                )
                await channel.send(embed=claim_embed)
    
    await channel.send(view=ClaimButton(message.author.id))
    await message.author.send("✅ **Support ticket created!** Staff will be with you shortly.")

# --- MAIN ---
if __name__ == "__main__":
    try:
        logger.info(f"🚀 Starting bot on port {PORT}...")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"❌ Failed to start: {e}")
