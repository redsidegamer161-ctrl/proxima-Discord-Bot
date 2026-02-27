import discord
from discord import app_commands
import aiosqlite
import datetime
import os
import asyncio
import time
from keep_alive import keep_alive

# --- IMPORTS FOR IMAGE GENERATION ---
from PIL import Image, ImageDraw, ImageFont
import io
import aiohttp
import urllib.request

# --- CONFIGURATION ---
TOKEN = os.environ.get('TOKEN')
DEFAULT_BG_FILE = "proxima_default.jpg"
DB_PATH = "team_manager.db"

# --- REPLACE THIS WITH YOUR DISCORD USER ID (right‑click your profile -> Copy ID) ---
OWNER_ID = 925817680848617486  # <-- CHANGE THIS TO YOUR OWN ID

# --- AUTO-DOWNLOAD FONT ---
def check_and_download_font():
    if not os.path.exists("font.ttf"):
        print("System: Font missing. Downloading Roboto-Bold...")
        try:
            url = "https://github.com/google/fonts/raw/main/apache/roboto/Roboto-Bold.ttf"
            urllib.request.urlretrieve(url, "font.ttf")
            print("System: Font downloaded successfully!")
        except Exception as e:
            print(f"System: Could not download font. Text will be small. Error: {e}")

check_and_download_font()

