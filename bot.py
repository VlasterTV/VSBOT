"""
=======================================================================
  VLASTER STUDIO — Fortnite / UEFN community bot
=======================================================================
  Requirements : discord.py >= 2.3.2
  Run          : python bot.py

  Environment variables
    DISCORD_BOT_TOKEN   (required)    — the bot token
    DISCORD_GUILD_ID    (recommended) — server ID, instant command sync
    PORT                (automatic)   — provided by Render
=======================================================================
"""

import os
import json
import time
import asyncio
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Dict

import discord
from discord.ext import commands
from discord import app_commands

# ======================================================================
#  CONFIGURATION
# ======================================================================

TOKEN = (os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()
GUILD_ID = (os.getenv("DISCORD_GUILD_ID") or os.getenv("GUILD_ID") or "").strip() or None

STUDIO_NAME = "VlasterStudio"

# Studio logo shown on every embed.
# Upload the image once to any Discord channel, right-click it → "Copy Link",
# then paste that cdn.discordapp.com URL here (or set the LOGO_URL env var).
# Leave empty to fall back to the server icon.
LOGO_URL = os.getenv("LOGO_URL", "").strip()

# --- Roles ---
R_ADMIN = "👑 Admin"
R_COWORKER = "🛠️ Co-Worker"
R_PLAYERS = "🎮 Players"
R_UNVERIFIED = "🌑 Unverified"

# --- Colours (0x000000 is treated as "no colour" by Discord) ---
C_ADMIN = 0xFF0A3C       # Neon red — studio identity
C_COWORKER = 0xFF8A3D     # Orange
C_PLAYERS = 0x4FC3F7      # Light blue
C_UNVERIFIED = 0x2B2D31   # Muted grey
C_BRAND = 0xFF0A3C        # Embed colour

# --- Anti-raid ---
MIN_ACCOUNT_AGE_DAYS = 3
RAID_JOIN_COUNT = 8
RAID_JOIN_WINDOW = 20
LOCKDOWN_DURATION = 300

DATA_FILE = "vlaster_data.json"

# ======================================================================
#  PERSISTENCE
# ======================================================================


def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"ticket_counter": 0, "guilds": {}}


def save_data(data: dict) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


DATA = load_data()


def guild_conf(guild_id: int) -> dict:
    return DATA["guilds"].setdefault(str(guild_id), {})


# ======================================================================
#  BOT
# ======================================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class VlasterBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.join_log: Dict[int, deque] = {}
        self.lockdown: Dict[int, float] = {}

    async def setup_hook(self):
        # Persistent views: buttons survive restarts
        self.add_view(VerifyView())
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())

        # Clear commands inherited from older code under this application
        self.tree.clear_commands(guild=None)

        synced_to_guild = False
        if GUILD_ID:
            try:
                guild = discord.Object(id=int(GUILD_ID))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"→ Commands synced to guild {GUILD_ID}.")
                synced_to_guild = True
            except discord.Forbidden:
                print(
                    "[CONFIG] Guild sync refused (Missing Access).\n"
                    f"         Either the bot is not in guild {GUILD_ID}, or it was invited\n"
                    "         without the 'applications.commands' scope.\n"
                    "         Re-invite it from OAuth2 → URL Generator with BOTH\n"
                    "         'bot' and 'applications.commands' ticked.\n"
                    "         Falling back to a global sync."
                )
            except ValueError:
                print(f"[CONFIG] DISCORD_GUILD_ID is not a valid ID: {GUILD_ID!r}")

        if not synced_to_guild:
            try:
                await self.tree.sync()
                print("→ Commands synced globally (up to 1h propagation delay).")
            except discord.HTTPException as exc:
                print(f"[CONFIG] Global sync failed: {exc}")


bot = VlasterBot()


@bot.event
async def on_ready():
    print(f"→ Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.playing, name="Fortnite • VlasterStudio"
        )
    )


# ======================================================================
#  HELPERS
# ======================================================================


def get_role(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    return discord.utils.get(guild.roles, name=name)


def is_staff(member: discord.Member) -> bool:
    names = {r.name for r in member.roles}
    return bool({R_ADMIN, R_COWORKER} & names) or member.guild_permissions.administrator


def brand_embed(title: str, description: str = "", colour: int = C_BRAND) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=discord.Colour(colour))


