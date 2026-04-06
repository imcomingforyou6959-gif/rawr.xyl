import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('BOT_TOKEN')
GUILD_ID = discord.Object(id=int(os.getenv('GUILD_ID')))
OWNER_ID = 1071330258172780594

WEBSITE_URL = "https://rawrs.zapto.org/"
STATUS_JSON_URL = "https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/main/status.json"

class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.tree.copy_global_to(guild=GUILD_ID)
        await self.tree.sync(guild=GUILD_ID)

bot = RawrBot()

@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(name="rawrs.zapto.org"))

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if isinstance(message.channel, discord.DMChannel):
        embed = discord.Embed(
            title="rawr.xyz Support",
            description="Thanks for reaching out! For script updates and links, use `/updates` or visit [rawrs.zapto.org](https://rawrs.zapto.org/).",
            color=0xef4444
        )
        await message.channel.send(embed=embed)
        
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(f"🔔 **New DM from {message.author} ({message.author.id}):**\n{message.content}")

    await bot.process_commands(message)

@bot.tree.command(name="dm", description="Send a DM to a user via the bot")
async def send_dm(interaction: discord.Interaction, user: discord.User, content: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ Access Denied.", ephemeral=True)
        return

    try:
        await user.send(content)
        await interaction.response.send_message(f"✅ Sent to **{user.name}**.", ephemeral=True)
    except:
        await interaction.response.send_message("❌ Failed to send DM.", ephemeral=True)

@bot.tree.command(name="website", description="Official link to rawr.xyz")
async def website(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Official rawr.xyz Website",
        description=f"Access the repository at [rawrs.zapto.org]({WEBSITE_URL})",
        color=0xef4444
    )
    embed.set_thumbnail(url="https://rawrs.zapto.org/logo.png")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="owner", description="Founder information")
async def owner(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Founder of rawr.xyz",
        description="**Founder:** Tie\n**Project:** rawr.xyz",
        color=0xef4444
    )
    embed.add_field(name="Links", value="[GitHub](https://github.com/imcomingforyou6959-gif) | [YouTube](https://www.youtube.com/@rawr.xy3)")
    embed.set_thumbnail(url="https://rawrs.zapto.org/logo.png")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="updates", description="Fetch live script status")
async def updates(interaction: discord.Interaction):
    async with aiohttp.ClientSession() as session:
        async with session.get(STATUS_JSON_URL) as response:
            if response.status == 200:
                data = await response.json()
                version = data.get("version", "v1.0.0")
                status = data.get("status", "Unknown")
                status_color = data.get("statusColor", "gray")
                embed_color = 0x22c55e if status_color == "green" else 0xef4444
                
                embed = discord.Embed(
                    title="🚀 rawr.xyz — Live Status",
                    description=f"Status: **{status.upper()}**",
                    color=embed_color
                )
                embed.add_field(name="Version", value=f"`{version}`")
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message("⚠️ Connection Error.")

if __name__ == "__main__":
    bot.run(TOKEN)
