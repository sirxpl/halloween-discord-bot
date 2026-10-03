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
from flask_session import Session
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
from urllib.parse import urlencode
import secrets
import requests
import re

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
command_access = db["command_access"]
economy_config = db["economy_config"]
activity_logs = db["activity_logs"]
logging_config = db["logging_config"]
member_controls = db["member_controls"]
daily_boosts = db["daily_boosts"]
arcane_level_rewards = db["arcane_level_rewards"]

ADMIN_USER_IDS = {777341204047331348, 793723672225382452}

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
app.config["SESSION_TYPE"] = "mongodb"
app.config["SESSION_MONGODB"] = mongo
app.config["SESSION_MONGODB_DB"] = "halloween_bot"
app.config["SESSION_MONGODB_COLLECT"] = "web_sessions"
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=31)
Session(app)


@app.before_request
def require_web_login_screen():
    if request.method not in {"GET", "HEAD"}:
        return None
    public_endpoints = {"home", "dashboard", "login", "oauth_callback", "logout", "status_json", "health"}
    if request.endpoint in public_endpoints:
        return None
    if not session.get("discord_user"):
        return redirect(url_for("dashboard"))
    return None


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

    # Keep the Flask session small enough for a browser cookie. Discord returns
    # many extra fields for guilds that the dashboard does not need.
    compact_guilds = [
        {
            "id": str(guild.get("id")),
            "name": guild.get("name", "Unnamed Server"),
            "permissions": str(guild.get("permissions", "0")),
        }
        for guild in guilds
        if guild.get("id")
    ]

    # Make the login session persistent and store only the data the dashboard
    # actually needs. This prevents large Discord guild payloads from causing
    # the session cookie to be dropped, which can make a successful login look
    # like the user is still signed out.
    session.permanent = True
    session["discord_user"] = me
    session["discord_guilds"] = compact_guilds
    if compact_guilds:
        session["selected_guild_id"] = compact_guilds[0]["id"]
    else:
        session.pop("selected_guild_id", None)

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


def bot_guilds():
    bot = BOT.get("instance")
    if bot is None or bot.is_closed():
        return []
    return sorted(
        [{"id": str(guild.id), "name": guild.name} for guild in bot.guilds],
        key=lambda item: item["name"].lower(),
    )


def selected_bot_guild():
    guilds = bot_guilds()
    wanted = str(request.args.get("guild") or session.get("daily_guild_id") or "")
    guild = next((g for g in guilds if str(g.get("id")) == wanted), None)
    if guild:
        session["daily_guild_id"] = str(guild["id"])
        return guild
    if guilds:
        session["daily_guild_id"] = str(guilds[0]["id"])
        return guilds[0]
    session.pop("daily_guild_id", None)
    return None


def get_bot_member(guild_id, user_id):
    bot = BOT.get("instance")
    if bot is None or bot.is_closed():
        return None
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return None
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    try:
        future = asyncio.run_coroutine_threadsafe(
            guild.fetch_member(int(user_id)),
            bot.loop,
        )
        return future.result(timeout=5)
    except Exception:
        log.exception("Could not fetch Discord member %s in guild %s", user_id, guild_id)
        return None


DAILY_REWARD = 100
ARCANE_BOT_IDS = {1217870452253397082, 437808476106784770}
ARCANE_LEVEL_BASE_BONUS = 250
ARCANE_LEVEL_BONUS_PER_LEVEL = 5
ARCANE_LEVEL_PATTERN = re.compile(r"<@!?(\d+)>\s+has reached level\s+\*\*(\d+)\*\*\.\s+GG!$", re.IGNORECASE)

TRICK_OR_TREAT_MIN = 25
TRICK_OR_TREAT_MAX = 150


def is_admin(user_id):
    return int(user_id) in ADMIN_USER_IDS


def is_command_enabled(command_name):
    record = command_access.find_one({"command_name": command_name})
    return record is None or record.get("enabled", True)



def get_economy_config():
    record = economy_config.find_one({"_id": "global"}) or {}
    return {
        "daily_reward": int(record.get("daily_reward", DAILY_REWARD)),
        "trick_or_treat_min": int(record.get("trick_or_treat_min", TRICK_OR_TREAT_MIN)),
        "trick_or_treat_max": int(record.get("trick_or_treat_max", TRICK_OR_TREAT_MAX)),
    }


def set_economy_config(daily_reward, trick_or_treat_min, trick_or_treat_max):
    economy_config.update_one(
        {"_id": "global"},
        {"$set": {
            "daily_reward": int(daily_reward),
            "trick_or_treat_min": int(trick_or_treat_min),
            "trick_or_treat_max": int(trick_or_treat_max),
            "updated_at": now_utc(),
        }},
        upsert=True,
    )


def get_member_controls(guild_id, user_id):
    record = member_controls.find_one({"guild_id": int(guild_id), "user_id": int(user_id)}) or {}
    return {"blocked_from_candy": bool(record.get("blocked_from_candy", False)), "leaderboard_excluded": bool(record.get("leaderboard_excluded", False))}

