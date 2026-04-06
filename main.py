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

PORT = int(os.getenv('PORT', 8080))  # Railway provides this
TICKET_CATEGORY_ID = int(os.getenv('TICKET_CATEGORY_ID', '0'))
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID', '0'))
MANAGER_ROLE_ID = int(os.getenv('MANAGER_ROLE_ID', '0'))

# Rate limiting
RATE_LIMIT_SECONDS = 5
MAX_MESSAGES_PER_MINUTE = 12

# --- MESSAGE STORAGE ---
message_queue = deque(maxlen=100)  # Store last 100 messages
sse_clients = set()  # Connected SSE clients
processed_ids = set()  # Prevent duplicates

class RateLimiter:
    def __init__(self):
        self.user_messages: Dict[int, deque] = {}
    
    def can_send(self, user_id: int, content: str) -> Tuple[bool, str]:
        current_time = time.time()
        
        if user_id not in self.user_messages:
            self.user_messages[user_id] = deque(maxlen=MAX_MESSAGES_PER_MINUTE)
        
        user_deque = self.user_messages[user_id]
        
        # Check rate limit
        if len(user_deque) >= MAX_MESSAGES_PER_MINUTE:
            oldest = user_deque[0]
            if current_time - oldest < 60:
                return False, f"Rate limited. Try again in {60 - (current_time - oldest):.0f}s"
        
        # Check cooldown
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
    """Broadcast message to all connected website clients"""
    if not sse_clients:
        return
    
    message['id'] = f"msg_{int(time.time() * 1000)}_{hashlib.md5(message.get('text', '').encode()).hexdigest()[:6]}"
    message['timestamp'] = datetime.utcnow().isoformat()
    
    # Add to queue
    message_queue.append(message)
    
    # Broadcast to all connected SSE clients
    data = f"data: {json.dumps(message)}\n\n"
    dead_clients = set()
    
    for client in sse_clients:
        try:
            await client.write(data.encode())
        except Exception:
            dead_clients.add(client)
    
    # Remove dead clients
    for client in dead_clients:
        sse_clients.discard(client)

async def broadcast_to_discord(channel, embed):
    """Send message to Discord channel"""
    try:
        await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Failed to send to Discord: {e}")

# --- SSE HTTP HANDLERS ---

async def sse_endpoint(request):
    """Server-Sent Events endpoint - website connects here"""
    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Access-Control-Allow-Origin': '*',
        'Connection': 'keep-alive'
    }
    
    response = web.StreamResponse(headers=headers)
    await response.prepare(request)
    
    # Add client to connected set
    sse_clients.add(response)
    logger.info(f"SSE client connected. Total: {len(sse_clients)}")
    
    # Send last 10 messages on connect
    for msg in list(message_queue)[-10:]:
        await response.write(f"data: {json.dumps(msg)}\n\n".encode())
    
    try:
        # Keep connection alive
        while True:
            await asyncio.sleep(30)
            await response.write(b': heartbeat\n\n')
    except Exception as e:
        logger.debug(f"SSE client disconnected: {e}")
    finally:
        sse_clients.discard(response)
        logger.info(f"SSE client disconnected. Total: {len(sse_clients)}")
    
    return response

async def send_message_endpoint(request):
    """Website sends message here"""
    try:
        data = await request.json()
        user_id_hash = hash(data.get('user', 'unknown'))
        
        # Rate limit check
        can_send, error = rate_limiter.can_send(user_id_hash, data.get('text', ''))
        if not can_send:
            return web.json_response({'error': error}, status=429)
        
        rate_limiter.record(user_id_hash)
        
        message = {
            'type': 'chat',
            'user': data.get('user', 'Guest'),
            'text': data.get('text', ''),
            'origin': 'web',
            'timestamp': datetime.utcnow().isoformat()
        }
        
        # Broadcast to all connected clients (including Discord bot)
        await broadcast_to_web(message)
        
        # Forward to Discord
        await forward_to_discord(message)
        
        return web.json_response({'status': 'ok', 'message': 'Message sent'})
    
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return web.json_response({'error': str(e)}, status=500)

