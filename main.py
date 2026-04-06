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
REVIEW_CHANNEL_ID = 1490558708214665296
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID', '0'))
MANAGER_ROLE_ID = int(os.getenv('MANAGER_ROLE_ID', '0'))

RATE_LIMIT_SECONDS = 5
MAX_MESSAGES_PER_MINUTE = 12

# --- BLACKLIST SYSTEM ---
blacklisted_users: Set[int] = set()
BLACKLIST_FILE = "blacklist.json"

def load_blacklist():
    global blacklisted_users
    try:
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r') as f:
                data = json.load(f)
                blacklisted_users = set(data.get('users', []))
                logger.info(f"Loaded blacklist: {len(blacklisted_users)} users")
    except Exception as e:
        logger.error(f"Failed to load blacklist: {e}")

def save_blacklist():
    try:
        with open(BLACKLIST_FILE, 'w') as f:
            json.dump({'users': list(blacklisted_users)}, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save blacklist: {e}")

# --- STATS TRACKING ---
total_tickets_handled = 0
total_messages_processed = 0

# --- WHITELIST SYSTEM ---
whitelisted_users: Set[int] = set()
whitelisted_roles: Set[int] = set()

WHITELIST_FILE = "whitelist.json"

def load_whitelist():
    global whitelisted_users, whitelisted_roles
    try:
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r') as f:
                data = json.load(f)
                whitelisted_users = set(data.get('users', []))
                whitelisted_roles = set(data.get('roles', []))
                logger.info(f"Loaded whitelist: {len(whitelisted_users)} users, {len(whitelisted_roles)} roles")
    except Exception as e:
        logger.error(f"Failed to load whitelist: {e}")

def save_whitelist():
    try:
        with open(WHITELIST_FILE, 'w') as f:
            json.dump({
                'users': list(whitelisted_users),
                'roles': list(whitelisted_roles)
            }, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save whitelist: {e}")

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
        self.transcript: List[Dict] = []
        self.review_sent = False
        self.created_by = created_by
        self.history: List[Dict] = []  # Track ticket history

class TicketManager:
    def __init__(self):
        self.tickets: Dict[int, Ticket] = {}
        self.channel_to_user: Dict[int, int] = {}
        self.user_history: Dict[int, List[Dict]] = {}
        self.lock = asyncio.Lock()
        self.handled_count = 0
    
    async def create_ticket(self, user_id: int, user_name: str, channel_id: int, ticket_type: TicketType, created_by: Optional[str] = None) -> Ticket:
        async with self.lock:
            ticket = Ticket(user_id, user_name, channel_id, ticket_type, created_by)
            self.tickets[user_id] = ticket
            self.channel_to_user[channel_id] = user_id
            
            # Add to user history
            if user_id not in self.user_history:
                self.user_history[user_id] = []
            self.user_history[user_id].append({
                'ticket_id': channel_id,
                'type': ticket_type.value,
                'created_at': ticket.created_at.isoformat(),
                'status': ticket.status.value,
                'created_by': created_by
            })
            
            logger.info(f"📫 Ticket created for {user_name} ({user_id})")
            return ticket
    
    async def get_ticket_by_user(self, user_id: int) -> Optional[Ticket]:
        async with self.lock:
            return self.tickets.get(user_id)
    
    async def get_ticket_by_channel(self, channel_id: int) -> Optional[Ticket]:
        async with self.lock:
            user_id = self.channel_to_user.get(channel_id)
            if user_id:
                return self.tickets.get(user_id)
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
                logger.info(f"✅ Ticket claimed by {staff_name} ({staff_role})")
                return ticket
            return None
    
    async def resolve_ticket(self, user_id: int) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]:
                ticket.status = TicketStatus.RESOLVED
                ticket.resolved_at = datetime.utcnow()
                self.handled_count += 1
                ticket.history.append({
                    'action': 'resolved',
                    'at': datetime.utcnow().isoformat()
                })
                logger.info(f"✅ Ticket resolved for {ticket.user_name}")
                return ticket
            return None
    
    async def close_ticket(self, user_id: int, channel_id: int = None) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                if channel_id:
                    self.channel_to_user.pop(channel_id, None)
                ticket.status = TicketStatus.CLOSED
                ticket.closed_at = datetime.utcnow()
                ticket.history.append({
                    'action': 'closed',
                    'at': datetime.utcnow().isoformat()
                })
                logger.info(f"🔒 Ticket closed for {ticket.user_name}")
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
                logger.info(f"🔄 Ticket force transferred to {new_staff_name}")
                return ticket
            return None
    
    async def add_message(self, user_id: int, message: str):
        async with self.lock:
            if user_id in self.tickets:
                self.tickets[user_id].message_count += 1
                self.tickets[user_id].transcript.append({
                    'timestamp': datetime.utcnow().isoformat(),
                    'message': message[:500]
                })
    
    async def get_user_history(self, user_id: int) -> List[Dict]:
        async with self.lock:
            return self.user_history.get(user_id, [])
    
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

# --- MESSAGE STORAGE ---
message_queue = deque(maxlen=100)
sse_clients = set()
processed_ids = set()

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
        self.total_handled = 0
        
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        load_whitelist()
        load_blacklist()
        
        self.web_app = web.Application()
        self.web_app.router.add_get('/events', sse_endpoint)
        self.web_app.router.add_post('/send', send_message_endpoint)
        self.web_app.router.add_get('/health', health_check)
        
        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', PORT)
        await site.start()
        
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        
        self.status_task.start()
    
    async def close(self):
        save_whitelist()
        save_blacklist()
        if self.runner:
            await self.runner.cleanup()
        await super().close()
    
    @tasks.loop(seconds=30)
    async def status_task(self):
        """Update bot status dynamically"""
        stats = await ticket_manager.get_stats()
        open_tickets = stats['open']
        claimed_tickets = stats['claimed']
        handled_total = stats['handled']
        
        statuses = [
            f"rawrs.zapto.org",
            f"{open_tickets} open | {handled_total} handled",
            f"{claimed_tickets} claimed | /help",
            f"{len(blacklisted_users)} blacklisted | rawrs.zapto.org"
        ]
        
        current_status = statuses[int(time.time() / 30) % len(statuses)]
        
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=current_status
            )
        )
    
    @status_task.before_loop
    async def before_status(self):
        await self.wait_until_ready()

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
        
        await interaction.response.send_message(
            "❌ **Access Denied**\nYou are not whitelisted to use this bot.",
            ephemeral=True
        )
        return False
    return app_commands.check(predicate)