def set_member_control(guild_id, user_id, field, enabled, updated_by=None):
    if field not in {"blocked_from_candy", "leaderboard_excluded"}:
        raise ValueError("Unsupported member control.")
    member_controls.update_one({"guild_id": int(guild_id), "user_id": int(user_id)}, {"$set": {
        field: bool(enabled), "guild_id": int(guild_id), "user_id": int(user_id),
        "updated_at": now_utc(), "updated_by": int(updated_by) if updated_by else None,
    }}, upsert=True)


def get_daily_boosts(guild_id):
    return list(daily_boosts.find({"guild_id": int(guild_id)}).sort("multiplier", -1))


def set_daily_boost(guild_id, role_id, role_name, multiplier, updated_by=None):
    multiplier = float(multiplier)
    if multiplier <= 1 or multiplier > 5:
        raise ValueError("Daily reward multiplier must be greater than 1 and no more than 5.")
    daily_boosts.update_one(
        {"guild_id": int(guild_id), "role_id": int(role_id)},
        {"$set": {
            "guild_id": int(guild_id),
            "role_id": int(role_id),
            "role_name": str(role_name),
            "multiplier": multiplier,
            "updated_at": now_utc(),
            "updated_by": int(updated_by) if updated_by else None,
        }},
        upsert=True,
    )


def remove_daily_boost(guild_id, role_id):
    daily_boosts.delete_one({"guild_id": int(guild_id), "role_id": int(role_id)})


def calculate_daily_reward(base_reward, role_ids, guild_id):
    boosts = get_daily_boosts(guild_id)
    role_ids = {int(role_id) for role_id in role_ids}
    matching = [boost for boost in boosts if int(boost.get("role_id", 0)) in role_ids]
    if not matching:
        return int(base_reward), None
    boost = max(matching, key=lambda item: float(item.get("multiplier", 1)))
    multiplier = float(boost.get("multiplier", 1))
    return max(1, int(round(int(base_reward) * multiplier))), boost


LOG_CATEGORIES = ("economy", "member_activity", "shop", "admin", "errors", "system")


def get_logging_config(guild_id):
    record = logging_config.find_one({"_id": str(guild_id)}) or {}
    return {"enabled": bool(record.get("enabled", True)), "mode": record.get("mode", "simple"),
            "main": record.get("main", {"type": "channel", "channel_id": ""}),
            "advanced": record.get("advanced", {}), "updated_at": record.get("updated_at")}


def save_logging_config(guild_id, mode, main, advanced):
    logging_config.update_one({"_id": str(guild_id)},
        {"$set": {"guild_id": int(guild_id), "mode": mode, "main": main, "advanced": advanced, "updated_at": now_utc()}},
        upsert=True)


def log_category(action):
    if action in {"daily", "trick_or_treat", "give", "web_buy"}: return "economy"
    if action == "buy": return "shop"
    if action in {"admin_add", "admin_subtract"}: return "admin"
    if action == "member_control": return "member_activity"
    if action.startswith("error"): return "errors"
    return "system"


async def _send_discord_log(guild_id, action, username, amount, details):
    cfg = get_logging_config(guild_id)
    if not cfg.get("enabled", True):
        return
    category = log_category(action)
    destination = cfg["advanced"].get(category) if cfg["mode"] == "advanced" else None
    if not destination: destination = cfg["main"]
    if not destination or not destination.get("type"): return
    message = f"**{action.replace('_', ' ').title()}**"
    if username: message += f" • {username}"
    if amount is not None: message += f" • {amount:,} 🍬"
    if details: message += f"\nDetails: {str(details)[:900]}"
    try:
        if destination["type"] == "webhook":
            webhook_url = (destination.get("url") or "").strip()
            if not webhook_url:
                raise ValueError("Webhook URL is empty.")
            response = requests.post(
                webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true",
                json={
                    "username": "Aureolis Logs",
                    "content": message,
                    "allowed_mentions": {"parse": []},
                },
                headers={"Content-Type": "application/json"},
                timeout=8,
            )
            if not response.ok:
                log.error(
                    "Discord webhook delivery failed: HTTP %s: %s",
                    response.status_code,
                    response.text[:500],
                )
        elif destination["type"] == "channel":
            bot = BOT.get("instance")
            channel = bot.get_channel(int(destination.get("channel_id", 0))) if bot else None
            if channel: await channel.send(message)
    except Exception:
        log.exception("Could not deliver activity log to Discord.")


def queue_discord_log(guild_id, action, username, amount, details):
    if not guild_id: return
    bot = BOT.get("instance")
    if not bot or bot.is_closed(): return
    try:
        asyncio.run_coroutine_threadsafe(_send_discord_log(guild_id, action, username, amount, details), bot.loop)
    except Exception:
        log.exception("Could not queue Discord activity log.")


