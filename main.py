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
                guild.get_role(STAFF_ROLE_ID): discord.PermissionOverwrite(read_messages=True, send_messages=True) if STAFF_ROLE_ID else None
            }
            # Remove None values
            overwrites = {k: v for k, v in overwrites.items() if v is not None}
            
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
    return web.json_response({
        'status': 'alive',
        'clients': len(sse_clients),
        'messages': len(message_queue),
        'maintenance': is_maintenance_mode
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
        # Start HTTP server
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
        logger.info("✅ Slash commands synced")
        
        # Start status task
        self.status_task.start()
    
    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        await super().close()
    
    @tasks.loop(minutes=30)
    async def status_task(self):
        status_text = f"{len(sse_clients)} online | /help"
        if is_maintenance_mode:
            status_text = f"MAINTENANCE | {status_text}"
        
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.competing,
                name=status_text
            )
        )
    
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

# --- SLASH COMMANDS ---

@bot.tree.command(name="reply", description="Reply to a web chat message")
@is_staff()
async def reply_command(interaction: discord.Interaction, message: str):
    """Reply to a web chat"""
    if is_maintenance_mode:
        await interaction.response.send_message("❌ Bot is in maintenance mode. Try again later.", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    if not interaction.channel or not interaction.channel.name.startswith("web-"):
        await interaction.followup.send("❌ Use this in a web chat channel", ephemeral=True)
        return
    
    username = interaction.channel.name.replace("web-", "").replace("-", " ")
    
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
    embed.set_footer(text=f"Sent by {interaction.user.display_name}")
    
    await interaction.followup.send(embed=embed)
    logger.info(f"Staff {interaction.user.name} replied to {username}")

@bot.tree.command(name="close", description="Close the current ticket channel")
@is_manager()
async def close_command(interaction: discord.Interaction):
    """Close ticket channel"""
    if not interaction.channel:
        await interaction.response.send_message("❌ Not a valid channel", ephemeral=True)
        return
    
    if TICKET_CATEGORY_ID and interaction.channel.category_id != TICKET_CATEGORY_ID:
        await interaction.response.send_message("❌ Not a ticket channel", ephemeral=True)
        return
    
    await interaction.response.send_message("🔒 Closing channel in 5 seconds...")
    await asyncio.sleep(5)
    
    channel_name = interaction.channel.name
    try:
        await interaction.channel.delete()
        logger.info(f"Closed channel: {channel_name} by {interaction.user.name}")
    except Exception as e:
        logger.error(f"Failed to close: {e}")

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
            await asyncio.sleep(0.5)  # Rate limit protection
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
    
    if is_maintenance_mode:
        embed.add_field(name="⚠️ Warning", value="New messages will be queued until maintenance ends", inline=False)
    
    await interaction.response.send_message(embed=embed)
    logger.warning(f"Maintenance mode {status} by {interaction.user.name}: {maintenance_reason}")

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
    
    # Wait a moment to send the message
    await asyncio.sleep(2)
    
    # Restart the bot
    await bot.close()
    os._exit(0)  # Force exit, Railway will auto-restart

@bot.tree.command(name="stats", description="Show bot statistics")
async def stats_command(interaction: discord.Interaction):
    """Show bot stats"""
    uptime = datetime.utcnow() - bot.boot_time
    days = uptime.days
    hours = uptime.seconds // 3600
    minutes = (uptime.seconds % 3600) // 60
    
    embed = discord.Embed(title="📊 Bot Statistics", color=0xef4444)
    embed.add_field(name="⏰ Uptime", value=f"{days}d {hours}h {minutes}m", inline=True)
    embed.add_field(name="⚡ Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🌐 Web Clients", value=str(len(sse_clients)), inline=True)
    embed.add_field(name="💬 Cached Messages", value=str(len(message_queue)), inline=True)
    embed.add_field(name="📁 Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="🛠️ Maintenance", value="Enabled" if is_maintenance_mode else "Disabled", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Check bot latency")
async def ping_command(interaction: discord.Interaction):
    """Check bot latency"""
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
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="commands", description="List all available commands")
async def commands_list(interaction: discord.Interaction):
    """List all commands"""
    embed = discord.Embed(title="📋 Bot Commands", color=0xef4444)
    embed.add_field(name="User Commands", value="/stats, /ping, /commands", inline=False)
    embed.add_field(name="Staff Commands", value="/reply <message> - Reply to web chat", inline=False)
    embed.add_field(name="Manager Commands", value="/close - Close current ticket", inline=False)
    embed.add_field(name="Owner Commands", value="/restart - Restart bot\n/maintenance [reason] - Toggle maintenance\n/purge - Close all tickets\n/clear_cache - Clear rate limits", inline=False)
    
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
    
    # List all synced commands
    guild = discord.Object(id=GUILD_ID)
    commands = await bot.tree.fetch_commands(guild=guild)
    logger.info(f"✅ Synced {len(commands)} slash commands:")
    for cmd in commands:
        logger.info(f"   /{cmd.name}")

