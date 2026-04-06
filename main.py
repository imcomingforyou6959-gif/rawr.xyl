import os
import aiohttp
import discord
import asyncio
import time
import logging
import json
from typing import Optional, Dict, Set, Any
from datetime import datetime
from discord import app_commands
from discord.ext import commands, tasks

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('RawrBot')

# --- CONFIGURATION FROM GITHUB SECRETS ---
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

GUILD_ID_INT = int(os.getenv('GUILD_ID', '0'))
if not GUILD_ID_INT:
    raise ValueError("GUILD_ID environment variable not set")

GUILD_ID = discord.Object(id=GUILD_ID_INT)

# Optional: Get category ID from secrets or use default
TICKET_CATEGORY_ID = int(os.getenv('TICKET_CATEGORY_ID', '1490508234526556321'))

# Permissions & IDs (can also be moved to secrets if needed)
OWNER_ID = int(os.getenv('OWNER_ID', '1071330258172780594'))
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID', '1489713077963456564'))
MANAGER_ROLE_ID = int(os.getenv('MANAGER_ROLE_ID', '1489435265914109972'))

# URLs (can be configured via secrets)
WEBSITE_URL = os.getenv('WEBSITE_URL', 'https://rawrs.zapto.org/')
CHAT_URL = os.getenv('CHAT_URL', 'https://rawr-chat-default-rtdb.firebaseio.com/messages.json')
STATUS_JSON_URL = os.getenv('STATUS_JSON_URL', 'https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/main/status.json')

# Constants (can be configured via secrets)
MESSAGE_FETCH_INTERVAL = int(os.getenv('MESSAGE_FETCH_INTERVAL', '3'))
CLOSE_DELAY_SECONDS = int(os.getenv('CLOSE_DELAY_SECONDS', '5'))
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
RETRY_DELAY = int(os.getenv('RETRY_DELAY', '1'))
MAX_STORED_IDS = int(os.getenv('MAX_STORED_IDS', '2000'))

# File for persistent storage (will be in /tmp for ephemeral storage on some platforms)
PROCESSED_IDS_FILE = os.getenv('PROCESSED_IDS_FILE', 'processed_msg_ids.json')

class PermissionChecker:
    """Centralized permission checking"""
    
    @staticmethod
    def is_staff():
        """Check if user is Owner, Manager, or Staff"""
        async def predicate(interaction: discord.Interaction) -> bool:
            if interaction.user.id == OWNER_ID:
                return True
            
            user_roles = {role.id for role in interaction.user.roles}
            return STAFF_ROLE_ID in user_roles or MANAGER_ROLE_ID in user_roles
        
        return app_commands.check(predicate)
    
    @staticmethod
    def is_manager():
        """Check if user is Owner or Manager"""
        async def predicate(interaction: discord.Interaction) -> bool:
            if interaction.user.id == OWNER_ID:
                return True
            
            user_roles = {role.id for role in interaction.user.roles}
            return MANAGER_ROLE_ID in user_roles
        
        return app_commands.check(predicate)

class ChannelManager:
    """Handle channel creation and management"""
    
    def __init__(self, bot):
        self.bot = bot
        self.processing_channels: Set[str] = set()
    
    async def create_or_get_ticket_channel(
        self, 
        guild: discord.Guild, 
        category: discord.CategoryChannel, 
        channel_name: str, 
        welcome_message: str
    ) -> Optional[discord.TextChannel]:
        """Create a new ticket channel or get existing one"""
        
        if channel_name in self.processing_channels:
            logger.debug(f"Channel {channel_name} is already being created")
            return None
        
        channel = discord.utils.get(category.text_channels, name=channel_name)
        if channel:
            return channel
        
        self.processing_channels.add(channel_name)
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            
            channel = await guild.create_text_channel(
                channel_name, 
                category=category, 
                overwrites=overwrites,
                reason=f"Ticket created for {channel_name}"
            )
            
            await channel.send(welcome_message)
            logger.info(f"Created new ticket channel: {channel_name}")
            return channel
            
        except discord.Forbidden:
            logger.error(f"Missing permissions to create channel {channel_name}")
        except Exception as e:
            logger.error(f"Failed to create channel {channel_name}: {e}")
        finally:
            self.processing_channels.remove(channel_name)
        
        return None