def log_activity(action, user_id=None, username=None, guild_id=None, amount=None, details=None):
    activity_logs.insert_one({
        "action": action, "user_id": int(user_id) if user_id is not None else None,
        "username": username, "guild_id": int(guild_id) if guild_id is not None else None,
        "amount": amount, "details": details, "created_at": now_utc(),
    })
    queue_discord_log(guild_id, action, username, amount, details)


def set_command_enabled(command_name, enabled):
    command_access.update_one(
        {"command_name": command_name},
        {"$set": {"command_name": command_name, "enabled": bool(enabled), "updated_at": now_utc()}},
        upsert=True,
    )


@app.get("/economy")
def economy_page():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    config = get_economy_config()
    try:
        total_users = users.count_documents({})
        total_candy = sum((doc.get("balance", 0) or 0) for doc in users.find({}, {"balance": 1}))
        recent = list(activity_logs.find({"action": {"$in": ["daily", "trick_or_treat", "give", "buy", "web_buy"]}}).sort("created_at", -1).limit(12))
    except PyMongoError:
        total_users, total_candy, recent = 0, 0, []
    return render_template("economy.html", config=config, total_users=total_users, total_candy=total_candy, recent=recent)

@app.post("/economy/settings")
def economy_settings():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        daily_reward = int(request.form.get("daily_reward", DAILY_REWARD))
        minimum = int(request.form.get("trick_or_treat_min", TRICK_OR_TREAT_MIN))
        maximum = int(request.form.get("trick_or_treat_max", TRICK_OR_TREAT_MAX))
        if daily_reward < 1 or minimum < 1 or maximum < minimum or maximum > 1000000:
            raise ValueError
    except (TypeError, ValueError):
        return redirect(url_for("economy_page", error="invalid"))
    set_economy_config(daily_reward, minimum, maximum)
    log_activity("economy_settings", user_id=user["id"], username=user.get("username"), details={"daily_reward": daily_reward, "trick_or_treat_min": minimum, "trick_or_treat_max": maximum})
    return redirect(url_for("economy_page", saved="1"))


@app.get("/logging")
def logging_page():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    bot = BOT.get("instance")
    guilds = []
    if bot and not bot.is_closed():
        guilds = [{"id": str(g.id), "name": g.name} for g in sorted(bot.guilds, key=lambda item: item.name.lower())]
    guild_id = request.args.get("guild") or (guilds[0]["id"] if guilds else "")
    if guild_id and not any(str(g["id"]) == str(guild_id) for g in guilds):
        guild_id = guilds[0]["id"] if guilds else ""
    logs = list(activity_logs.find({"guild_id": int(guild_id)}).sort("created_at", -1).limit(100)) if guild_id else []
    config = get_logging_config(guild_id) if guild_id else {"enabled": True, "mode": "simple", "main": {"type": "channel", "channel_id": ""}, "advanced": {}}
    channels = []
    if bot and not bot.is_closed() and guild_id:
        guild = bot.get_guild(int(guild_id))
        if guild:
            channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
    return render_template("logging.html", logs=logs, logging_config=config, logging_guilds=guilds,
                           logging_guild_id=str(guild_id), logging_channels=channels, log_categories=LOG_CATEGORIES)


@app.post("/logging/settings")
def logging_settings():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        guild_id = int(request.form.get("guild_id", ""))
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if guild is None:
            raise ValueError
        enabled = request.form.get("enabled") == "on"
        mode = request.form.get("mode", "simple")
        if mode not in {"simple", "advanced"}: raise ValueError
        previous = get_logging_config(guild_id)
        typ = request.form.get("destination_type", "channel")
        if typ == "webhook":
            url = request.form.get("webhook_url", "").strip() or previous.get("main", {}).get("url", "")
            if not (url.startswith("https://discord.com/api/webhooks/") or url.startswith("https://discordapp.com/api/webhooks/")): raise ValueError
            main = {"type": "webhook", "url": url}
        else:
            cid = request.form.get("channel_id", "").strip()
            if not cid.isdigit(): raise ValueError
            channel = guild.get_channel(int(cid))
            if channel is None or not isinstance(channel, discord.TextChannel): raise ValueError
            main = {"type": "channel", "channel_id": cid}
        advanced = {}
        for category in LOG_CATEGORIES:
            ctype = request.form.get(f"{category}_type", "inherit")
            if ctype == "channel":
                cid = request.form.get(f"{category}_channel_id", "").strip()
                if not cid.isdigit(): raise ValueError
                advanced[category] = {"type": "channel", "channel_id": cid}
            elif ctype == "webhook":
                url = request.form.get(f"{category}_webhook_url", "").strip() or previous.get("advanced", {}).get(category, {}).get("url", "")
                if not (url.startswith("https://discord.com/api/webhooks/") or url.startswith("https://discordapp.com/api/webhooks/")): raise ValueError
                advanced[category] = {"type": "webhook", "url": url}
        logging_config.update_one(
            {"_id": str(guild_id)},
            {"$set": {"guild_id": guild_id, "enabled": enabled, "mode": mode, "main": main, "advanced": advanced, "updated_at": now_utc()}},
            upsert=True,
        )
        return redirect(url_for("logging_page", guild=guild_id, saved="1"))
    except (TypeError, ValueError):
        return redirect(url_for("logging_page", guild=request.form.get("guild_id", ""), error="invalid"))


