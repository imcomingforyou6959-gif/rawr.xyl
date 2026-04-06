import os
import discord
import asyncio
import logging
import json
import time
import hashlib
from typing import Optional, Set, Dict, Any, Tuple
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
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID', '0'))
MANAGER_ROLE_ID = int(os.getenv('MANAGER_ROLE_ID', '0'))

RATE_LIMIT_SECONDS = 5
MAX_MESSAGES_PER_MINUTE = 12

# --- BOT STATE ---
is_maintenance_mode = False
maintenance_reason = ""

# --- TICKET SYSTEM ---
class TicketStatus(Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    CLOSED = "closed"

class TicketType(Enum):
    WEB = "web"
    DM = "dm"

class Ticket:
    def __init__(self, user_id: int, user_name: str, channel_id: int, ticket_type: TicketType):
        self.user_id = user_id
        self.user_name = user_name
        self.channel_id = channel_id
        self.ticket_type = ticket_type
        self.status = TicketStatus.OPEN
        self.claimed_by: Optional[int] = None
        self.claimed_by_name: Optional[str] = None
        self.created_at = datetime.utcnow()
        self.claimed_at: Optional[datetime] = None
        self.resolved_at: Optional[datetime] = None
        self.message_count = 0
        self.transcript: list = []

class TicketManager:
    def __init__(self):
        self.tickets: Dict[int, Ticket] = {}  # user_id -> Ticket
        self.channel_to_user: Dict[int, int] = {}  # channel_id -> user_id
        self.lock = asyncio.Lock()
    
    async def create_ticket(self, user_id: int, user_name: str, channel_id: int, ticket_type: TicketType) -> Ticket:
        async with self.lock:
            ticket = Ticket(user_id, user_name, channel_id, ticket_type)
            self.tickets[user_id] = ticket
            self.channel_to_user[channel_id] = user_id
            logger.info(f"📫 Ticket created for {user_name} ({user_id}) in channel {channel_id}")
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
    
    async def claim_ticket(self, user_id: int, staff_id: int, staff_name: str) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status == TicketStatus.OPEN:
                ticket.status = TicketStatus.CLAIMED
                ticket.claimed_by = staff_id
                ticket.claimed_by_name = staff_name
                ticket.claimed_at = datetime.utcnow()
                logger.info(f"✅ Ticket claimed by {staff_name} for {ticket.user_name}")
                return ticket
            return None
    
    async def resolve_ticket(self, user_id: int) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket and ticket.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]:
                ticket.status = TicketStatus.RESOLVED
                ticket.resolved_at = datetime.utcnow()
                logger.info(f"✅ Ticket resolved for {ticket.user_name}")
                return ticket
            return None
    
    async def close_ticket(self, user_id: int) -> Optional[Ticket]:
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                self.channel_to_user.pop(ticket.channel_id, None)
                ticket.status = TicketStatus.CLOSED
                logger.info(f"🔒 Ticket closed for {ticket.user_name}")
                return ticket
            return None
    
    async def add_message(self, user_id: int):
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
                'resolved': sum(1 for t in self.tickets.values() if t.status == TicketStatus.RESOLVED)
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
        logger.info(f"Web message (no category): {message['user']}: {message['text']}")
        return
    
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        logger.error(f"Guild {GUILD_ID} not found")
        return
    
    category = bot.get_channel(TICKET_CATEGORY_ID)
    if not category:
        logger.error(f"Category {TICKET_CATEGORY_ID} not found")
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
            logger.info(f"Created channel: {channel_name}")
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
        logger.info(f"Forwarded message from {message['user']}")
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
    logger.info(f"SSE client connected. Total: {len(sse_clients)}")
    
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
        logger.error(f"Error: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def health_check(request):
    stats = await ticket_manager.get_stats()
    return web.json_response({
        'status': 'alive',
        'clients': len(sse_clients),
        'messages': len(message_queue),
        'maintenance': is_maintenance_mode,
        'tickets': stats
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
        """Start HTTP server and sync commands"""
        self.web_app = web.Application()
        self.web_app.router.add_get('/events', sse_endpoint)
        self.web_app.router.add_post('/send', send_message_endpoint)
        self.web_app.router.add_get('/health', health_check)
        
        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"✅ HTTP server on port {PORT}")
        
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("✅ Slash commands synced")
        
        self.status_task.start()
        self.ticket_cleanup_task.start()
    
    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        await super().close()
    
    @tasks.loop(minutes=30)
    async def status_task(self):
        stats = await ticket_manager.get_stats()
        status_text = f"{stats['open']} open | {stats['claimed']} claimed"
        if is_maintenance_mode:
            status_text = f"MAINTENANCE | {status_text}"
        
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.competing,
                name=status_text
            )
        )
    
    @tasks.loop(hours=24)
    async def ticket_cleanup_task(self):
        """Clean up old resolved tickets"""
        # This would archive old tickets - implement as needed
        pass
    
    @status_task.before_loop
    async def before_status(self):
        await self.wait_until_ready()