async def health_check(request):
    """Health check endpoint for Railway"""
    return web.json_response({
        'status': 'alive',
        'clients': len(sse_clients),
        'messages': len(message_queue),
        'uptime': str(datetime.utcnow() - bot.boot_time) if hasattr(bot, 'boot_time') else 'unknown'
    })

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
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Web chat from {message['user']}"
            )
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
        """Start HTTP server for SSE"""
        self.web_app = web.Application()
        self.web_app.router.add_get('/events', sse_endpoint)
        self.web_app.router.add_post('/send', send_message_endpoint)
        self.web_app.router.add_get('/health', health_check)
        
        self.runner = web.AppRunner(self.web_app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', PORT)
        await site.start()
        
        logger.info(f"✅ SSE server running on port {PORT}")
        logger.info(f"   Events: http://localhost:{PORT}/events")
        logger.info(f"   Send: http://localhost:{PORT}/send")
        logger.info(f"   Health: http://localhost:{PORT}/health")
        
        # Sync slash commands
        self.tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
        await self.tree.sync(guild=discord.Object(id=GUILD_ID))
        
        self.status_task.start()
    
    async def close(self):
        """Cleanup"""
        if self.runner:
            await self.runner.cleanup()
        await super().close()
    
    @tasks.loop(minutes=30)
    async def status_task(self):
        """Update bot status"""
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.competing,
                name=f"{len(sse_clients)} online | /help"
            )
        )

bot = RawrBot()

# --- DISCORD COMMANDS ---

@bot.tree.command(name="reply", description="Reply to web chat")
@app_commands.default_permissions()
async def reply(interaction: discord.Interaction, message: str):
    """Reply to a web chat message"""
    if not interaction.channel or not interaction.channel.name.startswith("web-"):
        await interaction.response.send_message("❌ Use this in a web chat channel", ephemeral=True)
        return
    
    username = interaction.channel.name.replace("web-", "").replace("-", " ")
    
    # Send reply via SSE
    reply_msg = {
        'type': 'reply',
        'user': interaction.user.display_name,
        'text': message,
        'origin': 'discord',
        'target': username
    }
    
    await broadcast_to_web(reply_msg)
    
    embed = discord.Embed(
        description=message,
        color=0x00ff00,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=f"💬 Staff Reply to {username}")
    
    await interaction.response.send_message(embed=embed)
    logger.info(f"Staff {interaction.user.name} replied to {username}")

@bot.tree.command(name="close", description="Close ticket channel")
async def close_ticket(interaction: discord.Interaction):
    """Close the current ticket channel"""
    if not interaction.channel or interaction.channel.category_id != TICKET_CATEGORY_ID:
        await interaction.response.send_message("❌ Not a ticket channel", ephemeral=True)
        return
    
    await interaction.response.send_message("🔒 Closing in 5 seconds...")
    await asyncio.sleep(5)
    
    try:
        await interaction.channel.delete()
        logger.info(f"Closed channel: {interaction.channel.name}")
    except Exception as e:
        logger.error(f"Failed to close: {e}")

@bot.tree.command(name="stats", description="Bot statistics")
async def stats(interaction: discord.Interaction):
    """Show bot stats"""
    uptime = datetime.utcnow() - bot.boot_time
    days = uptime.days
    hours = uptime.seconds // 3600
    minutes = (uptime.seconds % 3600) // 60
    
    embed = discord.Embed(title="📊 Bot Statistics", color=0xef4444)
    embed.add_field(name="⏰ Uptime", value=f"{days}d {hours}h {minutes}m", inline=True)
    embed.add_field(name="⚡ Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🌐 Web Clients", value=str(len(sse_clients)), inline=True)
    embed.add_field(name="💬 Messages", value=str(len(message_queue)), inline=True)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ping", description="Check latency")
async def ping(interaction: discord.Interaction):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! `{latency}ms`", ephemeral=True)

# --- DISCORD EVENTS ---

@bot.event
async def on_ready():
    """Bot is ready"""
    logger.info(f"✅ Logged in as: {bot.user.name} ({bot.user.id})")
    logger.info(f"✅ Connected to {len(bot.guilds)} guilds")
    logger.info(f"✅ SSE endpoint: http://localhost:{PORT}/events")

@bot.event
async def on_message(message):
    """Handle incoming messages"""
    if message.author == bot.user:
        return
    
    # Handle DMs
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm(message)
        return
    
    await bot.process_commands(message)

async def handle_dm(message):
    """Handle DM messages"""
    can_send, error = rate_limiter.can_send(message.author.id, message.content)
    if not can_send:
        await message.author.send(f"❌ {error}")
        return
    
    rate_limiter.record(message.author.id)
    
    # Forward to web clients via SSE
    dm_msg = {
        'type': 'dm',
        'user': message.author.name,
        'text': message.content,
        'origin': 'discord',
        'is_dm': True
    }
    
    await broadcast_to_web(dm_msg)
    await message.author.send("✅ Message sent to support!")
    logger.info(f"DM from {message.author.name}: {message.content[:50]}")

# --- MAIN ---

if __name__ == "__main__":
    try:
        logger.info(f"🚀 Starting bot on port {PORT}...")
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Failed to start: {e}")