# --- DATABASE SETUP (async) ---
async def init_db():
    """Create tables and enable WAL mode."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        # 1. Global Settings
        await db.execute("""CREATE TABLE IF NOT EXISTS global_config (
                             guild_id INTEGER PRIMARY KEY,
                             manager_role_id INTEGER,
                             asst_role_id INTEGER,
                             contract_channel_id INTEGER,
                             free_agent_role_id INTEGER,
                             window_open INTEGER DEFAULT 1,
                             demand_limit INTEGER DEFAULT 3
                             )""")
        # 2. Teams Table
        await db.execute("""CREATE TABLE IF NOT EXISTS teams (
                             team_role_id INTEGER PRIMARY KEY,
                             logo TEXT,
                             roster_limit INTEGER,
                             transaction_image TEXT
                             )""")
        # 3. Free Agents
        await db.execute("""CREATE TABLE IF NOT EXISTS free_agents (
                             user_id INTEGER PRIMARY KEY,
                             region TEXT,
                             position TEXT,
                             description TEXT,
                             timestamp TEXT
                             )""")
        # 4. Player Stats
        await db.execute("""CREATE TABLE IF NOT EXISTS player_stats (
                             user_id INTEGER PRIMARY KEY,
                             transfers INTEGER DEFAULT 0,
                             demands INTEGER DEFAULT 0
                             )""")
        # Migrations (add columns if missing)
        try:
            await db.execute("ALTER TABLE global_config ADD COLUMN free_agent_role_id INTEGER")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE global_config ADD COLUMN window_open INTEGER DEFAULT 1")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE teams ADD COLUMN transaction_image TEXT")
        except aiosqlite.OperationalError:
            pass
        try:
            await db.execute("ALTER TABLE global_config ADD COLUMN demand_limit INTEGER DEFAULT 3")
        except aiosqlite.OperationalError:
            pass
        await db.commit()

# --- CACHE (simple TTL) ---
config_cache = {}
team_cache = {}
CACHE_TTL = 60  # seconds

async def get_global_config(guild_id):
    now = time.time()
    if guild_id in config_cache and now - config_cache[guild_id]['timestamp'] < CACHE_TTL:
        return config_cache[guild_id]['data']
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM global_config WHERE guild_id = ?", (guild_id,)) as cursor:
            row = await cursor.fetchone()
            data = tuple(row) if row else None
    config_cache[guild_id] = {'data': data, 'timestamp': now}
    return data

async def invalidate_config(guild_id):
    config_cache.pop(guild_id, None)

async def get_team_data(role_id):
    now = time.time()
    if role_id in team_cache and now - team_cache[role_id]['timestamp'] < CACHE_TTL:
        return team_cache[role_id]['data']
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM teams WHERE team_role_id = ?", (role_id,)) as cursor:
            row = await cursor.fetchone()
            data = tuple(row) if row else None
    team_cache[role_id] = {'data': data, 'timestamp': now}
    return data

async def invalidate_team(role_id):
    team_cache.pop(role_id, None)

async def get_all_teams():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM teams") as cursor:
            rows = await cursor.fetchall()
            return [tuple(row) for row in rows]

async def get_player_stats(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM player_stats WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            await db.execute("INSERT INTO player_stats (user_id, transfers, demands) VALUES (?, 0, 0)", (user_id,))
            await db.commit()
            return (user_id, 0, 0)
        return row

async def update_stat(user_id, stat_type, amount=1):
    async with aiosqlite.connect(DB_PATH) as db:
        await get_player_stats(user_id)  # ensure exists
        if stat_type == "transfer":
            await db.execute("UPDATE player_stats SET transfers = transfers + ? WHERE user_id = ?", (amount, user_id))
        elif stat_type == "demand":
            await db.execute("UPDATE player_stats SET demands = demands + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

def find_user_team_sync(member):
    """Synchronous helper to find a member's team (used in sync contexts, but we'll keep it sync)."""
    for role in member.roles:
        # Note: this calls get_team_data which is async, so we can't use it directly.
        # We'll keep this as a placeholder and replace calls with an async version.
        pass

# We need an async version for use in commands
async def find_user_team(member):
    for role in member.roles:
        data = await get_team_data(role.id)
        if data:
            trans_img = data[3] if len(data) > 3 else None
            return (role, data[1], data[2], trans_img)
    return None

def is_staff(interaction: discord.Interaction):
    return interaction.user.guild_permissions.administrator

async def is_window_open(guild_id):
    config = await get_global_config(guild_id)
    if not config:
        return True
    try:
        return config[5] == 1
    except IndexError:
        return True

async def get_managers_of_team(guild, team_role):
    config = await get_global_config(guild.id)
    if not config:
        return ([], [])
    mgr_id, asst_id = config[1], config[2]
    head_managers, assistants = [], []
    for member in team_role.members:
        r_ids = [r.id for r in member.roles]
        if mgr_id in r_ids:
            head_managers.append(member)
        elif asst_id in r_ids:
            assistants.append(member)
    return (head_managers, assistants)

async def cleanup_free_agent(guild, member):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM free_agents WHERE user_id = ?", (member.id,))
        await db.commit()
    config = await get_global_config(guild.id)
    if config and config[4]:
        role = guild.get_role(config[4])
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
            except:
                pass

def format_roster_list(members, mgr_id, asst_id):
    formatted_list = []
    for m in members:
        r_ids = [r.id for r in m.roles]
        name = m.mention
        if mgr_id in r_ids:
            name += " **(TM)**"
        elif asst_id in r_ids:
            name += " **(AM)**"
        formatted_list.append(name)
    return formatted_list

# --- MASTER CARD GENERATOR (now threaded) ---
async def generate_transaction_card(player, team_name, team_color, title_text="OFFICIAL SIGNING", custom_bg_url=None):
    # Run the blocking PIL code in a thread
    return await asyncio.to_thread(
        _generate_card_sync,
        player, team_name, team_color, title_text, custom_bg_url
    )

def _generate_card_sync(player, team_name, team_color, title_text, custom_bg_url):
    """Synchronous version of the card generator (runs in thread)."""
    W, H = 800, 400
    img = None

    # 1. Try Custom URL
    if custom_bg_url:
        try:
            # Need to fetch image synchronously in thread; use requests or urllib
            import urllib.request
            with urllib.request.urlopen(custom_bg_url) as resp:
                data = resp.read()
                bg_img = Image.open(io.BytesIO(data)).convert("RGB")
                img = bg_img.resize((W, H))
                # Dark Overlay
                overlay = Image.new("RGBA", (W, H), (0,0,0,0))
                draw_overlay = ImageDraw.Draw(overlay)
                draw_overlay.rectangle([(0, 240), (W, H)], fill=(0, 0, 0, 160))
                img.paste(overlay, (0,0), mask=overlay)
        except:
            img = None

    # 2. Try Local File
    if img is None and os.path.exists(DEFAULT_BG_FILE):
        try:
            bg_img = Image.open(DEFAULT_BG_FILE).convert("RGB")
            img = bg_img.resize((W, H))
        except:
            pass

    # 3. Fallback: team color
    if img is None:
        bg_color = team_color.to_rgb()
        if bg_color == (0, 0, 0):
            bg_color = (44, 47, 51)
        img = Image.new("RGB", (W, H), color=bg_color)

    draw = ImageDraw.Draw(img)

    # 4. Avatar (fetch synchronously in thread)
    try:
        import urllib.request
        with urllib.request.urlopen(player.display_avatar.url) as resp:
            data = resp.read()
            avatar = Image.open(io.BytesIO(data)).convert("RGBA")
            avatar = avatar.resize((200, 200))
            # Circular Mask
            mask = Image.new("L", (200, 200), 0)
            draw_mask = ImageDraw.Draw(mask)
            draw_mask.ellipse((0, 0, 200, 200), fill=255)
            img.paste(avatar, (300, 50), mask=mask)
            # Border
            draw.ellipse((300, 50, 500, 250), outline="white", width=3)
    except:
        pass

    # 5. Text
    try:
        font_large = ImageFont.truetype("font.ttf", 60)
        font_small = ImageFont.truetype("font.ttf", 40)
    except:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    draw.text((W/2, 290), title_text, fill="white", font=font_small, anchor="mm")
    draw.text((W/2, 350), player.name.upper(), fill="white", font=font_large, anchor="mm")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(buffer, filename="transaction.png")

# --- EMBED GENERATOR (unchanged, but uses async DB now) ---
def create_transaction_embed(guild, title, description, color, team_role, logo, coach, roster_count, limit):
    embed = discord.Embed(description=description, color=color, timestamp=datetime.datetime.now())
    embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
    embed.title = title
    if logo and "http" in logo:
        embed.set_thumbnail(url=logo)
    if coach:
        embed.add_field(name="Coach:", value=f"👔 {coach.mention}", inline=False)
    roster_text = f"{roster_count}/{limit}" if limit > 0 else f"{roster_count} (No Limit)"
    embed.add_field(name="Roster:", value=f"👥 {roster_text}", inline=False)
    embed.set_footer(text="Official Transaction")
    return embed

async def send_to_channel(guild, embed, file=None):
    config = await get_global_config(guild.id)
    if config and config[3]:
        channel = guild.get_channel(config[3])
        if channel:
            await channel.send(embed=embed, file=file)
            return True
    return False

async def send_dm(user, content=None, embed=None, view=None):
    try:
        await user.send(content=content, embed=embed, view=view)
        return True
    except:
        return False

# --- VIEWS (need async DB in buttons) ---

class TransferView(discord.ui.View):
    def __init__(self, guild, player, from_team, to_team, to_manager, logo):
        super().__init__(timeout=86400)
        self.guild = guild
        self.player = player
        self.from_team = from_team
        self.to_team = to_team
        self.to_manager = to_manager
        self.logo = logo

    @discord.ui.button(label="Accept Transfer", style=discord.ButtonStyle.green, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await is_window_open(self.guild.id):
            return await interaction.response.send_message("❌ **Transfer Window is CLOSED.**", ephemeral=True)

        await interaction.response.defer()

        try:
            member = self.guild.get_member(self.player.id)
            if not member:
                return await interaction.followup.send("❌ Player missing.", ephemeral=True)

            await member.remove_roles(self.from_team)
            await member.add_roles(self.to_team)
            await cleanup_free_agent(self.guild, member)
            await update_stat(member.id, "transfer")

            desc = f"🚨 **TRANSFER NEWS** 🚨\n\n{member.mention} has been transferred\nFrom: {self.from_team.mention}\nTo: {self.to_team.mention}"

            data = await get_team_data(self.to_team.id)
            limit = data[2] if data else 0
            custom_bg = data[3] if data and len(data) > 3 else None

            embed = create_transaction_embed(self.guild, "Official Transfer", desc, discord.Color.purple(),
                                             self.to_team, self.logo, self.to_manager,
                                             len(self.to_team.members), limit)

            file = await generate_transaction_card(member, self.to_team.name, self.to_team.color,
                                                   "OFFICIAL TRANSFER", custom_bg)
            embed.set_image(url="attachment://transaction.png")

            await send_to_channel(self.guild, embed, file)
            await send_dm(self.to_manager, f"✅ Transfer for **{member.name}** ACCEPTED!")

            self.stop()
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(content="✅ **Transfer Approved.**", view=self)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await send_dm(self.to_manager, f"❌ Transfer for **{self.player.name}** DECLINED.")
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(content="❌ **Transfer Declined.**", view=self)

class HelpView(discord.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=60)
        self.embeds = embeds
        self.current_page = 0
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.current_page == 0
        self.next.disabled = self.current_page == len(self.embeds) - 1

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

class ResetView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=30)
        self.guild_id = guild_id

    @discord.ui.button(label="⚠️ CONFIRM WIPE", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM global_config WHERE guild_id = ?", (self.guild_id,))
            await db.commit()
        await invalidate_config(self.guild_id)
        await interaction.followup.send("✅ **Configuration Wiped.** Please run `/setup_global` again.", ephemeral=True)
        # Also edit the original message to remove buttons
        await interaction.message.edit(content="✅ Configuration wiped.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ **Reset Cancelled.**", view=None, embed=None)

# --- BOT CLASS ---
class LeagueBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_db()  # initialize database on startup

    async def on_ready(self):
        # REMOVED automatic global sync – now manual via /sync
        print(f"✅ LOGGED IN AS: {self.user}")

client = LeagueBot()

# --- COMMANDS (all now async and properly deferred) ---

@client.tree.command(name="sync", description="Sync slash commands globally (owner only)")
async def sync_commands(interaction: discord.Interaction):
    """Owner-only command to sync the command tree globally."""
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ You are not the bot owner.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await client.tree.sync()
        await interaction.followup.send("✅ Commands synced globally!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

@client.tree.command(name="help", description="Show bot commands")
async def help_command(interaction: discord.Interaction):
    embed1 = discord.Embed(title="Help - General Commands (Page 1/3)", color=discord.Color.blue())
    embed1.add_field(name="/looking_for_team", value="Post yourself as a Free Agent", inline=False)
    embed1.add_field(name="/demand", value="Leave your current team (Uses Demand Limit)", inline=False)
    embed1.add_field(name="/team_view [role]", value="View a team's roster", inline=False)
    embed1.add_field(name="/free_agents", value="View available players", inline=False)

    embed2 = discord.Embed(title="Help - Manager Commands (Page 2/3)", color=discord.Color.green())
    embed2.add_field(name="/sign [player]", value="Sign a player to your team", inline=False)
    embed2.add_field(name="/release [player]", value="Release a player", inline=False)
    embed2.add_field(name="/transfer [player]", value="Request to buy a player", inline=False)
    embed2.add_field(name="/promote [player]", value="Promote player to Assistant Manager", inline=False)
    embed2.add_field(name="/tm_transfer [player]", value="Transfer Team Ownership to a player", inline=False)
    embed2.add_field(name="/decorate_transactions", value="Set custom transaction card background", inline=False)

    embed3 = discord.Embed(title="Help - Admin Commands (Page 3/3)", color=discord.Color.red())
    embed3.add_field(name="/setup_global", value="Configure bot roles/channels", inline=False)
    embed3.add_field(name="/setup_team", value="Register a new team", inline=False)
    embed3.add_field(name="/team_delete", value="Delete a team", inline=False)
    embed3.add_field(name="/window", value="Open/Close transfer window", inline=False)
    embed3.add_field(name="/reset_config", value="Wipe server configuration", inline=False)
    embed3.add_field(name="/transfer_list", value="View top transfers leaderboard", inline=False)

    view = HelpView([embed1, embed2, embed3])
    await interaction.response.send_message(embed=embed1, view=view, ephemeral=True)

@client.tree.command(name="tm_transfer", description="Transfer Team Ownership to another player")
async def tm_transfer(interaction: discord.Interaction, player: discord.Member):
    await interaction.response.defer(ephemeral=True)
    g_config = await get_global_config(interaction.guild.id)
    if not g_config:
        return await interaction.followup.send("❌ Config not set.", ephemeral=True)

    mgr_role_id = g_config[1]
    mgr_role = interaction.guild.get_role(mgr_role_id)

    if not mgr_role:
        return await interaction.followup.send("❌ Manager role missing from config.", ephemeral=True)

    if mgr_role not in interaction.user.roles:
        return await interaction.followup.send("❌ You are not a Team Manager.", ephemeral=True)

    team_info = await find_user_team(interaction.user)
    if not team_info:
        return await interaction.followup.send("❌ You don't have a team.", ephemeral=True)
    team_role = team_info[0]

    if team_role not in player.roles:
        return await interaction.followup.send("❌ That player is not on your team.", ephemeral=True)

    try:
        await interaction.user.remove_roles(mgr_role)
        await player.add_roles(mgr_role)
        await interaction.followup.send(f"✅ **Ownership Transferred!**\n{interaction.user.mention} ➝ {player.mention}\n{player.mention} is now the Manager of **{team_role.name}**.")
    except Exception as e:
        await interaction.followup.send(f"❌ Role Error: {e}", ephemeral=True)

@client.tree.command(name="reset_config", description="⚠️ WIPE SERVER DATA (Admin Only)")
async def reset_config(interaction: discord.Interaction):
    if not is_staff(interaction):
        return await interaction.response.send_message("❌ Admin Only", ephemeral=True)

    view = ResetView(interaction.guild.id)
    embed = discord.Embed(title="⚠️ DANGER ZONE",
                          description="Are you sure you want to **RESET** the bot configuration for this server?\n\nThis will delete:\n- Global Config (Roles/Channels)\n- Demand Limits\n\n(It will NOT delete Teams or Player Stats)",
                          color=discord.Color.dark_red())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@client.tree.command(name="setup_global", description="Set roles, channels, and limits.")
async def setup_global(interaction: discord.Interaction,
                       manager_role: discord.Role,
                       asst_role: discord.Role,
                       free_agent_role: discord.Role,
                       channel: discord.TextChannel,
                       demand_limit: int = 3):
    if not is_staff(interaction):
        return await interaction.response.send_message("❌ Admin Only", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    current_config = await get_global_config(interaction.guild.id)
    window_state = 1
    if current_config and len(current_config) > 5:
        window_state = current_config[5]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO global_config
            (guild_id, manager_role_id, asst_role_id, contract_channel_id, free_agent_role_id, window_open, demand_limit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (interaction.guild.id, manager_role.id, asst_role.id, channel.id,
              free_agent_role.id, window_state, demand_limit))
        await db.commit()
    await invalidate_config(interaction.guild.id)
    await interaction.followup.send(f"✅ **Config Saved!** (Demand Limit: {demand_limit})", ephemeral=True)

@client.tree.command(name="setup_team", description="Register a Team Role")
async def setup_team(interaction: discord.Interaction, team_role: discord.Role, logo: str, roster_limit: int = 20):
    if not is_staff(interaction):
        return await interaction.response.send_message("❌ Admin Only", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    existing = await get_team_data(team_role.id)
    trans_img = existing[3] if existing and len(existing) > 3 else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO teams VALUES (?, ?, ?, ?)",
                         (team_role.id, logo, roster_limit, trans_img))
        await db.commit()
    await invalidate_team(team_role.id)
    await interaction.followup.send(f"✅ **{team_role.name}** registered!", ephemeral=True)

@client.tree.command(name="team_delete", description="Unregister a team")
async def team_delete(interaction: discord.Interaction, team_role: discord.Role):
    if not is_staff(interaction):
        return await interaction.response.send_message("❌ Admin Only", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM teams WHERE team_role_id = ?", (team_role.id,))
        await db.commit()
    await invalidate_team(team_role.id)
    await interaction.followup.send(f"🗑️ **{team_role.name}** removed.", ephemeral=True)

@client.tree.command(name="window", description="Open/Close Window")
@app_commands.choices(status=[app_commands.Choice(name="Open ✅", value=1),
                              app_commands.Choice(name="Closed ❌", value=0)])
async def window(interaction: discord.Interaction, status: int):
    if not is_staff(interaction):
        return await interaction.response.send_message("❌ Admin Only", ephemeral=True)

    await interaction.response.defer()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE global_config SET window_open = ? WHERE guild_id = ?",
                         (status, interaction.guild.id))
        await db.commit()
    await invalidate_config(interaction.guild.id)

    msg = "✅ **Transfer Window OPEN!**" if status == 1 else "❌ **Transfer Window CLOSED!**"
    await interaction.followup.send(msg)

    conf = await get_global_config(interaction.guild.id)
    if conf and conf[3]:
        chan = interaction.guild.get_channel(conf[3])
        if chan:
            await chan.send(msg)

@client.tree.command(name="decorate_transactions", description="Set custom contract background (Upload Image OR Link)")
async def decorate_transactions(interaction: discord.Interaction,
                                image_file: discord.Attachment = None,
                                url: str = None):
    await interaction.response.defer(ephemeral=True)

    g_config = await get_global_config(interaction.guild.id)
    user_roles = [r.id for r in interaction.user.roles]
    if (g_config[1] not in user_roles) and (g_config[2] not in user_roles) and not interaction.user.guild_permissions.administrator:
        return await interaction.followup.send("❌ Managers or Admins only.", ephemeral=True)

    team_info = await find_user_team(interaction.user)
    if not team_info:
        return await interaction.followup.send("❌ You aren't managing a team.", ephemeral=True)
    team_role, _, _, _ = team_info

    final_url = None
    if url and url.lower() in ["reset", "none", "remove"]:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE teams SET transaction_image = NULL WHERE team_role_id = ?", (team_role.id,))
            await db.commit()
        await invalidate_team(team_role.id)
        return await interaction.followup.send(f"✅ **{team_role.name}** reverted to Proxima Default.", ephemeral=True)

    if image_file:
        if not image_file.content_type.startswith("image/"):
            return await interaction.followup.send("❌ File must be an image.", ephemeral=True)
        final_url = image_file.url
    elif url:
        if not url.startswith("http"):
            return await interaction.followup.send("❌ Invalid Link.", ephemeral=True)
        final_url = url
    else:
        return await interaction.followup.send("❌ Provide an **Image File** OR a **URL**.", ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE teams SET transaction_image = ? WHERE team_role_id = ?", (final_url, team_role.id))
        await db.commit()
    await invalidate_team(team_role.id)

    embed = discord.Embed(title="Background Updated",
                          description="Your future signings will look like this:",
                          color=discord.Color.green())
    embed.set_image(url=final_url)
    await interaction.followup.send(f"✅ **{team_role.name}** custom background set!", embed=embed, ephemeral=True)

@client.tree.command(name="sign", description="Sign a player to YOUR team")
async def sign(interaction: discord.Interaction, player: discord.Member):
    await interaction.response.defer()

    if not await is_window_open(interaction.guild.id):
        return await interaction.followup.send("❌ **Window Closed.**")

    g_config = await get_global_config(interaction.guild.id)
    user_roles = [r.id for r in interaction.user.roles]
    if (g_config[1] not in user_roles) and (g_config[2] not in user_roles):
        return await interaction.followup.send("❌ Not Authorized.")

    team_info = await find_user_team(interaction.user)
    if not team_info:
        return await interaction.followup.send("❌ No team role.")
    team_role, logo, limit, custom_bg = team_info

    if team_role in player.roles:
        return await interaction.followup.send("⚠️ Already on team.")
    if await find_user_team(player):
        return await interaction.followup.send(f"🚫 Player on another team. Use `/transfer`.")
    if len(team_role.members) >= limit:
        return await interaction.followup.send("❌ Roster Full!")

    await player.add_roles(team_role)
    await cleanup_free_agent(interaction.guild, player)
    await update_stat(player.id, "transfer")

    desc = f"The {team_role.mention} have **signed** {player.mention}"
    embed = create_transaction_embed(interaction.guild, f"{team_role.name} Transaction", desc,
                                     discord.Color.blue(), team_role, logo, interaction.user,
                                     len(team_role.members), limit)

    try:
        file = await generate_transaction_card(player, team_role.name, team_role.color,
                                               "OFFICIAL SIGNING", custom_bg)
        embed.set_image(url="attachment://transaction.png")
        await send_to_channel(interaction.guild, embed, file)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Signed, but image error: {e}")
        await send_to_channel(interaction.guild, embed)

    await send_dm(player, content=f"✅ You have been signed to **{team_role.name}**!", embed=embed)
    await interaction.followup.send("✅ Player Signed!")

@client.tree.command(name="release", description="Release a player")
async def release(interaction: discord.Interaction, player: discord.Member):
    await interaction.response.defer()

    if not await is_window_open(interaction.guild.id):
        return await interaction.followup.send("❌ Window Closed.")

    team_info = await find_user_team(interaction.user)
    if not team_info:
        return await interaction.followup.send("❌ No team.")
    team_role, logo, limit, custom_bg = team_info

    if team_role not in player.roles:
        return await interaction.followup.send("⚠️ Player not on team.")

    await player.remove_roles(team_role)

    desc = f"The **{team_role.name}** have **released** {player.mention}"
    embed = create_transaction_embed(interaction.guild, f"{team_role.name} Transaction", desc,
                                     discord.Color.red(), team_role, logo, interaction.user,
                                     len(team_role.members), limit)

    try:
        file = await generate_transaction_card(player, team_role.name, team_role.color,
                                               "OFFICIAL RELEASE", custom_bg)
        embed.set_image(url="attachment://transaction.png")
        await send_to_channel(interaction.guild, embed, file)
    except:
        await send_to_channel(interaction.guild, embed)

    await send_dm(player, content=f"⚠️ Released from **{team_role.name}**.", embed=embed)
    await interaction.followup.send("✅ Released!")

@client.tree.command(name="demand", description="Leave your current team (Uses Demand Limit)")
async def demand(interaction: discord.Interaction):
    await interaction.response.defer()

    team_info = await find_user_team(interaction.user)
    if not team_info:
        return await interaction.followup.send("❌ Not in a team.")
    team_role, logo, limit, _ = team_info

    g_conf = await get_global_config(interaction.guild.id)
    demand_limit = g_conf[6] if g_conf and len(g_conf) > 6 else 3
    stats = await get_player_stats(interaction.user.id)
    demands_used = stats[2]

    if demands_used >= demand_limit:
        return await interaction.followup.send(f"🚫 **Demand Limit Reached!** ({demands_used}/{demand_limit})\nYou cannot leave your team.")

    await interaction.user.remove_roles(team_role)
    await update_stat(interaction.user.id, "demand")
    demands_left = demand_limit - (demands_used + 1)

    config = await get_global_config(interaction.guild.id)
    if config and config[4]:
        fa_role = interaction.guild.get_role(config[4])
        if fa_role:
            await interaction.user.add_roles(fa_role)

    desc = f"{interaction.user.mention} has **Demanded Release** from the team.\n\n⚠️ **Demands Left:** {demands_left}"
    embed = create_transaction_embed(interaction.guild, "Transfer Demand", desc,
                                     discord.Color.dark_grey(), team_role, logo, None,
                                     len(team_role.members), limit)
    await send_to_channel(interaction.guild, embed)

    heads, assts = await get_managers_of_team(interaction.guild, team_role)
    for mgr in heads + assts:
        await send_dm(mgr, content=f"📢 {interaction.user.name} has left your team.")

    await interaction.followup.send(f"👋 Left **{team_role.name}**.\nDemands remaining: {demands_left}")

@client.tree.command(name="promote", description="Promote a player to Assistant Manager")
async def promote(interaction: discord.Interaction, player: discord.Member):
    await interaction.response.defer()

    g_config = await get_global_config(interaction.guild.id)
    user_roles = [r.id for r in interaction.user.roles]
    if g_config[1] not in user_roles and not interaction.user.guild_permissions.administrator:
        return await interaction.followup.send("❌ Head Managers only.")

    team_info = await find_user_team(interaction.user)
    if not team_info:
        return await interaction.followup.send("❌ You aren't managing a team.")
    team_role = team_info[0]

    if team_role not in player.roles:
        return await interaction.followup.send("❌ Player is not on your team.")

    asst_role_id = g_config[2]
    asst_role = interaction.guild.get_role(asst_role_id)
    if not asst_role:
        return await interaction.followup.send("❌ Assistant Role not configured.")

    await player.add_roles(asst_role)
    await interaction.followup.send(f"✅ Promoted {player.mention} to **Assistant Manager** of {team_role.name}!")

@client.tree.command(name="transfer_list", description="Show top players by transfer count")
async def transfer_list(interaction: discord.Interaction):
    if not is_staff(interaction):
        return await interaction.response.send_message("❌ Admin Only", ephemeral=True)

    await interaction.response.defer()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, transfers FROM player_stats ORDER BY transfers DESC LIMIT 15") as cursor:
            data = await cursor.fetchall()

    if not data:
        return await interaction.followup.send("No transfer history found.")

    embed = discord.Embed(title="📊 Most Transfers", color=discord.Color.gold())
    desc = ""
    for idx, (uid, count) in enumerate(data, 1):
        user = interaction.guild.get_member(uid)
        name = user.name if user else f"Unknown ({uid})"
        desc += f"**{idx}.** {name} — {count} Transfers\n"

    embed.description = desc
    await interaction.followup.send(embed=embed)

@client.tree.command(name="looking_for_team", description="Post yourself as a Free Agent")
@app_commands.choices(
    region=[app_commands.Choice(name="Asia", value="ASIA"),
            app_commands.Choice(name="Europe", value="EU"),
            app_commands.Choice(name="NA", value="NA"),
            app_commands.Choice(name="SA", value="SA")],
    position=[app_commands.Choice(name="ST", value="ST"),
              app_commands.Choice(name="MF", value="MF"),
              app_commands.Choice(name="DF", value="DF"),
              app_commands.Choice(name="GK", value="GK")])
async def looking_for_team(interaction: discord.Interaction, region: str, position: str, description: str):
    await interaction.response.defer(ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO free_agents VALUES (?, ?, ?, ?, ?)",
                         (interaction.user.id, region, position, description, str(datetime.datetime.now())))
        await db.commit()

    config = await get_global_config(interaction.guild.id)
    if config and config[4]:
        role = interaction.guild.get_role(config[4])
        if role:
            await interaction.user.add_roles(role)

    await interaction.followup.send(f"✅ Listed as **Free Agent** ({region} - {position})!", ephemeral=True)

@client.tree.command(name="free_agents", description="View available players")
async def free_agents(interaction: discord.Interaction):
    await interaction.response.defer()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM free_agents") as cursor:
            agents = await cursor.fetchall()

    if not agents:
        return await interaction.followup.send("🤷‍♂️ No Free Agents currently listed.")

    embed = discord.Embed(title="📄 Free Agency Market", color=discord.Color.teal())
    count = 0
    for agent in agents:
        uid, reg, pos, desc, _ = agent
        member = interaction.guild.get_member(uid)
        if member:
            embed.add_field(name=f"{pos} | {member.name} ({reg})", value=f"📝 {desc}", inline=False)
            count += 1
            if count >= 20:
                embed.set_footer(text="Showing first 20 agents...")
                break
    await interaction.followup.send(embed=embed)

@client.tree.command(name="team_list", description="List teams (Admin)")
async def team_list(interaction: discord.Interaction):
    if not is_staff(interaction):
        return await interaction.response.send_message("❌ Admin Only", ephemeral=True)

    await interaction.response.defer()

    g_conf = await get_global_config(interaction.guild.id)
    mgr_id = g_conf[1] if g_conf else 0
    asst_id = g_conf[2] if g_conf else 0

    all_teams = await get_all_teams()
    if not all_teams:
        return await interaction.followup.send("❌ No teams.")

    embed = discord.Embed(title="🏆 Registered Teams List", color=discord.Color.gold())
    for t_data in all_teams:
        role_id = t_data[0]
        logo = t_data[1]
        team_role = interaction.guild.get_role(role_id)
        if not team_role:
            continue
        header_emoji = logo if (logo and "http" not in logo) else "🛡️"
        members_formatted = format_roster_list(team_role.members, mgr_id, asst_id)
        player_str = "\n".join(members_formatted) if members_formatted else "*No players.*"
        embed.add_field(name=f"{header_emoji} {team_role.name} ({len(team_role.members)})",
                        value=player_str, inline=False)
    await interaction.followup.send(embed=embed)

@client.tree.command(name="team_view", description="View a specific team's roster")
async def team_view(interaction: discord.Interaction, team: discord.Role):
    await interaction.response.defer(ephemeral=True)

    data = await get_team_data(team.id)
    if not data:
        return await interaction.followup.send("❌ Not a registered team.", ephemeral=True)

    g_conf = await get_global_config(interaction.guild.id)
    mgr_id = g_conf[1] if g_conf else 0
    asst_id = g_conf[2] if g_conf else 0
    logo = data[1]
    header_emoji = logo if (logo and "http" not in logo) else "🛡️"
    members_formatted = format_roster_list(team.members, mgr_id, asst_id)
    player_str = "\n".join(members_formatted) if members_formatted else "*No players.*"

    embed = discord.Embed(title=f"{header_emoji} {team.name} Roster", color=team.color)
    if logo and "http" in logo:
        embed.set_thumbnail(url=logo)
    embed.description = player_str
    embed.set_footer(text=f"Total: {len(team.members)}")
    await interaction.followup.send(embed=embed, ephemeral=True)

@client.tree.command(name="transfer", description="Request to sign a player")
async def transfer(interaction: discord.Interaction, player: discord.Member):
    await interaction.response.defer(ephemeral=True)

    if not await is_window_open(interaction.guild.id):
        return await interaction.followup.send("❌ **Window CLOSED.**", ephemeral=True)

    my_team_info = await find_user_team(interaction.user)
    if not my_team_info:
        return await interaction.followup.send("❌ Not a manager.", ephemeral=True)
    my_team_role, my_logo, _, _ = my_team_info

    target_team_info = await find_user_team(player)
    if not target_team_info:
        return await interaction.followup.send("⚠️ Player not on a team.", ephemeral=True)
    target_team_role, _, _, _ = target_team_info

    if my_team_role.id == target_team_role.id:
        return await interaction.followup.send("⚠️ Already on your team!", ephemeral=True)

    heads, assts = await get_managers_of_team(interaction.guild, target_team_role)
    target_manager = heads[0] if heads else (assts[0] if assts else None)
    if not target_manager:
        return await interaction.followup.send(f"❌ **{target_team_role.name}** has no active Manager.", ephemeral=True)

    view = TransferView(interaction.guild, player, target_team_role, my_team_role, interaction.user, my_logo)
    dm_embed = discord.Embed(title="Transfer Offer 📝", color=discord.Color.gold())
    dm_embed.description = f"**{interaction.user.mention}** wants to buy **{player.name}**.\nDo you accept?"

    if await send_dm(target_manager, embed=dm_embed, view=view):
        await interaction.followup.send(f"✅ **Offer Sent!** Waiting for {target_manager.mention}.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Could not DM manager.", ephemeral=True)

@client.tree.command(name="test_card", description="TEST: Generates a sample signing card")
async def test_card(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        color = interaction.user.top_role.color
        if color == discord.Color.default():
            color = discord.Color.dark_grey()
        file = await generate_transaction_card(interaction.user, "Test Team", color, "TEST CARD")
        await interaction.followup.send("🖼️ **Test Image Generation:**", file=file)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# --- ERROR HANDLER (unchanged) ---
@client.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"⏳ **Take a breather!** Try again in {int(error.retry_after)}s.", ephemeral=True)
    elif isinstance(error, app_commands.BotMissingPermissions):
        await interaction.response.send_message("❌ I don't have permission to do that here.", ephemeral=True)
    else:
        print(f"⚠️ ERROR: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ **System Busy:** Please wait a moment and try again.", ephemeral=True)
        except:
            pass

# --- STARTUP ---
print("System: Loading Proxima V19 (Async DB, threaded images, manual sync)...")
if TOKEN:
    try:
        keep_alive()
        client.run(TOKEN)
    except Exception as e:
        print(f"❌ Error: {e}")