import os
import discord
import asyncio
import logging
import json
import time
import hashlib
from typing import Optional, Set, Dict, Any, Tuple
from datetime import datetime, timedelta
from discord import app_commands
from discord.ext import commands, tasks
import websockets
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('RawrBot')

# --- ENUMS & DATA CLASSES ---

class MessageOrigin(Enum):
    """Origin of messages"""
    DISCORD = "discord"
    WEB = "web"

class MessageType(Enum):
    """Types of messages"""
    DM = "dm"
    CHAT = "chat"
    REPLY = "reply"

@dataclass
class Config:
    """Centralized configuration"""
    token: str
    guild_id: int
    ticket_category_id: int
    owner_id: int
    staff_role_id: int
    manager_role_id: int
    websocket_url: str
    website_url: str
    rate_limit_seconds: int = 5
    max_messages_per_minute: int = 12
    duplicate_window_seconds: int = 30
    
    @staticmethod
    def from_env() -> 'Config':
        """Load configuration from environment variables"""
        token = os.getenv('BOT_TOKEN')
        if not token:
            raise ValueError("BOT_TOKEN environment variable not set")
        
        guild_id = os.getenv('GUILD_ID')
        if not guild_id:
            raise ValueError("GUILD_ID environment variable not set")
        
        return Config(
            token=token,
            guild_id=int(guild_id),
            ticket_category_id=int(os.getenv('TICKET_CATEGORY_ID', '1490508234526556321')),
            owner_id=int(os.getenv('OWNER_ID', '1071330258172780594')),
            staff_role_id=int(os.getenv('STAFF_ROLE_ID', '1489713077963456564')),
            manager_role_id=int(os.getenv('MANAGER_ROLE_ID', '1489435265914109972')),
            websocket_url=os.getenv('WEBSOCKET_URL', 'ws://localhost:8765'),
            website_url=os.getenv('WEBSITE_URL', 'https://rawrs.zapto.org/'),
        )

@dataclass
class Ticket:
    """Ticket data class"""
    user_id: int
    user_name: str
    channel_id: int
    claimed_by: Optional[int] = None
    claimed_by_name: Optional[str] = None
    claimed_by_rank: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'user_id': self.user_id,
            'user_name': self.user_name,
            'channel_id': self.channel_id,
            'claimed_by': self.claimed_by,
            'claimed_by_name': self.claimed_by_name,
            'claimed_by_rank': self.claimed_by_rank,
            'created_at': self.created_at.isoformat(),
        }

class TicketManager:
    """Manages support tickets"""
    
    def __init__(self):
        self.tickets: Dict[int, Ticket] = {}  # user_id -> Ticket
        self.lock = asyncio.Lock()
    
    async def create(self, user_id: int, user_name: str, channel_id: int) -> Optional[Ticket]:
        """Create a new ticket (if doesn't exist)"""
        async with self.lock:
            if user_id in self.tickets:
                return self.tickets[user_id]
            
            ticket = Ticket(
                user_id=user_id,
                user_name=user_name,
                channel_id=channel_id
            )
            self.tickets[user_id] = ticket
            logger.info(f"Ticket created for {user_name} ({user_id})")
            return ticket
    
    async def get(self, user_id: int) -> Optional[Ticket]:
        """Get ticket for user"""
        async with self.lock:
            return self.tickets.get(user_id)
    
    async def claim(self, user_id: int, staff_id: int, staff_name: str) -> Optional[Ticket]:
        """Claim a ticket"""
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if not ticket:
                return None
            
            ticket.claimed_by = staff_id
            ticket.claimed_by_name = staff_name
            logger.info(f"Ticket for {ticket.user_name} claimed by {staff_name}")
            return ticket
    
    async def close(self, user_id: int) -> Optional[Ticket]:
        """Close a ticket"""
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                logger.info(f"Ticket closed for {ticket.user_name}")
            return ticket
    
    async def get_all(self) -> list[Ticket]:
        """Get all open tickets"""
        async with self.lock:
            return list(self.tickets.values())

# --- RATE LIMITING ---

