import asyncio
import datetime as dt
import json
import logging
import os
import tempfile
from threading import Thread

import discord
from discord.ext import commands, tasks
from flask import Flask

# ============================================================
# VorstyAFK Discord Invite / Plan Bot
# Required Render environment variable: DISCORD_TOKEN
# ============================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vorstyafk")

# Render web service keep-alive endpoint
app = Flask(__name__)
_web_thread = None

@app.route("/")
def home():
    return "VorstyAFK Discord Invite System Online!", 200


def _run_web():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def keep_alive():
    global _web_thread
    if _web_thread is None or not _web_thread.is_alive():
        _web_thread = Thread(target=_run_web, daemon=True)
        _web_thread.start()


# Bot settings
ALLOWED_GUILD_ID = 1559826100153090080
ADMIN_CHANNEL_ID = 1559831974162473050
DB_FILE = os.getenv("DB_FILE", "database.json")
MAX_USERS = 100

# Plan key = invite cost; values are plan durations in days.
PLAN_DAYS = {
    2: 7,
    4: 14,
    8: 23,
    12: 34,
    16: 60,
    24: 365,
}

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True
bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)

invites_cache = {}


# -------------------- Database helpers --------------------
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Could not read database %s: %s", DB_FILE, exc)
        return {}


def save_db(data):
    """Write atomically so an interrupted write is less likely to corrupt JSON."""
    folder = os.path.dirname(os.path.abspath(DB_FILE))
    os.makedirs(folder, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=folder, delete=False
        ) as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            temp_path = f.name
        os.replace(temp_path, DB_FILE)
    except OSError:
        log.exception("Could not save database")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise


def ensure_user_record(db, user_id, username):
    key = str(user_id)
    if key not in db or not isinstance(db[key], dict):
        db[key] = {}
    info = db[key]
    info.setdefault("server_ip", "None")
    info.setdefault("expiry_date", "None")
    info.setdefault("status", "none")
    info.setdefault("total_invites", 0)
    info.setdefault("username", username)
    info.setdefault("spent_invites", 0)
    info["username"] = username
    return info


def get_expiry(info):
    value = info.get("expiry_date", "None")
    if not value or value == "None":
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def has_active_plan(info, now=None):
    now = now or dt.datetime.now()
    expiry = get_expiry(info)
    return info.get("status") == "active" and expiry is not None and expiry > now


def get_available_invites(info):
    # total_invites means currently available invites, not lifetime earned invites.
    try:
        return max(0, int(info.get("total_invites", 0)))
    except (TypeError, ValueError):
        return 0


def is_allowed_server():
    async def predicate(ctx):
        return ctx.guild is not None and ctx.guild.id == ALLOWED_GUILD_ID
    return commands.check(predicate)


def admin_only():
    return commands.has_permissions(administrator=True)


# -------------------- Error handling --------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permission to use this command.")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ This command can only be used in the configured server.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`. Use `$help` for examples.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid member or number. Check the command format with `$help`.")
        return
    log.error("Command error", exc_info=(type(error), error, error.__traceback__))
    await ctx.send("❌ Something went wrong while running that command. Check the bot logs.")


# -------------------- Invite tracking --------------------
async def cache_server_invites():
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if guild is None:
        log.warning("Configured guild %s is not available to the bot.", ALLOWED_GUILD_ID)
        return
    try:
        guild_invites = await guild.invites()
        invites_cache[guild.id] = {
            invite.code: (invite.uses or 0) for invite in guild_invites
        }
        log.info("Cached %s invites for guild %s", len(guild_invites), guild.id)
    except discord.Forbidden:
        log.error("Invite tracking needs the Manage Server permission.")
    except discord.HTTPException:
        log.exception("Could not fetch server invites")


@bot.event
async def on_invite_create(invite):
    if invite.guild and invite.guild.id == ALLOWED_GUILD_ID:
        invites_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0


@bot.event
async def on_invite_delete(invite):
    if invite.guild and invite.guild.id == ALLOWED_GUILD_ID:
        invites_cache.get(invite.guild.id, {}).pop(invite.code, None)