def logo_url(guild: Optional[discord.Guild] = None) -> Optional[str]:
    """Studio logo if configured, otherwise the server icon."""
    if LOGO_URL:
        return LOGO_URL
    if guild and guild.icon:
        return guild.icon.url
    return None


def brand(embed: discord.Embed, guild: Optional[discord.Guild] = None,
          footer: Optional[str] = None) -> discord.Embed:
    """Applies the studio logo as thumbnail, and optionally a branded footer."""
    url = logo_url(guild)
    if url:
        embed.set_thumbnail(url=url)
    if footer:
        embed.set_footer(text=footer, icon_url=url) if url else embed.set_footer(text=footer)
    return embed


async def log_event(guild: discord.Guild, embed: discord.Embed) -> None:
    channel_id = guild_conf(guild.id).get("log_channel")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


# ======================================================================
#  VIEW — VERIFICATION
# ======================================================================


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join the community",
        emoji="🎮",
        style=discord.ButtonStyle.success,
        custom_id="vlaster:verify",
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        role_players = get_role(guild, R_PLAYERS)
        role_unverified = get_role(guild, R_UNVERIFIED)

        if role_players is None:
            return await interaction.response.send_message(
                "⚠️ The Players role could not be found. An admin needs to run `/setup` again.",
                ephemeral=True,
            )

        if role_players in member.roles:
            return await interaction.response.send_message(
                "✅ You already have access to the server.", ephemeral=True
            )

        # Anti-raid lockdown
        if bot.lockdown.get(guild.id, 0) > time.time():
            return await interaction.response.send_message(
                "🛡️ The server is in **protection mode** after a wave of suspicious joins. "
                "Try again in a few minutes.",
                ephemeral=True,
            )

        # Account age check
        age = discord.utils.utcnow() - member.created_at
        if age.days < MIN_ACCOUNT_AGE_DAYS:
            await log_event(
                guild,
                brand_embed(
                    "🚫 Verification denied",
                    f"{member.mention} — account created **{age.days} day(s)** ago.",
                ),
            )
            return await interaction.response.send_message(
                f"🚫 Your Discord account is too new (**{age.days} day(s)**).\n"
                f"A minimum of **{MIN_ACCOUNT_AGE_DAYS} days** is required.",
                ephemeral=True,
            )

        try:
            await member.add_roles(role_players, reason="Verification passed")
            if role_unverified and role_unverified in member.roles:
                await member.remove_roles(role_unverified, reason="Verification passed")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⚠️ I don't have the required permissions. My role must sit **above** "
                "the roles I assign.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "🎉 **Welcome to the studio!**\n"
            "You now have access to the whole server. Check the announcements and "
            "code drop channels so you don't miss anything.",
            ephemeral=True,
        )

        await log_event(
            guild,
            brand_embed(
                "✅ New member verified",
                f"{member.mention} • `{member}`\nAccount created **{age.days} days** ago.",
                C_PLAYERS,
            ),
        )


# ======================================================================
#  VIEWS — TICKETS
# ======================================================================