bot = RawrBot()

# --- PERMISSION CHECKS ---
def is_staff():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        if not STAFF_ROLE_ID:
            return False
        user_roles = {role.id for role in interaction.user.roles}
        return STAFF_ROLE_ID in user_roles or MANAGER_ROLE_ID in user_roles
    return app_commands.check(predicate)

def is_manager():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        if not MANAGER_ROLE_ID:
            return False
        user_roles = {role.id for role in interaction.user.roles}
        return MANAGER_ROLE_ID in user_roles
    return app_commands.check(predicate)

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID
    return app_commands.check(predicate)

# --- TICKET COMMANDS ---

@bot.tree.command(name="reply", description="Reply to a ticket (web or DM)")
@is_staff()
async def reply_command(interaction: discord.Interaction, message: str, user_id: Optional[str] = None):
    """Reply to a ticket - automatically detects web or DM ticket"""
    if is_maintenance_mode:
        await interaction.response.send_message("❌ Bot is in maintenance mode.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    # Determine which ticket to reply to
    target_user_id = None
    ticket = None
    
    if user_id:
        # Manual reply by user ID
        try:
            target_user_id = int(user_id)
            ticket = await ticket_manager.get_ticket_by_user(target_user_id)
        except ValueError:
            await interaction.followup.send("❌ Invalid user ID", ephemeral=True)
            return
    else:
        # Auto-detect from channel
        ticket = await ticket_manager.get_ticket_by_channel(interaction.channel_id)
        if ticket:
            target_user_id = ticket.user_id
    
    if not ticket or not target_user_id:
        await interaction.followup.send("❌ No active ticket found. Use `/reply user_id message` or run this in a ticket channel.", ephemeral=True)
        return
    
    # Handle DM ticket
    if ticket.ticket_type == TicketType.DM:
        try:
            user = await bot.fetch_user(target_user_id)
            embed = discord.Embed(
                title="💬 Support Staff Response",
                description=message,
                color=0x00ff00,
                timestamp=datetime.utcnow()
            )
            embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
            embed.set_footer(text="Reply to this DM to continue the conversation")
            
            await user.send(embed=embed)
            
            # Log to ticket channel
            log_embed = discord.Embed(
                description=f"**Staff Reply to {user.name}:**\n{message}",
                color=0x00ff00,
                timestamp=datetime.utcnow()
            )
            log_embed.set_footer(text=f"Sent by {interaction.user.display_name}")
            
            channel = bot.get_channel(ticket.channel_id)
            if channel:
                await channel.send(embed=log_embed)
            
            await interaction.followup.send(f"✅ Reply sent to {user.name}")
            logger.info(f"Staff {interaction.user.name} replied to DM ticket for {user.name}")
            
        except discord.Forbidden:
            await interaction.followup.send("❌ Cannot DM user - they may have DMs disabled")
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {str(e)[:100]}")
    
    # Handle Web ticket
    elif ticket.ticket_type == TicketType.WEB:
        reply_msg = {
            'type': 'reply',
            'user': interaction.user.display_name,
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
        embed.set_author(name=f"💬 Staff Reply to {ticket.user_name}")
        embed.set_footer(text=f"Sent by {interaction.user.display_name}")
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        await interaction.followup.send(f"✅ Reply sent to web user {ticket.user_name}")

@bot.tree.command(name="claim", description="Claim a ticket")
@is_staff()
async def claim_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    """Claim a ticket to show you're handling it"""
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
    
    claimed = await ticket_manager.claim_ticket(target_user_id, interaction.user.id, interaction.user.display_name)
    
    if claimed:
        embed = discord.Embed(
            title="✅ Ticket Claimed",
            description=f"**User:** {ticket.user_name}\n**Claimed by:** {interaction.user.mention}\n**Type:** {ticket.ticket_type.value}",
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        # Notify user if DM ticket
        if ticket.ticket_type == TicketType.DM:
            try:
                user = await bot.fetch_user(ticket.user_id)
                await user.send(f"✅ **Support Ticket Claimed**\nA staff member ({interaction.user.display_name}) is now handling your ticket.")
            except:
                pass
        
        await interaction.response.send_message(f"✅ Claimed ticket for {ticket.user_name}", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Failed to claim ticket", ephemeral=True)

@bot.tree.command(name="resolve", description="Mark a ticket as resolved")
@is_staff()
async def resolve_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    """Mark ticket as resolved (closes after confirmation)"""
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
        embed = discord.Embed(
            title="✅ Ticket Resolved",
            description=f"Ticket for {ticket.user_name} has been marked as resolved.\nThe channel will close in 30 seconds.",
            color=0xffaa00,
            timestamp=datetime.utcnow()
        )
        
        channel = bot.get_channel(ticket.channel_id)
        if channel:
            await channel.send(embed=embed)
        
        # Notify user if DM ticket
        if ticket.ticket_type == TicketType.DM:
            try:
                user = await bot.fetch_user(ticket.user_id)
                await user.send(f"✅ **Ticket Resolved**\nYour ticket has been marked as resolved. It will close soon. If you need more help, just send another message!")
            except:
                pass
        
        await interaction.response.send_message(f"✅ Ticket resolved for {ticket.user_name}. Closing in 30s...", ephemeral=True)
        
        # Auto-close after 30 seconds
        await asyncio.sleep(30)
        await close_ticket_by_user(target_user_id, interaction.channel)
    else:
        await interaction.response.send_message("❌ Failed to resolve ticket", ephemeral=True)

@bot.tree.command(name="close", description="Close the current ticket")
@is_manager()
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
    """Helper function to close a ticket"""
    ticket = await ticket_manager.close_ticket(user_id)
    
    if ticket and channel:
        try:
            # Save transcript
            transcript_channel = discord.utils.get(channel.guild.text_channels, name="ticket-logs")
            if not transcript_channel:
                # Create logs channel if it doesn't exist
                overwrites = {
                    channel.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    channel.guild.me: discord.PermissionOverwrite(read_messages=True)
                }
                transcript_channel = await channel.guild.create_text_channel("ticket-logs", overwrites=overwrites)
            
            # Send closure log
            embed = discord.Embed(
                title="📋 Ticket Closed",
                description=f"**User:** {ticket.user_name}\n**User ID:** {ticket.user_id}\n**Type:** {ticket.ticket_type.value}\n**Messages:** {ticket.message_count}",
                color=0xef4444,
                timestamp=datetime.utcnow()
            )
            if ticket.claimed_by_name:
                embed.add_field(name="Claimed By", value=ticket.claimed_by_name, inline=True)
            embed.add_field(name="Open Duration", value=str(datetime.utcnow() - ticket.created_at).split('.')[0], inline=True)
            
            await transcript_channel.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Failed to save transcript: {e}")
        
        try:
            await channel.delete()
            logger.info(f"Closed ticket channel for {ticket.user_name}")
        except Exception as e:
            logger.error(f"Failed to delete channel: {e}")

@bot.tree.command(name="tickets", description="List all active tickets")
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
        claimed_by = ticket.claimed_by_name or "Unclaimed"
        
        embed.add_field(
            name=f"{status_emoji} {ticket.user_name} ({ticket.ticket_type.value})",
            value=f"Status: {ticket.status.value} | Claimed by: {claimed_by} | Messages: {ticket.message_count}\nUser ID: `{ticket.user_id}`",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ticket_info", description="Get info about a ticket")
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
        await interaction.response.send_message(f"❌ No active ticket found for user {user_id}", ephemeral=True)
        return
    
    embed = discord.Embed(title=f"📋 Ticket Info - {ticket.user_name}", color=0x3b82f6)
    embed.add_field(name="User ID", value=str(ticket.user_id), inline=True)
    embed.add_field(name="Type", value=ticket.ticket_type.value, inline=True)
    embed.add_field(name="Status", value=ticket.status.value, inline=True)
    embed.add_field(name="Created", value=ticket.created_at.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.add_field(name="Messages", value=str(ticket.message_count), inline=True)
    
    if ticket.claimed_by_name:
        embed.add_field(name="Claimed By", value=ticket.claimed_by_name, inline=True)
        embed.add_field(name="Claimed At", value=ticket.claimed_at.strftime("%Y-%m-%d %H:%M:%S") if ticket.claimed_at else "N/A", inline=True)
    
    channel = bot.get_channel(ticket.channel_id)
    if channel:
        embed.add_field(name="Channel", value=channel.mention, inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="transfer", description="Transfer ticket to another staff member")
@is_manager()
async def transfer_command(interaction: discord.Interaction, user_id: str, new_staff_id: str):
    """Transfer a ticket to another staff member"""
    try:
        uid = int(user_id)
        staff_uid = int(new_staff_id)
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
    
    staff_user = await bot.fetch_user(staff_uid)
    if not staff_user:
        await interaction.response.send_message("❌ Staff user not found", ephemeral=True)
        return
    
    old_staff = ticket.claimed_by_name
    
    async with ticket_manager.lock:
        ticket.claimed_by = staff_uid
        ticket.claimed_by_name = staff_user.display_name
        ticket.claimed_at = datetime.utcnow()
    
    embed = discord.Embed(
        title="🔄 Ticket Transferred",
        description=f"**User:** {ticket.user_name}\n**From:** {old_staff}\n**To:** {staff_user.display_name}",
        color=0xffaa00,
        timestamp=datetime.utcnow()
    )
    
    channel = bot.get_channel(ticket.channel_id)
    if channel:
        await channel.send(embed=embed)
    
    await interaction.response.send_message(f"✅ Ticket transferred to {staff_user.display_name}", ephemeral=True)

@bot.tree.command(name="note", description="Add a private note to a ticket (staff only)")
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
    
    embed = discord.Embed(
        title="📝 Staff Note Added",
        description=note,
        color=0x3b82f6,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text=f"Added by {interaction.user.display_name}")
    
    # Send to staff channel or log channel
    log_channel = discord.utils.get(interaction.guild.text_channels, name="staff-notes")
    if not log_channel:
        # Create a private staff notes channel
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True)
        }
        if STAFF_ROLE_ID:
            staff_role = interaction.guild.get_role(STAFF_ROLE_ID)
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True)
        log_channel = await interaction.guild.create_text_channel("staff-notes", overwrites=overwrites)
    
    await log_channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Note added to ticket for {ticket.user_name}", ephemeral=True)

# --- ADMIN COMMANDS ---

@bot.tree.command(name="purge", description="Close ALL support tickets")
@is_owner()
async def purge_command(interaction: discord.Interaction):
    """Close all support tickets"""
    await interaction.response.send_message("🔍 Finding all ticket channels...", ephemeral=True)
    
    if not TICKET_CATEGORY_ID:
        await interaction.followup.send("❌ No ticket category configured", ephemeral=True)
        return
    
    category = bot.get_channel(TICKET_CATEGORY_ID)
    if not category:
        await interaction.followup.send("❌ Category not found", ephemeral=True)
        return
    
    ticket_channels = [ch for ch in category.text_channels if ch.name.startswith(("web-", "ticket-"))]
    
    if not ticket_channels:
        await interaction.followup.send("📭 No ticket channels found", ephemeral=True)
        return
    
    await interaction.edit_original_response(content=f"🗑️ Closing {len(ticket_channels)} ticket channels...")
    
    closed_count = 0
    for channel in ticket_channels:
        try:
            await channel.delete()
            closed_count += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Failed to delete {channel.name}: {e}")
    
    await interaction.edit_original_response(
        content=f"✅ Closed {closed_count}/{len(ticket_channels)} ticket channels"
    )
    logger.info(f"Purged {closed_count} tickets by {interaction.user.name}")

@bot.tree.command(name="maintenance", description="Put bot in maintenance mode")
@is_owner()
async def maintenance_command(interaction: discord.Interaction, reason: Optional[str] = None):
    """Toggle maintenance mode"""
    global is_maintenance_mode, maintenance_reason
    
    is_maintenance_mode = not is_maintenance_mode
    maintenance_reason = reason or "No reason provided"
    
    status = "ENABLED" if is_maintenance_mode else "DISABLED"
    
    embed = discord.Embed(
        title=f"🛠️ Maintenance Mode {status}",
        description=f"**Reason:** {maintenance_reason}\n**Changed by:** {interaction.user.mention}",
        color=0xffaa00 if is_maintenance_mode else 0x00ff00,
        timestamp=datetime.utcnow()
    )
    
    await interaction.response.send_message(embed=embed)
    logger.warning(f"Maintenance mode {status} by {interaction.user.name}")

@bot.tree.command(name="restart", description="Restart the bot")
@is_owner()
async def restart_command(interaction: discord.Interaction):
    """Restart the bot"""
    embed = discord.Embed(
        title="🔄 Restarting Bot",
        description="Bot is restarting... This may take 30-60 seconds.",
        color=0xffaa00,
        timestamp=datetime.utcnow()
    )
    await interaction.response.send_message(embed=embed)
    
    logger.warning(f"Bot restart initiated by {interaction.user.name}")
    await asyncio.sleep(2)
    await bot.close()
    os._exit(0)

@bot.tree.command(name="stats", description="Show bot statistics")
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
    embed.add_field(name="💬 Cached Messages", value=str(len(message_queue)), inline=True)
    embed.add_field(name="📁 Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="🛠️ Maintenance", value="Enabled" if is_maintenance_mode else "Disabled", inline=True)
    embed.add_field(name="🎫 Total Tickets", value=str(ticket_stats['total']), inline=True)
    embed.add_field(name="🟢 Open Tickets", value=str(ticket_stats['open']), inline=True)
    embed.add_field(name="🟡 Claimed Tickets", value=str(ticket_stats['claimed']), inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
async def ping_command(interaction: discord.Interaction):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! `{latency}ms`", ephemeral=True)

@bot.tree.command(name="commands", description="List all available commands")
async def commands_list(interaction: discord.Interaction):
    """List all commands"""
    embed = discord.Embed(title="📋 Bot Commands", color=0xef4444)
    embed.add_field(name="📌 User Commands", value="/stats, /ping, /commands", inline=False)
    embed.add_field(name="👥 Staff Commands", value="/reply [user_id] message\n/claim [user_id]\n/resolve [user_id]\n/tickets\n/ticket_info user_id\n/note user_id message", inline=False)
    embed.add_field(name="⭐ Manager Commands", value="/close\n/transfer user_id new_staff_id", inline=False)
    embed.add_field(name="👑 Owner Commands", value="/restart\n/maintenance [reason]\n/purge\n/clear_cache [user_id]", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear_cache", description="Clear rate limit cache")
@is_manager()
async def clear_cache_command(interaction: discord.Interaction, user_id: Optional[str] = None):
    """Clear rate limit cache"""
    if user_id:
        try:
            uid = int(user_id)
            rate_limiter.user_messages.pop(uid, None)
            await interaction.response.send_message(f"✅ Cleared cache for user {user_id}", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user ID", ephemeral=True)
    else:
        rate_limiter.user_messages.clear()
        await interaction.response.send_message("✅ Cleared all rate limit caches", ephemeral=True)

# --- ERROR HANDLING ---
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle command errors"""
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("⛔ You don't have permission to use this command.", ephemeral=True)
    elif isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"⏰ Try again in {error.retry_after:.1f}s", ephemeral=True)
    else:
        logger.error(f"Command error: {error}")
        await interaction.response.send_message(f"❌ Error: {str(error)[:100]}", ephemeral=True)

# --- DISCORD EVENTS ---
@bot.event
async def on_ready():
    """Bot is ready"""
    logger.info(f"✅ Logged in as: {bot.user.name} ({bot.user.id})")
    logger.info(f"✅ Connected to {len(bot.guilds)} guilds")
    logger.info(f"✅ SSE endpoint: http://localhost:{PORT}/events")
    
    guild = discord.Object(id=GUILD_ID)
    commands = await bot.tree.fetch_commands(guild=guild)
    logger.info(f"✅ Synced {len(commands)} slash commands")

@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages"""
    if message.author == bot.user:
        return
    
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm(message)
        return
    
    await bot.process_commands(message)

async def handle_dm(message: discord.Message):
    """Handle DM messages and create/update tickets"""
    global is_maintenance_mode
    
    if is_maintenance_mode:
        await message.author.send(f"🛠️ **Bot is in maintenance mode**\nReason: {maintenance_reason}\nPlease try again later.")
        return
    
    # Check rate limit
    can_send, error = rate_limiter.can_send(message.author.id, message.content)
    if not can_send:
        await message.author.send(f"❌ {error}")
        return
    
    rate_limiter.record(message.author.id)
    
    # Check for existing ticket
    ticket = await ticket_manager.get_ticket_by_user(message.author.id)
    
    if ticket:
        # Update existing ticket
        await ticket_manager.add_message(message.author.id)
        
        # Send to existing channel
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
        
        logger.info(f"DM from {message.author.name} added to existing ticket")
        return
    
    # Create new ticket
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await message.author.send("❌ Support system unavailable. Please try again later.")
        logger.error(f"Guild {GUILD_ID} not found")
        return
    
    # Create or get support category
    category = None
    if TICKET_CATEGORY_ID:
        category = bot.get_channel(TICKET_CATEGORY_ID)
    
    if not category:
        # Find or create a category
        category = discord.utils.get(guild.categories, name="SUPPORT TICKETS")
        if not category:
            category = await guild.create_category("SUPPORT TICKETS")
    
    # Create ticket channel
    channel_name = f"ticket-{message.author.name.lower().replace(' ', '-')}"
    
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
    
    channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
    
    # Create ticket in manager
    ticket = await ticket_manager.create_ticket(message.author.id, message.author.name, channel.id, TicketType.DM)
    
    # Send welcome message
    embed = discord.Embed(
        title="🎫 New DM Support Ticket",
        description=f"**User:** {message.author.mention}\n**Name:** {message.author.name}\n**ID:** `{message.author.id}`",
        color=0xef4444,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Account Created", value=message.author.created_at.strftime("%Y-%m-%d"), inline=True)
    
    await channel.send(embed=embed)
    
    # Send user's message
    msg_embed = discord.Embed(
        description=message.content,
        color=0x3b82f6,
        timestamp=datetime.utcnow()
    )
    msg_embed.set_author(name=f"📬 {message.author.name}", icon_url=message.author.display_avatar.url)
    msg_embed.set_footer(text=f"User ID: {message.author.id}")
    
    await channel.send(embed=msg_embed)
    
    # Add claim button
    class ClaimButton(discord.ui.View):
        def __init__(self, user_id: int):
            super().__init__(timeout=None)
            self.user_id = user_id
            self.claimed = False
        
        @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="✋")
        async def claim_button(self, interaction_button: discord.Interaction, button: discord.ui.Button):
            if self.claimed:
                await interaction_button.response.send_message("❌ Ticket already claimed", ephemeral=True)
                return
            
            result = await ticket_manager.claim_ticket(self.user_id, interaction_button.user.id, interaction_button.user.display_name)
            
            if result:
                self.claimed = True
                button.disabled = True
                await interaction_button.message.edit(view=self)
                
                await interaction_button.response.send_message(f"✅ Claimed ticket for {message.author.name}", ephemeral=True)
                
                try:
                    await message.author.send(f"✅ **Ticket Claimed**\nStaff member {interaction_button.user.display_name} is now handling your ticket.")
                except:
                    pass
                
                claim_embed = discord.Embed(
                    title="✅ Ticket Claimed",
                    description=f"Claimed by {interaction_button.user.mention}",
                    color=0x00ff00,
                    timestamp=datetime.utcnow()
                )
                await channel.send(embed=claim_embed)
            else:
                await interaction_button.response.send_message("❌ Failed to claim ticket", ephemeral=True)
    
    await channel.send(view=ClaimButton(message.author.id))
    await message.author.send("✅ **Support ticket created!** Staff will be with you shortly. You'll receive a DM when someone claims your ticket.")
    
    logger.info(f"New DM ticket created for {message.author.name}")

# --- MAIN ---
if __name__ == "__main__":
    try:
        logger.info(f"🚀 Starting bot on port {PORT}...")
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Invalid bot token")
    except Exception as e:
        logger.error(f"❌ Failed to start: {e}")
