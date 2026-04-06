import os
import aiohttp
import discord
import asyncio
import time
import logging
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

# --- CONFIGURATION ---
TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

GUILD_ID_INT = int(os.getenv('GUILD_ID', '0'))
if not GUILD_ID_INT:
    raise ValueError("GUILD_ID environment variable not set")

GUILD_ID = discord.Object(id=GUILD_ID_INT)

# Permissions & IDs
OWNER_ID = 1071330258172780594
STAFF_ROLE_ID = 1489713077963456564
MANAGER_ROLE_ID = 1489435265914109972
TICKET_CATEGORY_ID = 1490508234526556321  # <-- UPDATE THIS WITH YOUR CATEGORY ID

# URLs
WEBSITE_URL = "https://rawrs.zapto.org/"
CHAT_URL = "https://rawr-chat-default-rtdb.firebaseio.com/messages.json"
STATUS_JSON_URL = "https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/main/status.json"

# Constants
MESSAGE_FETCH_INTERVAL = 3  # seconds
CLOSE_DELAY_SECONDS = 5
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds

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
    
    async def setup_hook(self):
        """Setup the bot before running"""
        self.session = aiohttp.ClientSession()
        self.tree.copy_global_to(guild=GUILD_ID)
        await self.tree.sync(guild=GUILD_ID)
        self.check_live_chat.start()
        logger.info("Bot setup completed")
    
    async def close(self):
        """Cleanup when bot closes"""
        if self.session:
            await self.session.close()
        await super().close()
        logger.info("Bot closed successfully")
    
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
                        logger.warning(f"Firebase returned status {resp.status}")
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
                
                for msg_id, msg in data.items():
                    await self.process_web_message(msg_id, msg)
                    
        except aiohttp.ClientError as e:
            logger.error(f"Network error in Firebase loop: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in Firebase loop: {e}")
    
    async def process_web_message(self, msg_id: str, msg: Dict[str, Any]):
        """Process an individual web message"""
        # Skip if already processed
        if msg_id in self.processed_msg_ids:
            return
        
        # Skip non-web messages
        if msg.get('origin') != 'web':
            self.processed_msg_ids.add(msg_id)
            return
        
        # Skip historical messages
        msg_ts = msg.get('timestamp', 0)
        if msg_ts < self.boot_time:
            self.processed_msg_ids.add(msg_id)
            return
        
        # Process new message
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
        logger.info(f"Created DM ticket for {message.author.name}")
    except discord.Forbidden:
        logger.error(f"Cannot send message to channel {channel_name}")
    except Exception as e:
        logger.error(f"Failed to handle DM message: {e}")
        await message.author.send("❌ **Error:** Could not deliver your message.")

# --- SLASH COMMANDS ---

@bot.tree.command(name="reply", description="Send a message to a Web or DM ticket")
@PermissionChecker.is_staff()
async def reply(interaction: discord.Interaction, content: str):
    """Reply to a web or DM ticket"""
    await interaction.response.defer(ephemeral=False)
    
    # Web Ticket
    if interaction.channel.name.startswith("web-"):
        user = interaction.channel.name.replace("web-", "").replace("-", " ")
        success = await bot.send_web_reply(user, content, interaction.user.display_name)
        
        if success:
            await interaction.followup.send(f"**[Web Reply to {user}]** {content}")
        else:
            await interaction.followup.send("❌ Failed to send web reply. Please try again.")
    
    # DM Ticket
    elif interaction.channel.name.startswith("ticket-"):
        member_name = interaction.channel.name.replace("ticket-", "")
        member = discord.utils.get(interaction.guild.members, name=member_name)
        
        if member:
            try:
                await member.send(f"💬 **rawr.xyz Staff ({interaction.user.display_name}):** {content}")
                await interaction.followup.send(f"**[DM Sent to {member.name}]** {content}")
                logger.info(f"Sent DM reply to {member.name}")
            except discord.Forbidden:
                await interaction.followup.send("❌ Cannot DM user (DMs closed or user left).")
            except Exception as e:
                logger.error(f"Failed to send DM: {e}")
                await interaction.followup.send("❌ Failed to send DM.")
        else:
            await interaction.followup.send("❌ User is no longer in the server.")
    else:
        await interaction.followup.send("❌ Run this in a valid ticket channel.")

@bot.tree.command(name="close", description="Close and delete the current support channel")
@PermissionChecker.is_manager()
async def close(interaction: discord.Interaction):
    """Close and delete a ticket channel"""
    if interaction.channel.category_id != TICKET_CATEGORY_ID:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)
        return
    
    await interaction.response.send_message(f"🔒 **Archiving and closing in {CLOSE_DELAY_SECONDS} seconds...**")
    await asyncio.sleep(CLOSE_DELAY_SECONDS)
    
    channel_name = interaction.channel.name
    try:
        await interaction.channel.delete()
        logger.info(f"Closed ticket channel: {channel_name}")
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing permissions to delete this channel.")
    except Exception as e:
        logger.error(f"Failed to delete channel: {e}")
        await interaction.followup.send("❌ Failed to close ticket.")

@bot.tree.command(name="website", description="Official Link")
async def website(interaction: discord.Interaction):
    """Get the website link"""
    embed = discord.Embed(
        title="🌐 Rawr.xyz",
        description=f"Explore our website at:\n{WEBSITE_URL}",
        color=0xef4444
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="updates", description="Fetch current script status")
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
                        value=f"**{data.get('status', 'Unknown').upper()}**"
                    )
                    embed.add_field(
                        name="Version", 
                        value=f"`{data.get('version', 'N/A')}`"
                    )
                    if data.get('changelog'):
                        embed.add_field(
                            name="Changes", 
                            value=data['changelog'][:1024],
                            inline=False
                        )
                    await interaction.response.send_message(embed=embed)
                else:
                    await interaction.response.send_message(
                        "⚠️ Status JSON unreachable.", 
                        ephemeral=True
                    )
        except asyncio.TimeoutError:
            await interaction.response.send_message(
                "⚠️ Request timed out.", 
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Failed to fetch updates: {e}")
            await interaction.response.send_message(
                "⚠️ Failed to fetch updates.", 
                ephemeral=True
            )

@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    """Check bot response time"""
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Latency: `{latency}ms`",
        color=0xef4444 if latency > 100 else 0x00ff00
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
    
    embed = discord.Embed(title="📊 Bot Statistics", color=0xef4444)
    embed.add_field(name="Uptime", value=f"{days}d {hours}h {minutes}m")
    embed.add_field(name="Processed Messages", value=str(len(bot.processed_msg_ids)))
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms")
    embed.add_field(name="Guilds", value=str(len(bot.guilds)))
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- ERROR HANDLING ---

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, 
    error: app_commands.AppCommandError
):
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
        logger.error(f"Unhandled command error: {error}")
        await interaction.response.send_message(
            "❌ An unexpected error occurred. Please try again later.", 
            ephemeral=True
        )

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid bot token")
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