TICKET_TYPES = {
    "bug": {
        "label": "Report a bug",
        "emoji": "🐛",
        "desc": "Something is broken on a map or game mode",
        "prefix": "bug",
        "intro": (
            "Thanks for helping us improve the maps. Please tell us:\n"
            "> **1.** The island code of the map\n"
            "> **2.** What happened, and what should have happened\n"
            "> **3.** How to reproduce it\n"
            "> **4.** A screenshot or clip, if you have one\n\n"
            "We'll look into it as soon as we can."
        ),
    },
    "suggestion": {
        "label": "Share an idea",
        "emoji": "💡",
        "desc": "A suggestion for a map, mode or feature",
        "prefix": "idea",
        "intro": (
            "The best ideas often come from the community. Tell us:\n"
            "> **1.** Your idea in one sentence\n"
            "> **2.** What it would bring to players\n"
            "> **3.** A reference or example, if you have one\n\n"
            "We read everything, even if we can't build it all."
        ),
    },
    "collab": {
        "label": "Collaboration",
        "emoji": "🤝",
        "desc": "Creator, partnership or joint project",
        "prefix": "collab",
        "intro": (
            "Glad you want to work with us. Let us know:\n"
            "> **1.** Who you are and what you do\n"
            "> **2.** Your past work or portfolio\n"
            "> **3.** What exactly you're proposing\n\n"
            "Someone from the studio will get back to you."
        ),
    },
    "help": {
        "label": "Need help",
        "emoji": "❓",
        "desc": "A question about the server or the maps",
        "prefix": "help",
        "intro": (
            "Ask your question here and we'll answer as soon as we're around.\n\n"
            "If it's about a specific map, remember to include its island code."
        ),
    },
}


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="📩 Pick a reason for your ticket…",
        custom_id="vlaster:ticket_select",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label=v["label"], value=k, emoji=v["emoji"], description=v["desc"]
            )
            for k, v in TICKET_TYPES.items()
        ],
    )
    async def select_ticket(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True, thinking=True)

        kind = select.values[0]
        cfg = TICKET_TYPES[kind]
        guild = interaction.guild
        member = interaction.user

        # One open ticket at a time
        existing = discord.utils.find(
            lambda c: c.topic == f"ticket-owner:{member.id}", guild.text_channels
        )
        if existing:
            return await interaction.followup.send(
                f"⚠️ You already have an open ticket: {existing.mention}", ephemeral=True
            )

        category = guild.get_channel(guild_conf(guild.id).get("ticket_category", 0))
        if category is None:
            return await interaction.followup.send(
                "⚠️ Ticket category not found. An admin needs to run `/setup` again.",
                ephemeral=True,
            )

        role_admin = get_role(guild, R_ADMIN)
        role_coworker = get_role(guild, R_COWORKER)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        for role in (role_admin, role_coworker):
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True
                )

        DATA["ticket_counter"] += 1
        number = DATA["ticket_counter"]
        save_data(DATA)

        channel = await guild.create_text_channel(
            name=f"{cfg['emoji']}・{cfg['prefix']}-{number:04d}",
            category=category,
            overwrites=overwrites,
            topic=f"ticket-owner:{member.id}",
            reason=f"Ticket {kind} opened by {member}",
        )

        embed = brand_embed(
            f"{cfg['emoji']}  {cfg['label']}  •  Ticket #{number:04d}", cfg["intro"]
        )
        embed.set_footer(text=f"Opened by {member}", icon_url=member.display_avatar.url)
        brand(embed, guild)
        embed.timestamp = discord.utils.utcnow()

        ping = role_coworker.mention if role_coworker else ""
        await channel.send(
            content=f"{member.mention} {ping}",
            embed=embed,
            view=TicketControlView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )

        await interaction.followup.send(
            f"✅ Your ticket has been created: {channel.mention}", ephemeral=True
        )

        await log_event(
            guild,
            brand_embed(
                "🎟️ Ticket opened",
                f"**{cfg['label']}** — {channel.mention}\nBy {member.mention}",
            ),
        )


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim",
        emoji="🙋",
        style=discord.ButtonStyle.primary,
        custom_id="vlaster:ticket_claim",
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "⛔ Staff only.", ephemeral=True
            )

        button.disabled = True
        button.label = f"Claimed by {interaction.user.display_name}"
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(
            embed=brand_embed(
                "", f"🙋 {interaction.user.mention} is handling this ticket.", C_COWORKER
            )
        )

    @discord.ui.button(
        label="Close",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="vlaster:ticket_close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=brand_embed(
                "", "⚠️ **Confirm closing?** The channel will be deleted in 5 seconds."
            ),
            view=ConfirmCloseView(),
        )


class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="🔒 Closing ticket…", embed=None, view=None
        )
        await log_event(
            interaction.guild,
            brand_embed(
                "🔒 Ticket closed",
                f"`#{interaction.channel.name}` closed by {interaction.user.mention}",
            ),
        )
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Closing cancelled.", embed=None, view=None
        )