@app.post("/logging/test")
def logging_test():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id = request.form.get("guild_id", "")
    try:
        bot = BOT.get("instance")
        if not bot or bot.is_closed(): raise RuntimeError
        asyncio.run_coroutine_threadsafe(_send_discord_log(int(guild_id), "logging_test", user.get("username"), None, {"test": True}), bot.loop)
        return redirect(url_for("logging_page", guild=guild_id, tested="1"))
    except Exception:
        return redirect(url_for("logging_page", guild=guild_id, error="test"))


@app.get("/profile")
def profile_page():
    user = session.get("discord_user")
    if not user:
        return render_template(
            "profile.html",
            logged_in=False,
            user=None,
            avatar_url=None,
            guild=None,
            profile=None,
            rank=None,
            account_created=None,
            next_daily=None,
            inventory_value=0,
        )
    guild = selected_guild()
    profile = None
    rank = None
    inventory_value = 0
    next_daily = None
    if guild:
        guild_id = int(guild["id"])
        profile = users.find_one({"guild_id": guild_id, "user_id": int(user["id"])})
        if profile:
            rank = users.count_documents({"guild_id": guild_id, "balance": {"$gt": profile.get("balance", 0)}}) + 1
            inventory_value = sum((item.get("price", 0) or 0) for item in profile.get("inventory", []))
            if profile.get("last_daily"):
                next_daily = profile["last_daily"] + timedelta(hours=24)
    account_created = None
    try:
        account_created = discord.utils.snowflake_time(int(user["id"]))
    except (ValueError, TypeError):
        pass
    return render_template(
        "profile.html",
        logged_in=True,
        user=user,
        avatar_url=discord_avatar_url(user),
        guild=guild,
        profile=profile,
        rank=rank,
        account_created=account_created,
        next_daily=next_daily,
        inventory_value=inventory_value,
    )


@app.get("/users")
def users_page():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403

    # Users are tracked globally across every server. A record exists here
    # whenever someone has interacted with the bot's economy features.
    search = request.args.get("q", "").strip()
    query = {}
    if search:
        terms = [
            {"username": {"$regex": search, "$options": "i"}},
            {"display_name": {"$regex": search, "$options": "i"}},
        ]
        if search.isdigit():
            terms.append({"user_id": int(search)})
        query["$or"] = terms

    records = list(users.find(query).sort("balance", -1).limit(500))

    bot = BOT.get("instance")
    guild_names = {}
    if bot is not None:
        guild_names = {int(guild.id): guild.name for guild in bot.guilds}

    # Group server-specific records into one global user entry.
    grouped = {}
    for record in records:
        uid = int(record.get("user_id", 0))
        if not uid:
            continue
        guild_id = int(record.get("guild_id", 0))
        record["server_name"] = guild_names.get(guild_id)
        entry = grouped.setdefault(uid, {
            "user_id": uid,
            "username": record.get("username"),
            "display_name": record.get("display_name"),
            "total_balance": 0,
            "servers": [],
        })
        entry["username"] = record.get("username") or entry["username"]
        entry["display_name"] = record.get("display_name") or entry["display_name"]
        entry["total_balance"] += int(record.get("balance", 0) or 0)
        entry["servers"].append(record)

    global_users = sorted(
        grouped.values(),
        key=lambda item: (-item["total_balance"], (item.get("display_name") or item.get("username") or "").lower())
    )[:100]

    for entry in global_users:
        server_ids = [int(record.get("guild_id", 0)) for record in entry["servers"] if record.get("guild_id")]
        control_map = {
            (int(item["guild_id"]), int(item["user_id"])): item
            for item in member_controls.find(
                {"user_id": entry["user_id"], "guild_id": {"$in": server_ids}}
            )
        }
        for record in entry["servers"]:
            control = control_map.get(
                (int(record.get("guild_id", 0)), entry["user_id"]), {}
            )
            record["blocked_from_candy"] = bool(control.get("blocked_from_candy", False))
            record["leaderboard_excluded"] = bool(control.get("leaderboard_excluded", False))

    return render_template(
        "users.html",
        user=user,
        avatar_url=discord_avatar_url(user),
        search=search,
        users=global_users,
    )


