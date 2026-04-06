import os
import aiohttp
import discord
import asyncio
import time
from discord import app_commands
from discord.ext import commands, tasks

# --- CONFIGURATION ---
TOKEN = os.getenv('BOT_TOKEN')
GUILD_ID_INT = int(os.getenv('GUILD_ID'))
GUILD_ID = discord.Object(id=GUILD_ID_INT)

# Permissions & IDs
OWNER_ID = 1071330258172780594
STAFF_ROLE_ID = 1489713077963456564
MANAGER_ROLE_ID = 1489435265914109972
TICKET_CATEGORY_ID = 1490508234526556321 # <-- ENSURE THIS IS CORRECT

# URLs
WEBSITE_URL = "https://rawrs.zapto.org/"
CHAT_URL = "https://rawr-chat-default-rtdb.firebaseio.com/messages.json"
STATUS_JSON_URL = "https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/main/status.json"

def is_staff():
    """Check if user is Owner, Manager, or Staff"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        role_ids = [role.id for role in interaction.user.roles]
        return STAFF_ROLE_ID in role_ids or MANAGER_ROLE_ID in role_ids
    return app_commands.check(predicate)

class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self.last_chat_timestamp = time.time() * 1000
        self.processed_msg_ids = set() # Extra layer of double-post protection
        self.processing_channels = set()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.tree.copy_global_to(guild=GUILD_ID)
        await self.tree.sync(guild=GUILD_ID)
        self.check_live_chat.start()

    @tasks.loop(seconds=3)
    async def check_live_chat(self):
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(CHAT_URL) as response:
                    if response.status != 200: return
                    data = await response.json()
                    if not data: return
                    
                    for msg_id, msg in data.items():
                        msg_ts = msg.get('timestamp', 0)
                        
                        # Only process web messages newer than our last check
                        if msg.get('origin') == 'web' and msg_ts > self.last_chat_timestamp and msg_id not in self.processed_msg_ids:
                            self.last_chat_timestamp = msg_ts
                            self.processed_msg_ids.add(msg_id)
                            
                            guild = self.get_guild(GUILD_ID_INT)
                            category = self.get_channel(TICKET_CATEGORY_ID)
                            if not category: continue

                            user_id = msg['user'].lower().replace(" ", "-")
                            channel_name = f"web-{user_id}"
                            
                            if channel_name in self.processing_channels: continue

                            channel = discord.utils.get(category.text_channels, name=channel_name)
                            
                            if not channel:
                                self.processing_channels.add(channel_name)
                                try:
                                    overwrites = {
                                        guild.default_role: discord.PermissionOverride(read_messages=False),
                                        guild.me: discord.PermissionOverride(read_messages=True)
                                    }
                                    channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
                                    await channel.send(f"🚀 **Session Started:** `{msg['user']}`\nUse `/reply` to chat back.")
                                finally:
                                    self.processing_channels.remove(channel_name)
                            
                            embed = discord.Embed(description=msg['text'], color=0xef4444)
                            embed.set_author(name=f"Web: {msg['user']}")
                            await channel.send(embed=embed)

            except Exception as e:
                print(f"Loop Error: {e}")

bot = RawrBot()

@bot.event
async def on_ready():
    print(f'🚀 rawr.xyz Security Protocol Online')
    print(f'Staff Role: {STAFF_ROLE_ID} | Manager Role: {MANAGER_ROLE_ID}')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.competing, name="rawrs.zapto.org"))

@bot.event
async def on_message(message):
    if message.author == bot.user: return

    # DM Ticket System
    if isinstance(message.channel, discord.DMChannel):
        guild = bot.get_guild(GUILD_ID_INT)
        category = bot.get_channel(TICKET_CATEGORY_ID)
        if not category: return

        channel_name = f"ticket-{message.author.name}".lower().replace(" ", "-")
        channel = discord.utils.get(category.text_channels, name=channel_name)
        
        if not channel:
            overwrites = {
                guild.default_role: discord.PermissionOverride(read_messages=False),
                guild.me: discord.PermissionOverride(read_messages=True)
            }
            channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)
            await channel.send(f"🎫 **New DM Ticket**\nUser: {message.author.mention}\nID: `{message.author.id}`")

        embed = discord.Embed(description=message.content, color=0xef4444)
        embed.set_author(name=message.author.name, icon_url=message.author.display_avatar.url)
        await channel.send(embed=embed)
        await message.author.send("✅ **Delivered.** A staff member will be with you shortly.")

    await bot.process_commands(message)

# --- STAFF COMMANDS ---

@bot.tree.command(name="reply", description="Reply to a web or DM user")
@is_staff()
async def reply(interaction: discord.Interaction, content: str):
    # Web Channel Logic
    if interaction.channel.name.startswith("web-"):
        payload = {
            "user": interaction.user.display_name,
            "text": content,
            "timestamp": time.time() * 1000,
            "origin": "discord"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(CHAT_URL, json=payload) as resp:
                if resp.status == 200:
                    await interaction.response.send_message(f"**[Web Reply]** {content}")
                else:
                    await interaction.response.send_message("❌ Sync error.")

    # DM Ticket Logic
    elif interaction.channel.name.startswith("ticket-"):
        member_name = interaction.channel.name.replace("ticket-", "")
        member = discord.utils.get(interaction.guild.members, name=member_name)
        if member:
            try:
                await member.send(f"💬 **rawr.xyz Support:** {content}")
                await interaction.response.send_message(f"**[DM Reply to {member.name}]** {content}")
            except:
                await interaction.response.send_message("❌ User has DMs disabled.")
        else:
            await interaction.response.send_message("❌ User not found.")
    else:
        await interaction.response.send_message("❌ This is not a ticket channel.", ephemeral=True)

@bot.tree.command(name="close", description="Close the current ticket")
@is_staff()
async def close(interaction: discord.Interaction):
    if interaction.channel.category_id == TICKET_CATEGORY_ID:
        await interaction.response.send_message("🔒 **Closing and archiving in 5s...**")
        await asyncio.sleep(5)
        await interaction.channel.delete()
    else:
        await interaction.response.send_message("❌ Not a valid ticket channel.", ephemeral=True)

# --- PUBLIC COMMANDS ---

@bot.tree.command(name="website", description="Official Site")
async def website(interaction: discord.Interaction):
    await interaction.response.send_message(f"🔗 **Explore:** {WEBSITE_URL}", ephemeral=True)

@bot.tree.command(name="updates", description="Current script health")
async def updates(interaction: discord.Interaction):
    async with aiohttp.ClientSession() as session:
        async with session.get(STATUS_JSON_URL) as response:
            if response.status == 200:
                data = await response.json()
                embed = discord.Embed(title="🚀 System Status", color=0xef4444)
                embed.add_field(name="Status", value=f"**{data.get('status', 'Online').upper()}**")
                embed.add_field(name="Version", value=f"`{data.get('version', 'Latest')}`")
                await interaction.response.send_message(embed=embed)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("⛔ You do not have the required roles to use this command.", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
