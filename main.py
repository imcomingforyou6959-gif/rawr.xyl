import os
import discord
import asyncio
import logging
import json
import time
import hashlib
from typing import Optional, Set, Dict, Any
from datetime import datetime, timedelta
from discord import app_commands
from discord.ext import commands, tasks
import websockets
from collections import deque

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('RawrBot')

# --- CONFIGURATION FROM ENVIRONMENT ---
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

GUILD_ID_INT = int(os.getenv('GUILD_ID', '0'))
if not GUILD_ID_INT:
    raise ValueError("GUILD_ID environment variable not set")

GUILD_ID = discord.Object(id=GUILD_ID_INT)

# Discord Configuration
TICKET_CATEGORY_ID = int(os.getenv('TICKET_CATEGORY_ID', '1490508234526556321'))
OWNER_ID = int(os.getenv('OWNER_ID', '1071330258172780594'))
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID', '1489713077963456564'))
MANAGER_ROLE_ID = int(os.getenv('MANAGER_ROLE_ID', '1489435265914109972'))

# WebSocket Configuration
WEBSOCKET_URL = os.getenv('WEBSOCKET_URL', 'ws://localhost:8765')
WEBSITE_URL = os.getenv('WEBSITE_URL', 'https://rawrs.zapto.org/')

# Rate Limiting Configuration
RATE_LIMIT_SECONDS = 5  # One message per 5 seconds
MAX_MESSAGES_PER_MINUTE = 12  # Maximum 12 messages per minute
MESSAGE_CACHE_SIZE = 100  # Store last 100 messages for duplicate detection
DUPLICATE_WINDOW_SECONDS = 30  # Check for duplicates within 30 seconds

class RateLimiter:
    """Rate limiting manager to prevent message spam"""
    
    def __init__(self):
        self.user_messages: Dict[int, deque] = {}  # User ID -> deque of timestamps
        self.message_hashes: Dict[str, float] = {}  # Message hash -> timestamp
        self.cleanup_task = None
    
    def start_cleanup(self):
        """Start periodic cleanup of old rate limit data"""
        async def cleanup():
            while True:
                await asyncio.sleep(60)  # Clean every minute
                self.cleanup_old_data()
        
        asyncio.create_task(cleanup())
    
    def cleanup_old_data(self):
        """Remove old rate limit entries"""
        current_time = time.time()
        
        # Clean user message history
        for user_id in list(self.user_messages.keys()):
            # Remove timestamps older than 1 minute
            while self.user_messages[user_id] and current_time - self.user_messages[user_id][0] > 60:
                self.user_messages[user_id].popleft()
            
            # Remove user if no messages
            if not self.user_messages[user_id]:
                del self.user_messages[user_id]
        
        # Clean message hashes older than DUPLICATE_WINDOW_SECONDS
        for msg_hash in list(self.message_hashes.keys()):
            if current_time - self.message_hashes[msg_hash] > DUPLICATE_WINDOW_SECONDS:
                del self.message_hashes[msg_hash]
    
    def can_send(self, user_id: int, message_content: str) -> tuple[bool, str]:
        """Check if user can send a message"""
        current_time = time.time()
        
        # Check rate limit per user
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque()
        
        # Check messages per minute
        if len(self.user_messages[user_id]) >= MAX_MESSAGES_PER_MINUTE:
            oldest = self.user_messages[user_id][0]
            if current_time - oldest < 60:
                return False, f"Rate limit exceeded. Maximum {MAX_MESSAGES_PER_MINUTE} messages per minute."
        
        # Check cooldown between messages
        if self.user_messages[user_id]:
            last_message = self.user_messages[user_id][-1]
            if current_time - last_message < RATE_LIMIT_SECONDS:
                wait_time = RATE_LIMIT_SECONDS - (current_time - last_message)
                return False, f"Please wait {wait_time:.1f} seconds between messages."
        
        # Check for duplicate message content
        message_hash = hashlib.md5(f"{user_id}:{message_content}".encode()).hexdigest()
        if message_hash in self.message_hashes:
            if current_time - self.message_hashes[message_hash] < DUPLICATE_WINDOW_SECONDS:
                return False, "Duplicate message detected. Please don't send the same message multiple times."
        
        # Allow the message
        self.user_messages[user_id].append(current_time)
        self.message_hashes[message_hash] = current_time
        
        return True, "OK"
    
    def clear_user_cache(self, user_id: int):
        """Clear rate limit cache for a specific user"""
        if user_id in self.user_messages:
            del self.user_messages[user_id]
        logger.info(f"Cleared rate limit cache for user {user_id}")
    
    def clear_all_cache(self):
        """Clear all rate limit caches"""
        self.user_messages.clear()
        self.message_hashes.clear()
        logger.info("Cleared all rate limit caches")