# ======================================================================
#  EVENTS
# ======================================================================


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    # Join-wave detection
    log = bot.join_log.setdefault(guild.id, deque())
    now = time.time()
    log.append(now)
    while log and now - log[0] > RAID_JOIN_WINDOW:
        log.popleft()

    if len(log) >= RAID_JOIN_COUNT and bot.lockdown.get(guild.id, 0) < now:
        bot.lockdown[guild.id] = now + LOCKDOWN_DURATION
        await log_event(
            guild,
            brand_embed(
                "🛡️ ANTI-RAID LOCKDOWN ENABLED",
                f"**{len(log)} joins** in {RAID_JOIN_WINDOW}s detected.\n"
                f"Verification blocked for **{LOCKDOWN_DURATION // 60} minutes**.",
            ),
        )

    # Unverified role
    role_unverified = get_role(guild, R_UNVERIFIED)
    if role_unverified:
        try:
            await member.add_roles(role_unverified, reason="New join")
        except discord.Forbidden:
            pass

    # Public welcome message
    channel_id = guild_conf(guild.id).get("welcome_channel")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            e = brand_embed(
                "🎮  A new player just dropped in",
                f"{member.mention} just joined **{guild.name}**.\n"
                f"We're now **{guild.member_count}** in the studio.",
            )
            e.set_thumbnail(url=member.display_avatar.url)
            url = logo_url(guild)
            if url:
                e.set_author(name=STUDIO_NAME, icon_url=url)
            try:
                await channel.send(embed=e)
            except discord.HTTPException:
                pass

    age = (discord.utils.utcnow() - member.created_at).days
    await log_event(
        guild,
        brand_embed(
            "📥 Join",
            f"{member.mention} • `{member}`\nAccount is **{age} day(s)** old.",
            C_PLAYERS if age >= MIN_ACCOUNT_AGE_DAYS else 0xE67E22,
        ),
    )


@bot.event
async def on_member_remove(member: discord.Member):
    await log_event(
        member.guild,
        brand_embed("📤 Leave", f"`{member}` left the server.", C_UNVERIFIED),
    )


# ======================================================================
#  /setup COMMAND
# ======================================================================