@bot.event
async def on_member_join(member):
    if member.guild.id != ALLOWED_GUILD_ID or member.bot:
        return

    old_invites = invites_cache.get(member.guild.id, {})
    try:
        current_invites = await member.guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Could not identify invite used by %s", member)
        return

    inviter = None
    updated_cache = {invite.code: (invite.uses or 0) for invite in current_invites}
    for invite in current_invites:
        previous_uses = old_invites.get(invite.code)
        current_uses = invite.uses or 0
        if previous_uses is not None and current_uses > previous_uses:
            inviter = invite.inviter
            break
    invites_cache[member.guild.id] = updated_cache

    if inviter is None or inviter.bot:
        return

    db = load_db()
    info = ensure_user_record(db, inviter.id, inviter.name)
    info["total_invites"] = get_available_invites(info) + 1
    save_db(db)
    log.info("Tracked invite for %s; available invites: %s", inviter, info["total_invites"])


# -------------------- Expiry processing --------------------
@tasks.loop(minutes=1)
async def check_expirations():
    db = load_db()
    now = dt.datetime.now()
    changed = False
    admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)

    for user_id, info in list(db.items()):
        if not isinstance(info, dict) or info.get("status") != "active":
            continue
        expiry = get_expiry(info)
        if expiry is not None and now >= expiry:
            info["status"] = "expired"
            changed = True
            if admin_channel:
                embed = discord.Embed(
                    title="⚠️ Plan Expired",
                    color=discord.Color.red(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="User", value=f"{info.get('username', 'Unknown')} (<@{user_id}>)", inline=False)
                embed.add_field(name="Server IP", value=info.get("server_ip", "None"), inline=True)
                embed.add_field(name="Expiry", value=info.get("expiry_date", "None"), inline=True)
                try:
                    await admin_channel.send(embed=embed)
                except discord.HTTPException:
                    log.exception("Could not send plan expiry notification")

    if changed:
        save_db(db)


@check_expirations.before_loop
async def before_check_expirations():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Connected as %s (ID: %s)", bot.user, bot.user.id)
    keep_alive()
    await cache_server_invites()
    if not check_expirations.is_running():
        check_expirations.start()


# -------------------- Help command --------------------
@bot.command(name="help")
@is_allowed_server()
async def help_command(ctx):
    embed = discord.Embed(
        title="🤖 VorstyAFK Bot Commands",
        description="Choose a plan using the invite cost you want to spend.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="👥 Member Commands",
        value=(
            "`$claim 2 ip: play.example.com` — 7-day plan, costs 2 invites\n"
            "`$claim 4 ip: play.example.com` — 14-day plan, costs 4 invites\n"
            "`$claim 8 ip: play.example.com` — 23-day plan, costs 8 invites\n"
            "`$claim 12 ip: play.example.com` — 34-day plan, costs 12 invites\n"
            "`$claim 16 ip: play.example.com` — 60-day plan, costs 16 invites\n"
            "`$claim 24 ip: play.example.com` — 365-day plan, costs 24 invites\n"
            "`$upgrade need auto-jump` — request a feature upgrade"
        ),
        inline=False,
    )
    if ctx.author.guild_permissions.administrator:
        embed.add_field(
            name="👑 Admin Commands",
            value=(
                "`$setinvite @User 10` — set available invites\n"
                "`$userstatus @User` — view plan status\n"
                "`$dm @User message` — send a DM\n"
                "`$check invite` / `$checkinvites` — view invite database"
            ),
            inline=False,
        )
    embed.add_field(
        name="📅 Plan Prices",
        value="`2 → 7d` · `4 → 14d` · `8 → 23d` · `12 → 34d` · `16 → 60d` · `24 → 365d`",
        inline=False,
    )
    embed.set_footer(text="Prefix: $ | One active plan per user")
    await ctx.send(embed=embed)


# -------------------- Claim plan --------------------
@bot.command(name="claim")
@is_allowed_server()
async def claim(ctx, plan_invites: int, *, ip: str = None):
    """Example: $claim 4 ip: play.example.com"""
    if plan_invites not in PLAN_DAYS:
        allowed = ", ".join(str(n) for n in PLAN_DAYS)
        await ctx.send(f"❌ Invalid plan. Choose one of these invite costs: **{allowed}**. See `$help`.")
        return

    if not ip:
        await ctx.send(f"❌ Add your server IP too. Example: `$claim {plan_invites} ip: play.example.com`")
        return

    # Accept both 'ip: host' and a plain host after the plan number.
    server_ip = ip.strip()
    if server_ip.lower().startswith("ip:"):
        server_ip = server_ip[3:].strip()
    if not server_ip or server_ip.lower() in {"none", "null"}:
        await ctx.send("❌ Please provide a valid server IP/address after `ip:`.")
        return

    db = load_db()
    user_id = str(ctx.author.id)
    info = ensure_user_record(db, ctx.author.id, ctx.author.name)
    now = dt.datetime.now()

    # Do not allow another claim while the current plan is still valid.
    if has_active_plan(info, now):
        await ctx.send(
            f"⏳ {ctx.author.mention}, you already have an active plan until **{info['expiry_date']}**. "
            "You can claim another plan after it expires."
        )
        return

    # Fix stale active status if expiry has already passed.
    old_expiry = get_expiry(info)
    if info.get("status") == "active" and (old_expiry is None or old_expiry <= now):
        info["status"] = "expired"

    cost = plan_invites
    available = get_available_invites(info)
    if available < cost:
        await ctx.send(
            f"❌ {ctx.author.mention}, this plan costs **{cost} invites**, "
            f"but you only have **{available} available invites**."
        )
        return

    active_count = 0
    for other_id, other_info in db.items():
        if not isinstance(other_info, dict) or other_id == user_id:
            continue
        if has_active_plan(other_info, now):
            active_count += 1
    if active_count >= MAX_USERS:
        await ctx.send(f"❌ All active plan slots are full ({MAX_USERS}/{MAX_USERS}).")
        return

    days = PLAN_DAYS[cost]
    expiry = now + dt.timedelta(days=days)

    # Deduct only after every validation has passed, then persist the plan and balance.
    info["total_invites"] = available - cost
    info["spent_invites"] = int(info.get("spent_invites", 0) or 0) + cost
    info.update({
        "username": ctx.author.name,
        "server_ip": server_ip,
        "expiry_date": expiry.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "active",
        "plan_invites": cost,
        "plan_days": days,
    })
    db[user_id] = info
    try:
        save_db(db)
    except OSError:
        await ctx.send("❌ I couldn't save the claim. No confirmation was sent; please contact an admin.")
        return

    embed = discord.Embed(title="✅ Plan Activated", color=discord.Color.green())
    embed.add_field(name="Member", value=ctx.author.mention, inline=False)
    embed.add_field(name="Plan Cost", value=f"{cost} invites", inline=True)
    embed.add_field(name="Duration", value=f"{days} days", inline=True)
    embed.add_field(name="Remaining Invites", value=str(info["total_invites"]), inline=True)
    embed.add_field(name="Server IP", value=server_ip, inline=False)
    embed.add_field(name="Expires", value=info["expiry_date"], inline=False)
    await ctx.send(embed=embed)

    try:
        await ctx.author.send(embed=embed)
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        log.warning("Could not DM plan confirmation to %s", ctx.author)

    admin_channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if admin_channel:
        try:
            await admin_channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Could not send plan activation to admin channel")


# -------------------- Upgrade request --------------------
@bot.command(name="upgrade")
@is_allowed_server()
async def upgrade(ctx, *, details: str = None):
    if not details:
        await ctx.send("❌ Usage: `$upgrade need auto-jump feature`")
        return
    db = load_db()
    info = db.get(str(ctx.author.id), {})
    invites = get_available_invites(info)
    if invites < 2:
        await ctx.send("❌ You need at least 2 available invites to request an upgrade.")
        return

    await ctx.send(f"✅ {ctx.author.mention}, your upgrade request was sent to the administration team.")
    channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if channel:
        embed = discord.Embed(title="⚙️ Custom Upgrade Request", color=discord.Color.orange())
        embed.add_field(name="Member", value=f"{ctx.author} ({ctx.author.mention})", inline=False)
        embed.add_field(name="Available Invites", value=str(invites), inline=True)
        embed.add_field(name="Requested Features", value=details[:1024], inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Could not send upgrade request")


# -------------------- Invite list: both command spellings --------------------
async def send_invite_report(ctx):
    db = load_db()
    if not db:
        await ctx.send("📂 Invite database is empty so far.")
        return

    # Discord embeds allow at most 25 fields; use multiple pages/messages.
    records = list(db.items())
    pages = [records[i:i + 20] for i in range(0, len(records), 20)]
    for page_number, page in enumerate(pages, start=1):
        embed = discord.Embed(
            title=f"📊 Invite Tracker ({page_number}/{len(pages)})",
            color=discord.Color.purple(),
        )
        for uid, info in page:
            if not isinstance(info, dict):
                continue
            embed.add_field(
                name=str(info.get("username", "Unknown"))[:256],
                value=(
                    f"Available invites: **{get_available_invites(info)}**\n"
                    f"Spent invites: **{info.get('spent_invites', 0)}**\n"
                    f"Plan status: **{info.get('status', 'none')}**\n"
                    f"User: <@{uid}>"
                )[:1024],
                inline=True,
            )
        await ctx.send(embed=embed)


@bot.command(name="checkinvites", aliases=["checkinvite", "check_invites"])
@commands.has_permissions(administrator=True)
@is_allowed_server()
async def checkinvites(ctx):
    await send_invite_report(ctx)


@bot.group(name="check", invoke_without_command=True)
@commands.has_permissions(administrator=True)
@is_allowed_server()
async def check_group(ctx):
    await ctx.send("Usage: `$check invite` or `$checkinvites`.")


@check_group.command(name="invite", aliases=["invites"])
@commands.has_permissions(administrator=True)
@is_allowed_server()
async def check_invite_subcommand(ctx):
    await send_invite_report(ctx)


# -------------------- Admin commands --------------------
@bot.command(name="setinvite")
@admin_only()
@is_allowed_server()
async def setinvite(ctx, member: discord.Member, count: int):
    if count < 0:
        await ctx.send("❌ Invite count cannot be negative.")
        return
    db = load_db()
    info = ensure_user_record(db, member.id, member.name)
    info["total_invites"] = count
    save_db(db)
    await ctx.send(f"✅ {member.mention} now has **{count} available invites**.")


@bot.command(name="userstatus")
@admin_only()
@is_allowed_server()
async def userstatus(ctx, member: discord.Member):
    db = load_db()
    info = db.get(str(member.id))
    if not isinstance(info, dict):
        await ctx.send("❌ No database record found for that member.")
        return

    embed = discord.Embed(title="👤 User Plan Details", color=discord.Color.blue())
    embed.add_field(name="Member", value=f"{member} ({member.mention})", inline=False)
    embed.add_field(name="Available Invites", value=str(get_available_invites(info)), inline=True)
    embed.add_field(name="Spent Invites", value=str(info.get("spent_invites", 0)), inline=True)
    embed.add_field(name="Plan Cost", value=str(info.get("plan_invites", "None")), inline=True)
    embed.add_field(name="Server IP", value=str(info.get("server_ip", "None"))[:1024], inline=False)
    embed.add_field(name="Status", value=str(info.get("status", "none")), inline=True)
    embed.add_field(name="Expiry Date", value=str(info.get("expiry_date", "None")), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="dm")
@admin_only()
@is_allowed_server()
async def dm_member(ctx, member: discord.Member, *, message: str):
    embed = discord.Embed(
        title="📩 Message from Server Administration",
        description=message[:4000],
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Sent by {ctx.author}")
    try:
        await member.send(embed=embed)
        await ctx.send(f"✅ Message sent to {member.mention}.")
    except discord.Forbidden:
        await ctx.send("❌ I couldn't DM this member; their privacy settings may block the bot.")
    except discord.HTTPException:
        await ctx.send("❌ Discord could not deliver that message. Try again later.")


# -------------------- Start bot --------------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing from the Render Environment settings.")
    bot.run(token)