class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        # Internal Tracking
        self.boot_time = int(time.time() * 1000)
        self.processed_msg_ids: Set[str] = set()
        self.channel_manager = ChannelManager(self)
        self.session: Optional[aiohttp.ClientSession] = None
        
        super().__init__(command_prefix="!", intents=intents)
    
    def load_processed_ids(self):
        """Load previously processed message IDs from file"""
        try:
            if os.path.exists(PROCESSED_IDS_FILE):
                with open(PROCESSED_IDS_FILE, 'r') as f:
                    data = json.load(f)
                    self.processed_msg_ids = set(data.get('processed_ids', []))
                    logger.info(f"Loaded {len(self.processed_msg_ids)} processed message IDs from {PROCESSED_IDS_FILE}")
            else:
                logger.info("No previous processed IDs file found, starting fresh")
                self.processed_msg_ids = set()
        except Exception as e:
            logger.error(f"Failed to load processed IDs: {e}")
            self.processed_msg_ids = set()
    
    def save_processed_ids(self):
        """Save processed message IDs to file"""
        try:
            # Keep only the most recent IDs to prevent file from growing too large
            ids_to_save = list(self.processed_msg_ids)[-MAX_STORED_IDS:]
            data = {
                'processed_ids': ids_to_save,
                'last_updated': time.time(),
                'total_processed': len(self.processed_msg_ids)
            }
            with open(PROCESSED_IDS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Saved {len(ids_to_save)} processed message IDs to {PROCESSED_IDS_FILE}")
        except Exception as e:
            logger.error(f"Failed to save processed IDs: {e}")
    
    async def setup_hook(self):
        """Setup the bot before running"""
        # Load saved processed IDs
        self.load_processed_ids()
        
        # Initialize session
        self.session = aiohttp.ClientSession()
        
        # Sync commands
        self.tree.copy_global_to(guild=GUILD_ID)
        await self.tree.sync(guild=GUILD_ID)
        
        # Start background tasks
        self.check_live_chat.start()
        self.save_processed_ids_task.start()
        self.cleanup_old_ids_task.start()
        self.keep_alive_task.start()  # For 24/7 hosting
        
        logger.info("Bot setup completed")
        logger.info(f"Bot is running in guild: {GUILD_ID_INT}")
    
    async def close(self):
        """Cleanup when bot closes"""
        # Save processed IDs before closing
        self.save_processed_ids()
        
        # Close session
        if self.session:
            await self.session.close()
        
        # Stop tasks
        self.check_live_chat.cancel()
        self.save_processed_ids_task.cancel()
        self.cleanup_old_ids_task.cancel()
        self.keep_alive_task.cancel()
        
        await super().close()
        logger.info("Bot closed successfully")
    
    @tasks.loop(minutes=5)  # Save every 5 minutes
    async def save_processed_ids_task(self):
        """Periodically save processed message IDs"""
        self.save_processed_ids()
    
    @tasks.loop(hours=24)  # Clean up once per day
    async def cleanup_old_ids_task(self):
        """Clean up old processed message IDs to prevent memory bloat"""
        if len(self.processed_msg_ids) > MAX_STORED_IDS:
            old_count = len(self.processed_msg_ids)
            # Keep only the most recent IDs
            self.processed_msg_ids = set(list(self.processed_msg_ids)[-MAX_STORED_IDS:])
            logger.info(f"Cleaned up old processed IDs: {old_count} -> {len(self.processed_msg_ids)}")
            self.save_processed_ids()
    
    @tasks.loop(minutes=30)  # Keep alive for 24/7 hosting
    async def keep_alive_task(self):
        """Keep the bot active on 24/7 hosting platforms"""
        logger.debug("Keep-alive signal sent")
        # Update presence occasionally to show activity
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.competing,
                name=f"{len(self.guilds)} servers | /help"
            )
        )
    
    async def send_web_reply(self, user: str, content: str, staff_name: str) -> bool:
        """Send a reply to the web chat"""
        payload = {
            "user": staff_name,
            "text": content,
            "timestamp": int(time.time() * 1000),
            "origin": "discord"
        }
        
        for attempt in range(MAX_RETRIES):
            try:
                async with self.session.post(CHAT_URL, json=payload) as resp:
                    if resp.status == 200:
                        logger.info(f"Web reply sent to {user}")
                        return True
                    else:
                        logger.warning(f"Firebase returned status {resp.status} (attempt {attempt + 1})")
            except Exception as e:
                logger.error(f"Failed to send web reply (attempt {attempt + 1}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
        
        return False
    
    @tasks.loop(seconds=MESSAGE_FETCH_INTERVAL)
    async def check_live_chat(self):
        """Check for new web chat messages"""
        if not self.session:
            return
        
        try:
            async with self.session.get(CHAT_URL) as response:
                if response.status != 200:
                    logger.warning(f"Firebase returned status {response.status}")
                    return
                
                data = await response.json()
                if not data:
                    return
                
                # Sort messages by timestamp to ensure proper order
                sorted_messages = sorted(data.items(), key=lambda x: x[1].get('timestamp', 0))
                
                for msg_id, msg in sorted_messages:
                    await self.process_web_message(msg_id, msg)
                    
        except aiohttp.ClientError as e:
            logger.error(f"Network error in Firebase loop: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in Firebase loop: {e}")
    
    async def process_web_message(self, msg_id: str, msg: Dict[str, Any]):
        """Process an individual web message"""
        # Skip if already processed (using persistent storage)
        if msg_id in self.processed_msg_ids:
            return
        
        # Skip non-web messages
        if msg.get('origin') != 'web':
            self.processed_msg_ids.add(msg_id)
            return
        
        # Skip historical messages from before bot started
        msg_ts = msg.get('timestamp', 0)
        if msg_ts < self.boot_time:
            self.processed_msg_ids.add(msg_id)
            return
        
        # Mark as processed immediately to prevent duplicates
        self.processed_msg_ids.add(msg_id)
        
        guild = self.get_guild(GUILD_ID_INT)
        category = self.get_channel(TICKET_CATEGORY_ID)
        
        if not guild or not category:
            logger.error(f"Missing guild or category: guild={guild}, category={category}")
            return
        
        user_id = msg['user'].lower().replace(" ", "-")
        channel_name = f"web-{user_id}"
        
        welcome_msg = f"🚀 **Session Started:** `{msg['user']}`\nUse `/reply` to chat back."
        channel = await self.channel_manager.create_or_get_ticket_channel(
            guild, category, channel_name, welcome_msg
        )
        
        if not channel:
            return
        
        embed = discord.Embed(
            description=msg['text'], 
            color=0xef4444,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name=f"Web: {msg['user']}")
        embed.set_footer(text=f"ID: {msg_id[:8]}")
        
        try:
            await channel.send(embed=embed)
            logger.info(f"Forwarded web message from {msg['user']} to {channel_name}")
        except discord.Forbidden:
            logger.error(f"Cannot send message to channel {channel_name}")
        except Exception as e:
            logger.error(f"Failed to send message to channel: {e}")

# Create bot instance
bot = RawrBot()

# --- EVENT HANDLERS ---

@bot.event
async def on_ready():
    """Called when bot is ready"""
    logger.info(f"Logged in as: {bot.user.name} (ID: {bot.user.id})")
    logger.info(f"Connected to {len(bot.guilds)} guilds")
    logger.info(f"Loaded {len(bot.processed_msg_ids)} cached message IDs")
    
    # Set initial presence
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.competing, 
            name="rawrs.zapto.org"
        )
    )
    
    # Log guild information
    for guild in bot.guilds:
        logger.info(f"Guild: {guild.name} (ID: {guild.id})")