class RateLimiter:
    """Rate limiting manager with improved data structures"""
    
    def __init__(self, config: Config):
        self.config = config
        self.user_messages: Dict[int, deque] = {}
        self.message_hashes: Dict[str, float] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def start_cleanup(self) -> None:
        """Start periodic cleanup of old rate limit data"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Rate limiter cleanup started")
    
    async def _cleanup_loop(self) -> None:
        """Cleanup loop running in background"""
        try:
            while True:
                await asyncio.sleep(60)
                self._cleanup_old_data()
        except asyncio.CancelledError:
            logger.info("Rate limiter cleanup cancelled")
    
    def _cleanup_old_data(self) -> None:
        """Remove expired rate limit entries"""
        current_time = time.time()
        cleaned_users = 0
        cleaned_hashes = 0
        
        for user_id in list(self.user_messages.keys()):
            while self.user_messages[user_id] and current_time - self.user_messages[user_id][0] > 60:
                self.user_messages[user_id].popleft()
            
            if not self.user_messages[user_id]:
                del self.user_messages[user_id]
                cleaned_users += 1
        
        for msg_hash in list(self.message_hashes.keys()):
            if current_time - self.message_hashes[msg_hash] > self.config.duplicate_window_seconds:
                del self.message_hashes[msg_hash]
                cleaned_hashes += 1
        
        if cleaned_users > 0 or cleaned_hashes > 0:
            logger.debug(f"Cleaned {cleaned_users} users, {cleaned_hashes} message hashes")
    
    def can_send(self, user_id: int, message_content: str) -> Tuple[bool, str]:
        """Check if user can send a message"""
        current_time = time.time()
        
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=self.config.max_messages_per_minute)
        
        user_deque = self.user_messages[user_id]
        
        if len(user_deque) >= self.config.max_messages_per_minute:
            oldest = user_deque[0]
            if current_time - oldest < 60:
                remaining = 60 - (current_time - oldest)
                return False, f"Rate limit: {self.config.max_messages_per_minute} messages per minute. Retry in {remaining:.0f}s"
        
        if user_deque:
            time_since_last = current_time - user_deque[-1]
            if time_since_last < self.config.rate_limit_seconds:
                wait_time = self.config.rate_limit_seconds - time_since_last
                return False, f"Wait {wait_time:.1f}s before sending another message"
        
        message_hash = self._hash_message(user_id, message_content)
        if message_hash in self.message_hashes:
            time_since_duplicate = current_time - self.message_hashes[message_hash]
            if time_since_duplicate < self.config.duplicate_window_seconds:
                return False, "Duplicate message detected within 30s"
        
        user_deque.append(current_time)
        self.message_hashes[message_hash] = current_time
        
        return True, "OK"
    
    @staticmethod
    def _hash_message(user_id: int, content: str) -> str:
        """Generate hash of user + message for duplicate detection"""
        return hashlib.md5(f"{user_id}:{content}".encode()).hexdigest()
    
    def clear_user(self, user_id: int) -> None:
        """Clear rate limit cache for specific user"""
        if user_id in self.user_messages:
            del self.user_messages[user_id]
        logger.info(f"Cleared rate limit cache for user {user_id}")
    
    def clear_all(self) -> None:
        """Clear all rate limit data"""
        self.user_messages.clear()
        self.message_hashes.clear()
        logger.info("Cleared all rate limit caches")
    
    def stop(self) -> None:
        """Stop cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()

# --- WEBSOCKET MANAGEMENT ---