def is_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        if not STAFF_ROLE_ID:
            return False
        user_roles = {role.id for role in interaction.user.roles}
        return STAFF_ROLE_ID in user_roles or MANAGER_ROLE_ID in user_roles
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

# --- BLACKLIST COMMANDS ---

@bot.tree.command(name="blacklist", description="Blacklist a user from creating tickets")
@is_whitelisted()
@is_staff()
async def blacklist_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    """Blacklist a user from creating tickets"""
    if user.id == OWNER_ID:
        await interaction.response.send_message("❌ Cannot blacklist the bot owner.", ephemeral=True)
        return
    
    blacklisted_users.add(user.id)
    save_blacklist()
    
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
    
    blacklisted_users.discard(user.id)
    save_blacklist()
    
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
async def blacklist_list_command(interaction: discord.Interaction):
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

# --- WHITELIST COMMANDS ---

@bot.tree.command(name="whitelist_add", description="Add a user or role to the whitelist")
@is_whitelisted()
async def whitelist_add(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    """Add a user or role to the whitelist"""
    if user:
        whitelisted_users.add(user.id)
        save_whitelist()
        await interaction.response.send_message(f"✅ Added {user.mention} to whitelist", ephemeral=True)
    elif role:
        whitelisted_roles.add(role.id)
        save_whitelist()
        await interaction.response.send_message(f"✅ Added {role.mention} role to whitelist", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Please specify either a user or a role", ephemeral=True)

@bot.tree.command(name="whitelist_remove", description="Remove a user or role from the whitelist")
@is_whitelisted()
async def whitelist_remove(interaction: discord.Interaction, user: Optional[discord.User] = None, role: Optional[discord.Role] = None):
    """Remove a user or role from the whitelist"""
    if user:
        whitelisted_users.discard(user.id)
        save_whitelist()
        await interaction.response.send_message(f"✅ Removed {user.mention} from whitelist", ephemeral=True)
    elif role:
        whitelisted_roles.discard(role.id)
        save_whitelist()
        await interaction.response.send_message(f"✅ Removed {role.mention} role from whitelist", ephemeral=True)
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

# --- TICKET COMMANDS ---

@bot.tree.command(name="force_ticket", description="Force open a ticket for a user")
@is_whitelisted()
@is_staff()
async def force_ticket_command(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None):
    """Force open a ticket for a user"""
    # Check if user is blacklisted
    if user.id in blacklisted_users:
        await interaction.response.send_message(f"❌ {user.mention} is blacklisted and cannot create tickets.", ephemeral=True)
        return
    
    # Check if user already has a ticket
    existing_ticket = await ticket_manager.get_ticket_by_user(user.id)
    if existing_ticket:
        await interaction.response.send_message(f"❌ {user.mention} already has an open ticket.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    # Create ticket channel
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
    
    # Create ticket
    ticket = await ticket_manager.create_ticket(user.id, user.name, channel.id, TicketType.FORCED, interaction.user.name)
    
    embed = discord.Embed(
        title="🎫 Force Ticket Created",
        description=f"**User:** {user.mention}\n**Created by:** {interaction.user.mention}\n**Reason:** {reason or 'No reason provided'}",
        color=0xffaa00,
        timestamp=datetime.utcnow()
    )
    await channel.send(embed=embed)
    
    # Notify user
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
        
        # Notify new staff
        try:
            await new_staff.send(f"📋 You have been force assigned to ticket for {user.name}. Channel: {channel.mention if channel else 'Unknown'}")
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
    history = await ticket_manager.get_user_history(user.id)
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
        for idx, ticket in enumerate(reversed(history[-5:]), 1):  # Show last 5 tickets
            created_at = datetime.fromisoformat(ticket['created_at']).strftime("%Y-%m-%d %H:%M")
            embed.add_field(
                name=f"Ticket #{idx} - {ticket['type'].upper()}",
                value=f"**Status:** {ticket['status']}\n**Created:** {created_at}\n**Created by:** {ticket.get('created_by', 'User')}",
                inline=False
            )
    
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
async def resolve_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    """Mark ticket as resolved"""
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
    
    resolved = await ticket_manager.resolve_ticket(target_user_id)
    
    if resolved:
        staff_role = get_staff_role_name(interaction.user)
        
        embed = discord.Embed(
            title="✅ Ticket Resolved",
            description=f"Ticket for {ticket.user_name} has been marked as resolved.\nThe channel will close in 30 seconds.\n\n**Resolved by:** {interaction.user.mention} ({staff_role})",
            color=0xffaa00,
            timestamp=datetime.utcnow()
        )
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        if ticket.ticket_type in [TicketType.DM, TicketType.FORCED]:
            try:
                user = await bot.fetch_user(ticket.user_id)
                await user.send(f"✅ **Ticket Resolved**\nYour ticket has been resolved by {staff_role}. It will close soon.\n\nThank you for contacting support!")
            except:
                pass
        
        await interaction.response.send_message(f"✅ Ticket resolved for {ticket.user_name}. Closing in 30s...", ephemeral=True)
        
        await asyncio.sleep(30)
        await close_ticket_by_user(target_user_id, interaction.channel if channel else None)
    else:
        await interaction.response.send_message("❌ Failed to resolve ticket", ephemeral=True)

@bot.tree.command(name="close", description="Close the current ticket")
@is_whitelisted()
async def close_command(interaction: discord.Interaction):
    """Close the current ticket channel"""
    if not interaction.channel:
        await interaction.response.send_message("❌ Not a valid channel", ephemeral=True)
        return
    
    ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
    
    if not ticket:
        await interaction.response.send_message("❌ Not a ticket channel", ephemeral=True)
        return
    
    await interaction.response.send_message("🔒 Closing channel in 5 seconds...")
    await asyncio.sleep(5)
    
    await close_ticket_by_user(ticket.user_id, interaction.channel)

async def close_ticket_by_user(user_id: int, channel: discord.TextChannel = None):
    """Close a ticket and send review request"""
    ticket = await ticket_manager.close_ticket(user_id, channel.id if channel else None)
    
    if ticket and channel:
        try:
            # Send closure log
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
            
            # Send review request ONLY when ticket is resolved (not closed)
            if ticket.ticket_type in [TicketType.DM, TicketType.FORCED] and ticket.resolved_at and not ticket.review_sent:
                ticket.review_sent = True
                
                try:
                    user = await bot.fetch_user(user_id)
                    
                    review_embed = discord.Embed(
                        title="⭐ How was your support experience?",
                        description="Please rate your experience by clicking one of the buttons below.",
                        color=0xef4444
                    )
                    
                    class ReviewButtons(discord.ui.View):
                        def __init__(self):
                            super().__init__(timeout=120)
                        
                        @discord.ui.button(label="⭐ 1", style=discord.ButtonStyle.danger)
                        async def one_star(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                            await self.submit_review(button_interaction, 1)
                        
                        @discord.ui.button(label="⭐⭐ 2", style=discord.ButtonStyle.danger)
                        async def two_stars(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                            await self.submit_review(button_interaction, 2)
                        
                        @discord.ui.button(label="⭐⭐⭐ 3", style=discord.ButtonStyle.secondary)
                        async def three_stars(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                            await self.submit_review(button_interaction, 3)
                        
                        @discord.ui.button(label="⭐⭐⭐⭐ 4", style=discord.ButtonStyle.primary)
                        async def four_stars(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                            await self.submit_review(button_interaction, 4)
                        
                        @discord.ui.button(label="⭐⭐⭐⭐⭐ 5", style=discord.ButtonStyle.success)
                        async def five_stars(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                            await self.submit_review(button_interaction, 5)
                        
                        async def submit_review(self, button_interaction: discord.Interaction, rating: int):
                            review_channel = button_interaction.guild.get_channel(REVIEW_CHANNEL_ID)
                            
                            if review_channel:
                                star_emojis = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐", 4: "⭐⭐⭐⭐", 5: "⭐⭐⭐⭐⭐"}
                                
                                review_embed = discord.Embed(
                                    title="📝 New Support Review",
                                    description=f"**User:** {ticket.user_name}\n**User ID:** {ticket.user_id}\n**Rating:** {star_emojis[rating]} ({rating}/5)\n**Staff:** {ticket.claimed_by_name if ticket.claimed_by_name else 'Unknown'} ({ticket.claimed_by_role if ticket.claimed_by_role else 'Staff'})",
                                    color=0x00ff00 if rating >= 4 else (0xffaa00 if rating == 3 else 0xef4444),
                                    timestamp=datetime.utcnow()
                                )
                                
                                if ticket.created_by:
                                    review_embed.add_field(name="Ticket Created By", value=ticket.created_by, inline=False)
                                
                                await review_channel.send(embed=review_embed)
                            
                            await button_interaction.response.send_message(f"✅ Thank you for your {rating}-star review!", ephemeral=True)
                    
                    await user.send(embed=review_embed, view=ReviewButtons())
                except:
                    pass
            
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
        
        embed.add_field(
            name=f"{status_emoji} {type_icon} {ticket.user_name}",
            value=f"Type: {ticket.ticket_type.value} | Claimed: {claimed_by} | Messages: {ticket.message_count}",
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
    
    channel = bot.get_channel(ticket.channel_id)
    if channel:
        embed.add_field(name="Channel", value=channel.mention, inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="transfer", description="Transfer ticket to another staff member")
@is_whitelisted()
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
            value="`/reply <message>` - Reply to a ticket\n`/claim` - Claim a ticket\n`/resolve` - Mark as resolved\n`/force_ticket @user` - Force open ticket\n`/force_transfer @user @staff` - Force transfer\n`/history @user` - View user history\n`/blacklist @user` - Blacklist user\n`/tickets` - List all tickets\n`/info <user_id>` - Get ticket info",
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
    embed.add_field(name="🎫 Total Tickets", value=str(ticket_stats['total']), inline=True)
    embed.add_field(name="🟢 Open Tickets", value=str(ticket_stats['open']), inline=True)
    embed.add_field(name="🟡 Claimed Tickets", value=str(ticket_stats['claimed']), inline=True)
    embed.add_field(name="✅ Handled Tickets", value=str(ticket_stats['handled']), inline=True)
    embed.add_field(name="👥 Whitelisted", value=f"{len(whitelisted_users)} users, {len(whitelisted_roles)} roles", inline=True)
    embed.add_field(name="🚫 Blacklisted", value=str(len(blacklisted_users)), inline=True)
    
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
    logger.info(f"✅ Whitelist: {len(whitelisted_users)} users, {len(whitelisted_roles)} roles")
    logger.info(f"✅ Blacklist: {len(blacklisted_users)} users")
    
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