@app.post("/users/candy")
def users_candy():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        guild_id = int(request.form.get("guild_id", ""))
        member_id = int(request.form.get("member_id", ""))
        amount = int(request.form.get("amount", "0"))
        action = request.form.get("action", "")
        if amount < 1 or action not in {"add", "subtract"}:
            raise ValueError
        if action == "add":
            result = users.find_one_and_update(
                {"guild_id": guild_id, "user_id": member_id},
                {"$inc": {"balance": amount}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        else:
            result = users.find_one_and_update(
                {"guild_id": guild_id, "user_id": member_id, "balance": {"$gte": amount}},
                {"$inc": {"balance": -amount}},
                return_document=ReturnDocument.AFTER,
            )
            if result is None:
                return redirect(url_for("users_page", q=request.form.get("q", ""), error="subtract"))
        log_activity("admin_add" if action == "add" else "admin_subtract",
                     user_id=member_id, guild_id=guild_id, amount=amount,
                     details={"changed_by": user["id"], "source": "web_users"})
        return redirect(url_for("users_page", q=request.form.get("q", ""), saved=action))
    except (TypeError, ValueError):
        return redirect(url_for("users_page", q=request.form.get("q", ""), error="invalid"))


@app.post("/users/control")
def users_control():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        guild_id = int(request.form.get("guild_id", ""))
        member_id = int(request.form.get("member_id", ""))
        field = request.form.get("field", "")
        enabled = request.form.get("enabled") == "1"
        if field not in {"blocked_from_candy", "leaderboard_excluded"}:
            raise ValueError
        set_member_control(guild_id, member_id, field, enabled, user["id"])
        log_activity("member_control", user_id=member_id, guild_id=guild_id,
                     details={"control": field, "enabled": enabled, "source": "web_users", "changed_by": user["id"]})
        return redirect(url_for("users_page", q=request.form.get("q", ""), saved="control"))
    except (TypeError, ValueError):
        return redirect(url_for("users_page", q=request.form.get("q", ""), error="invalid"))


@app.get("/access-control")
def access_control_page():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return render_template(
            "access_control.html",
            user=user,
            avatar_url=discord_avatar_url(user),
            allowed=False,
            bot_guilds=[],
            member_controls=[],
        ), 403

    bot = BOT.get("instance")
    bot_guilds = []
    slash_commands = []
    selected_boost_guild_id = str(request.args.get("boost_guild") or "")
    boost_roles = []
    if bot is not None and not bot.is_closed():
        bot_guilds = sorted(
            [{"id": str(guild.id), "name": guild.name, "member_count": guild.member_count or 0}
             for guild in bot.guilds],
            key=lambda item: item["name"].lower(),
        )
        if not selected_boost_guild_id and bot_guilds:
            selected_boost_guild_id = bot_guilds[0]["id"]
        selected_boost_guild = bot.get_guild(int(selected_boost_guild_id)) if selected_boost_guild_id.isdigit() else None
        if selected_boost_guild is not None:
            boost_roles = sorted(
                [{"id": str(role.id), "name": role.name, "position": role.position}
                 for role in selected_boost_guild.roles if not role.is_default()],
                key=lambda item: (-item["position"], item["name"].lower()),
            )
        slash_commands = sorted(
            [{
                "name": command.name,
                "description": command.description or "No description available.",
                "enabled": is_command_enabled(command.name),
            } for command in bot.tree.get_commands()],
            key=lambda item: item["name"].lower(),
        )
    saved_daily_boosts = get_daily_boosts(int(selected_boost_guild_id)) if selected_boost_guild_id.isdigit() else []
    return render_template(
        "access_control.html",
        user=user,
        avatar_url=discord_avatar_url(user),
        allowed=True,
        bot_guilds=bot_guilds,
        slash_commands=slash_commands,
        member_controls=list(member_controls.find({}).sort("updated_at", -1).limit(100)),
        boost_guild_id=selected_boost_guild_id,
        boost_roles=boost_roles,
        daily_boosts=saved_daily_boosts,
    )


@app.post("/access-control/member-control")
def access_control_member_control():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        guild_id = int(request.form.get("guild_id", ""))
        member_id = int(request.form.get("member_id", ""))
        field = request.form.get("field", "")
        enabled = request.form.get("enabled") == "1"
        if field not in {"blocked_from_candy", "leaderboard_excluded"}:
            raise ValueError
        set_member_control(guild_id, member_id, field, enabled, user["id"])
        log_activity("member_control", user_id=member_id, guild_id=guild_id,
                     details={"control": field, "enabled": enabled, "changed_by": user["id"]})
    except (TypeError, ValueError):
        return redirect(url_for("access_control_page", error="invalid_member"))
    return redirect(url_for("access_control_page", saved="member"))


@app.post("/access-control/daily-boost")
def access_control_daily_boost():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        guild_id = int(request.form.get("guild_id", ""))
        role_id = int(request.form.get("role_id", ""))
        multiplier = float(request.form.get("multiplier", ""))
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        role = guild.get_role(role_id) if guild else None
        if guild is None or role is None or role.is_default():
            raise ValueError
        set_daily_boost(guild_id, role_id, role.name, multiplier, user["id"])
        log_activity("daily_boost", user_id=user["id"], username=user.get("username"), guild_id=guild_id,
                     details={"role_id": role_id, "role_name": role.name, "multiplier": multiplier, "changed_by": user["id"]})
    except (TypeError, ValueError):
        return redirect(url_for("access_control_page", boost_guild=request.form.get("guild_id", ""), error="invalid_boost"))
    return redirect(url_for("access_control_page", boost_guild=guild_id, saved="boost"))


@app.post("/access-control/daily-boost/remove")
def access_control_daily_boost_remove():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        guild_id = int(request.form.get("guild_id", ""))
        role_id = int(request.form.get("role_id", ""))
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if guild is None or guild.get_role(role_id) is None:
            raise ValueError
        remove_daily_boost(guild_id, role_id)
        log_activity("daily_boost_removed", user_id=user["id"], username=user.get("username"), guild_id=guild_id,
                     details={"role_id": role_id, "changed_by": user["id"]})
    except (TypeError, ValueError):
        return redirect(url_for("access_control_page", boost_guild=request.form.get("guild_id", ""), error="invalid_boost"))
    return redirect(url_for("access_control_page", boost_guild=guild_id, saved="boost_removed"))


@app.post("/access-control/command/<command_name>")
def access_control_command(command_name):
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403

    bot = BOT["instance"]
    if bot is None or bot.is_closed():
        return redirect(url_for("access_control_page"))

    command = next(
        (item for item in bot.tree.get_commands() if item.name == command_name),        None,
    )
    if command is None:
        return redirect(url_for("access_control_page"))

    enabled = request.form.get("enabled") == "1"
    set_command_enabled(command.name, enabled)
    return redirect(url_for("access_control_page"))


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
    user = session.get("discord_user")
    guilds = bot_guilds()
    guild = selected_bot_guild() if user else None
    profile = None
    next_daily = None
    if user and guild:
        profile = users.find_one({"guild_id": int(guild["id"]), "user_id": int(user["id"])})
        if profile and profile.get("last_daily"):
            next_daily = profile["last_daily"] + timedelta(hours=24)
    return render_template(
        "daily.html",
        user=user,
        guild=guild,
        profile=profile,
        next_daily=next_daily,
        daily_reward=get_economy_config()["daily_reward"],
        daily_guilds=guilds,
    )


@app.post("/daily/claim")
def daily_claim():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild = selected_bot_guild()
    if not guild:
        return redirect(url_for("daily_page", error="no_server"))

    guild_id = int(guild["id"])
    user_id = int(user["id"])
    if get_member_controls(guild_id, user_id)["blocked_from_candy"]:
        return redirect(url_for("daily_page", guild=guild_id, error="restricted"))
    member = get_bot_member(guild_id, user_id)
    role_ids = [role.id for role in member.roles] if member else []
    reward, boost = calculate_daily_reward(get_economy_config()["daily_reward"], role_ids, guild_id)
    now = now_utc()
    ensure_user(
        guild_id,
        user_id,
        user.get("username"),
        user.get("global_name") or user.get("username"),
    )
    cutoff = now - timedelta(hours=24)
    updated = users.find_one_and_update(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "$or": [
                {"last_daily": {"$exists": False}},
                {"last_daily": {"$lte": cutoff}},
            ],
        },
        {"$set": {"last_daily": now}, "$inc": {"balance": reward}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return redirect(url_for("daily_page", guild=guild_id, error="cooldown"))
    log_activity(
        "daily",
        user_id=user_id,
        username=user.get("username"),
        guild_id=guild_id,
        amount=reward,
        details={"boost_role": boost.get("role_name") if boost else None, "multiplier": boost.get("multiplier") if boost else 1, "source": "web_daily"},
    )
    return redirect(url_for("daily_page", guild=guild_id, claimed="1", reward=reward, boost=boost.get("role_name") if boost else ""))


@app.get("/leaderboard")
def leaderboard_page():
    try:
        excluded_pairs = {
            (int(record["guild_id"]), int(record["user_id"]))
            for record in member_controls.find(
                {"leaderboard_excluded": True}, {"guild_id": 1, "user_id": 1}
            )
        }
        candidates = list(users.find({}).sort("balance", -1).limit(200))
        top_users = [doc for doc in candidates if (int(doc.get("guild_id", 0)), int(doc.get("user_id", 0))) not in excluded_pairs][:25]
        discord_bot = BOT["instance"]
        leaderboard = []
        for doc in top_users:
            user_id = doc.get("user_id")
            discord_user = discord_bot.get_user(user_id) if discord_bot else None

            # Existing MongoDB records may not have a stored username yet.            # If the user is not cached, fetch their Discord account directly
            # through the bot's running event loop instead of showing the ID.
            if discord_user is None and discord_bot and not discord_bot.is_closed():
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
                    log.exception("Could not fetch Discord profile for %s", user_id)


            name = (
                f"@{doc.get('username')}"
                if doc.get("username")
                else (f"@{discord_user.name}" if discord_user else "@Unknown User")
            )
            avatar_url = None
            if discord_user:
                try:
                    avatar_url = str(discord_user.display_avatar.url)
                except Exception:
                    avatar_url = None

            leaderboard.append({
                "name": name,
                "balance": doc.get("balance", 0) or 0,
                "avatar_url": avatar_url,
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
        "web": {"ok": True},    }


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
# Arcane level-up rewards read the level-up message, so Message Content is required.
# Keep this opt-in so the bot does not request the privileged intent until Discord allows it.
ARCANE_LEVEL_BONUS_ENABLED = os.getenv("ARCANE_LEVEL_BONUS_ENABLED", "false").lower() == "true"
intents.message_content = ARCANE_LEVEL_BONUS_ENABLED


class AccessControlledTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        if command is None:
            return True

        if await db(is_command_enabled, command.name):
            return True

        await interaction.response.send_message(
            f"🚫 **/{command.name}** is currently disabled by the bot administrator.",
            ephemeral=True,
        )
        return False


def create_bot():
    return commands.Bot(
        command_prefix="!",
        intents=intents,
        tree_cls=AccessControlledTree,
    )


class HalloweenBot(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not ARCANE_LEVEL_BONUS_ENABLED:
            return
        if message.guild is None or message.author.id not in ARCANE_BOT_IDS:
            return

        # Arcane can send the level-up text either as normal message content
        # or inside an embed, so inspect both formats.
        message_texts = []
        if message.content:
            message_texts.append(message.content)
        for embed in message.embeds:
            if embed.title:
                message_texts.append(embed.title)
            if embed.description:
                message_texts.append(embed.description)
            for field in embed.fields:
                if field.name:
                    message_texts.append(field.name)
                if field.value:
                    message_texts.append(field.value)

        match = None
        for text in message_texts:
            match = ARCANE_LEVEL_PATTERN.fullmatch(text.strip())
            if match:
                break
        if not match:
            log.info(
                "Arcane level-up message did not match in guild %s. content=%r embeds=%s",
                message.guild.id,
                message.content,
                len(message.embeds),
            )
            return

        user_id = int(match.group(1))
        level = int(match.group(2))
        bonus = ARCANE_LEVEL_BASE_BONUS + (level * ARCANE_LEVEL_BONUS_PER_LEVEL)
        guild_id = message.guild.id

        # Use a unique document per guild/member/level so duplicate deliveries
        # or repeated Arcane test messages cannot award the same level twice.
        claim = await db(
            arcane_level_rewards.update_one,
            {"guild_id": guild_id, "user_id": user_id, "level": level},
            {
                "$setOnInsert": {
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "level": level,
                    "bonus": bonus,
                    "created_at": now_utc(),
                }
            },
            upsert=True,
        )
        if claim.upserted_id is None:
            return

        await db(ensure_user, guild_id, user_id)
        updated = await db(add_candy, guild_id, user_id, ARCANE_LEVEL_BONUS)
        await db(
            log_activity,
            "arcane_level_bonus",
            user_id,
            f"<@{user_id}>",
            guild_id,
            ARCANE_LEVEL_BONUS,
            {"level": level, "source_bot_id": message.author.id},
        )
        log.info(
            "Awarded %s Candy to user %s for Arcane level %s in guild %s. New balance: %s",
            ARCANE_LEVEL_BONUS,
            user_id,
            level,
            guild_id,
            updated.get("balance", 0),
        )

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
        controls = await db(get_member_controls, guild_id, user_id)
        if controls["blocked_from_candy"]:
            await interaction.response.send_message("🚫 You are not allowed to participate in Candy activities in this server.", ephemeral=True)
            return
        now = now_utc()
        await db(ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name)
        config = get_economy_config()
        reward, boost = await db(
            calculate_daily_reward,
            config["daily_reward"],
            [role.id for role in getattr(interaction.user, "roles", [])],
            guild_id,
        )
        cutoff = now - timedelta(hours=24)
        updated = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": user_id,             "$or": [{"last_daily": {"$exists": False}}, {"last_daily": {"$lte": cutoff}}]},
            {"$set": {"last_daily": now}, "$inc": {"balance": reward}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            user = await db(get_user, guild_id, user_id)
            timestamp = int((user["last_daily"] + timedelta(hours=24)).timestamp())
            await interaction.response.send_message(
                f"⏰ You already claimed your daily Candy. Try again <t:{timestamp}:R>.", ephemeral=True
            )
            return
        await db(log_activity, "daily", interaction.user.id, interaction.user.name, guild_id, reward,
                 {"boost_role": boost.get("role_name") if boost else None, "multiplier": boost.get("multiplier") if boost else 1})
        if boost:
            await interaction.response.send_message(
                f"🎃 You claimed your daily reward: **+{reward:,} 🍬 Candy** (**{boost['multiplier']:g}× boost** from **{boost['role_name']}**)!"
            )
        else:
            await interaction.response.send_message(f"🎃 You claimed your daily reward: **+{reward:,} 🍬 Candy**!")

    @app_commands.command(name="trickortreat", description="Go trick-or-treating for a random Candy reward.")
    async def trick_or_treat(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        controls = await db(get_member_controls, guild_id, user_id)
        if controls["blocked_from_candy"]:
            await interaction.response.send_message("🚫 You are not allowed to participate in Candy activities in this server.", ephemeral=True)
            return
        now = now_utc()
        await db(ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name)
        config = get_economy_config()
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
        await db(log_activity, "trick_or_treat", interaction.user.id, interaction.user.name, guild_id, reward)
        await interaction.response.send_message(f"🎃 **Trick or treat!** You found **{reward:,} 🍬 Candy**!")

    @app_commands.command(name="add", description="Admin: add Candy to a member's balance.")
    @app_commands.describe(member="Member receiving Candy.", amount="Amount of Candy to add.")
    async def add(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000000]):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("🚫 Only Aureolis admins can use this command.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        await db(ensure_user, interaction.guild.id, member.id, member.name, member.display_name)
        updated = await db(users.find_one_and_update, {"guild_id": interaction.guild.id, "user_id": member.id},
            {"$inc": {"balance": int(amount)}}, return_document=ReturnDocument.AFTER)
        await db(log_activity, "admin_add", member.id, member.name, interaction.guild.id, int(amount), {"changed_by": interaction.user.id})
        await interaction.response.send_message(f"✅ Added **{amount:,} 🍬 Candy** to {member.mention}. New balance: **{updated['balance']:,}**.")

    @app_commands.command(name="subtract", description="Admin: subtract Candy from a member's balance.")
    @app_commands.describe(member="Member losing Candy.", amount="Amount of Candy to subtract.")
    async def subtract(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000000]):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("🚫 Only Aureolis admins can use this command.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        await db(ensure_user, interaction.guild.id, member.id, member.name, member.display_name)
        updated = await db(users.find_one_and_update, {"guild_id": interaction.guild.id, "user_id": member.id, "balance": {"$gte": int(amount)}},
            {"$inc": {"balance": -int(amount)}}, return_document=ReturnDocument.AFTER)
        if not updated:
            current = await db(get_user, interaction.guild.id, member.id)
            await interaction.response.send_message(f"❌ {member.mention} only has **{(current or {}).get('balance', 0):,} 🍬 Candy**, so that amount cannot be subtracted.", ephemeral=True)
            return
        await db(log_activity, "admin_subtract", member.id, member.name, interaction.guild.id, int(amount), {"changed_by": interaction.user.id})
        await interaction.response.send_message(f"✅ Subtracted **{amount:,} 🍬 Candy** from {member.mention}. New balance: **{updated['balance']:,}**.")

    @app_commands.command(name="give", description="Give Candy to another member.")
    @app_commands.describe(member="The member receiving Candy.", amount="Amount of Candy to give.")
    async def give(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("🤖 You can't give Candy to a bot.", ephemeral=True)
            return
        sender_controls = await db(get_member_controls, interaction.guild.id, interaction.user.id)
        recipient_controls = await db(get_member_controls, interaction.guild.id, member.id)
        if sender_controls["blocked_from_candy"]:
            await interaction.response.send_message("🚫 You are not allowed to participate in Candy activities in this server.", ephemeral=True)
            return
        if recipient_controls["blocked_from_candy"]:
            await interaction.response.send_message("🚫 That member is not allowed to participate in Candy activities in this server.", ephemeral=True)
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
        await db(log_activity, "give", interaction.user.id, interaction.user.name, interaction.guild.id, amount, {"recipient_id": member.id, "recipient": member.name})
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
                users.find({"guild_id": interaction.guild.id, "user_id": {"$nin": [
                        record["user_id"] for record in member_controls.find(
                            {"guild_id": interaction.guild.id, "leaderboard_excluded": True}, {"user_id": 1}
                        )
                    ]}}).sort("balance", -1).limit(10)
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
        if (await db(get_member_controls, guild_id, user_id))["blocked_from_candy"]:
            await interaction.response.send_message("🚫 You are not allowed to participate in Candy activities in this server.", ephemeral=True)
            return
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
        await db(log_activity, "buy", interaction.user.id, interaction.user.name, guild_id, item_data["price"], {"item": item_data["name"]})
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
                "Discord 429 during login: %s | code=%s | headers=%s | body=%r",
                exc, info.get("code"), info.get("headers"), info.get("body")
            )
            d.update(
                state="rate_limited",
                last_error=f"Discord HTTP 429; retrying in {wait} seconds",
                retry_until=now_utc() + timedelta(seconds=wait),
            )
            await asyncio.sleep(wait)
            retry_delay = min(max(retry_delay * 2, 30), 3600)


if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    asyncio.run(main())