class WebSocketManager:
    """Manages WebSocket connection with improved error handling"""
    
    MAX_RETRIES = 5
    RETRY_DELAY = 5
    HEARTBEAT_INTERVAL = 30
    HEARTBEAT_TIMEOUT = 45
    PROCESSED_IDS_MAX = 1000
    PROCESSED_IDS_TRIM = 500
    
    def __init__(self, bot: 'RawrBot', config: Config):
        self.bot = bot
        self.config = config
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.processed_ids: Set[str] = set()
        self.last_heartbeat = time.time()
        self.is_connected = False
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
    
    async def connect(self) -> bool:
        """Connect to WebSocket server with exponential backoff"""
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                self.websocket = await asyncio.wait_for(
                    websockets.connect(
                        self.config.websocket_url,
                        ping_interval=20,
                        ping_timeout=10,
                        close_timeout=5
                    ),
                    timeout=10
                )
                self.is_connected = True
                self.last_heartbeat = time.time()
                logger.info(f"Connected to WebSocket: {self.config.websocket_url}")
                
                self._listen_task = asyncio.create_task(self._listen_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                
                return True
                
            except asyncio.TimeoutError:
                logger.warning(f"WebSocket connection timeout (attempt {attempt}/{self.MAX_RETRIES})")
            except Exception as e:
                logger.warning(f"WebSocket connection failed (attempt {attempt}/{self.MAX_RETRIES}): {e}")
            
            if attempt < self.MAX_RETRIES:
                wait_time = self.RETRY_DELAY * attempt
                logger.info(f"Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
        
        logger.error("Max WebSocket connection retries exceeded")
        return False
    
    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat"""
        try:
            while self.is_connected and self.websocket:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                time_since_heartbeat = time.time() - self.last_heartbeat
                if time_since_heartbeat > self.HEARTBEAT_TIMEOUT:
                    logger.warning("Heartbeat timeout, reconnecting...")
                    await self.reconnect()
        except asyncio.CancelledError:
            logger.debug("Heartbeat loop cancelled")
    
    async def _listen_loop(self) -> None:
        """Listen for incoming messages"""
        try:
            async for message in self.websocket:
                self.last_heartbeat = time.time()
                try:
                    data = json.loads(message)
                    await self.process_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON from WebSocket: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e.rcvd} {e.reason}")
            await self.reconnect()
        except Exception as e:
            logger.error(f"WebSocket listen error: {e}", exc_info=True)
            await self.reconnect()
    
    async def reconnect(self) -> None:
        """Reconnect to WebSocket server"""
        self.is_connected = False
        
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.debug(f"Error closing WebSocket: {e}")
        
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        await asyncio.sleep(5)
        await self.connect()
    
    async def process_message(self, data: Dict[str, Any]) -> None:
        """Process incoming WebSocket message"""
        msg_id = data.get('id')
        if not msg_id:
            logger.warning("Received message without ID")
            return
        
        if msg_id in self.processed_ids:
            logger.debug(f"Skipping duplicate message: {msg_id}")
            return
        
        self.processed_ids.add(msg_id)
        
        if len(self.processed_ids) > self.PROCESSED_IDS_MAX:
            self.processed_ids = set(list(self.processed_ids)[-self.PROCESSED_IDS_TRIM:])
        
        msg_type = data.get('type')
        msg_origin = data.get('origin')
        
        if msg_origin == 'web' and msg_type == 'chat':
            await self._process_web_chat(data)
        elif msg_type == 'reply':
            logger.info(f"Staff reply from: {data.get('user')}")
        else:
            logger.debug(f"Unhandled message type: {msg_type}")
    
    async def _process_web_chat(self, msg: Dict[str, Any]) -> None:
        """Process web chat message"""
        guild = self.bot.get_guild(self.config.guild_id)
        category = self.bot.get_channel(self.config.ticket_category_id)
        
        if not guild or not category:
            logger.error(f"Missing guild or category")
            return
        
        user_name = msg.get('user', 'unknown')
        channel_name = f"web-{user_name.lower().replace(' ', '-')}"
        
        channel = discord.utils.get(category.text_channels, name=channel_name)
        if not channel:
            channel = await self._create_web_channel(guild, category, channel_name, user_name)
            if not channel:
                return
        
        embed = discord.Embed(
            description=msg.get('text', ''),
            color=0xef4444,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"🌐 Web: {user_name}")
        embed.set_footer(text=f"ID: {msg.get('id', 'unknown')[:8]}")
        
        try:
            await channel.send(embed=embed)
            logger.info(f"Forwarded web message from {user_name}")
        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")
    
    async def _create_web_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        channel_name: str,
        user_name: str
    ) -> Optional[discord.TextChannel]:
        """Create new web chat channel"""
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Web chat from {user_name}"
            )
            await channel.send(f"🚀 **Chat Session Started:** `{user_name}`")
            logger.info(f"Created web channel: {channel_name}")
            return channel
        except discord.Forbidden:
            logger.error(f"Permission denied creating channel {channel_name}")
        except discord.HTTPException as e:
            logger.error(f"HTTP error creating channel: {e}")
        return None
    
    async def send_message(self, message: Dict[str, Any]) -> bool:
        """Send message through WebSocket"""
        if not self.is_connected or not self.websocket:
            logger.error("WebSocket not connected")
            return False
        
        try:
            message['id'] = self._generate_message_id(message)
            message['timestamp'] = datetime.utcnow().isoformat()
            
            await self.websocket.send(json.dumps(message))
            logger.info(f"Sent message type: {message.get('type')}")
            return True
        except Exception as e:
            logger.error(f"Failed to send WebSocket message: {e}")
            return False
    
    @staticmethod
    def _generate_message_id(message: Dict[str, Any]) -> str:
        """Generate unique message ID"""
        text_hash = hashlib.md5(message.get('text', '').encode()).hexdigest()[:8]
        return f"msg_{int(time.time() * 1000)}_{text_hash}"
    
    async def close(self) -> None:
        """Close WebSocket connection"""
        self.is_connected = False
        
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.debug(f"Error closing WebSocket: {e}")

# --- BOT CLASS ---

class RawrBot(commands.Bot):
    """Main Discord bot"""
    
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        self.config = config
        self.boot_time = datetime.utcnow()
        self.ws_manager = WebSocketManager(self, config)
        self.rate_limiter = RateLimiter(config)
        self.ticket_manager = TicketManager()
        self.user_first_message: Set[int] = set()
        
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self) -> None:
        """Initialize bot"""
        logger.info("Bot setup starting...")
        
        await self.ws_manager.connect()
        self.rate_limiter.start_cleanup()
        
        self.tree.copy_global_to(guild=discord.Object(id=self.config.guild_id))
        await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
        
        self.cache_cleanup_task.start()
        self.status_update_task.start()
        
        logger.info("Bot setup completed")
    
    async def close(self) -> None:
        """Cleanup when bot stops"""
        logger.info("Bot shutting down...")
        
        if self.cache_cleanup_task.is_running():
            self.cache_cleanup_task.cancel()
        if self.status_update_task.is_running():
            self.status_update_task.cancel()
        
        await self.ws_manager.close()
        self.rate_limiter.stop()
        
        await super().close()
        logger.info("Bot shutdown complete")
    
    @tasks.loop(hours=6)
    async def cache_cleanup_task(self) -> None:
        """Periodically clean caches"""
        logger.info("Running cache cleanup")
        self.rate_limiter.clear_all()
    
    @tasks.loop(minutes=30)
    async def status_update_task(self) -> None:
        """Update bot status"""
        try:
            guild_count = len(self.guilds)
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.competing,
                    name=f"{guild_count} servers | /help"
                )
            )
        except Exception as e:
            logger.error(f"Failed to update status: {e}")
    
    @cache_cleanup_task.before_loop
    @status_update_task.before_loop
    async def before_loop(self) -> None:
        """Wait for bot to be ready"""
        await self.wait_until_ready()
    
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Handle prefix command errors"""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏰ Try again in {error.retry_after:.1f}s", delete_after=5)
        else:
            logger.error(f"Command error: {error}", exc_info=True)

# --- EVENT HANDLERS ---

async def setup_events(bot: RawrBot) -> None:
    """Register event handlers"""
    
    @bot.event
    async def on_ready() -> None:
        """Called when bot is ready"""
        logger.info(f"Logged in: {bot.user.name} ({bot.user.id})")
        logger.info(f"Connected to {len(bot.guilds)} guilds")
        
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.competing,
                name="rawrs.zapto.org"
            )
        )
    
    @bot.event
    async def on_message(message: discord.Message) -> None:
        """Handle incoming messages"""
        if message.author == bot.user:
            return
        
        if isinstance(message.channel, discord.DMChannel):
            await handle_dm(bot, message)
            return
        
        await bot.process_commands(message)