@bot.tree.command(name="setup", description="Builds the whole VlasterStudio server.")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    guild = interaction.guild
    await interaction.response.send_message(
        "⚙️ Building the server, hang tight…", ephemeral=True
    )

    # ------------------------------------------------------------------
    #  1. ROLES
    # ------------------------------------------------------------------
    perms_admin = discord.Permissions(administrator=True)
    perms_coworker = discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True,
        attach_files=True, embed_links=True, add_reactions=True,
        connect=True, speak=True, manage_messages=True, mute_members=True,
        move_members=True, kick_members=True,
    )
    perms_players = discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True,
        attach_files=True, embed_links=True, add_reactions=True,
        connect=True, speak=True, stream=True, use_voice_activation=True,
    )
    perms_none = discord.Permissions.none()

    role_specs = [
        (R_ADMIN, C_ADMIN, perms_admin, True),
        (R_COWORKER, C_COWORKER, perms_coworker, True),
        (R_PLAYERS, C_PLAYERS, perms_players, False),
        (R_UNVERIFIED, C_UNVERIFIED, perms_none, False),
    ]

    roles: Dict[str, discord.Role] = {}
    for name, colour, perms, hoist in role_specs:
        existing = get_role(guild, name)
        if existing:
            roles[name] = existing
            continue
        roles[name] = await guild.create_role(
            name=name, colour=discord.Colour(colour), permissions=perms,
            hoist=hoist, mentionable=True, reason="VlasterStudio setup",
        )
        await asyncio.sleep(0.3)

    r_admin = roles[R_ADMIN]
    r_cowork = roles[R_COWORKER]
    r_players = roles[R_PLAYERS]
    r_unverif = roles[R_UNVERIFIED]

    # ------------------------------------------------------------------
    #  2. Lock down @everyone
    # ------------------------------------------------------------------
    try:
        await guild.default_role.edit(
            permissions=discord.Permissions(
                view_channel=False, read_message_history=False,
                send_messages=False, connect=False,
            ),
            reason="Setup: global lockdown",
        )
    except discord.Forbidden:
        pass

    # ------------------------------------------------------------------
    #  3. PERMISSION TEMPLATES
    # ------------------------------------------------------------------
    DENY = discord.PermissionOverwrite(view_channel=False)
    SEE = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
    TALK = discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=True,
        attach_files=True, embed_links=True, add_reactions=True,
    )
    READONLY = discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=False,
        add_reactions=True,
    )
    VOICE = discord.PermissionOverwrite(
        view_channel=True, connect=True, speak=True, stream=True,
        use_voice_activation=True,
    )
    STAFF = discord.PermissionOverwrite(
        view_channel=True, read_message_history=True, send_messages=True,
        manage_messages=True, connect=True, speak=True,
    )

    ow_verify = {
        guild.default_role: DENY,
        r_unverif: discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False
        ),
        r_players: DENY,
        r_admin: SEE,
    }
    ow_read = {guild.default_role: DENY, r_unverif: DENY, r_players: READONLY,
               r_cowork: STAFF, r_admin: STAFF}
    ow_talk = {guild.default_role: DENY, r_unverif: DENY, r_players: TALK,
               r_cowork: STAFF, r_admin: STAFF}
    ow_voice = {guild.default_role: DENY, r_unverif: DENY, r_players: VOICE,
                r_cowork: STAFF, r_admin: STAFF}
    ow_staff = {guild.default_role: DENY, r_unverif: DENY, r_players: DENY,
                r_cowork: STAFF, r_admin: STAFF}

    # ------------------------------------------------------------------
    #  4. CATEGORIES & CHANNELS
    # ------------------------------------------------------------------
    async def purge_bot_messages(channel):
        """Prevents duplicates when /setup is run again."""
        try:
            async for msg in channel.history(limit=50):
                if msg.author.id == guild.me.id:
                    await msg.delete()
                    await asyncio.sleep(0.4)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def make_category(name, overwrites):
        existing = discord.utils.get(guild.categories, name=name)
        if existing:
            try:
                await existing.edit(overwrites=overwrites)
            except discord.Forbidden:
                pass
            return existing
        cat = await guild.create_category(name, overwrites=overwrites, reason="Setup")
        await asyncio.sleep(0.3)
        return cat

    async def make_text(category, name, overwrites, topic=None):
        existing = discord.utils.get(guild.text_channels, name=name.lower())
        if existing:
            await purge_bot_messages(existing)
            return existing
        ch = await guild.create_text_channel(
            name, category=category, overwrites=overwrites, topic=topic, reason="Setup"
        )
        await asyncio.sleep(0.3)
        return ch

    async def make_voice(category, name, overwrites):
        existing = discord.utils.get(guild.voice_channels, name=name)
        if existing:
            return existing
        ch = await guild.create_voice_channel(
            name, category=category, overwrites=overwrites, reason="Setup"
        )
        await asyncio.sleep(0.3)
        return ch

    # --- 🔓 ACCESS ---
    cat_verif = await make_category("🔓・ACCESS", ow_verify)
    ch_verif = await make_text(cat_verif, "╰🎮・verification", ow_verify)

    # --- 📌 INFORMATION ---
    cat_info = await make_category("📌・INFORMATION", ow_read)
    ch_welcome = await make_text(cat_info, "├👋・welcome", ow_read)
    ch_rules = await make_text(cat_info, "├📜・rules", ow_read)
    ch_news = await make_text(cat_info, "├📣・announcements", ow_read)
    ch_codes = await make_text(cat_info, "╰🎯・code-drop", ow_read)

    # --- 💬 COMMUNITY ---
    cat_com = await make_category("💬・COMMUNITY", ow_talk)
    await make_text(cat_com, "├🗨️・general-chat", ow_talk)
    await make_text(cat_com, "├🤖・bot-commands", ow_talk)
    await make_text(cat_com, "╰🔗・socials", ow_read)

    # --- 📸 SHOWCASE ---
    cat_media = await make_category("📸・SHOWCASE", ow_talk)
    await make_text(cat_media, "├🖼️・screenshots", ow_talk)
    await make_text(cat_media, "├🎬・clips", ow_talk)
    await make_text(cat_media, "╰💡・suggestions", ow_talk)

    # --- 🎟️ SUPPORT ---
    cat_ticket = await make_category("🎟️・SUPPORT", ow_read)
    ch_open = await make_text(cat_ticket, "╰📩・open-a-ticket", ow_read)

    # --- 🔊 VOICE ---
    cat_voice = await make_category("🔊・VOICE", ow_voice)
    await make_voice(cat_voice, "├🎙️・Lounge", ow_voice)
    await make_voice(cat_voice, "├🎮・Game 1", ow_voice)
    await make_voice(cat_voice, "├🎮・Game 2", ow_voice)
    await make_voice(cat_voice, "╰🎧・AFK", ow_voice)

    # --- 🛠️ STAFF ---
    cat_staff = await make_category("🛠️・STAFF", ow_staff)
    ch_staff = await make_text(cat_staff, "├🔒・staff-chat", ow_staff)
    ch_logs = await make_text(cat_staff, "├📊・logs", ow_staff)
    await make_voice(cat_staff, "╰🔊・Staff Voice", ow_staff)

    # ------------------------------------------------------------------
    #  5. CONFIG
    # ------------------------------------------------------------------
    conf = guild_conf(guild.id)
    conf.update({
        "ticket_category": cat_ticket.id,
        "log_channel": ch_logs.id,
        "verify_channel": ch_verif.id,
        "welcome_channel": ch_welcome.id,
    })
    save_data(DATA)

    # ------------------------------------------------------------------
    #  6. MESSAGES
    # ------------------------------------------------------------------

    # --- Verification ---
    e = brand_embed(
        "🔓  SERVER ACCESS",
        f"Welcome to **{guild.name}**.\n\n"
        "One click on the button below unlocks the whole server: map codes, "
        "announcements, voice channels and the community.\n\n"
        f"> ⏳ Your Discord account must be at least **{MIN_ACCOUNT_AGE_DAYS} days** old\n"
        "> 🛡️ Access is paused for a few minutes during unusual join activity\n"
        "> 🎮 You'll automatically receive the **Players** role",
    )
    brand(e, guild, footer="Anti-raid protection enabled")
    await ch_verif.send(embed=e, view=VerifyView())

    # --- Welcome ---
    e = brand_embed(
        f"👋  Welcome to {STUDIO_NAME}",
        "You've just joined the studio's official server.\n\n"
        "**What you'll find here:**\n"
        f"> 🎯 Our map codes, first in {ch_codes.mention}\n"
        f"> 📣 Releases and updates in {ch_news.mention}\n"
        "> 🖼️ A place to share your screenshots and clips\n"
        "> 💡 A suggestions channel where your ideas actually get read\n"
        "> 🔊 Voice channels to play together\n\n"
        "**Getting started:**\n"
        f"> **1.** Read the {ch_rules.mention}\n"
        f"> **2.** Grab a code from {ch_codes.mention}\n"
        "> **3.** Drop by general chat and say hi\n\n"
        "Have fun. 🎮",
    )
    brand(e, guild)
    await ch_welcome.send(embed=e)

    # --- Rules ---
    e = brand_embed("📜  SERVER RULES", "Staying on this server means you accept these rules.")
    e.add_field(
        name="1️⃣  Respect first",
        value="No insults, no discriminatory language, no harassment. "
              "The studio is a welcoming space and we intend to keep it that way.",
        inline=False,
    )
    e.add_field(
        name="2️⃣  No spam, no advertising",
        value="Neither in channels nor in DMs. Sharing your own content belongs in the "
              "dedicated channels, in moderation.",
        inline=False,
    )
    e.add_field(
        name="3️⃣  Use the right channel",
        value="Screenshots in screenshots, ideas in suggestions. "
              "It keeps the server readable for everyone.",
        inline=False,
    )
    e.add_field(
        name="4️⃣  Keep it appropriate",
        value="Nothing NSFW, shocking or illegal. The server welcomes players of all ages.",
        inline=False,
    )
    e.add_field(
        name="5️⃣  Bugs go through a ticket",
        value=f"Found an issue on a map? Open a ticket in {ch_open.mention} rather than "
              "posting publicly — we'll handle it faster.",
        inline=False,
    )
    e.add_field(
        name="6️⃣  Discord Terms",
        value="Discord's [Terms of Service](https://discord.com/terms) apply. 13+ only.",
        inline=False,
    )
    brand(e, guild, footer="Staff may sanction any disruptive behaviour.")
    await ch_rules.send(embed=e)

    # --- Announcements ---
    e = brand_embed(
        "📣  ANNOUNCEMENTS",
        "This channel is reserved for official studio announcements: map releases, "
        "updates, events and giveaways.\n\n"
        "Turn on notifications for this channel so you don't miss anything.",
    )
    brand(e, guild)
    await ch_news.send(embed=e)

    # --- Code drop ---
    e = brand_embed(
        "🎯  CODE DROP",
        "Every map code from the studio gets posted here.\n\n"
        "**How to play:**\n"
        "> **1.** Launch Fortnite and open **Discover**\n"
        "> **2.** Click the search bar in the top right\n"
        "> **3.** Enter the island code\n"
        "> **4.** Start the game\n\n"
        "Found a bug or got feedback on a map? Open a ticket — it helps us a lot.",
    )
    brand(e, guild)
    await ch_codes.send(embed=e)

    # --- Ticket panel ---
    e = brand_embed(
        "📩  OPEN A TICKET",
        "**Need to reach us?**\n\n"
        "Pick a reason from the menu below. A private channel opens between you and "
        "the studio team.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🐛 **Report a bug**\n"
        "> Something isn't working on a map. Share the code and describe the issue.\n\n"
        "💡 **Share an idea**\n"
        "> A suggestion for a map, mode or improvement. We read everything.\n\n"
        "🤝 **Collaboration**\n"
        "> You're a creator and want to work with us. Introduce yourself.\n\n"
        "❓ **Need help**\n"
        "> A question about the server or the maps.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "> 🔒 Your ticket is only visible to you and the staff\n"
        "> ⚠️ One open ticket at a time",
    )
    brand(e, guild, footer="Pick a reason from the menu below ⬇️")
    await ch_open.send(embed=e, view=TicketPanelView())

    # --- Staff ---
    e = brand_embed(
        "🔒  Staff channel",
        "Internal coordination.\n\n"
        "**Available commands:**\n"
        "`/setup` — rebuilds the server (safe to re-run)\n"
        "`/panel` — resends the ticket panel in the current channel\n"
        "`/verifypanel` — resends the verification panel\n"
        "`/announce` — posts a formatted announcement\n"
        "`/drop` — posts a map code\n"
        "`/add @member` — adds someone to the current ticket\n"
        "`/lockdown` — toggles anti-raid protection mode\n"
        "`/clear` — bulk-deletes messages",
    )
    brand(e, guild)
    await ch_staff.send(embed=e)

    await interaction.followup.send(
        "✅ **Server built.**\n\n"
        "**One last required step:** go to *Server Settings → Roles* and drag the bot's "
        f"role **above** {r_admin.mention}, otherwise it won't be able to assign roles.\n\n"
        "Also consider setting the server's *verification level* to **Medium** "
        "under Moderation.",
        ephemeral=True,
    )