class WebSocketManager:
    """Manages WebSocket connection to the chat server"""
    
    def __init__(self, bot):
        self.bot = bot
        self.websocket = None
        self.reconnect_task = None
        self.processed_ids: Set[str] = set()
        self.last_heartbeat = time.time()
        self.is_connected = False
        
    async def connect(self):
        """Connect to WebSocket server with retry logic"""
        retry_count = 0
        max_retries = 5
        retry_delay = 5
        
        while retry_count < max_retries:
            try:
                self.websocket = await websockets.connect(
                    WEBSOCKET_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                )
                self.is_connected = True
                self.last_heartbeat = time.time()
                logger.info(f"Connected to WebSocket server at {WEBSOCKET_URL}")
                
                # Start listening for messages
                asyncio.create_task(self.listen())
                asyncio.create_task(self.heartbeat())
                
                return True
                
            except Exception as e:
                retry_count += 1
                logger.error(f"Failed to connect to WebSocket (attempt {retry_count}/{max_retries}): {e}")
                
                if retry_count < max_retries:
                    await asyncio.sleep(retry_delay * retry_count)
                else:
                    logger.error("Max retries reached. WebSocket connection failed.")
                    return False
    
    async def heartbeat(self):
        """Send heartbeat to keep connection alive"""
        while self.is_connected and self.websocket:
            try:
                await asyncio.sleep(30)
                if time.time() - self.last_heartbeat > 45:
                    logger.warning("No heartbeat received, reconnecting...")
                    await self.reconnect()
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
    
    async def listen(self):
        """Listen for incoming WebSocket messages"""
        try:
            async for message in self.websocket:
                self.last_heartbeat = time.time()
                data = json.loads(message)
                await self.process_message(data)
                
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket connection closed: {e}")
            await self.reconnect()
        except Exception as e:
            logger.error(f"WebSocket listen error: {e}")
            await self.reconnect()
    
    async def reconnect(self):
        """Reconnect to WebSocket server"""
        self.is_connected = False
        if self.websocket:
            try:
                await self.websocket.close()
            except:
                pass
        
        await asyncio.sleep(5)
        await self.connect()
    
    async def process_message(self, data: Dict[str, Any]):
        """Process incoming WebSocket messages"""
        msg_id = data.get('id')
        
        # Skip if already processed
        if msg_id in self.processed_ids:
            logger.debug(f"Skipping duplicate message: {msg_id}")
            return
        
        # Add to processed IDs
        self.processed_ids.add(msg_id)
        
        # Keep only last 1000 IDs
        if len(self.processed_ids) > 1000:
            self.processed_ids = set(list(self.processed_ids)[-500:])
        
        # Process based on message type
        if data.get('origin') == 'web' and data.get('type') == 'chat':
            await self.process_web_message(data)
        elif data.get('type') == 'reply':
            logger.info(f"Received reply from staff: {data.get('user')}")
    
    async def process_web_message(self, msg: Dict[str, Any]):
        """Process message from website and forward to Discord"""
        guild = self.bot.get_guild(GUILD_ID_INT)
        category = self.bot.get_channel(TICKET_CATEGORY_ID)
        
        if not guild or not category:
            logger.error(f"Missing guild or category")
            return
        
        # Create or get channel for this user
        user_id = msg['user'].lower().replace(" ", "-")
        channel_name = f"web-{user_id}"
        
        channel = discord.utils.get(category.text_channels, name=channel_name)
        
        if not channel:
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
                channel = await guild.create_text_channel(
                    channel_name, 
                    category=category, 
                    overwrites=overwrites,
                    reason=f"Web chat started by {msg['user']}"
                )
                await channel.send(f"🚀 **Chat Session Started:** `{msg['user']}`\nUse `/reply` to respond.")
                logger.info(f"Created new web channel: {channel_name}")
            except Exception as e:
                logger.error(f"Failed to create channel: {e}")
                return
        
        # Send message to Discord
        embed = discord.Embed(
            description=msg['text'],
            color=0xef4444,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"🌐 Web: {msg['user']}")
        embed.set_footer(text=f"ID: {msg.get('id', 'unknown')[:8]}")
        
        try:
            await channel.send(embed=embed)
            logger.info(f"Forwarded web message from {msg['user']} to {channel_name}")
        except Exception as e:
            logger.error(f"Failed to send message to Discord: {e}")
    
    async def send_message(self, message: Dict[str, Any]) -> bool:
        """Send message through WebSocket"""
        if not self.is_connected or not self.websocket:
            logger.error("WebSocket not connected")
            return False
        
        try:
            # Add unique ID to prevent duplicates
            message['id'] = f"msg_{int(time.time() * 1000)}_{hashlib.md5(message.get('text', '').encode()).hexdigest()[:8]}"
            message['timestamp'] = datetime.utcnow().isoformat()
            
            await self.websocket.send(json.dumps(message))
            logger.info(f"Sent message to WebSocket: {message.get('type', 'unknown')}")
            return True
        except Exception as e:
            logger.error(f"Failed to send WebSocket message: {e}")
            return False