async def handle_dm(bot: RawrBot, message: discord.Message) -> None:
    """Handle DM messages"""
    user_id = message.author.id
    
    if user_id in bot.user_first_message:
        logger.debug(f"Ignoring repeat DM from {message.author.name} (ticket pending)")
        return
    
    allowed, reason = bot.rate_limiter.can_send(user_id, message.content)
    if not allowed:
        await message.author.send(f"❌ {reason}")
        return
    
    guild = bot.get_guild(bot.config.guild_id)
    if not guild:
        await message.author.send("❌ Support system unavailable")
        return
    
    bot.user_first_message.add(user_id)
    
    category = bot.get_channel(bot.config.ticket_category_id)
    if not category:
        await message.author.send("❌ Support system unavailable")
        return
    
    channel_name = f"ticket-{message.author.name.lower().replace(' ', '-')}"
    channel = discord.utils.get(category.text_channels, name=channel_name)
    
    if not channel:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.get_role(bot.config.staff_role_id): discord.PermissionOverwrite(read_messages=True),
            }
            
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Support ticket from {message.author.name}"
            )
            logger.info(f"Created ticket channel: {channel_name}")
        except Exception as e:
            logger.error(f"Failed to create channel: {e}")
            await message.author.send("❌ Failed to create support ticket")
            bot.user_first_message.discard(user_id)
            return
    
    ticket = await bot.ticket_manager.create(user_id, message.author.name, channel.id)
    
    embed = discord.Embed(
        title="🌐 RAWr.xyz Support",
        description="Thanks for reaching out! Our support team will assist you shortly.\n\nFor script updates and links, use `/updates` or visit **rawrs.zapto.org**",
        color=0xef4444
    )
    embed.add_field(name="Status", value="⏳ Waiting for support staff...", inline=False)
    embed.set_footer(text="Your support ticket has been created")
    
    try:
        await message.author.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send embed to user: {e}")
    
    staff_embed = discord.Embed(
        title="📋 New Support Ticket",
        description=f"**User:** {message.author.name} ({message.author.id})\n**Message:** {message.content}",
        color=0x3b82f6,
        timestamp=datetime.utcnow()
    )
    staff_embed.set_footer(text=f"Ticket ID: {user_id}")
    
    class ClaimButton(discord.ui.View):
        def __init__(self, ticket_user_id: int):
            super().__init__(timeout=None)
            self.ticket_user_id = ticket_user_id
        
        @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="✋")
        async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            """Claim ticket"""
            user_roles = {role.id for role in interaction.user.roles}
            if bot.config.owner_id != interaction.user.id and not (
                bot.config.staff_role_id in user_roles or bot.config.manager_role_id in user_roles
            ):
                await interaction.response.send_message("❌ You don't have permission to claim tickets", ephemeral=True)
                return
            
            ticket = await bot.ticket_manager.claim(
                self.ticket_user_id,
                interaction.user.id,
                interaction.user.name
            )
            
            if not ticket:
                await interaction.response.send_message("❌ Ticket not found", ephemeral=True)
                return
            
            rank = "Manager"
            if bot.config.manager_role_id in user_roles:
                rank = "Manager"
            elif bot.config.staff_role_id in user_roles:
                rank = "Staff"
            else:
                rank = "Owner"
            
            button.disabled = True
            await interaction.message.edit(view=self)
            
            await interaction.response.send_message(
                f"✅ You've claimed this ticket! Channel: <#{ticket.channel_id}>",
                ephemeral=True
            )
            
            try:
                user = await bot.fetch_user(self.ticket_user_id)
                claim_embed = discord.Embed(
                    title="✅ Support Staff Assigned",
                    description=f"**Staff Member:** {interaction.user.name}\n**Rank:** {rank}\n\nA staff member has claimed your ticket and will be assisting you shortly!",
                    color=0x22c55e
                )
                claim_embed.add_field(name="What's Next?", value="The staff member will respond to your message in this DM shortly.", inline=False)
                await user.send(embed=claim_embed)
            except Exception as e:
                logger.error(f"Failed to send claim notification to user: {e}")
            
            ticket_embed = discord.Embed(
                title=f"Ticket Claimed by {interaction.user.name}",
                description=f"This ticket has been claimed.\n\n**Rank:** {rank}\n**Response Time:** Usually within minutes",
                color=0x22c55e
            )
            
            try:
                ch = bot.get_channel(ticket.channel_id)
                if ch:
                    await ch.send(embed=ticket_embed)
            except Exception as e:
                logger.error(f"Failed to send to ticket channel: {e}")
    
    try:
        await channel.send(embed=staff_embed, view=ClaimButton(user_id))
        logger.info(f"Support ticket created for {message.author.name}")
    except Exception as e:
        logger.error(f"Failed to send to ticket channel: {e}")

