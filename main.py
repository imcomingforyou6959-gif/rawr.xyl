import os
import discord
from discord import app_commands
from discord.ext import commands

# This pulls the token from your GitHub Secrets safely
TOKEN = os.getenv('BOT_TOKEN')
# This pulls your Server ID from GitHub Secrets safely
GUILD_ID = discord.Object(id=int(os.getenv('GUILD_ID')))
WEBSITE_URL = "https://rawrs.zapto.org/"
YOUTUBE_URL = "https://www.youtube.com/@rawr.xy3"

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # This syncs your slash commands to your specific server instantly
        self.tree.copy_global_to(guild=GUILD_ID)
        await self.tree.sync(guild=GUILD_ID)
        print(f"Synced slash commands to {GUILD_ID.id}")

bot = MyBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    await bot.change_presence(activity=discord.Game(name="rawrs.zapto.org"))

# --- SLASH COMMANDS ---

@bot.tree.command(name="website", description="Get the official link to rawr.xyz")
async def website(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Official Website",
        description=f"Access our roblox script at [rawrs.zapto.org]({WEBSITE_URL})",
        color=0xef4444 # Matches your site's red
    )
    embed.set_thumbnail(url="https://rawrs.zapto.org/logo.png")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="owner", description="Information about the developer")
async def owner(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Owner of rawr.xyz",
        description="**Owner:** Tie\n**Good at:** Lua, Python, Java\n**Project:** rawr.xyz",
        color=0xef4444
    )
    embed.add_field(name="Links", value=f"[YouTube]({YOUTUBE_URL}) | [GitHub](https://github.com/imcomingforyou6959-gif)")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="updates", description="Check the latest script status and versions")
async def updates(interaction: discord.Interaction):
    # Pro-tip: You could use 'requests' here to fetch your live status.json from GitHub!
    embed = discord.Embed(
        title="🚀 Latest Updates",
        description="**Current Version:** v1.5.1\n**Status:** 🟢 ONLINE\n\n- Improved ESP performance\n- Added Sirhurt compatibility\n- Improved etc",
        color=0x27c93f # Green for 'Online' vibe
    )
    await interaction.response.send_message(embed=embed)

bot.run(TOKEN)