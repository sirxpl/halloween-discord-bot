import os
import time
import logging
import random
import threading
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
from urllib.parse import urlencode
import secrets
import requests

load_dotenv()

discord.utils.setup_logging(level=logging.INFO)
log = logging.getLogger("halloween-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
PORT = int(os.getenv("PORT", "10000"))
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
OAUTH2_REDIRECT_URI = os.getenv("OAUTH2_REDIRECT_URI")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not configured.")
if not FLASK_SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY is not configured.")

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=2500)
db = mongo["halloween_bot"]
users = db["users"]
settings = db["settings"]

SHOP_ITEMS = [
    {"name": "🎃 Pumpkin Lantern", "price": 250, "description": "A spooky lantern for your Halloween inventory."},
    {"name": "🧙 Witch Hat", "price": 500, "description": "A classic witch hat for your collection."},
    {"name": "👻 Ghost Companion", "price": 1000, "description": "A friendly little ghost to haunt your inventory."},
    {"name": "🎃 Golden Pumpkin", "price": 2500, "description": "A rare golden pumpkin for dedicated Candy collectors."},
]

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/dashboard")
def dashboard():
    try:
        total_users = users.count_documents({})
        total_candy = sum(
            (doc.get("balance", 0) or 0)
            for doc in users.find({}, {"balance": 1})
        )
        db_status = "Connected"
    except PyMongoError:
        log.exception("Dashboard could not reach MongoDB")
        total_users, total_candy, db_status = 0, 0, "Unavailable"

    d = STATE["discord"]
    labels = {
        "online": "Online",
        "starting": "Starting",
        "connecting": "Connecting",
        "reconnecting": "Reconnecting",
        "rate_limited": "Rate limited",
        "error": "Offline",
    }
    discord_status = labels.get(d["state"], "Unknown")
    user = session.get("discord_user")
    return render_template(
        "dashboard.html",
        total_users=total_users,
        total_candy=total_candy,
        command_count=8,
        bot_status=discord_status if d["state"] != "online" else "Online",
        discord_status=discord_status,
        db_status=db_status,
        discord_ok=d["state"] == "online",
        user=user,
        avatar_url=discord_avatar_url(user),
    )


@app.get("/halloween-quests")
def halloween_quests_page():
    return render_template("halloween_quests.html")

@app.get("/announcements")
def announcements_page():
    return render_template("announcements.html")


@app.get("/shop")
def shop_page():
    return render_template("shop.html", items=SHOP_ITEMS)


@app.get("/rewards")
def rewards_page():
    return render_template("rewards.html")


@app.get("/inventory")
def inventory_page():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild = selected_guild()
    profile = None
    if guild:
        profile = users.find_one({"guild_id": int(guild["id"]), "user_id": int(user["id"])})
    return render_template("inventory.html", user=user, avatar_url=discord_avatar_url(user), guilds=session.get("discord_guilds", []), guild=guild, profile=profile)


@app.post("/shop/buy/<item_name>")
def buy_item(item_name):
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild = selected_guild()
    item = next((i for i in SHOP_ITEMS if i["name"] == item_name), None)
    if not guild or not item:
        return redirect(url_for("shop_page"))
    guild_id = int(guild["id"])
    user_id = int(user["id"])
    result = users.find_one_and_update(
        {"guild_id": guild_id, "user_id": user_id, "balance": {"$gte": item["price"]}},
        {"$inc": {"balance": -item["price"]}, "$push": {"inventory": {"name": item["name"], "price": item["price"], "purchased_at": now_utc()}}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        return redirect(url_for("shop_page", guild=guild_id, error="not_enough"))
    return redirect(url_for("inventory_page", guild=guild_id, purchased=item["name"]))


@app.get("/events")
def events_page():
    return render_template("events.html")


@app.get("/login")
def login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not OAUTH2_REDIRECT_URI:
        return "Discord OAuth2 is not configured on this deployment.", 503
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    params = {"client_id": DISCORD_CLIENT_ID, "redirect_uri": OAUTH2_REDIRECT_URI, "response_type": "code", "scope": "identify guilds", "state": state}
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))