@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages"""
    if message.author == bot.user:
        return
    
    # Handle DMs
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm(message)
        return
    
    await bot.process_commands(message)

async def handle_dm(message: discord.Message):
    """Handle DM messages and forward to support channels"""
    global is_maintenance_mode
    
    # Check maintenance mode
    if is_maintenance_mode:
        await message.author.send(f"🛠️ **Bot is in maintenance mode**\nReason: {maintenance_reason}\nPlease try again later.")
        return
    
    # Rate limit check
    can_send, error = rate_limiter.can_send(message.author.id, message.content)
    if not can_send:
        await message.author.send(f"❌ {error}")
        return
    
    rate_limiter.record(message.author.id)
    
    # Forward to support channel
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await message.author.send("❌ Support system unavailable. Please try again later.")
        logger.error(f"Guild {GUILD_ID} not found for DM from {message.author.name}")
        return
    
    # Find or create support channel
    support_channel_name = "📩-support-tickets"
    support_channel = discord.utils.get(guild.text_channels, name=support_channel_name)
    
    if not support_channel:
        # Try to find any channel named support or create one
        support_channel = discord.utils.get(guild.text_channels, name="support")
        
        if not support_channel:
            try:
                # Find a suitable category (usually the first text channel's category)
                category = None
                for channel in guild.text_channels:
                    if channel.category:
                        category = channel.category
                        break
                
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
                
                # Add staff roles if configured
                if STAFF_ROLE_ID:
                    staff_role = guild.get_role(STAFF_ROLE_ID)
                    if staff_role:
                        overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                
                if MANAGER_ROLE_ID:
                    manager_role = guild.get_role(MANAGER_ROLE_ID)
                    if manager_role:
                        overwrites[manager_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                
                support_channel = await guild.create_text_channel(
                    "support-tickets",
                    category=category,
                    overwrites=overwrites,
                    reason="Auto-created for DM support"
                )
                logger.info(f"Created support channel: support-tickets")
            except Exception as e:
                logger.error(f"Failed to create support channel: {e}")
                await message.author.send("❌ Support system unavailable. Please try again later.")
                return
    
    # Create an embed for the support message
    embed = discord.Embed(
        title="📬 New DM from User",
        description=message.content,
        color=0xef4444,
        timestamp=datetime.utcnow()
    )
    embed.set_author(name=message.author.name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="User ID", value=str(message.author.id), inline=True)
    embed.add_field(name="User Mention", value=message.author.mention, inline=True)
    embed.add_field(name="Account Created", value=message.author.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.set_footer(text=f"Reply with /reply @{message.author.name} <message>")
    
    # Add a claim button
    class ClaimButton(discord.ui.View):
        def __init__(self, user_id: int, user_name: str):
            super().__init__(timeout=None)
            self.user_id = user_id
            self.user_name = user_name
            self.claimed = False
        
        @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.success, emoji="✋")
        async def claim_button(self, interaction_button: discord.Interaction, button: discord.ui.Button):
            if self.claimed:
                await interaction_button.response.send_message("❌ This ticket has already been claimed", ephemeral=True)
                return
            
            self.claimed = True
            button.disabled = True
            await interaction_button.message.edit(view=self)
            
            # Send confirmation to staff
            await interaction_button.response.send_message(f"✅ You have claimed this ticket! Respond to the user via DM or in this channel.", ephemeral=True)
            
            # Try to DM the user
            try:
                user = await bot.fetch_user(self.user_id)
                await user.send(f"✅ **Support Ticket Claimed**\nA staff member ({interaction_button.user.name}) has claimed your ticket. They will respond shortly.")
            except:
                pass
            
            # Update the embed
            new_embed = interaction_button.message.embeds[0]
            new_embed.add_field(name="Claimed By", value=f"{interaction_button.user.mention} ({interaction_button.user.name})", inline=False)
            await interaction_button.message.edit(embed=new_embed)
    
    try:
        await support_channel.send(embed=embed, view=ClaimButton(message.author.id, message.author.name))
        await message.author.send("✅ **Message sent to support!** Staff will respond shortly. You'll receive a DM when someone claims your ticket.")
        logger.info(f"DM from {message.author.name} forwarded to {support_channel.name}")
    except Exception as e:
        logger.error(f"Failed to forward DM: {e}")
        await message.author.send("❌ Failed to send message to support. Please try again later.")

# --- MAIN ---
if __name__ == "__main__":
    try:
        logger.info(f"🚀 Starting bot on port {PORT}...")
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Invalid bot token")
    except Exception as e:
        logger.error(f"❌ Failed to start: {e}")