@bot.event
async def on_message(message: discord.Message):
    """Handle incoming messages"""
    if message.author == bot.user:
        return
    
    # DM to Ticket Channel Logic
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_message(message)
        return
    
    await bot.process_commands(message)

async def handle_dm_message(message: discord.Message):
    """Handle DM messages and create tickets"""
    guild = bot.get_guild(GUILD_ID_INT)
    category = bot.get_channel(TICKET_CATEGORY_ID)
    
    if not guild or not category:
        logger.error("Cannot handle DM: missing guild or category")
        await message.author.send("❌ **System Error:** Support system unavailable. Please try again later.")
        return
    
    channel_name = f"ticket-{message.author.name}".lower().replace(" ", "-")
    welcome_msg = f"🎫 **New Private Ticket**\nUser: {message.author.mention}\nID: `{message.author.id}`"
    
    channel = await bot.channel_manager.create_or_get_ticket_channel(
        guild, category, channel_name, welcome_msg
    )
    
    if not channel:
        await message.author.send("❌ **System Error:** Could not create ticket. Please try again later.")
        return
    
    embed = discord.Embed(
        description=message.content, 
        color=0xef4444,
        timestamp=datetime.utcnow()
    )
    embed.set_author(
        name=message.author.name, 
        icon_url=message.author.display_avatar.url
    )
    
    try:
        await channel.send(embed=embed)
        await message.author.send("✅ **Message Delivered.** Staff will contact you here shortly.")
        logger.info(f"Created DM ticket for {message.author.name} (ID: {message.author.id})")
    except discord.Forbidden:
        logger.error(f"Cannot send message to channel {channel_name}")
        await message.author.send("❌ **Error:** Could not deliver your message due to permissions.")
    except Exception as e:
        logger.error(f"Failed to handle DM message: {e}")
        await message.author.send("❌ **Error:** Could not deliver your message.")

