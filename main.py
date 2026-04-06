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

class TicketStatus(Enum):
    """Ticket status states"""
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    CLOSED = "closed"

def safe_int_env(var_name: str, default: Optional[int] = None) -> Optional[int]:
    """Safely convert environment variable to int"""
    value = os.getenv(var_name)
    if not value or value.strip() == '':
        if default is not None:
            logger.warning(f"{var_name} not set, using default: {default}")
            return default
        logger.warning(f"{var_name} not set, will be None")
        return None
    try:
        return int(value)
    except ValueError:
        logger.error(f"Invalid integer for {var_name}: {value}")
        if default is not None:
            return default
        return None

@dataclass
class Config:
    """Centralized configuration"""
    token: str
    guild_id: int
    ticket_category_id: Optional[int]
    owner_id: Optional[int]
    staff_role_id: Optional[int]
    manager_role_id: Optional[int]
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
        
        guild_id_str = os.getenv('GUILD_ID')
        if not guild_id_str:
            raise ValueError("GUILD_ID environment variable not set")
        
        try:
            guild_id = int(guild_id_str)
        except ValueError:
            raise ValueError(f"GUILD_ID must be an integer, got: {guild_id_str}")
        
        return Config(
            token=token,
            guild_id=guild_id,
            ticket_category_id=safe_int_env('TICKET_CATEGORY_ID'),
            owner_id=safe_int_env('OWNER_ID', 1071330258172780594),
            staff_role_id=safe_int_env('STAFF_ROLE_ID'),
            manager_role_id=safe_int_env('MANAGER_ROLE_ID'),
            websocket_url=os.getenv('WEBSOCKET_URL', 'ws://localhost:8765'),
            website_url=os.getenv('WEBSITE_URL', 'https://rawrs.zapto.org/'),
        )

@dataclass
class Ticket:
    """Enhanced ticket data class"""
    user_id: int
    user_name: str
    channel_id: int
    status: TicketStatus = TicketStatus.OPEN
    claimed_by: Optional[int] = None
    claimed_by_name: Optional[str] = None
    claimed_by_rank: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    claimed_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    message_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'user_id': self.user_id,
            'user_name': self.user_name,
            'channel_id': self.channel_id,
            'status': self.status.value,
            'claimed_by': self.claimed_by,
            'claimed_by_name': self.claimed_by_name,
            'claimed_by_rank': self.claimed_by_rank,
            'created_at': self.created_at.isoformat(),
            'claimed_at': self.claimed_at.isoformat() if self.claimed_at else None,
            'closed_at': self.closed_at.isoformat() if self.closed_at else None,
            'message_count': self.message_count,
        }