# --- PERMISSION CHECKS ---

def is_staff() -> app_commands.check:
    """Check if user is staff"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.client.config.owner_id:
            return True
        
        user_roles = {role.id for role in interaction.user.roles}
        required_roles = {
            interaction.client.config.staff_role_id,
            interaction.client.config.manager_role_id
        }
        return bool(user_roles & required_roles)
    
    return app_commands.check(predicate)

def is_manager() -> app_commands.check:
    """Check if user is manager"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.client.config.owner_id:
            return True
        
        user_roles = {role.id for role in interaction.user.roles}
        return interaction.client.config.manager_role_id in user_roles
    
    return app_commands.check(predicate)

# --- SLASH COMMANDS ---

async def setup_commands(bot: RawrBot) -> None:
    """Register all slash commands"""
    
    @bot.tree.command(name="reply", description="Reply to a support ticket")
    @is_staff()
    async def reply(interaction: discord.Interaction, content: str) -> None:
        """Reply to a support ticket"""
        allowed, reason = bot.rate_limiter.can_send(interaction.user.id, content)
        if not allowed:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        if not interaction.channel or not interaction.channel.name.startswith("ticket-"):
            await interaction.followup.send("❌ Use in ticket channels only")
            return
        
        channel_name = interaction.channel.name
        
        message = {
            "type": MessageType.REPLY.value,
            "user": interaction.user.display_name,
            "user_id": interaction.user.id,
            "text": content,
            "origin": MessageOrigin.DISCORD.value,
        }
        
        success = await bot.ws_manager.send_message(message)
        
        embed = discord.Embed(
            description=content,
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"💬 {interaction.user.display_name}")
        embed.set_footer(text=f"Support Response")
        
        try:
            await interaction.channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send reply: {e}")
        
        await interaction.followup.send("✅ Reply sent!", ephemeral=True)
        logger.info(f"Staff {interaction.user.name} replied in {channel_name}")
    
    @bot.tree.command(name="close", description="Close a support ticket")
    @is_manager()
    async def close(interaction: discord.Interaction) -> None:
        """Close ticket"""
        # FIX: corrected the logic — block if NOT in ticket category
        if not interaction.channel or interaction.channel.category_id != bot.config.ticket_category_id:
            await interaction.response.send_message("❌ Not a ticket channel", ephemeral=True)
            return
        
        # FIX: respond first, then delete — avoids double-response error
        await interaction.response.send_message("🔒 Closing in 5s...")
        await asyncio.sleep(5)
        
        channel_name = interaction.channel.name
        try:
            await interaction.channel.delete()
            logger.info(f"Closed ticket: {channel_name}")
        except discord.Forbidden:
            logger.error(f"Permission denied closing {channel_name}")
            await interaction.followup.send("❌ Permission denied", ephemeral=True)
    
    @bot.tree.command(name="clear_cache", description="Clear rate limits")
    @is_manager()
    async def clear_cache(interaction: discord.Interaction, user_id: Optional[int] = None) -> None:
        """Clear rate limit cache"""
        if user_id:
            bot.rate_limiter.clear_user(user_id)
            bot.user_first_message.discard(user_id)
            msg = f"✅ Cleared cache for user {user_id}"
        else:
            bot.rate_limiter.clear_all()
            bot.user_first_message.clear()
            msg = "✅ Cleared all caches"
        
        await interaction.response.send_message(msg, ephemeral=True)
    
    @bot.tree.command(name="stats", description="Show bot stats")
    @is_staff()
    async def stats(interaction: discord.Interaction) -> None:
        """Display bot statistics"""
        uptime = datetime.utcnow() - bot.boot_time
        days, remainder = divmod(int(uptime.total_seconds()), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        
        ws_status = "✅ Connected" if bot.ws_manager.is_connected else "❌ Disconnected"
        
        embed = discord.Embed(title="📊 Bot Stats", color=0xef4444)
        embed.add_field(name="⏰ Uptime", value=f"{days}d {hours}h {minutes}m", inline=True)
        embed.add_field(name="⚡ Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="🌐 Guilds", value=str(len(bot.guilds)), inline=True)
        embed.add_field(name="🔌 WebSocket", value=ws_status, inline=True)
        embed.add_field(name="🎟️ Open Tickets", value=str(len(bot.ticket_manager.tickets)), inline=True)
        embed.add_field(name="💬 Cached IDs", value=str(len(bot.ws_manager.processed_ids)), inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="ping", description="Check latency")
    async def ping(interaction: discord.Interaction) -> None:
        """Check bot latency"""
        latency_ms = round(bot.latency * 1000)
        
        if latency_ms < 100:
            color, status = 0x00ff00, "🟢 Excellent"
        elif latency_ms < 200:
            color, status = 0xffaa00, "🟡 Good"
        else:
            color, status = 0xef4444, "🔴 Poor"
        
        ws_status = "✅ Connected" if bot.ws_manager.is_connected else "❌ Disconnected"
        
        embed = discord.Embed(title="🏓 Pong!", color=color)
        embed.add_field(name="Latency", value=f"{latency_ms}ms", inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="WebSocket", value=ws_status, inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="website", description="Get website link")
    async def website(interaction: discord.Interaction) -> None:
        """Send website link"""
        embed = discord.Embed(
            title="🌐 Rawr.xyz",
            description=f"[Visit Website]({bot.config.website_url})\nLive chat support available",
            color=0xef4444
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @bot.tree.command(name="updates", description="Get latest updates")
    async def updates(interaction: discord.Interaction) -> None:
        """Show latest updates and links"""
        embed = discord.Embed(
            title="📦 Latest Updates",
            color=0xef4444
        )
        embed.add_field(
            name="Script Updates",
            value=f"Visit [GitHub Repository](https://github.com/imcomingforyou6959-gif/rawr.xyz) for the latest version",
            inline=False
        )
        embed.add_field(
            name="Support",
            value=f"For help, visit [RAWr.xyz]({bot.config.website_url}) or join our [Discord](https://discord.gg/eMpUQzFrNG)",
            inline=False
        )
        embed.add_field(
            name="Changelog",
            value="v2.0.0 - Major stability improvements and new features",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# --- ERROR HANDLING ---

async def setup_error_handlers(bot: RawrBot) -> None:
    """Register error handlers"""
    
    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError
    ) -> None:
        """Handle slash command errors"""
        if isinstance(error, app_commands.CheckFailure):
            # Use followup if already responded, otherwise respond normally
            if interaction.response.is_done():
                await interaction.followup.send(
                    "⛔ Access denied: Staff/Manager role required",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "⛔ Access denied: Staff/Manager role required",
                    ephemeral=True
                )
        elif isinstance(error, app_commands.CommandOnCooldown):
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"⏰ Try again in {error.retry_after:.1f}s",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"⏰ Try again in {error.retry_after:.1f}s",
                    ephemeral=True
                )
        else:
            logger.error(f"Command error: {error}", exc_info=True)
            if interaction.response.is_done():
                await interaction.followup.send("❌ An error occurred", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "❌ An error occurred",
                    ephemeral=True
                )

# --- MAIN EXECUTION ---

async def main() -> None:
    """Main entry point"""
    try:
        config = Config.from_env()
        bot = RawrBot(config)
        
        await setup_events(bot)
        await setup_commands(bot)
        await setup_error_handlers(bot)
        
        logger.info("Starting bot...")
        async with bot:
            await bot.start(config.token)
            
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}")
        exit(1)