# --- SLASH COMMANDS ---

@bot.tree.command(name="reply", description="Send a message to a Web or DM ticket")
@PermissionChecker.is_staff()
async def reply(interaction: discord.Interaction, content: str):
    """Reply to a web or DM ticket"""
    await interaction.response.defer(ephemeral=False)
    
    # Validate content
    if not content or len(content) > 2000:
        await interaction.followup.send("❌ Message must be between 1 and 2000 characters.")
        return
    
    # Web Ticket
    if interaction.channel.name.startswith("web-"):
        user = interaction.channel.name.replace("web-", "").replace("-", " ")
        success = await bot.send_web_reply(user, content, interaction.user.display_name)
        
        if success:
            await interaction.followup.send(f"✅ **[Web Reply to {user}]** {content}")
            logger.info(f"Staff {interaction.user.name} replied to web user {user}")
        else:
            await interaction.followup.send("❌ Failed to send web reply. Please try again.")
    
    # DM Ticket
    elif interaction.channel.name.startswith("ticket-"):
        member_name = interaction.channel.name.replace("ticket-", "")
        member = discord.utils.get(interaction.guild.members, name=member_name)
        
        if member:
            try:
                await member.send(f"💬 **rawr.xyz Staff ({interaction.user.display_name}):** {content}")
                await interaction.followup.send(f"✅ **[DM Sent to {member.name}]** {content}")
                logger.info(f"Staff {interaction.user.name} sent DM reply to {member.name}")
            except discord.Forbidden:
                await interaction.followup.send("❌ Cannot DM user (DMs closed or user left the server).")
            except Exception as e:
                logger.error(f"Failed to send DM: {e}")
                await interaction.followup.send("❌ Failed to send DM. Please try again.")
        else:
            await interaction.followup.send("❌ User is no longer in the server.")
    else:
        await interaction.followup.send("❌ Run this command in a valid ticket channel (web- or ticket-).")

@bot.tree.command(name="close", description="Close and delete the current support channel")
@PermissionChecker.is_manager()
async def close(interaction: discord.Interaction):
    """Close and delete a ticket channel"""
    if interaction.channel.category_id != TICKET_CATEGORY_ID:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return
    
    channel_name = interaction.channel.name
    await interaction.response.send_message(f"🔒 **Closing channel in {CLOSE_DELAY_SECONDS} seconds...**")
    await asyncio.sleep(CLOSE_DELAY_SECONDS)
    
    try:
        await interaction.channel.delete()
        logger.info(f"Closed ticket channel: {channel_name} by {interaction.user.name}")
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing permissions to delete this channel.")
    except Exception as e:
        logger.error(f"Failed to delete channel: {e}")
        await interaction.followup.send("❌ Failed to close ticket. Please try again or contact an admin.")

@bot.tree.command(name="website", description="Get the official website link")
async def website(interaction: discord.Interaction):
    """Get the website link"""
    embed = discord.Embed(
        title="🌐 Rawr.xyz",
        description=f"Explore our website at:\n{WEBSITE_URL}",
        color=0xef4444
    )
    embed.add_field(name="Features", value="• Free Scripts\n• Active Support\n• Regular Updates", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="updates", description="Fetch current script status and updates")