class TicketManager:
    """Enhanced ticket management system"""
    
    def __init__(self):
        self.tickets: Dict[int, Ticket] = {}
        self.locked_users: Set[int] = set()
        self.lock = asyncio.Lock()
        self.message_log: Dict[int, deque] = {}
    
    async def create(self, user_id: int, user_name: str, channel_id: int) -> Optional[Ticket]:
        """Create a new ticket with safety checks"""
        async with self.lock:
            if user_id in self.tickets:
                existing = self.tickets[user_id]
                if existing.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]:
                    logger.warning(f"User {user_name} already has ticket: {existing.channel_id}")
                    return existing
            
            if user_id in self.locked_users:
                logger.warning(f"User {user_name} already creating ticket")
                return None
            
            self.locked_users.add(user_id)
        
        try:
            ticket = Ticket(
                user_id=user_id,
                user_name=user_name,
                channel_id=channel_id,
                status=TicketStatus.OPEN
            )
            
            async with self.lock:
                self.tickets[user_id] = ticket
                self.message_log[user_id] = deque(maxlen=100)
            
            logger.info(f"✅ Ticket created for {user_name} ({user_id}) in channel {channel_id}")
            return ticket
        finally:
            async with self.lock:
                self.locked_users.discard(user_id)
    
    async def get(self, user_id: int) -> Optional[Ticket]:
        """Get ticket for user"""
        async with self.lock:
            return self.tickets.get(user_id)
    
    async def claim(self, user_id: int, staff_id: int, staff_name: str, staff_rank: str) -> Optional[Ticket]:
        """Claim a ticket with validation"""
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if not ticket:
                logger.warning(f"Ticket not found for user {user_id}")
                return None
            
            if ticket.status == TicketStatus.CLAIMED:
                logger.warning(f"Ticket already claimed by {ticket.claimed_by_name}")
                return None
            
            if ticket.status in [TicketStatus.CLOSED, TicketStatus.RESOLVED]:
                logger.warning(f"Cannot claim closed/resolved ticket")
                return None
            
            ticket.status = TicketStatus.CLAIMED
            ticket.claimed_by = staff_id
            ticket.claimed_by_name = staff_name
            ticket.claimed_by_rank = staff_rank
            ticket.claimed_at = datetime.utcnow()
            
            logger.info(f"✅ Ticket claimed by {staff_name} ({staff_rank}) for {ticket.user_name}")
            return ticket
    
    async def add_message(self, user_id: int) -> None:
        """Track message count"""
        async with self.lock:
            if user_id in self.tickets:
                self.tickets[user_id].message_count += 1
                if user_id in self.message_log:
                    self.message_log[user_id].append(datetime.utcnow())
    
    async def resolve(self, user_id: int) -> Optional[Ticket]:
        """Mark ticket as resolved (not fully closed yet)"""
        async with self.lock:
            ticket = self.tickets.get(user_id)
            if ticket:
                ticket.status = TicketStatus.RESOLVED
                logger.info(f"✅ Ticket resolved for {ticket.user_name}")
            return ticket
    
    async def close(self, user_id: int) -> Optional[Ticket]:
        """Close and remove ticket"""
        async with self.lock:
            ticket = self.tickets.pop(user_id, None)
            if ticket:
                ticket.status = TicketStatus.CLOSED
                ticket.closed_at = datetime.utcnow()
                logger.info(f"✅ Ticket closed for {ticket.user_name} (was open {(ticket.closed_at - ticket.created_at).total_seconds():.0f}s)")
            
            self.message_log.pop(user_id, None)
            self.locked_users.discard(user_id)
            return ticket
    
    async def get_all_open(self) -> list[Ticket]:
        """Get all open/claimed tickets"""
        async with self.lock:
            return [t for t in self.tickets.values() if t.status in [TicketStatus.OPEN, TicketStatus.CLAIMED]]
    
    async def get_stats(self) -> Dict[str, int]:
        """Get ticket statistics"""
        async with self.lock:
            stats = {
                'total': len(self.tickets),
                'open': sum(1 for t in self.tickets.values() if t.status == TicketStatus.OPEN),
                'claimed': sum(1 for t in self.tickets.values() if t.status == TicketStatus.CLAIMED),
                'locking': len(self.locked_users),
            }
            return stats

# --- RATE LIMITING ---

class RateLimiter:
    """Improved rate limiting with per-user tracking"""
    
    def __init__(self, config: Config):
        self.config = config
        self.user_messages: Dict[int, deque] = {}
        self.message_hashes: Set[str] = set()
        self._cleanup_task: Optional[asyncio.Task] = None
    
    def start_cleanup(self) -> None:
        """Start periodic cleanup"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Rate limiter cleanup started")
    
    async def _cleanup_loop(self) -> None:
        """Cleanup loop"""
        try:
            while True:
                await asyncio.sleep(300)
                self._cleanup_old_data()
        except asyncio.CancelledError:
            logger.info("Rate limiter cleanup cancelled")
    
    def _cleanup_old_data(self) -> None:
        """Remove expired entries"""
        current_time = time.time()
        cleaned_users = 0
        
        for user_id in list(self.user_messages.keys()):
            while self.user_messages[user_id] and current_time - self.user_messages[user_id][0] > 60:
                self.user_messages[user_id].popleft()
            
            if not self.user_messages[user_id]:
                del self.user_messages[user_id]
                cleaned_users += 1
        
        if len(self.message_hashes) > 5000:
            self.message_hashes.clear()
        
        if cleaned_users > 0:
            logger.debug(f"Cleaned {cleaned_users} users from rate limiter")
    
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
                return False, f"Rate limited. Retry in {remaining:.0f}s"
        
        if user_deque:
            time_since_last = current_time - user_deque[-1]
            if time_since_last < self.config.rate_limit_seconds:
                wait_time = self.config.rate_limit_seconds - time_since_last
                return False, f"Wait {wait_time:.1f}s before sending another message"
        
        message_hash = self._hash_message(user_id, message_content)
        if message_hash in self.message_hashes:
            return False, "Duplicate message detected"
        
        return True, "OK"
    
    def record_message(self, user_id: int, message_content: str) -> None:
        """Record a message only after successful processing"""
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=self.config.max_messages_per_minute)
        
        self.user_messages[user_id].append(time.time())
        message_hash = self._hash_message(user_id, message_content)
        self.message_hashes.add(message_hash)
    
    @staticmethod
    def _hash_message(user_id: int, content: str) -> str:
        """Generate hash for duplicate detection"""
        return hashlib.md5(f"{user_id}:{content}".encode()).hexdigest()
    
    def clear_user(self, user_id: int) -> None:
        """Clear rate limit for user"""
        if user_id in self.user_messages:
            del self.user_messages[user_id]
        logger.info(f"Cleared rate limit for user {user_id}")
    
    def clear_all(self) -> None:
        """Clear all rate limits"""
        self.user_messages.clear()
        self.message_hashes.clear()
        logger.info("Cleared all rate limits")
    
    def stop(self) -> None:
        """Stop cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()