@app.get("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        return redirect(url_for("profile_page"))
    state = request.args.get("state")
    expected = session.pop("oauth_state", None)
    if not state or not expected or not secrets.compare_digest(state, expected):
        return "Invalid OAuth2 state.", 400
    code = request.args.get("code")
    if not code:
        return "Missing OAuth2 authorization code.", 400
    token = requests.post("https://discord.com/api/oauth2/token", data={"client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": OAUTH2_REDIRECT_URI}, timeout=10)
    if not token.ok:
        log.error("Discord OAuth2 token exchange failed: %s %s", token.status_code, token.text[:300])
        return "Discord OAuth2 sign-in failed.", 502
    access_token = token.json().get("access_token")
    if not access_token:
        return "Discord OAuth2 did not return an access token.", 502
    headers = {"Authorization": f"Bearer {access_token}"}
    me_response = requests.get("https://discord.com/api/users/@me", headers=headers, timeout=10)
    guild_response = requests.get("https://discord.com/api/users/@me/guilds", headers=headers, timeout=10)
    if not me_response.ok or not guild_response.ok:
        return "Discord account information could not be loaded.", 502
    me = me_response.json()
    guilds = guild_response.json()
    session["discord_user"] = me
    session["discord_guilds"] = guilds
    if guilds:
        session["selected_guild_id"] = str(guilds[0]["id"])
    return redirect(url_for("profile_page"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


def discord_avatar_url(user):
    if not user or not user.get("avatar"):
        return None
    return f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png?size=256"


def selected_guild(require_manage=False):
    guilds = session.get("discord_guilds", [])
    wanted = str(request.args.get("guild") or session.get("selected_guild_id") or "")
    guild = next((g for g in guilds if str(g.get("id")) == wanted), None)
    if guild:
        session["selected_guild_id"] = str(guild["id"])
        return guild
    if guilds:
        session["selected_guild_id"] = str(guilds[0]["id"])
        return guilds[0]
    return None


def get_guild_settings(guild_id):
    defaults = {"event_enabled": True, "daily_reward": 100, "trick_or_treat_min": 25, "trick_or_treat_max": 150}
    stored = settings.find_one({"guild_id": guild_id}) or {}
    defaults.update({k: stored[k] for k in defaults if k in stored})
    return defaults


@app.get("/profile")
def profile_page():
    user = session.get("discord_user")
    if not user:
        return render_template("profile.html", logged_in=False, user=None, avatar_url=None)
    guild = selected_guild()
    profile = None
    rank = None
    if guild:
        guild_id = int(guild["id"])
        profile = users.find_one({"guild_id": guild_id, "user_id": int(user["id"])})
        if profile:
            rank = users.count_documents({"guild_id": guild_id, "balance": {"$gt": profile.get("balance", 0)}}) + 1
    return render_template("profile.html", logged_in=True, user=user, avatar_url=discord_avatar_url(user), guilds=session.get("discord_guilds", []), guild=guild, profile=profile, rank=rank)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild = selected_guild()
    if not guild:
        return render_template("settings.html", user=user, avatar_url=discord_avatar_url(user), guilds=[], guild=None, config=None, error="You need Manage Server or Administrator permission in a Discord server to configure it.")
    guild_id = int(guild["id"])
    permissions = int(guild.get("permissions", 0))
    if not (permissions & 0x8 or permissions & 0x20):
        return render_template("settings.html", user=user, avatar_url=discord_avatar_url(user), guilds=session.get("discord_guilds", []), guild=guild, config=None, error="You need Manage Server or Administrator permission for this server.")
    if request.method == "POST":
        try:
            daily_reward = max(0, min(int(request.form.get("daily_reward", 100)), 100000))
            minimum = max(1, min(int(request.form.get("trick_or_treat_min", 25)), 100000))
            maximum = max(minimum, min(int(request.form.get("trick_or_treat_max", 150)), 100000))
        except (TypeError, ValueError):
            return render_template("settings.html", user=user, avatar_url=discord_avatar_url(user), guilds=session.get("discord_guilds", []), guild=guild, config=get_guild_settings(guild_id), error="Please enter valid numeric settings.")
        config = {"event_enabled": request.form.get("event_enabled") == "on", "daily_reward": daily_reward, "trick_or_treat_min": minimum, "trick_or_treat_max": maximum, "updated_at": now_utc(), "updated_by": int(user["id"])}
        settings.update_one({"guild_id": guild_id}, {"$set": config, "$setOnInsert": {"guild_id": guild_id}}, upsert=True)
    return render_template("settings.html", user=user, avatar_url=discord_avatar_url(user), guilds=session.get("discord_guilds", []), guild=guild, config=get_guild_settings(guild_id), saved=request.method == "POST")


@app.get("/statistics")
def statistics_page():
    try:
        total_users = users.count_documents({})
        total_candy = sum((doc.get("balance", 0) or 0) for doc in users.find({}, {"balance": 1}))
        db_ok = True
    except PyMongoError:
        log.exception("Statistics could not reach MongoDB")
        total_users, total_candy, db_ok = 0, 0, False
    d = STATE["discord"]
    labels = {"online":"Online","starting":"Starting","connecting":"Connecting","reconnecting":"Reconnecting","rate_limited":"Rate limited","error":"Offline"}
    bot_status = labels.get(d["state"], "Unknown")
    return render_template("statistics.html", total_users=total_users, total_candy=total_candy, command_count=7, bot_status=bot_status, db_ok=db_ok)

@app.get("/daily")
def daily_page():
    return render_template("daily.html")


@app.get("/leaderboard")
def leaderboard_page():
    try:
        top_users = list(
            users.find({}).sort("balance", -1).limit(25)
        )
        discord_bot = BOT["instance"]
        leaderboard = []
        for doc in top_users:
            user_id = doc.get("user_id")
            discord_user = discord_bot.get_user(user_id) if discord_bot else None

            # Existing MongoDB records may not have a stored username yet.
            # If the user is not cached, fetch their Discord account directly
            # through the bot's running event loop instead of showing the ID.
            if not doc.get("username") and discord_user is None and discord_bot and not discord_bot.is_closed():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        discord_bot.fetch_user(user_id),
                        discord_bot.loop,
                    )
                    discord_user = future.result(timeout=5)
                    if discord_user:
                        users.update_one(
                            {"guild_id": doc.get("guild_id"), "user_id": user_id},
                            {"$set": {
                                "username": discord_user.name,
                                "display_name": discord_user.display_name,
                            }},
                        )
                except Exception:
                    log.exception("Could not fetch Discord username for %s", user_id)

            name = (
                f"@{doc.get('username')}"
                if doc.get("username")
                else (f"@{discord_user.name}" if discord_user else "@Unknown User")
            )
            leaderboard.append({
                "name": name,
                "balance": doc.get("balance", 0) or 0,
            })
        db_ok = True
    except PyMongoError:
        log.exception("Leaderboard could not reach MongoDB")
        leaderboard, db_ok = [], False
    return render_template("leaderboard.html", leaderboard=leaderboard, db_ok=db_ok)

@app.get("/status")
def status_page():
    return render_template("status.html", s=build_status())


@app.get("/status.json")
def status_json():
    return jsonify(build_status())


@app.get("/health")
def health_check():
    try:
        mongo.admin.command("ping")
        return jsonify({"status": "ok", "database": "connected"}), 200
    except Exception:
        return jsonify({"status": "error", "database": "unavailable"}), 503


def run_web_server():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


async def db(fn, *args, **kwargs):
    """Run a blocking pymongo call in a worker thread so the Discord loop never freezes."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def now_utc():
    return datetime.now(timezone.utc)


# ---------- live status tracking (feeds /status and /dashboard) ----------
STATE = {
    "started_at": now_utc(),
    "discord": {
        "state": "starting",  # starting | connecting | online | reconnecting | rate_limited | error
        "attempts": 0,
        "last_attempt": None,
        "connected_at": None,
        "user": None,
        "last_error": None,
        "retry_until": None,
        "rate_limit": None,
    },
}
BOT = {"instance": None}


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else None


def fmt_duration(seconds):
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def describe_rate_limit(exc: discord.HTTPException):
    """Work out whether a 429 is a normal Discord rate limit or a Cloudflare/IP block."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    body = str(getattr(exc, "text", "") or "")
    body_l = body.lower()
    wanted = [
        "Retry-After", "X-RateLimit-Global", "X-RateLimit-Scope", "X-RateLimit-Limit",
        "X-RateLimit-Remaining", "X-RateLimit-Reset-After", "Via", "Server", "CF-RAY",
    ]
    picked = {k: headers.get(k) for k in wanted if headers.get(k) is not None}

    if "<html" in body_l or "<!doctype" in body_l or "1015" in body_l or "cloudflare" in body_l:
        kind = "ip_block"
        label = "Blocked at Cloudflare (this host's IP is being throttled)"
    elif "retry_after" in body_l or "rate limited" in body_l:
        kind = "api_limit"
        label = "Normal Discord API rate limit"
    else:
        kind = "unknown"
        label = "Unrecognised 429 response"

    retry_after = None
    try:
        retry_after = float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        pass
    return {"kind": kind, "label": label, "retry_after": retry_after, "headers": picked, "body": body[:200]}


def build_status():
    d = STATE["discord"]
    bot = BOT["instance"]
    now = now_utc()

    latency_ms, guilds = None, None
    if bot is not None and d["state"] == "online" and not bot.is_closed():
        lat = bot.latency
        if lat == lat and lat != float("inf"):
            latency_ms = round(lat * 1000)
        guilds = len(bot.guilds)

    db_ok, db_ms, db_error = False, None, None
    started = time.perf_counter()
    try:
        mongo.admin.command("ping")
        db_ok = True
        db_ms = round((time.perf_counter() - started) * 1000)
    except PyMongoError as exc:
        db_error = exc.__class__.__name__

    retry_in = None
    if d["retry_until"] and d["retry_until"] > now:
        retry_in = int((d["retry_until"] - now).total_seconds())

    discord_ok = d["state"] == "online"
    if discord_ok and db_ok:
        overall, overall_label = "ok", "All systems operational"
    elif discord_ok or db_ok:
        overall, overall_label = "warn", "Partial outage"
    else:
        overall, overall_label = "down", "Major outage"

    rl = d["rate_limit"]
    return {
        "overall": overall,
        "overall_label": overall_label,
        "uptime": fmt_duration((now - STATE["started_at"]).total_seconds()),
        "checked_at": fmt_dt(now),
        "discord": {
            "state": d["state"],
            "ok": discord_ok,
            "user": d["user"],
            "latency_ms": latency_ms,
            "guilds": guilds,
            "attempts": d["attempts"],
            "last_attempt": fmt_dt(d["last_attempt"]),
            "connected_at": fmt_dt(d["connected_at"]),
            "last_error": d["last_error"],
            "retry_in": retry_in,
            "rate_limit": None if not rl else {
                "kind": rl["kind"],
                "label": rl["label"],
                "retry_after": rl["retry_after"],
                "at": fmt_dt(rl["at"]),
            },
        },
        "database": {"ok": db_ok, "latency_ms": db_ms, "error": db_error},
        "web": {"ok": True},
    }


def get_user(guild_id: int, user_id: int):
    return users.find_one({"guild_id": guild_id, "user_id": user_id})


def ensure_user(guild_id: int, user_id: int, username=None, display_name=None):
    update = {
        "$setOnInsert": {
            "guild_id": guild_id,
            "user_id": user_id,
            "balance": 0,
            "inventory": [],
            "created_at": now_utc(),
        }
    }
    identity = {}
    if username:
        identity["username"] = username
    if display_name:
        identity["display_name"] = display_name
    if identity:
        update["$set"] = identity
    return users.find_one_and_update(
        {"guild_id": guild_id, "user_id": user_id},
        update,
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def add_candy(guild_id: int, user_id: int, amount: int):
    return users.find_one_and_update(
        {"guild_id": guild_id, "user_id": user_id},
        {"$inc": {"balance": amount}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


intents = discord.Intents.default()


def create_bot():
    return commands.Bot(command_prefix="!", intents=intents)


class HalloweenBot(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_

    @app_commands.command(name="balance", description="Check your Candy balance.")
    async def balance(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        user = await db(ensure_user, interaction.guild.id, interaction.user.id, interaction.user.name, interaction.user.display_name)
        await interaction.response.send_message(
            f"🍬 **{interaction.user.display_name}** has **{user['balance']:,} Candy**."
        )

    @app_commands.command(name="daily", description="Claim your daily Candy reward.")
    async def daily(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        now = now_utc()
        await db(ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name)
        config = await db(get_guild_settings, guild_id)
        cutoff = now - timedelta(hours=24)
        updated = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": user_id,
             "$or": [{"last_daily": {"$exists": False}}, {"last_daily": {"$lte": cutoff}}]},
            {"$set": {"last_daily": now}, "$inc": {"balance": config["daily_reward"]}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            user = await db(get_user, guild_id, user_id)
            timestamp = int((user["last_daily"] + timedelta(hours=24)).timestamp())
            await interaction.response.send_message(
                f"⏰ You already claimed your daily Candy. Try again <t:{timestamp}:R>.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"🎃 You claimed your daily reward: **+{config['daily_reward']:,} 🍬 Candy**!")

    @app_commands.command(name="trickortreat", description="Go trick-or-treating for a random Candy reward.")
    async def trick_or_treat(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        now = now_utc()
        await db(ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name)
        config = await db(get_guild_settings, guild_id)
        cutoff = now - timedelta(hours=1)
        reward = random.randint(config["trick_or_treat_min"], config["trick_or_treat_max"])
        updated = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": user_id,
             "$or": [{"last_trick_or_treat": {"$exists": False}}, {"last_trick_or_treat": {"$lte": cutoff}}]},
            {"$set": {"last_trick_or_treat": now}, "$inc": {"balance": reward}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            user = await db(get_user, guild_id, user_id)
            timestamp = int((user["last_trick_or_treat"] + timedelta(hours=1)).timestamp())
            await interaction.response.send_message(
                f"🏠 No more Candy yet! Try again <t:{timestamp}:R>.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"🎃 **Trick or treat!** You found **{reward:,} 🍬 Candy**!")

    @app_commands.command(name="give", description="Give Candy to another member.")
    @app_commands.describe(member="The member receiving Candy.", amount="Amount of Candy to give.")
    async def give(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("🤖 You can't give Candy to a bot.", ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message("🍬 You can't give Candy to yourself.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ The amount must be greater than 0.", ephemeral=True)
            return
        sender = await db(ensure_user, interaction.guild.id, interaction.user.id, interaction.user.name, interaction.user.display_name)
        debited = await db(
            users.find_one_and_update,
            {"guild_id": interaction.guild.id, "user_id": interaction.user.id, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}},
            return_document=ReturnDocument.AFTER,
        )
        if not debited:
            await interaction.response.send_message(
                f"❌ You don't have enough Candy. You currently have **{sender['balance']:,} 🍬**.",
                ephemeral=True,
            )
            return
        await db(ensure_user, interaction.guild.id, member.id, member.name, member.display_name)
        await db(add_candy, interaction.guild.id, member.id, amount)
        await interaction.response.send_message(
            f"🍬 {interaction.user.mention} gave **{amount:,} Candy** to {member.mention}!"
        )

    @app_commands.command(name="leaderboard", description="Show the server Candy leaderboard.")
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        top_users = await db(
            lambda: list(
                users.find({"guild_id": interaction.guild.id}).sort("balance", -1).limit(10)
            )
        )
        if not top_users:
            await interaction.response.send_message("🍬 Nobody has earned Candy yet!")
            return
        lines = []
        for index, user in enumerate(top_users, start=1):
            member = interaction.guild.get_member(user["user_id"])
            mention = member.mention if member else f"<@{user['user_id']}>"
            lines.append(f"**{index}.** {mention} — **{user['balance']:,} 🍬**")
        embed = discord.Embed(
            title="🍬 Candy Leaderboard",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="buy", description="Buy an item from the Halloween Candy shop.")
    @app_commands.describe(item="The exact shop item name.")
    async def buy(self, interaction: discord.Interaction, item: str):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        item_data = next((i for i in SHOP_ITEMS if i["name"].lower() == item.lower()), None)
        if not item_data:
            choices = ", ".join(i["name"] for i in SHOP_ITEMS)
            await interaction.response.send_message(f"❌ Item not found. Available items: {choices}", ephemeral=True)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        await db(ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name)
        purchased = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": user_id, "balance": {"$gte": item_data["price"]}},
            {"$inc": {"balance": -item_data["price"]}, "$push": {"inventory": {"name": item_data["name"], "price": item_data["price"], "purchased_at": now_utc()}}},
            return_document=ReturnDocument.AFTER,
        )
        if not purchased:
            current = await db(get_user, guild_id, user_id)
            balance = current.get("balance", 0) if current else 0
            await interaction.response.send_message(f"❌ You need **{item_data['price']:,} 🍬** but only have **{balance:,} 🍬**.", ephemeral=True)
            return
        await interaction.response.send_message(f"🛒 You bought **{item_data['name']}** for **{item_data['price']:,} 🍬**! Your item is now in your inventory.")

    @app_commands.command(name="shop", description="View the Halloween Candy shop.")
    async def shop(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛒 Halloween Candy Shop",
            description="Spend your hard-earned 🍬 Candy on spooky collectibles.",
            color=discord.Color.orange(),
        )
        for item in SHOP_ITEMS:
            embed.add_field(
                name=f"{item['name']} — {item['price']:,} 🍬",
                value=item["description"],
                inline=False,
            )
        embed.set_footer(text="Shop catalog • More purchasing features can be added later")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="profile", description="View your Candy profile.")
    async def profile(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        user = await db(ensure_user, interaction.guild.id, interaction.user.id, interaction.user.name, interaction.user.display_name)
        inventory = user.get("inventory", [])
        embed = discord.Embed(
            title=f"🎃 {interaction.user.display_name}'s Profile",
            color=discord.Color.orange(),
        )
        embed.add_field(name="🍬 Candy", value=f"{user['balance']:,}", inline=True)
        embed.add_field(name="🎒 Inventory", value=str(len(inventory)), inline=True)
        await interaction.response.send_message(embed=embed)


async def setup_bot(bot):
    await bot.add_cog(HalloweenBot(bot))

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        log.error("Command /%s failed: %r", getattr(interaction.command, "name", "?"), original, exc_info=original)
        if isinstance(original, PyMongoError):
            msg = "🎃 The candy vault is unreachable right now. Please try again in a moment."
        else:
            msg = "❌ Something went wrong running that command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    @bot.event
    async def on_disconnect():
        if STATE["discord"]["state"] == "online":
            STATE["discord"]["state"] = "reconnecting"

    @bot.event
    async def on_resumed():
        STATE["discord"]["state"] = "online"

    @bot.event
    async def on_ready():
        d = STATE["discord"]
        d.update(state="online", user=str(bot.user), connected_at=now_utc(),
                 last_error=None, retry_until=None)
        log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
        log.info(f"Connected to {len(bot.guilds)} guild(s).")
        if not getattr(bot, "_commands_synced", False):
            synced = await bot.tree.sync()
            bot._commands_synced = True
            log.info(f"Synced {len(synced)} application command(s).")


async def check_database():
    try:
        await db(mongo.admin.command, "ping")
        log.info("MongoDB connection OK.")
    except PyMongoError as exc:
        log.error(
            "MongoDB check FAILED (%s). Commands will not work until MONGODB_URI "
            "(username/password/network access) is fixed.", exc.__class__.__name__
        )


async def park_forever(reason: str):
    """Keep the web server (and /status) alive instead of crash-looping and re-hitting Discord."""
    log.error("%s Bot is stopped; /status stays up. Fix the cause and redeploy.", reason)
    await asyncio.Event().wait()


async def main():
    await check_database()
    d = STATE["discord"]
    retry_delay = 30
    while True:
        bot = create_bot()
        BOT["instance"] = bot
        await setup_bot(bot)
        d["attempts"] += 1
        d["last_attempt"] = now_utc()
        d["state"] = "connecting"
        d["retry_until"] = None
        try:
            await bot.start(DISCORD_TOKEN)
            return
        except discord.HTTPException as exc:
            await bot.close()
            if exc.status != 429:
                d.update(state="error", last_error=f"Discord HTTP {exc.status}")
                await park_forever(f"Discord returned HTTP {exc.status} during login ({exc.text[:200]!r}).")
            info = describe_rate_limit(exc)
            wait = retry_delay
            if info["retry_after"]:
                wait = max(wait, min(info["retry_after"] + 5, 3600))
            log.warning(
                "Discord 429 during login: %s | code=%s | headers=%s | body=%r",
                info["label"], getattr(exc, "code", None), info["headers"], info["body"],
            )
            log.warning("Waiting %ss before retry #%s.", int(wait), d["attempts"] + 1)
            d.update(
                state="rate_limited",
                rate_limit={**info, "at": now_utc()},
                last_error=info["label"],
                retry_until=now_utc() + timedelta(seconds=wait),
            )
            await asyncio.sleep(wait)
            retry_delay = min(retry_delay * 2, 600)
        except discord.LoginFailure:
            await bot.close()
            d.update(state="error", last_error="Invalid Discord token")
            await park_forever("Discord rejected DISCORD_TOKEN (invalid token).")
        except discord.PrivilegedIntentsRequired:
            await bot.close()
            d.update(state="error", last_error="Privileged intents not enabled")
            await park_forever("A privileged intent is required but not enabled in the Developer Portal.")
        except Exception as exc:
            await bot.close()
            d.update(state="error", last_error=exc.__class__.__name__)
            log.exception("Unexpected error while running the bot")
            await park_forever("Unexpected error.")


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main())