class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        self.boot_time = datetime.utcnow()
        self.ws_manager = WebSocketManager(self)
        self.rate_limiter = RateLimiter()
        self.command_cooldown = commands.CooldownMapping.from_cooldown(1, 5, commands.BucketType.user)
        
        super().__init__(command_prefix="!", intents=intents)
    
    async def setup_hook(self):
        """Setup the bot before running"""
        # Connect to WebSocket
        await self.ws_manager.connect()
        
        # Start rate limiter cleanup
        self.rate_limiter.start_cleanup()
        
        # Sync slash commands
        self.tree.copy_global_to(guild=GUILD_ID)
        await self.tree.sync(guild=GUILD_ID)
        
        # Start background tasks
        self.clear_cache_task.start()
        self.status_update_task.start()
        
        logger.info("Bot setup completed")
    
    async def close(self):
        """Cleanup when bot closes"""
        if self.ws_manager.websocket:
            await self.ws_manager.websocket.close()
        
        self.clear_cache_task.cancel()
        self.status_update_task.cancel()
        
        await super().close()
        logger.info("Bot closed successfully")
    
    @tasks.loop(hours=6)
    async def clear_cache_task(self):
        """Periodically clear rate limit cache"""
        self.rate_limiter.clear_all_cache()
        logger.info("Rate limit cache cleared")
    
    @tasks.loop(minutes=30)
    async def status_update_task(self):
        """Update bot status periodically"""
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.competing,
                name=f"{len(self.guilds)} servers | /help"
            )
        )
    
    async def on_command_error(self, ctx, error):
        """Handle command errors"""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏰ Command on cooldown. Try again in {error.retry_after:.1f} seconds.", delete_after=5)

# Create bot instance
bot = RawrBot()

# --- PERMISSION CHECKERS ---

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

# --- EVENT HANDLERS ---

@bot.event
async def on_ready():
    """Called when bot is ready"""
    logger.info(f"Logged in as: {bot.user.name} (ID: {bot.user.id})")
    logger.info(f"Connected to {len(bot.guilds)} guilds")
    
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.competing,
            name="rawrs.zapto.org"
        )
    )