# --- WEBSOCKET MANAGEMENT ---

class WebSocketManager:
    """Improved WebSocket manager"""
    
    MAX_RETRIES = 5
    RETRY_DELAY = 5
    HEARTBEAT_INTERVAL = 30
    HEARTBEAT_TIMEOUT = 45
    
    def __init__(self, bot: 'RawrBot', config: Config):
        self.bot = bot
        self.config = config
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.processed_ids: Set[str] = set()
        self.last_heartbeat = time.time()
        self.is_connected = False
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._processing_lock = asyncio.Lock()
        self._web_channel_locks: Dict[str, asyncio.Lock] = {}
    
    async def connect(self) -> bool:
        """Connect to WebSocket server"""
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
                logger.info(f"✅ WebSocket connected: {self.config.websocket_url}")
                
                self._listen_task = asyncio.create_task(self._listen_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                
                return True
                
            except asyncio.TimeoutError:
                logger.warning(f"WebSocket timeout (attempt {attempt}/{self.MAX_RETRIES})")
            except Exception as e:
                logger.warning(f"WebSocket connection failed (attempt {attempt}/{self.MAX_RETRIES}): {e}")
            
            if attempt < self.MAX_RETRIES:
                wait_time = self.RETRY_DELAY * attempt
                logger.info(f"Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
        
        logger.error("❌ Max WebSocket retries exceeded")
        return False
    
    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat"""
        try:
            while self.is_connected and self.websocket:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                time_since_heartbeat = time.time() - self.last_heartbeat
                if time_since_heartbeat > self.HEARTBEAT_TIMEOUT:
                    logger.warning("⚠️ Heartbeat timeout, reconnecting...")
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
                    logger.error(f"Invalid JSON: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}")
            await self.reconnect()
        except Exception as e:
            logger.error(f"WebSocket listen error: {e}", exc_info=True)
            await self.reconnect()
    
    async def reconnect(self) -> None:
        """Reconnect to WebSocket"""
        self.is_connected = False
        
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        await asyncio.sleep(5)
        await self.connect()
    
    async def process_message(self, data: Dict[str, Any]) -> None:
        """Process WebSocket message"""
        msg_id = data.get('id')
        if not msg_id:
            logger.warning("Message without ID received")
            return
        
        async with self._processing_lock:
            if msg_id in self.processed_ids:
                return
            self.processed_ids.add(msg_id)
        
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
        
        if not guild:
            logger.error(f"Guild {self.config.guild_id} not found")
            return
        
        # Only create web channels if category ID is set
        if self.config.ticket_category_id:
            category = self.bot.get_channel(self.config.ticket_category_id)
            if not category:
                logger.error(f"Category {self.config.ticket_category_id} not found")
                return
            
            user_name = msg.get('user', 'unknown')
            channel_name = f"web-{user_name.lower().replace(' ', '-')}"
            
            if channel_name not in self._web_channel_locks:
                self._web_channel_locks[channel_name] = asyncio.Lock()
            
            async with self._web_channel_locks[channel_name]:
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
                logger.info(f"📨 Web message from {user_name}")
            except Exception as e:
                logger.error(f"Failed to send message: {e}")
        else:
            logger.info(f"Web message from {msg.get('user')}: {msg.get('text', '')[:50]} (no category set)")
    
    async def _create_web_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        channel_name: str,
        user_name: str
    ) -> Optional[discord.TextChannel]:
        """Create web chat channel"""
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
            await channel.send(f"🚀 **Web Chat Session Started:** `{user_name}`")
            logger.info(f"✅ Web channel created: {channel_name}")
            return channel
        except Exception as e:
            logger.error(f"Failed to create channel: {e}")
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
            logger.debug(f"📤 Sent message type: {message.get('type')}")
            return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False
    
    @staticmethod
    def _generate_message_id(message: Dict[str, Any]) -> str:
        """Generate unique message ID"""
        text_hash = hashlib.md5(message.get('text', '').encode()).hexdigest()[:8]
        return f"msg_{int(time.time() * 1000)}_{text_hash}"
    
    async def close(self) -> None:
        """Close WebSocket"""
        self.is_connected = False
        
        if self._listen_task:
            self._listen_task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass

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
        
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self) -> None:
        """Initialize bot"""
        logger.info("🚀 Bot setup starting...")
        
        await self.ws_manager.connect()
        self.rate_limiter.start_cleanup()
        
        self.tree.copy_global_to(guild=discord.Object(id=self.config.guild_id))
        await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
        
        self.cache_cleanup_task.start()
        self.status_update_task.start()
        self.ticket_status_task.start()
        
        logger.info("✅ Bot setup completed")
    
    async def close(self) -> None:
        """Cleanup on shutdown"""
        logger.info("🛑 Bot shutting down...")
        
        if self.cache_cleanup_task.is_running():
            self.cache_cleanup_task.cancel()
        if self.status_update_task.is_running():
            self.status_update_task.cancel()
        if self.ticket_status_task.is_running():
            self.ticket_status_task.cancel()
        
        await self.ws_manager.close()
        self.rate_limiter.stop()
        
        await super().close()
        logger.info("✅ Bot shutdown complete")
    
    @tasks.loop(hours=6)
    async def cache_cleanup_task(self) -> None:
        """Periodic cleanup"""
        logger.info("🧹 Running cache cleanup")
        self.rate_limiter.clear_all()
    
    @tasks.loop(minutes=30)
    async def status_update_task(self) -> None:
        """Update bot status"""
        try:
            guild_count = len(self.guilds)
            ticket_count = len(await self.ticket_manager.get_all_open())
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.competing,
                    name=f"{ticket_count} tickets | {guild_count} servers | /help"
                )
            )
        except Exception as e:
            logger.error(f"Failed to update status: {e}")
    
    @tasks.loop(minutes=5)
    async def ticket_status_task(self) -> None:
        """Log ticket statistics"""
        try:
            stats = await self.ticket_manager.get_stats()
            logger.info(f"📊 Tickets - Open: {stats['open']}, Claimed: {stats['claimed']}, Locking: {stats['locking']}")
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
    
    @cache_cleanup_task.before_loop
    @status_update_task.before_loop
    @ticket_status_task.before_loop
    async def before_loop(self) -> None:
        """Wait for bot to be ready"""
        await self.wait_until_ready()
    
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Handle command errors"""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏰ Try again in {error.retry_after:.1f}s", delete_after=5)
        else:
            logger.error(f"Command error: {error}", exc_info=True)

# --- PERMISSION CHECKS ---

def is_staff() -> app_commands.check:
    """Check if staff"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.client.config.owner_id:
            return True
        if not interaction.client.config.staff_role_id:
            return False
        user_roles = {role.id for role in interaction.user.roles}
        return bool(user_roles & {interaction.client.config.staff_role_id, interaction.client.config.manager_role_id or 0})
    return app_commands.check(predicate)

def is_manager() -> app_commands.check:
    """Check if manager"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.client.config.owner_id:
            return True
        if not interaction.client.config.manager_role_id:
            return False
        user_roles = {role.id for role in interaction.user.roles}
        return interaction.client.config.manager_role_id in user_roles
    return app_commands.check(predicate)

# --- MAIN ---

async def main() -> None:
    """Main entry point"""
    try:
        config = Config.from_env()
        bot = RawrBot(config)
        
        logger.info("🚀 Starting bot...")
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
