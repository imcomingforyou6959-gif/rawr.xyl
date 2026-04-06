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
OWNER_ID = 1071330258172780594
TICKET_CATEGORY_ID = 1490508234526556321

WEBSITE_URL = "https://rawrs.zapto.org/"
CHAT_URL = "https://rawr-chat-default-rtdb.firebaseio.com/messages.json"
STATUS_JSON_URL = "https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/main/status.json"

class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self.last_chat_timestamp = time.time() * 1000
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.tree.copy_global_to(guild=GUILD_ID)
        await self.tree.sync(guild=GUILD_ID)
        self.check_live_chat.start()

    @tasks.loop(seconds=5)
    async def check_live_chat(self):
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(CHAT_URL) as response:
                    if response.status == 200:
                        data = await response.json()
                        if not data: return
                        
                        for msg_id, msg in data.items():
                            msg_ts = msg.get('timestamp', 0)
                            if msg.get('origin') == 'web' and msg_ts > self.last_chat_timestamp:
                                guild = self.get_guild(GUILD_ID_INT)
                                category = self.get_channel(TICKET_CATEGORY_ID)
                                channel_name = f"web-{msg['user']}".lower().replace(" ", "-")
                                
                                channel = discord.utils.get(category.text_channels, name=channel_name)
                                if not channel:
                                    channel = await guild.create_text_channel(channel_name, category=category)
                                    await channel.send(f"🌐 **New Web Session:** {msg['user']}")
                                
                                embed = discord.Embed(description=msg['text'], color=0xef4444)
                                embed.set_author(name=f"Web: {msg['user']}")
                                await channel.send(embed=embed)
                                self.last_chat_timestamp = msg_ts
            except Exception as e:
                print(f"Chat Loop Error: {e}")

bot = RawrBot()

@bot.event
async def on_ready():
    print(f'🚀 rawr.xyz bot is ONLINE as {bot.user}')
    await bot.change_presence(activity=discord.Game(name="rawrs.zapto.org"))

@bot.event
async def on_message(message):
    if message.author == bot.user: return

    # Handle DM to Ticket Channel
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
            await channel.send(f"🎫 **New Ticket Channel**\nUser: {message.author.mention}\nID: `{message.author.id}`")

        embed = discord.Embed(description=message.content, color=0xef4444)
        embed.set_author(name=message.author.name, icon_url=message.author.display_avatar.url)
        await channel.send(embed=embed)
        await message.author.send("✅ **Message Received.** Our staff has been notified.")

    await bot.process_commands(message)

# --- COMMANDS ---

@bot.tree.command(name="reply", description="Reply to a ticket or website user")
@app_commands.describe(content="The message to send back")
async def reply(interaction: discord.Interaction, content: str):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ Unauthorized.", ephemeral=True)

    # Web Chat Reply
    if interaction.channel.name.startswith("web-"):
        payload = {
            "user": "Support",
            "text": content,
            "timestamp": time.time() * 1000,
            "origin": "discord"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(CHAT_URL, json=payload) as resp:
                if resp.status == 200:
                    await interaction.response.send_message(f"✅ Web Reply: {content}")
                else:
                    await interaction.response.send_message("❌ Firebase Error.")

    # DM Ticket Reply
    elif interaction.channel.name.startswith("ticket-"):
        try:
            # Extract user ID from channel history or search members
            member_name = interaction.channel.name.replace("ticket-", "")
            member = discord.utils.get(interaction.guild.members, name=member_name)
            if member:
                await member.send(f"💬 **rawr.xyz Support:** {content}")
                await interaction.response.send_message(f"✅ DM Sent to {member.name}")
            else:
                await interaction.response.send_message("❌ User not found in server.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}")
    else:
        await interaction.response.send_message("❌ Run this command inside a ticket channel.", ephemeral=True)

@bot.tree.command(name="close", description="Close and delete the ticket channel")
async def close(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID: return
    if interaction.channel.category_id == TICKET_CATEGORY_ID:
        await interaction.response.send_message("🔒 Closing ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

@bot.tree.command(name="website", description="Official link to rawr.xyz")
async def website(interaction: discord.Interaction):
    embed = discord.Embed(title="🌐 Official rawr.xyz website", description=f"[rawrs.zapto.org]({WEBSITE_URL})", color=0xef4444)
    embed.set_thumbnail(url="https://rawrs.zapto.org/logo.png")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="owner", description="Founder information")
async def owner(interaction: discord.Interaction):
    embed = discord.Embed(title="Founder of rawr.xyz", description="**Owner:** Tie\n**Project:** rawr.xyz", color=0xef4444)
    embed.add_field(name="Links", value="[GitHub](https://github.com/imcomingforyou6959-gif) | [YouTube](https://www.youtube.com/@rawr.xy3)")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="updates", description="Fetch live script status")
async def updates(interaction: discord.Interaction):
    async with aiohttp.ClientSession() as session:
        async with session.get(STATUS_JSON_URL) as response:
            if response.status == 200:
                data = await response.json()
                status = data.get("status", "Unknown")
                version = data.get("version", "N/A")
                embed = discord.Embed(title="🚀 Status Update", description=f"Current Status: **{status.upper()}**", color=0xef4444)
                embed.add_field(name="Version", value=f"`{version}`")
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message("⚠️ Status unavailable.")

if __name__ == "__main__":
    bot.run(TOKEN)