# ======================================================================
#  STAFF COMMANDS
# ======================================================================


@bot.tree.command(name="panel", description="Resends the ticket panel in this channel.")
@app_commands.checks.has_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    e = brand_embed(
        "📩  OPEN A TICKET",
        "Pick a reason from the menu below to open a private channel with the team.",
    )
    brand(e, interaction.guild)
    await interaction.channel.send(embed=e, view=TicketPanelView())
    await interaction.response.send_message("✅ Panel sent.", ephemeral=True)


@bot.tree.command(name="verifypanel", description="Resends the verification panel here.")
@app_commands.checks.has_permissions(administrator=True)
async def verifypanel(interaction: discord.Interaction):
    e = brand_embed(
        "🔓  SERVER ACCESS",
        "Click the button below to join the community.",
    )
    brand(e, interaction.guild)
    await interaction.channel.send(embed=e, view=VerifyView())
    await interaction.response.send_message("✅ Panel sent.", ephemeral=True)


@bot.tree.command(name="announce", description="Posts a formatted announcement.")
@app_commands.describe(
    title="Announcement title",
    message="Body text (use \\n for a line break)",
    channel="Target channel (defaults to the current one)",
    ping="Mention @everyone",
)
async def announce(
    interaction: discord.Interaction,
    title: str,
    message: str,
    channel: Optional[discord.TextChannel] = None,
    ping: bool = False,
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("⛔ Staff only.", ephemeral=True)

    target = channel or interaction.channel
    e = brand_embed(f"📣  {title}", message.replace("\\n", "\n"))
    brand(e, interaction.guild)
    e.set_footer(text=f"Posted by {interaction.user.display_name}",
                 icon_url=interaction.user.display_avatar.url)
    e.timestamp = discord.utils.utcnow()

    await target.send(
        content="@everyone" if ping else None,
        embed=e,
        allowed_mentions=discord.AllowedMentions(everyone=ping),
    )
    await interaction.response.send_message(
        f"✅ Announcement posted in {target.mention}.", ephemeral=True
    )


@bot.tree.command(name="drop", description="Posts a map code.")
@app_commands.describe(
    name="Map name",
    code="Island code (0000-0000-0000)",
    description="Short description of the mode",
    channel="Target channel (defaults to the current one)",
    ping="Mention @everyone",
)
async def drop(
    interaction: discord.Interaction,
    name: str,
    code: str,
    description: Optional[str] = None,
    channel: Optional[discord.TextChannel] = None,
    ping: bool = False,
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("⛔ Staff only.", ephemeral=True)

    target = channel or interaction.channel
    e = brand_embed(
        f"🎯  {name}",
        (f"{description}\n\n" if description else "")
        + f"**Island code**\n```\n{code}\n```\n"
        "Search this code in Fortnite's **Discover** tab to launch the map.",
    )
    brand(e, interaction.guild, footer=f"{STUDIO_NAME} • Have fun")
    e.timestamp = discord.utils.utcnow()

    await target.send(
        content="@everyone" if ping else None,
        embed=e,
        allowed_mentions=discord.AllowedMentions(everyone=ping),
    )
    await interaction.response.send_message(
        f"✅ Code posted in {target.mention}.", ephemeral=True
    )


@bot.tree.command(name="add", description="Adds a member to the current ticket.")
@app_commands.describe(member="The member to add")
async def add(interaction: discord.Interaction, member: discord.Member):
    if not (interaction.channel.topic or "").startswith("ticket-owner:"):
        return await interaction.response.send_message(
            "⛔ This command only works inside a ticket.", ephemeral=True
        )
    if not is_staff(interaction.user):
        return await interaction.response.send_message("⛔ Staff only.", ephemeral=True)

    await interaction.channel.set_permissions(
        member, view_channel=True, send_messages=True, read_message_history=True
    )
    await interaction.response.send_message(f"✅ {member.mention} was added to the ticket.")


@bot.tree.command(name="clear", description="Bulk-deletes messages.")
@app_commands.describe(amount="Number of messages to delete (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"✅ {len(deleted)} message(s) deleted.", ephemeral=True)


@bot.tree.command(name="lockdown", description="Toggles anti-raid protection mode.")
@app_commands.checks.has_permissions(administrator=True)
async def lockdown_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    if bot.lockdown.get(gid, 0) > time.time():
        bot.lockdown[gid] = 0
        await interaction.response.send_message("🔓 Protection mode **disabled**.", ephemeral=True)
    else:
        bot.lockdown[gid] = time.time() + LOCKDOWN_DURATION
        await interaction.response.send_message(
            f"🛡️ Protection mode **enabled** for {LOCKDOWN_DURATION // 60} minutes.",
            ephemeral=True,
        )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ You don't have permission to use this command."
    else:
        msg = f"⚠️ Something went wrong: `{type(error).__name__}`"
        print(f"[ERROR] {error!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ======================================================================
#  KEEPALIVE HTTP SERVER (Render free plan)
# ======================================================================


class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = "online" if bot.is_ready() else "starting"
        body = f"{STUDIO_NAME} bot: {status}".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, *args):
        pass  # Keeps Render logs clean of every ping


def start_keepalive():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"→ Keepalive server started on port {port}")


# ======================================================================

def main() -> None:
    # Open the port first: Render detects it even if config is incomplete.
    start_keepalive()

    if not TOKEN:
        found = sorted(
            k for k in os.environ
            if any(word in k.upper() for word in ("DISCORD", "TOKEN", "GUILD"))
        )
        raise SystemExit(
            "❌ Token not found. Neither DISCORD_BOT_TOKEN nor DISCORD_TOKEN is set.\n"
            f"Detected variables: {found or 'none'}"
        )

    if not GUILD_ID:
        print("[CONFIG] DISCORD_GUILD_ID missing: global sync (up to 1h delay).")

    bot.run(TOKEN)


if __name__ == "__main__":
    main()