@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages"""
    if message.author == bot.user:
        return
    
    # Handle DM messages
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_message(message)
        return
    
    await bot.process_commands(message)

async def handle_dm_message(message: discord.Message):
    """Handle DM messages and forward to WebSocket"""
    # Rate limit check
    can_send, error_msg = bot.rate_limiter.can_send(message.author.id, message.content)
    if not can_send:
        await message.author.send(f"❌ {error_msg}")
        return
    
    guild = bot.get_guild(GUILD_ID_INT)
    category = bot.get_channel(TICKET_CATEGORY_ID)
    
    if not guild or not category:
        await message.author.send("❌ Support system unavailable. Please try again later.")
        return
    
    # Send to WebSocket
    ws_message = {
        "type": "dm",
        "user": message.author.name,
        "user_id": message.author.id,
        "text": message.content,
        "origin": "discord",
        "channel": "dm"
    }
    
    success = await bot.ws_manager.send_message(ws_message)
    
    if success:
        await message.author.send("✅ Message sent to support staff. They will reply shortly.")
        logger.info(f"DM from {message.author.name} forwarded to WebSocket")
    else:
        await message.author.send("❌ Failed to send message. Please try again.")

# --- SLASH COMMANDS ---

@bot.tree.command(name="reply", description="Reply to a web chat message")
@is_staff()
async def reply(interaction: discord.Interaction, content: str):
    """Reply to a web chat message"""
    # Rate limit check
    can_send, error_msg = bot.rate_limiter.can_send(interaction.user.id, content)
    if not can_send:
        await interaction.response.send_message(f"❌ {error_msg}", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    # Check if in web channel
    if not interaction.channel.name.startswith("web-"):
        await interaction.followup.send("❌ This command can only be used in web chat channels.")
        return
    
    # Extract username from channel name
    username = interaction.channel.name.replace("web-", "").replace("-", " ")
    
    # Send reply through WebSocket
    message = {
        "type": "reply",
        "user": interaction.user.display_name,
        "user_id": interaction.user.id,
        "text": content,
        "origin": "discord",
        "target_user": username,
        "channel_id": interaction.channel.id
    }
    
    success = await bot.ws_manager.send_message(message)
    
    if success:
        embed = discord.Embed(
            description=content,
            color=0x00ff00,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"💬 Staff Reply to {username}")
        embed.set_footer(text=f"Sent by {interaction.user.display_name}")
        
        await interaction.followup.send(embed=embed)
        logger.info(f"Staff {interaction.user.name} replied to {username}")
    else:
        await interaction.followup.send("❌ Failed to send reply. WebSocket connection issue.")

@bot.tree.command(name="close", description="Close and delete the current ticket channel")
@is_manager()
async def close(interaction: discord.Interaction):
    """Close and delete the current ticket channel"""
    if interaction.channel.category_id != TICKET_CATEGORY_ID:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return
    
    await interaction.response.send_message("🔒 Closing channel in 5 seconds...")
    await asyncio.sleep(5)
    
    channel_name = interaction.channel.name
    
    try:
        await interaction.channel.delete()
        logger.info(f"Channel closed: {channel_name} by {interaction.user.name}")
    except Exception as e:
        logger.error(f"Failed to delete channel: {e}")
        await interaction.followup.send("❌ Failed to close channel.")

@bot.tree.command(name="clear_cache", description="Clear rate limit cache for a user")
@is_manager()
async def clear_cache(interaction: discord.Interaction, user_id: Optional[int] = None):
    """Clear rate limit cache for a specific user or all users"""
    if user_id:
        bot.rate_limiter.clear_user_cache(user_id)
        await interaction.response.send_message(f"✅ Cleared rate limit cache for user {user_id}", ephemeral=True)
    else:
        bot.rate_limiter.clear_all_cache()
        await interaction.response.send_message("✅ Cleared all rate limit caches", ephemeral=True)

@bot.tree.command(name="stats", description="Show bot statistics")
@is_staff()
async def stats(interaction: discord.Interaction):
    """Display bot statistics"""
    uptime = datetime.utcnow() - bot.boot_time
    days = uptime.days
    hours = uptime.seconds // 3600
    minutes = (uptime.seconds % 3600) // 60
    
    embed = discord.Embed(title="📊 Bot Statistics", color=0xef4444)
    embed.add_field(name="⏰ Uptime", value=f"{days}d {hours}h {minutes}m", inline=True)
    embed.add_field(name="⚡ Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🌐 Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="🔌 WebSocket", value="Connected" if bot.ws_manager.is_connected else "Disconnected", inline=True)
    embed.add_field(name="💬 Cached Msgs", value=str(len(bot.ws_manager.processed_ids)), inline=True)
    embed.add_field(name="🚦 Rate Limited Users", value=str(len(bot.rate_limiter.user_messages)), inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    """Check bot response time"""
    latency = round(bot.latency * 1000)
    
    if latency < 100:
        color = 0x00ff00
        status = "🟢 Excellent"
    elif latency < 200:
        color = 0xffaa00
        status = "🟡 Good"
    else:
        color = 0xef4444
        status = "🔴 Poor"
    
    embed = discord.Embed(title="🏓 Pong!", color=color)
    embed.add_field(name="Latency", value=f"{latency}ms", inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="WebSocket", value="✅ Connected" if bot.ws_manager.is_connected else "❌ Disconnected", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="website", description="Get the official website link")
async def website(interaction: discord.Interaction):
    """Get the website link"""
    embed = discord.Embed(
        title="🌐 Rawr.xyz",
        description=f"**Website:** {WEBSITE_URL}\n**Support:** Live chat available",
        color=0xef4444
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- ERROR HANDLING ---

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handle command errors"""
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "⛔ **Access Denied:** Staff or Manager role required.", 
            ephemeral=True
        )
    elif isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏰ **Cooldown:** Try again in {error.retry_after:.1f} seconds.", 
            ephemeral=True
        )
    else:
        logger.error(f"Command error: {error}")
        await interaction.response.send_message(
            "❌ An unexpected error occurred.", 
            ephemeral=True
        )

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    try:
        logger.info("Starting Rawr.xyz Discord Bot...")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
