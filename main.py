import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# config
TOKEN = os.getenv('BOT_TOKEN')
# 23
try:
    GUILD_ID_INT = int(os.getenv('GUILD_ID'))
    GUILD_ID = discord.Object(id=GUILD_ID_INT)
except (TypeError, ValueError):
    print("ERROR: GUILD_ID is not set correctly in GitHub Secrets.")
    GUILD_ID = None

# url
WEBSITE_URL = "https://rawrs.zapto.org/"
# 23
STATUS_JSON_URL = "https://raw.githubusercontent.com/imcomingforyou6959-gif/rawr.xyz/main/status.json"

class RawrBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # B3
        intents.message_content = True 
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # b6
        if GUILD_ID:
            self.tree.copy_global_to(guild=GUILD_ID)
            await self.tree.sync(guild=GUILD_ID)
            print(f"✅ Synced slash commands to Guild: {GUILD_ID.id}")
        else:
            await self.tree.sync()
            print("✅ Synced slash commands globally.")

bot = RawrBot()

@bot.event
async def on_ready():
    print(f'🚀 rawr.xyz bot is ONLINE as {bot.user}')
    # 2
    await bot.change_presence(activity=discord.Game(name="rawrs.zapto.org"))

# Commands

@bot.tree.command(name="website", description="Get the official link to rawr.xyz")
async def website(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Official rawr.xyz Hub",
        description=f"Access the loadstring at [rawrs.zapto.org]({WEBSITE_URL})",
        color=0xef4444 
    )
    embed.set_thumbnail(url="https://rawrs.zapto.org/logo.png")
    embed.set_footer(text="Verified Official Website")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="owner", description="Information about the rawr.xyz owner")
async def owner(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Owner of Rawr.xyz",
        description="**Owner:** Tie\n**Specialization:** Lua, Python, Java\n**Current Project:** rawr.xyz",
        color=0xef4444
    )
    embed.add_field(name="Resources", value="[GitHub](https://github.com/imcomingforyou6959-gif) | [YouTube](https://www.youtube.com/@rawr.xy3)")
    embed.set_thumbnail(url="https://rawrs.zapto.org/logo.png")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="updates", description="Fetch live script status and version info")
async def updates(interaction: discord.Interaction):
    # 1
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(STATUS_JSON_URL) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    version = data.get("version", "v1.0.0")
                    status = data.get("status", "Unknown")
                    status_color = data.get("statusColor", "gray")
                    
                    embed_color = 0x22c55e if status_color == "green" else 0xef4444
                    
                    embed = discord.Embed(
                        title="🚀 rawr.xyz — Live Intelligence",
                        description=f"Current Status: **{status.upper()}**",
                        color=embed_color
                    )
                    embed.add_field(name="Current Version", value=f"`{version}`", inline=True)
                    embed.set_footer(text="Data synced live from rawrs.zapto.org")
                    
                    await interaction.response.send_message(embed=embed)
                else:
                    await interaction.response.send_message("⚠️ Error: Could not reach the status server.")
        except Exception as e:
            print(f"Error fetching status: {e}")
            await interaction.response.send_message("❌ An unexpected error occurred while fetching live data.")

# INIT
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("FATAL ERROR: BOT_TOKEN not found in environment variables.")