async def updates(interaction: discord.Interaction):
    """Get current script status"""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(STATUS_JSON_URL, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    embed = discord.Embed(
                        title="🚀 Script Update", 
                        color=0xef4444,
                        timestamp=datetime.utcnow()
                    )
                    embed.add_field(
                        name="Status", 
                        value=f"**{data.get('status', 'Unknown').upper()}**",
                        inline=True
                    )
                    embed.add_field(
                        name="Version", 
                        value=f"`{data.get('version', 'N/A')}`",
                        inline=True
                    )
                    if data.get('changelog'):
                        embed.add_field(
                            name="📝 Changes", 
                            value=data['changelog'][:1024],
                            inline=False
                        )
                    await interaction.response.send_message(embed=embed)
                else:
                    await interaction.response.send_message(
                        "⚠️ Status JSON unreachable. Please try again later.", 
                        ephemeral=True
                    )
        except asyncio.TimeoutError:
            await interaction.response.send_message(
                "⚠️ Request timed out. The status service might be down.", 
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Failed to fetch updates: {e}")
            await interaction.response.send_message(
                "⚠️ Failed to fetch updates. Please try again later.", 
                ephemeral=True
            )

@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    """Check bot response time"""
    latency = round(bot.latency * 1000)
    
    if latency < 100:
        color = 0x00ff00
        status = "Excellent"
    elif latency < 200:
        color = 0xffaa00
        status = "Good"
    else:
        color = 0xef4444
        status = "Poor"
    
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"**Latency:** `{latency}ms`\n**Status:** {status}",
        color=color
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stats", description="Show bot statistics")
@PermissionChecker.is_staff()
async def stats(interaction: discord.Interaction):
    """Display bot statistics"""
    uptime = int(time.time() * 1000) - bot.boot_time
    uptime_seconds = uptime // 1000
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60
    
    embed = discord.Embed(
        title="📊 Bot Statistics", 
        color=0xef4444,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="⏰ Uptime", value=f"{days}d {hours}h {minutes}m {seconds}s", inline=True)
    embed.add_field(name="💬 Processed Messages", value=str(len(bot.processed_msg_ids)), inline=True)
    embed.add_field(name="⚡ Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🌐 Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="📁 Stored IDs", value=f"{len(bot.processed_msg_ids)}/{MAX_STORED_IDS}", inline=True)
    embed.add_field(name="🔄 Last Save", value="Auto-save active", inline=True)
    embed.set_footer(text="Rawr.xyz Bot", icon_url=bot.user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="purge_ids", description="Clear cached message IDs (Admin only)")
@PermissionChecker.is_manager()
async def purge_ids(interaction: discord.Interaction):
    """Clear the processed message IDs cache (admin command)"""
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ This command is owner-only.", ephemeral=True)
        return
    
    old_count = len(bot.processed_msg_ids)
    bot.processed_msg_ids.clear()
    bot.save_processed_ids()
    
    await interaction.response.send_message(f"✅ Cleared {old_count} cached message IDs.", ephemeral=True)
    logger.warning(f"Message ID cache purged by owner {interaction.user.name}")

# --- ERROR HANDLING ---

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, 
    error: app_commands.AppCommandError
):
    """Handle command errors"""
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "⛔ **Access Denied:** Staff or Manager role required for this command.", 
            ephemeral=True
        )
    elif isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏰ **Cooldown:** Try again in {error.retry_after:.1f} seconds.", 
            ephemeral=True
        )
    elif isinstance(error, discord.Forbidden):
        await interaction.response.send_message(
            "❌ I don't have permission to do that. Please check my role permissions.", 
            ephemeral=True
        )
    elif isinstance(error, discord.HTTPException):
        await interaction.response.send_message(
            "❌ A network error occurred. Please try again later.", 
            ephemeral=True
        )
    else:
        logger.error(f"Unhandled command error: {error}", exc_info=True)
        await interaction.response.send_message(
            "❌ An unexpected error occurred. The developers have been notified.", 
            ephemeral=True
        )

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    try:
        logger.info("Starting Rawr.xyz Discord Bot...")
        logger.info(f"Configuration loaded from environment variables")
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid bot token. Please check your BOT_TOKEN environment variable.")
    except discord.PrivilegedIntentsRequired:
        logger.error("Privileged intents required but not enabled. Enable them in Discord Developer Portal.")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
