import os
import json
from typing import Optional
import time
import logging
import random
import threading
import asyncio
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from jinja2 import ChoiceLoader, FunctionLoader
from markupsafe import Markup, escape
from discord import app_commands
from discord.ext import commands
from flask import Flask, Response, jsonify, render_template, request, redirect, session, url_for
from flask_session import Session
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError, DuplicateKeyError
from dotenv import load_dotenv
from urllib.parse import urlencode, urlparse, parse_qs, quote
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
give_limits = db["give_limits"]
give_tax_settings = db["give_tax_settings"]
trick_cooldowns = db["trick_cooldowns"]
shop_items = db["shop_items"]
trivia_questions = db["trivia_questions"]
role_shop_items = db["role_shop_items"]
role_shop_claims = db["role_shop_claims"]
presentation_settings = db["presentation_settings"]
member_badge_preferences = db["member_badge_preferences"]
arcane_level_rewards = db["arcane_level_rewards"]
user_authorizations = db["user_authorizations"]
oauth_transactions = db["oauth_transactions"]
clans = db["clans"]
site_assets = db["site_assets"]
survey_submissions = db["survey_submissions"]
clan_creation_requests = db["clan_creation_requests"]
clan_join_requests = db["clan_join_requests"]
clan_invites = db["clan_invites"]
clan_config = db["clan_config"]
event_announcement_settings = db["event_announcement_settings"]
event_records = db["event_records"]

# Bump this whenever templates/terms.html or templates/privacy.html change in a way people
# should re-accept. Everyone whose stored version differs is asked to authorize again.
TOS_VERSION = "2026-10-04"

ADMIN_USER_IDS = {777341204047331348, 793723672225382452, 931543094086750299, 1012751329845841921, 920286069696655400, 1304116930956754985, 1133812673671331911, 765636656430579722, 1448753303533125682}
BADGE_TYPES = {
    "tax": ("Tax", "tax", "1559355733772861481"),
    "staff": ("Staff", "stafficon", "1559355732795727903"),
    "verified": ("Verified", "verified", "1559355731839557683"),
    "verified-plus": ("Verified+", "verifiedplus", "1559355730866212974"),
    "carry-team": ("Carry Team", "carryteamicon", "1559355729952112731"),
    "administrator": ("Administrator", "administratoricon", "1559355728446365726"),
}
CLAN_VERIFICATION_TYPES = ("verified", "verified-plus")
CLAN_DEFAULT_DISCOUNT_THRESHOLD = 5000
CLAN_DEFAULT_DAILY_THRESHOLD = 5000
CLAN_DISCOUNT_PERCENT = 10
CLAN_DAILY_MULTIPLIER = 1.25
CLAN_ICON_MAX_BYTES = 2 * 1024 * 1024
CLAN_BANNER_MAX_BYTES = 4 * 1024 * 1024
SURVEY_BANNER_MAX_BYTES = 4 * 1024 * 1024
SURVEY_REWARD_CANDY = 10_000
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = CLAN_BANNER_MAX_BYTES + CLAN_ICON_MAX_BYTES + 1024 * 1024
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
    public_endpoints = {"static", "home", "dashboard", "login", "oauth_callback", "logout", "status_page", "status_json", "health", "terms_page", "privacy_page", "authorize_page", "authorized_page", "clan_icon", "clan_banner", "survey_page", "survey_login", "survey_banner_asset"}
    if request.endpoint in public_endpoints:
        return None
    if not session.get("discord_user"):
        return redirect(url_for("dashboard"))
    return None


def _template_alias_loader(name):
    """Downloads/uploads sometimes drop the leading underscore, so templates/_sidebar.html
    ends up named templates/sidebar.html. Accept either name instead of crashing every page."""
    if name == "_sidebar.html":
        path = os.path.join(app.root_path, "templates", "sidebar.html")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read(), path, lambda: True
    return None


app.jinja_loader = ChoiceLoader([app.jinja_loader, FunctionLoader(_template_alias_loader)])


@app.get("/terms")
def terms_page():
    return render_template("terms.html")


@app.get("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.context_processor
def inject_sidebar():
    """Single source of truth for the shared sidebar (templates/_sidebar.html)."""
    user = session.get("discord_user")
    admin = False
    if user:
        try:
            admin = is_admin(user["id"])
        except (KeyError, TypeError, ValueError):
            admin = False
    return {
        "sidebar_user": user,
        "sidebar_avatar": discord_avatar_url(user) if user else None,
        "sidebar_is_admin": admin,
    }


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
        command_count=registered_command_count(9),
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


def clan_guild_options():
    guilds = bot_guilds()
    user = session.get("discord_user")
    if user and is_admin(user["id"]):
        return guilds
    member_guild_ids = {
        str(guild.get("id")) for guild in session.get("discord_guilds", [])
    }
    return [guild for guild in guilds if str(guild["id"]) in member_guild_ids]


def send_clan_dm(user_id, message):
    bot = BOT.get("instance")
    if bot is None or bot.is_closed():
        log.warning("Could not send clan notification to %s: Discord bot is offline.", user_id)
        return False

    async def send():
        target = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        await target.send(message, allowed_mentions=discord.AllowedMentions.none())

    try:
        _run_on_bot_loop(bot, send(), 12)
        return True
    except Exception:
        log.warning("Could not send clan notification to %s.", user_id, exc_info=True)
        return False


def clan_public_data(guild_id, clan):
    progress = clan_progress(guild_id, clan)
    verification_badge = clan_verification_presentation(clan.get("verification_badge"))
    return {
        "clan_id": clan["clan_id"],
        "name": clan["name"],
        "description": clan.get("description", ""),
        "owner_id": int(clan["owner_id"]),
        "icon_url": url_for("clan_icon", guild_id=int(guild_id), clan_id=clan["clan_id"])
            if clan.get("icon_data") else None,
        "banner_url": url_for("clan_banner", guild_id=int(guild_id), clan_id=clan["clan_id"])
            if clan.get("banner_data") else None,
        **verification_badge,
        **progress,
    }


def clan_verification_presentation(badge_type):
    if badge_type not in CLAN_VERIFICATION_TYPES:
        return {}
    label, emoji_name, emoji_id = BADGE_TYPES[badge_type]
    return {
        "verification_badge": badge_type,
        "verification_label": label,
        "verification_emoji": f"<:{emoji_name}:{emoji_id}>",
        "verification_icon_url": f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=64",
    }


def clan_member_list(guild_id, clan):
    records = list(users.find(
        {"guild_id": int(guild_id), "clan_id": str(clan["clan_id"])},
        {"user_id": 1, "username": 1, "candy_earned_total": 1, "clan_candy_earned_at_join": 1},
    ))
    member_keys = [
        (int(guild_id), int(record["user_id"]))
        for record in records
        if record.get("user_id") is not None
    ]
    discord_members = get_bot_members_for_display(member_keys)
    badge_configs = get_badge_configs(guild_id)
    clan_members = []
    for record in records:
        if record.get("user_id") is None:
            continue
        user_id = int(record["user_id"])
        member = discord_members.get((int(guild_id), user_id))
        avatar_url = None
        if member:
            try:
                avatar_url = str(member.display_avatar.url)
            except Exception:
                log.exception("Could not load clan member avatar for %s", user_id)
        badges, name_gradient, _, _ = badge_presentation_for_member(
            user_id,
            member,
            badge_configs,
            get_member_badge_preference(guild_id, user_id),
        )
        clan_members.append({
            "user_id": user_id,
            "name": f"@{member.name}" if member else (
                f"@{record['username']}" if record.get("username") else "@Unknown User"
            ),
            "avatar_url": avatar_url,
            "badges": badges,
            "name_gradient": name_gradient,
            "contributed": max(
                0,
                int(record.get("candy_earned_total", 0) or 0)
                - int(record.get("clan_candy_earned_at_join", 0) or 0),
            ),
            "is_owner": user_id == int(clan["owner_id"]),
        })
    return sorted(
        clan_members,
        key=lambda member: (-member["contributed"], member["name"].casefold(), member["user_id"]),
    )


def clan_member_contribution_leaderboard(guild_id, clan, limit=10):
    records = users.find(
        {"guild_id": int(guild_id), "clan_id": str(clan["clan_id"])},
        {"user_id": 1, "username": 1, "candy_earned_total": 1, "clan_candy_earned_at_join": 1},
    )
    contributions = [
        {
            "user_id": int(record["user_id"]),
            "username": str(record.get("username") or "Unknown User"),
            "contributed": max(
                0,
                int(record.get("candy_earned_total", 0) or 0)
                - int(record.get("clan_candy_earned_at_join", 0) or 0),
            ),
        }
        for record in records
        if record.get("user_id") is not None
    ]
    contributions.sort(
        key=lambda member: (-member["contributed"], member["username"].casefold(), member["user_id"])
    )
    return contributions[:max(0, int(limit))]


async def clan_name_autocomplete(interaction, current):
    if interaction.guild is None:
        return []
    pattern = re.escape(str(current or "").strip())
    matches = await db(lambda: list(
        clans.find({
            "guild_id": interaction.guild.id,
            "status": "active",
            "name": {"$regex": pattern, "$options": "i"},
        }).sort("name", 1).limit(25)
    ))
    return [
        app_commands.Choice(name=clan["name"][:100], value=clan["name"][:100])
        for clan in matches
    ]


def clan_upload_data():
    uploaded = request.files.get("clan_icon")
    if uploaded is None or not uploaded.filename:
        return None, None
    data = uploaded.stream.read(CLAN_ICON_MAX_BYTES + 1)
    if not data or len(data) > CLAN_ICON_MAX_BYTES:
        raise ValueError("icon_size")
    mime_type = clan_image_mime(data)
    if mime_type is None:
        raise ValueError("icon_type")
    return data, mime_type


def clan_upload_banner_data():
    uploaded = request.files.get("clan_banner")
    if uploaded is None or not uploaded.filename:
        return None, None
    data = uploaded.stream.read(CLAN_BANNER_MAX_BYTES + 1)
    if not data or len(data) > CLAN_BANNER_MAX_BYTES:
        raise ValueError("banner_size")
    mime_type = clan_image_mime(data)
    if mime_type is None:
        raise ValueError("banner_type")
    return data, mime_type


@app.get("/clans")
def clans_page():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guilds = clan_guild_options()
    wanted = str(request.args.get("guild") or session.get("clan_guild_id") or "")
    guild = next((item for item in guilds if str(item["id"]) == wanted), None)
    if guild is None and guilds:
        guild = guilds[0]
    if guild:
        session["clan_guild_id"] = str(guild["id"])
    guild_id = int(guild["id"]) if guild else None
    member_record = users.find_one({"guild_id": guild_id, "user_id": int(user["id"])}) if guild_id else None
    my_clan = clans.find_one(
        {"guild_id": guild_id, "clan_id": member_record.get("clan_id"), "status": "active"}
    ) if guild_id and member_record and member_record.get("clan_id") else None
    my_clan_data = clan_public_data(guild_id, my_clan) if my_clan else None
    my_clan_members = clan_member_list(guild_id, my_clan) if my_clan else []
    clan_listing = [
        clan_public_data(guild_id, clan)
        for clan in clans.find({"guild_id": guild_id, "status": "active"}).sort("name", 1)
    ] if guild_id else []
    my_requests = list(clan_join_requests.find({
        "guild_id": guild_id, "clan_id": my_clan["clan_id"], "status": "pending",
    }).sort("created_at", 1)) if my_clan and int(my_clan["owner_id"]) == int(user["id"]) else []
    my_invites = list(clan_invites.find({
        "guild_id": guild_id, "target_id": int(user["id"]), "status": "pending",
    }).sort("created_at", -1)) if guild_id else []
    for invite in my_invites:
        invite["clan"] = clans.find_one({
            "guild_id": guild_id, "clan_id": invite["clan_id"], "status": "active",
        })
    for invite in my_invites:
        if invite.get("clan"):
            invite["clan"] = clan_public_data(guild_id, invite["clan"])
    creation_requests = list(clan_creation_requests.find({
        "guild_id": guild_id, "status": "pending",
    }).sort("created_at", 1)) if guild_id and is_admin(user["id"]) else []
    pending_join_clan_ids = {
        item["clan_id"] for item in clan_join_requests.find({
            "guild_id": guild_id, "user_id": int(user["id"]), "status": "pending",
        }, {"clan_id": 1})
    } if guild_id else set()
    review_id = request.args.get("review", "")
    return render_template(
        "clans.html",
        user=user,
        avatar_url=discord_avatar_url(user),
        guilds=guilds,
        guild=guild,
        clans=clan_listing,
        my_clan=my_clan_data,
        my_clan_members=my_clan_members,
        member_balance=int(member_record.get("balance", 0) or 0) if member_record else 0,
        my_clan_owner=bool(my_clan and int(my_clan["owner_id"]) == int(user["id"])),
        my_requests=my_requests,
        my_invites=my_invites,
        creation_requests=creation_requests,
        pending_join_clan_ids=pending_join_clan_ids,
        review_id=review_id,
        error=request.args.get("error"),
        saved=request.args.get("saved"),
    )


@app.get("/clans/<int:guild_id>/<clan_id>/icon")
def clan_icon(guild_id, clan_id):
    clan = clans.find_one(
        {"guild_id": int(guild_id), "clan_id": str(clan_id), "status": "active"},
        {"icon_data": 1, "icon_mime": 1},
    )
    if not clan or not clan.get("icon_data") or clan.get("icon_mime") not in {
        "image/png", "image/jpeg", "image/webp",
    }:
        return "", 404
    response = Response(bytes(clan["icon_data"]), mimetype=clan["icon_mime"])
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/clans/<int:guild_id>/<clan_id>/banner")
def clan_banner(guild_id, clan_id):
    clan = clans.find_one({"guild_id": int(guild_id), "clan_id": str(clan_id), "status": "active"}, {"banner_data": 1, "banner_mime": 1})
    if not clan or not clan.get("banner_data") or clan.get("banner_mime") not in {"image/png", "image/jpeg", "image/webp"}:
        return "", 404
    response = Response(bytes(clan["banner_data"]), mimetype=clan["banner_mime"])
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/clans/create")
def clans_create():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild_id_raw = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id_raw)
        if str(guild_id) not in {str(item["id"]) for item in clan_guild_options()}:
            raise ValueError("invalid_guild")
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if guild is None or get_bot_member(guild_id, user["id"]) is None:
            raise ValueError("not_member")
        if users.find_one({"guild_id": guild_id, "user_id": int(user["id"]), "clan_id": {"$exists": True}}):
            raise ValueError("already_in_clan")
        name = " ".join(request.form.get("name", "").split())
        description = request.form.get("description", "").strip()
        normalized_name = name.casefold()
        icon_data, icon_mime = clan_upload_data()
        banner_data, banner_mime = clan_upload_banner_data()
        if not 3 <= len(name) <= 32 or len(description) > 500:
            raise ValueError("invalid")
        if clans.find_one({"guild_id": guild_id, "normalized_name": normalized_name, "status": "active"}):
            raise ValueError("name_taken")
        if clan_creation_requests.find_one({
            "guild_id": guild_id, "normalized_name": normalized_name, "status": "pending",
        }):
            raise ValueError("name_pending")
        if clan_creation_requests.find_one({
            "guild_id": guild_id, "creator_id": int(user["id"]), "status": "pending",
        }):
            raise ValueError("request_pending")
    except ValueError as exc:
        error = str(exc) if str(exc) in {
            "invalid_guild", "not_member", "already_in_clan", "name_taken", "name_pending",
            "request_pending", "icon_size", "icon_type", "banner_size", "banner_type",
        } else "invalid"
        return redirect(url_for("clans_page", guild=guild_id_raw, error=error))

    request_id = secrets.token_urlsafe(12)
    clan_creation_requests.insert_one({
        "request_id": request_id,
        "guild_id": guild_id,
        "creator_id": int(user["id"]),
        "creator_name": user.get("global_name") or user.get("username", "Member"),
        "name": name,
        "normalized_name": normalized_name,
        "description": description,
        "icon_data": icon_data,
        "icon_mime": icon_mime,
        "banner_data": banner_data,
        "banner_mime": banner_mime,
        "status": "pending",
        "created_at": now_utc(),
    })
    review_url = url_for("clans_page", guild=guild_id, review=request_id, _external=True)
    send_clan_dm(
        777341204047331348,
        f"New clan creation request for **{name}** in **{guild.name}** from "
        f"{user.get('global_name') or user.get('username', 'a member')}.\n"
        f"Review it here: {review_url}",
    )
    return redirect(url_for("clans_page", guild=guild_id, saved="request_submitted"))


@app.post("/clans/creation/<request_id>")
def clans_creation_decision(request_id):
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    action = request.form.get("action")
    creation = clan_creation_requests.find_one({"request_id": request_id, "status": "pending"})
    if action not in {"approve", "deny"} or creation is None:
        return redirect(url_for("clans_page", error="request_unavailable"))
    guild_id = int(creation["guild_id"])
    claimed = clan_creation_requests.update_one(
        {"request_id": request_id, "status": "pending"},
        {"$set": {"status": "processing", "reviewed_by": int(user["id"])}},
    )
    if not claimed.modified_count:
        return redirect(url_for("clans_page", guild=guild_id, error="request_unavailable"))
    if action == "deny":
        clan_creation_requests.update_one(
            {"request_id": request_id, "status": "processing"},
            {"$set": {"status": "denied", "reviewed_at": now_utc()}},
        )
        send_clan_dm(creation["creator_id"], f"Your request to create **{creation['name']}** was not approved.")
        return redirect(url_for("clans_page", guild=guild_id, saved="creation_denied"))

    bot = BOT.get("instance")
    guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
    owner_id = int(creation["creator_id"])
    if (guild is None or get_bot_member(guild_id, owner_id) is None
            or clans.find_one({"guild_id": guild_id, "normalized_name": creation["normalized_name"], "status": "active"})):
        clan_creation_requests.update_one(
            {"request_id": request_id, "status": "processing"},
            {"$set": {"status": "pending"}, "$unset": {"reviewed_by": ""}},
        )
        return redirect(url_for("clans_page", guild=guild_id, error="creation_failed"))
    clan = {
        "guild_id": guild_id,
        "clan_id": secrets.token_urlsafe(12),
        "name": creation["name"],
        "normalized_name": creation["normalized_name"],
        "description": creation.get("description", ""),
        "owner_id": owner_id,
        "icon_data": creation.get("icon_data"),
        "icon_mime": creation.get("icon_mime"),
        "banner_data": creation.get("banner_data"),
        "banner_mime": creation.get("banner_mime"),
        "status": "active",
        "created_at": now_utc(),
    }
    clans.insert_one(clan)
    assigned = clan_member_assign(guild_id, owner_id, clan)
    if assigned is None:
        clans.delete_one({"guild_id": guild_id, "clan_id": clan["clan_id"]})
        clan_creation_requests.update_one(
            {"request_id": request_id, "status": "processing"},
            {"$set": {"status": "pending"}, "$unset": {"reviewed_by": ""}},
        )
        return redirect(url_for("clans_page", guild=guild_id, error="creator_in_clan"))
    clan_creation_requests.update_one(
        {"request_id": request_id, "status": "processing"},
        {"$set": {"status": "approved", "reviewed_at": now_utc(), "clan_id": clan["clan_id"]}},
    )
    send_clan_dm(owner_id, f"Your clan **{clan['name']}** has been approved. Visit {url_for('clans_page', guild=guild_id, _external=True)} to manage it.")
    return redirect(url_for("clans_page", guild=guild_id, saved="creation_approved"))


@app.post("/clans/join/<clan_id>")
def clans_join_request(clan_id):
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild_id_raw = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id_raw)
        if str(guild_id) not in {str(item["id"]) for item in clan_guild_options()}:
            raise ValueError
        clan = clans.find_one({"guild_id": guild_id, "clan_id": clan_id, "status": "active"})
        if clan is None or get_bot_member(guild_id, user["id"]) is None:
            raise ValueError
        current = users.find_one({"guild_id": guild_id, "user_id": int(user["id"]), "clan_id": {"$exists": True}})
        if current:
            raise ValueError
        if clan_join_requests.find_one({
            "guild_id": guild_id, "clan_id": clan_id, "user_id": int(user["id"]), "status": "pending",
        }):
            raise ValueError
    except (TypeError, ValueError):
        return redirect(url_for("clans_page", guild=guild_id_raw, error="join_unavailable"))
    request_id = secrets.token_urlsafe(12)
    clan_join_requests.insert_one({
        "request_id": request_id,
        "guild_id": guild_id,
        "clan_id": clan_id,
        "user_id": int(user["id"]),
        "username": user.get("global_name") or user.get("username", "Member"),
        "status": "pending",
        "created_at": now_utc(),
    })
    review_url = url_for("clans_page", guild=guild_id, review=request_id, _external=True)
    send_clan_dm(
        clan["owner_id"],
        f"{user.get('global_name') or user.get('username', 'A member')} requested to join **{clan['name']}**.\n"
        f"Review the request here: {review_url}",
    )
    return redirect(url_for("clans_page", guild=guild_id, saved="join_requested"))


@app.post("/clans/invite")
def clans_invite():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild_id_raw = request.form.get("guild_id", "")
    target_raw = request.form.get("member_id", "").strip()
    try:
        guild_id = int(guild_id_raw)
        target_id = int(target_raw)
        if str(guild_id) not in {str(item["id"]) for item in clan_guild_options()}:
            raise ValueError
        member_record = users.find_one({"guild_id": guild_id, "user_id": int(user["id"])})
        clan = clans.find_one({
            "guild_id": guild_id,
            "clan_id": member_record.get("clan_id") if member_record else None,
            "status": "active",
        })
        if clan is None or get_bot_member(guild_id, target_id) is None:
            raise ValueError
        target_record = users.find_one({"guild_id": guild_id, "user_id": target_id, "clan_id": {"$exists": True}})
        if target_record or target_id == int(user["id"]):
            raise ValueError
        if clan_invites.find_one({
            "guild_id": guild_id, "clan_id": clan["clan_id"], "target_id": target_id, "status": "pending",
        }):
            raise ValueError
    except (TypeError, ValueError):
        return redirect(url_for("clans_page", guild=guild_id_raw, error="invite_invalid"))
    invite_id = secrets.token_urlsafe(12)
    clan_invites.insert_one({
        "invite_id": invite_id,
        "guild_id": guild_id,
        "clan_id": clan["clan_id"],
        "invited_by": int(user["id"]),
        "target_id": target_id,
        "status": "pending",
        "created_at": now_utc(),
    })
    invitation_url = url_for("clans_page", guild=guild_id, _external=True)
    send_clan_dm(
        target_id,
        f"You were invited to join **{clan['name']}** in **{next((item['name'] for item in clan_guild_options() if str(item['id']) == str(guild_id)), 'a server')}**.\n"
        f"Sign in and accept or decline the invitation here: {invitation_url}",
    )
    return redirect(url_for("clans_page", guild=guild_id, saved="invite_sent"))


@app.post("/clans/invite/<invite_id>/decision")
def clans_invite_decision(invite_id):
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    invitation = clan_invites.find_one({
        "invite_id": invite_id, "target_id": int(user["id"]), "status": "pending",
    })
    if invitation is None or request.form.get("action") not in {"accept", "decline"}:
        return redirect(url_for("clans_page", error="invite_unavailable"))
    guild_id = int(invitation["guild_id"])
    claim = clan_invites.update_one(
        {"invite_id": invite_id, "target_id": int(user["id"]), "status": "pending"},
        {"$set": {"status": "processing"}},
    )
    if not claim.modified_count:
        return redirect(url_for("clans_page", guild=guild_id, error="invite_unavailable"))
    action = request.form["action"]
    if action == "decline":
        clan_invites.update_one(
            {"invite_id": invite_id, "status": "processing"},
            {"$set": {"status": "declined", "responded_at": now_utc()}},
        )
        return redirect(url_for("clans_page", guild=guild_id, saved="invite_declined"))
    clan = clans.find_one({
        "guild_id": guild_id, "clan_id": invitation["clan_id"], "status": "active",
    })
    if clan is None or get_bot_member(guild_id, user["id"]) is None:
        clan_invites.update_one(
            {"invite_id": invite_id, "status": "processing"},
            {"$set": {"status": "pending"}},
        )
        return redirect(url_for("clans_page", guild=guild_id, error="invite_unavailable"))
    assigned = clan_member_assign(guild_id, user["id"], clan)
    if assigned is None:
        clan_invites.update_one(
            {"invite_id": invite_id, "status": "processing"},
            {"$set": {"status": "pending"}},
        )
        return redirect(url_for("clans_page", guild=guild_id, error="already_in_clan"))
    clan_invites.update_one(
        {"invite_id": invite_id, "status": "processing"},
        {"$set": {"status": "accepted", "responded_at": now_utc()}},
    )
    clan_withdraw_pending_membership_requests(guild_id, user["id"])
    return redirect(url_for("clans_page", guild=guild_id, saved="invite_accepted"))


@app.post("/clans/join-request/<request_id>/decision")
def clans_join_decision(request_id):
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    join_request = clan_join_requests.find_one({"request_id": request_id, "status": "pending"})
    if join_request is None or request.form.get("action") not in {"approve", "deny"}:
        return redirect(url_for("clans_page", error="request_unavailable"))
    guild_id = int(join_request["guild_id"])
    clan = clans.find_one({
        "guild_id": guild_id, "clan_id": join_request["clan_id"], "owner_id": int(user["id"]), "status": "active",
    })
    if clan is None:
        return "Forbidden", 403
    claim = clan_join_requests.update_one(
        {"request_id": request_id, "status": "pending"},
        {"$set": {"status": "processing"}},
    )
    if not claim.modified_count:
        return redirect(url_for("clans_page", guild=guild_id, error="request_unavailable"))
    action = request.form["action"]
    if action == "deny":
        clan_join_requests.update_one(
            {"request_id": request_id, "status": "processing"},
            {"$set": {"status": "denied", "responded_at": now_utc()}},
        )
        send_clan_dm(join_request["user_id"], f"Your request to join **{clan['name']}** was not approved.")
        return redirect(url_for("clans_page", guild=guild_id, saved="join_denied"))
    if get_bot_member(guild_id, join_request["user_id"]) is None:
        clan_join_requests.update_one(
            {"request_id": request_id, "status": "processing"},
            {"$set": {"status": "pending"}},
        )
        return redirect(url_for("clans_page", guild=guild_id, error="join_unavailable"))
    assigned = clan_member_assign(guild_id, join_request["user_id"], clan)
    if assigned is None:
        clan_join_requests.update_one(
            {"request_id": request_id, "status": "processing"},
            {"$set": {"status": "pending"}},
        )
        return redirect(url_for("clans_page", guild=guild_id, error="already_in_clan"))
    clan_join_requests.update_one(
        {"request_id": request_id, "status": "processing"},
        {"$set": {"status": "approved", "responded_at": now_utc()}},
    )
    clan_withdraw_pending_membership_requests(guild_id, join_request["user_id"])
    send_clan_dm(join_request["user_id"], f"Your request to join **{clan['name']}** was approved.")
    return redirect(url_for("clans_page", guild=guild_id, saved="join_approved"))


@app.post("/clans/leave")
def clans_leave():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    try:
        guild_id = int(request.form.get("guild_id", ""))
    except (TypeError, ValueError):
        return redirect(url_for("clans_page", error="clan_unavailable"))
    member_record = users.find_one({"guild_id": guild_id, "user_id": int(user["id"])})
    clan = clans.find_one({
        "guild_id": guild_id,
        "clan_id": member_record.get("clan_id") if member_record else None,
        "status": "active",
    })
    if clan is None:
        return redirect(url_for("clans_page", guild=guild_id, error="clan_unavailable"))
    clan_member_departure(guild_id, user["id"], clan["clan_id"])
    clan_member_clear(guild_id, user["id"])
    return redirect(url_for("clans_page", guild=guild_id, saved="left_clan"))


@app.post("/clans/settings")
def clans_settings():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    try:
        guild_id = int(request.form.get("guild_id", ""))
        member_record = users.find_one({"guild_id": guild_id, "user_id": int(user["id"])})
        clan = clans.find_one({
            "guild_id": guild_id,
            "clan_id": member_record.get("clan_id") if member_record else None,
            "owner_id": int(user["id"]),
            "status": "active",
        })
        name = " ".join(request.form.get("name", "").split())
        description = request.form.get("description", "").strip()
        icon_data, icon_mime = clan_upload_data()
        banner_data, banner_mime = clan_upload_banner_data()
        if clan is None or not 3 <= len(name) <= 32 or len(description) > 500:
            raise ValueError
        normalized_name = name.casefold()
        if clans.find_one({
            "guild_id": guild_id,
            "normalized_name": normalized_name,
            "clan_id": {"$ne": clan["clan_id"]},
            "status": "active",
        }):
            raise ValueError
    except (TypeError, ValueError) as exc:
        error = str(exc) if str(exc) in {"icon_size", "icon_type", "banner_size", "banner_type"} else "clan_settings"
        return redirect(url_for("clans_page", guild=request.form.get("guild_id", ""), error=error))
    update = {
        "name": name,
        "normalized_name": normalized_name,
        "description": description,
        "updated_at": now_utc(),
    }
    if icon_data is not None:
        update.update({"icon_data": icon_data, "icon_mime": icon_mime})
    if banner_data is not None:
        update.update({"banner_data": banner_data, "banner_mime": banner_mime})
    clans.update_one({"guild_id": guild_id, "clan_id": clan["clan_id"]}, {"$set": update})
    return redirect(url_for("clans_page", guild=guild_id, saved="clan_updated"))


@app.get("/shop")
def shop_page():
    guild = selected_guild()
    guild_id = int(guild["id"]) if guild else -1
    migrate_legacy_shop_items(guild_id)
    items = list(shop_items.find({
        "guild_id": guild_id,
        "$or": [
            {"enabled": True},
            {"enabled": False, "show_when_disabled": True},
        ],
    }).sort("price", 1))
    user = session.get("discord_user")
    clan_perks = member_clan_perks(guild_id, user["id"]) if user and guild_id > 0 else None
    for item in items:
        item["display_price"], item["clan_discount_applied"] = effective_shop_price(
            guild_id, user["id"], item, clan_perks,
        ) if user and guild_id > 0 else (int(item.get("price", 0)), False)
    result = request.args.get("role_result")
    purchase_message = SHOP_RESULT_MESSAGES.get(result) if result else None
    return render_template("shop.html", items=items, guild=guild, purchase_message=purchase_message,
                           purchase_ok=(result == "success"), requirement_mode=item_requirement_mode,
                           purchased_item=request.args.get("item_name"),
                           purchase_price=request.args.get("price"))


@app.get("/shop-panel")
def shop_panel_page():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    bot = BOT.get("instance")
    guilds = bot_guilds()
    wanted = str(request.args.get("guild") or session.get("shop_panel_guild_id") or (guilds[0]["id"] if guilds else ""))
    guild = bot.get_guild(int(wanted)) if bot and not bot.is_closed() and wanted.isdigit() else None
    if guild is None and guilds:
        guild = bot.get_guild(int(guilds[0]["id"]))
    gid = str(guild.id) if guild else ""
    if gid:
        session["shop_panel_guild_id"] = gid
    roles = sorted([{"id": str(r.id), "name": r.name, "position": r.position,
                     "color": f"#{r.color.value:06x}" if r.color.value else "#99aab5",
                     "assignable": bot_can_assign(guild, r)[0]}
                    for r in guild.roles if not r.is_default() and not r.managed],
                   key=lambda x: (-x["position"], x["name"].lower())) if guild else []
    emojis = [{"value": f"<{'a' if e.animated else ''}:{e.name}:{e.id}>", "name": e.name, "url": str(e.url)}
              for e in guild.emojis if e.available] if guild else []
    migrate_legacy_shop_items(int(gid) if gid else -1)
    listings = list(shop_items.find({"guild_id": int(gid) if gid else -1}).sort("price", 1))
    assignable = {r["id"]: r["assignable"] for r in roles}
    for listing in listings:
        listing["mode"] = item_requirement_mode(listing)
        rid = str(listing.get("reward", {}).get("role_id", ""))
        listing["cannot_assign"] = listing.get("type") == "role" and assignable.get(rid, False) is False
    edit_id = request.args.get("edit", "")
    edit_item = next((item for item in listings if item.get("item_id") == edit_id), None)
    if edit_item:
        edit_item["required_role_ids"] = [str(role_id) for role_id in edit_item.get("required_role_ids", [])]
    errors = {
        "invalid": "That item configuration is not valid. Check the name and price.",
        "item_not_found": "That shop item no longer exists. Refresh the list and try again.",
        "role_required": "Pick the Discord role this item should give.",
        "role_unassignable": "That role cannot be given by the bot. Choose a normal role (not @everyone or a bot-managed role).",
        "role_above_bot": "The bot cannot give that role yet. In Server Settings → Roles, give the bot Manage Roles and drag the bot's role ABOVE the reward role, then try again.",
        "requirements_empty": "Pick at least one required role, or set “Who can buy this?” to Everyone.",
        "emoji_invalid": "That emoji is not valid. Use a normal emoji, or pick one of the server's custom emojis.",
    }
    return render_template("shop_panel.html", user=user, avatar_url=discord_avatar_url(user),
                           bot_guilds=guilds, guild_id=gid, roles=roles, listings=listings, emojis=emojis,
                           edit_item=edit_item,
                           error_message=errors.get(request.args.get("error")))


class ShopConfigError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@app.post("/shop-panel/save")
def shop_panel_save():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    gid = request.form.get("guild_id", "")
    try:
        gid = int(request.form["guild_id"])
        name = request.form["name"].strip()
        description = request.form.get("description", "").strip()
        try:
            emoji = normalize_item_emoji(request.form.get("emoji", ""))
        except ValueError:
            raise ShopConfigError("emoji_invalid")
        item_type = request.form.get("item_type", "custom").strip().lower()
        price = int(request.form["price"])
        enabled = request.form.get("enabled") == "on"
        show_when_disabled = request.form.get("show_when_disabled") == "on"
        clan_discount_excluded = request.form.get("clan_discount_excluded") == "on"
        requirement_mode = request.form.get("requirement_mode", "everyone").lower()
        required_role_values = request.form.getlist("required_role_ids")
        if any(not str(value).isdigit() for value in required_role_values):
            raise ShopConfigError("invalid")
        required_role_ids = list(dict.fromkeys(int(value) for value in required_role_values))
        role_id_raw = request.form.get("role_id", "").strip()
        role_id = int(role_id_raw) if role_id_raw.isdigit() else None
        item_id = request.form.get("item_id", "").strip()
        if not name or price < 1 or item_type not in {"role", "custom"} or requirement_mode not in REQUIREMENT_MODES:
            raise ShopConfigError("invalid")
        if requirement_mode == "everyone":
            required_role_ids = []
        elif not required_role_ids:
            raise ShopConfigError("requirements_empty")
        bot = BOT.get("instance")
        guild = bot.get_guild(gid) if bot and not bot.is_closed() else None
        if guild is None:
            raise ShopConfigError("invalid")
        if any(guild.get_role(required_id) is None or guild.get_role(required_id).is_default()
               for required_id in required_role_ids):
            raise ShopConfigError("invalid")
        if item_type == "role":
            role = guild.get_role(role_id) if role_id else None
            if role is None:
                raise ShopConfigError("role_required")
            ok, reason = bot_can_assign(guild, role)
            if not ok:
                raise ShopConfigError("role_above_bot" if reason in {"role_above_bot", "missing_manage_roles"} else "role_unassignable")
            reward = {"role_id": role.id, "role_name": role.name}
        else:
            reward = {}
        now = now_utc()
        item_data = {"name": name,
            "description": description or "A Halloween shop item.", "emoji": emoji, "price": price,
            "type": item_type, "enabled": enabled, "show_when_disabled": show_when_disabled,
            "clan_discount_excluded": clan_discount_excluded,
            "required_role_ids": required_role_ids,
            "requirement_mode": requirement_mode, "reward": reward,
            "updated_at": now, "updated_by": int(user["id"])}
        if item_id:
            existing = shop_items.find_one({"guild_id": gid, "item_id": item_id})
            if existing is None:
                raise ShopConfigError("item_not_found")
            result = shop_items.update_one({"guild_id": gid, "item_id": item_id}, {"$set": item_data})
            if not result.matched_count:
                raise ShopConfigError("item_not_found")
        else:
            item_id = secrets.token_urlsafe(10)
            shop_items.insert_one({"guild_id": gid, "item_id": item_id, **item_data, "created_at": now})
    except ShopConfigError as exc:
        return redirect(url_for("shop_panel_page", guild=gid, error=exc.code))
    except (KeyError, TypeError, ValueError):
        return redirect(url_for("shop_panel_page", guild=gid, error="invalid"))
    return redirect(url_for("shop_panel_page", guild=gid, saved="updated" if request.form.get("item_id") else "1"))


@app.post("/shop-panel/toggle")
def shop_panel_toggle():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        gid = int(request.form["guild_id"])
        item_id = request.form["item_id"].strip()
        enabled_value = request.form.get("enabled")
        if not item_id or enabled_value not in {"0", "1"}:
            raise ValueError
        enabled = enabled_value == "1"
    except (KeyError, TypeError, ValueError):
        return "Invalid request", 400
    result = shop_items.update_one(
        {"guild_id": gid, "item_id": item_id},
        {"$set": {"enabled": enabled, "updated_at": now_utc(), "updated_by": int(user["id"])}},
    )
    if not result.matched_count:
        return redirect(url_for("shop_panel_page", guild=gid, error="item_not_found"))
    return redirect(url_for("shop_panel_page", guild=gid, updated="1"))


@app.post("/shop-panel/remove")
def shop_panel_remove():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    try:
        gid = int(request.form["guild_id"]); item_id = request.form["item_id"]
    except (KeyError, TypeError, ValueError):
        return "Invalid request", 400
    shop_items.delete_one({"guild_id": gid, "item_id": item_id})
    return redirect(url_for("shop_panel_page", guild=gid, removed="1"))


@app.post("/shop/buy/<item_id>")
def shop_item_buy_web(item_id):
    user = session.get("discord_user"); guild = selected_guild()
    if not user or not guild:
        return redirect(url_for("login"))
    item_before_purchase = shop_items.find_one({
        "guild_id": int(guild["id"]),
        "item_id": str(item_id),
        "enabled": True,
    })
    result = purchase_shop_item(int(guild["id"]), int(user["id"]), item_id)
    redirect_args = {"guild": guild["id"], "role_result": result}
    if result == "success" and item_before_purchase:
        paid_price, _ = effective_shop_price(
            int(guild["id"]), int(user["id"]), item_before_purchase,
        )
        redirect_args.update({
            "item_name": item_before_purchase.get("name", "Shop Item"),
            "price": paid_price,
        })
    return redirect(url_for("shop_page", **redirect_args))


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
        if profile:
            profile["inventory"] = [
                {
                    **item,
                    "purchase_timestamp_ms": (
                        int(as_utc(item["purchased_at"]).timestamp() * 1000)
                        if isinstance(item.get("purchased_at"), datetime) else None
                    ),
                }
                for item in profile.get("inventory", []) or []
                if isinstance(item, dict)
            ]
    return render_template("inventory.html", user=user, avatar_url=discord_avatar_url(user), guilds=session.get("discord_guilds", []), guild=guild, profile=profile)


@app.post("/inventory/remove")
def inventory_remove():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild = selected_guild()
    if not guild:
        return redirect(url_for("inventory_page", error="no_server"))
    guild_id = int(guild["id"])
    user_id = int(user["id"])
    item_id = request.form.get("item_id", "").strip()
    purchase_id = request.form.get("purchase_id", "").strip()
    remove_role = request.form.get("remove_role") == "on"
    if not item_id:
        return redirect(url_for("inventory_page", error="invalid"))

    record = users.find_one({"guild_id": guild_id, "user_id": user_id})
    inventory = (record or {}).get("inventory", []) or []
    if purchase_id:
        purchase_match = {"item_id": item_id, "purchase_id": purchase_id}
        purchase = next(
            (item for item in inventory
             if isinstance(item, dict) and all(item.get(key) == value for key, value in purchase_match.items())),
            None,
        )
    else:
        try:
            purchased_at_ms = int(request.form.get("purchased_at_ms", ""))
        except (TypeError, ValueError):
            return redirect(url_for("inventory_page", error="invalid"))
        purchase = next(
            (item for item in inventory
             if isinstance(item, dict)
             and item.get("item_id") == item_id
             and isinstance(item.get("purchased_at"), datetime)
             and int(as_utc(item["purchased_at"]).timestamp() * 1000) == purchased_at_ms),
            None,
        )
        if purchase:
            purchase_match = {"item_id": item_id, "purchased_at": purchase["purchased_at"]}
        else:
            purchase_match = {"item_id": item_id, "purchased_at": None}
    if purchase is None:
        return redirect(url_for("inventory_page", error="purchase_missing"))

    if remove_role and purchase.get("type") == "role":
        role_id = purchase.get("role_id")
        if not role_id:
            shop_item = shop_items.find_one({"guild_id": guild_id, "item_id": item_id})
            role_id = (shop_item or {}).get("reward", {}).get("role_id")
        if not role_id:
            return redirect(url_for("inventory_page", error="role_unknown"))

        bot = BOT.get("instance")
        discord_guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if discord_guild is None:
            return redirect(url_for("inventory_page", error="role_offline"))
        role = discord_guild.get_role(int(role_id))
        if role is not None:
            try:
                member = _run_on_bot_loop(bot, discord_guild.fetch_member(user_id), 10)
            except discord.NotFound:
                member = None
            except Exception:
                log.exception("Could not fetch member %s in guild %s before inventory removal.", user_id, guild_id)
                return redirect(url_for("inventory_page", error="role_remove"))
            if member is not None and role in member.roles:
                try:
                    _run_on_bot_loop(
                        bot,
                        member.remove_roles(role, atomic=False, reason="Member removed shop reward from inventory"),
                        15,
                    )
                    verified = _run_on_bot_loop(bot, discord_guild.fetch_member(user_id), 10)
                    if role in verified.roles:
                        raise RuntimeError("Discord still reports the role after removal.")
                except discord.NotFound:
                    pass
                except Exception:
                    log.exception("Could not remove role %s from member %s in guild %s.", role_id, user_id, guild_id)
                    return redirect(url_for("inventory_page", error="role_remove"))

    removed = users.update_one(
        {"guild_id": guild_id, "user_id": user_id, "inventory": {"$elemMatch": purchase_match}},
        {"$pull": {"inventory": purchase_match}},
    )
    if not removed.modified_count:
        return redirect(url_for("inventory_page", error="purchase_missing"))
    log_activity(
        "shop_purchase_removed",
        user_id=user_id,
        username=record.get("username"),
        guild_id=guild_id,
        amount=0,
        details={
            "item_id": item_id,
            "item": purchase.get("name", "Shop Item"),
            "purchase_id": purchase.get("purchase_id"),
            "refunded": False,
            "removed_by": user_id,
            "source": "member_inventory",
            "role_removed": bool(remove_role and purchase.get("type") == "role"),
        },
    )
    return redirect(url_for("inventory_page", removed="1"))


# Standalone survey portal; deliberately omitted from dashboard navigation.
@app.get("/survey")
def survey_page():
    user = session.get("discord_user")
    available_guilds = []
    if user:
        joined_ids = {str(item.get("id")) for item in session.get("discord_guilds", [])}
        available_guilds = [item for item in bot_guilds() if str(item.get("id")) in joined_ids]
    banner = site_assets.find_one({"_id": "survey_banner"}, {"mime": 1})
    return render_template(
        "survey.html", user=user, guilds=available_guilds,
        banner_url=url_for("survey_banner_asset") if banner and banner.get("mime") else None,
        csrf_token=_csrf_token(), error=request.args.get("error"),
        saved=request.args.get("saved"), reward=SURVEY_REWARD_CANDY,
    )


@app.get("/survey/login")
def survey_login():
    if session.get("discord_user"):
        return redirect(url_for("survey_page"))
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not OAUTH2_REDIRECT_URI:
        return "Discord OAuth2 is not configured on this deployment.", 503
    try:
        state = _create_oauth_transaction("survey")
    except PyMongoError:
        log.exception("Could not create survey OAuth transaction")
        return "Discord OAuth2 is temporarily unavailable. Please try again.", 503
    params = {"client_id": DISCORD_CLIENT_ID, "redirect_uri": OAUTH2_REDIRECT_URI,
              "response_type": "code", "scope": "identify guilds", "state": state}
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))


@app.get("/survey/banner")
def survey_banner_asset():
    asset = site_assets.find_one({"_id": "survey_banner"}, {"data": 1, "mime": 1})
    if not asset or not asset.get("data") or asset.get("mime") not in {"image/png", "image/jpeg", "image/webp"}:
        return "", 404
    response = Response(bytes(asset["data"]), mimetype=asset["mime"])
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def survey_submission_notification_view(submission, guild_name, avatar_url=None):
    """Components V2 private notification containing the full survey submission and submitter identity."""
    ui = discord.ui
    user_id = int(submission["user_id"])
    created_at = submission.get("created_at") or now_utc()
    timestamp = int(created_at.timestamp()) if hasattr(created_at, "timestamp") else int(now_utc().timestamp())

    def safe(value):
        value = str(value or "").strip()
        return discord.utils.escape_markdown(value) if value else "—"

    header = (
        "## 📝 New Survey Submission\n"
        f"**Discord username:** {safe(submission.get('discord_username') or submission.get('username'))}\n"
        f"**Display name:** {safe(submission.get('display_name') or submission.get('username'))}\n"
        f"**Discord mention:** <@{user_id}>\n"
        f"**User ID:** `{user_id}`\n"
        f"**User profile:** https://discord.com/users/{user_id}\n"
        f"**Server:** {safe(guild_name)}\n"
        f"**Server ID:** `{int(submission['guild_id'])}`\n"
        f"**Submitted:** <t:{timestamp}:F> (<t:{timestamp}:R>)\n"
        f"**Overall rating:** {int(submission.get('rating', 0))}/5 ⭐\n"
        f"**Survey reward:** {int(submission.get('reward_candy', SURVEY_REWARD_CANDY)):,} 🍬"
    )
    items = [
        ui.Section(ui.TextDisplay(header), accessory=ui.Thumbnail(avatar_url))
        if avatar_url else ui.TextDisplay(header),
        ui.Separator(),
        ui.TextDisplay("### 💬 How was their experience?\n" + safe(submission.get("experience"))),
        ui.Separator(),
        ui.TextDisplay("### 💡 Suggestions\n" + safe(submission.get("suggestions"))),
        ui.Separator(),
        ui.TextDisplay("### ✨ Features they'd like\n" + safe(submission.get("features"))),
        ui.Separator(),
        ui.TextDisplay("-# Private survey notification • Replies are not sent back to the respondent."),
    ]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0x7C3AED))
    return view


def notify_survey_submission(submission, guild_name, avatar_url=None):
    """Best-effort DM to the configured survey notification recipient; never rolls back a saved survey."""
    recipient_id = int(os.getenv("SURVEY_NOTIFICATION_USER_ID", "777341204047331348"))
    bot = BOT.get("instance")
    if bot is None or bot.is_closed() or not bot.is_ready():
        log.warning("Survey was saved, but the notification DM could not be sent because the bot is offline.")
        return False

    async def send():
        recipient = bot.get_user(recipient_id) or await bot.fetch_user(recipient_id)
        await recipient.send(
            view=survey_submission_notification_view(submission, guild_name, avatar_url),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    try:
        _run_on_bot_loop(bot, send(), 12)
        return True
    except Exception:
        log.warning("Survey was saved, but its notification DM failed for recipient %s.", recipient_id, exc_info=True)
        return False


@app.post("/survey/submit")
def survey_submit():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("survey_login"))
    sent, expected = request.form.get("csrf_token", ""), session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        return redirect(url_for("survey_page", error="expired"))
    try:
        guild_id, rating = int(request.form.get("guild_id", "")), int(request.form.get("rating", ""))
    except (TypeError, ValueError):
        return redirect(url_for("survey_page", error="invalid"))
    joined_ids = {str(item.get("id")) for item in session.get("discord_guilds", [])}
    bot = BOT.get("instance")
    guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
    if str(guild_id) not in joined_ids or guild is None or get_bot_member(guild_id, user["id"]) is None:
        return redirect(url_for("survey_page", error="guild"))
    if rating not in range(1, 6):
        return redirect(url_for("survey_page", error="invalid"))
    experience = request.form.get("experience", "").strip()
    suggestions = request.form.get("suggestions", "").strip()
    features = request.form.get("features", "").strip()
    if not experience or any(len(value) > 2000 for value in (experience, suggestions, features)):
        return redirect(url_for("survey_page", error="answers"))
    username = str(user.get("global_name") or user.get("username") or "Discord member")[:100]
    submission = {
        "_id": f"{guild_id}:{int(user['id'])}", "guild_id": guild_id, "user_id": int(user["id"]),
        "username": username, "discord_username": str(user.get("username") or username)[:100],
        "display_name": str(user.get("global_name") or user.get("username") or "Discord member")[:100],
        "rating": rating, "experience": experience,
        "suggestions": suggestions, "features": features, "reward_candy": SURVEY_REWARD_CANDY,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        with mongo.start_session() as mongo_session:
            def save_submission(tx_session):
                survey_submissions.insert_one(submission, session=tx_session)
                users.update_one(
                    {"guild_id": guild_id, "user_id": int(user["id"])},
                    {"$inc": {"balance": SURVEY_REWARD_CANDY, "candy_earned_total": SURVEY_REWARD_CANDY},
                     "$set": {"username": username},
                     "$setOnInsert": {"guild_id": guild_id, "user_id": int(user["id"])}},
                    upsert=True, session=tx_session,
                )
            mongo_session.with_transaction(save_submission)
    except DuplicateKeyError:
        return redirect(url_for("survey_page", error="already_submitted"))
    except PyMongoError:
        log.exception("Could not save survey or grant survey Candy reward")
        return redirect(url_for("survey_page", error="save_failed"))
    log_activity("survey_submitted", user_id=int(user["id"]), username=username, guild_id=guild_id,
                 amount=SURVEY_REWARD_CANDY, details={"rating": rating, "source": "survey_portal"})
    avatar_url = discord_avatar_url(user)
    notify_survey_submission(submission, guild.name, avatar_url)
    return redirect(url_for("survey_page", saved="1"))


@app.post("/access-control/survey-banner/save")
def access_control_survey_banner_save():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    sent, expected = request.form.get("csrf_token", ""), session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        return redirect(url_for("access_control_page", error="banner_csrf"))
    uploaded = request.files.get("survey_banner")
    if uploaded is None or not uploaded.filename:
        return redirect(url_for("access_control_page", error="banner_missing"))
    data = uploaded.stream.read(SURVEY_BANNER_MAX_BYTES + 1)
    if not data or len(data) > SURVEY_BANNER_MAX_BYTES:
        return redirect(url_for("access_control_page", error="banner_size"))
    mime_type = clan_image_mime(data)
    if mime_type is None:
        return redirect(url_for("access_control_page", error="banner_type"))
    site_assets.update_one({"_id": "survey_banner"},
        {"$set": {"data": data, "mime": mime_type, "updated_at": datetime.now(timezone.utc),
                  "updated_by": int(user["id"])}}, upsert=True)
    return redirect(url_for("access_control_page", saved="survey_banner"))


@app.post("/access-control/survey-banner/remove")
def access_control_survey_banner_remove():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    sent, expected = request.form.get("csrf_token", ""), session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        return redirect(url_for("access_control_page", error="banner_csrf"))
    site_assets.delete_one({"_id": "survey_banner"})
    return redirect(url_for("access_control_page", saved="survey_banner_removed"))


@app.get("/events")
def events_page():
    now = now_utc()
    records = list(event_records.find({}).sort("created_at", -1).limit(100))
    for event in records:
        event["id"] = str(event.get("_id", ""))
        event["title"] = str(event.get("title", "Untitled event"))
        event["description"] = str(event.get("description", ""))
        event["date_time"] = str(event.get("date_time", "Time to be announced"))
        event["location"] = str(event.get("location", ""))
        event["notes"] = str(event.get("notes", ""))
        event["status"] = event.get("status", "upcoming")
        event["status_label"] = {
            "upcoming": "Upcoming", "ongoing": "Ongoing",
            "ended": "Ended", "cancelled": "Cancelled",
        }.get(event["status"], "Upcoming")
    user = session.get("discord_user")
    admin = bool(user and is_admin(user.get("id", 0)))
    return render_template(
        "events.html",
        events=records,
        is_event_admin=admin,
        csrf_token=session.get("csrf_token", ""),
    )


@app.post("/events/manage/status")
def event_manage_status():
    user = session.get("discord_user")
    if not user or not is_admin(user.get("id", 0)):
        return "Forbidden", 403
    sent, expected = request.form.get("csrf_token", ""), session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        return redirect(url_for("events_page", error="csrf"))
    event_id = request.form.get("event_id", "").strip()
    status = request.form.get("status", "").strip().lower()
    if status not in {"upcoming", "ongoing", "ended", "cancelled"}:
        return redirect(url_for("events_page", error="status"))
    try:
        from bson import ObjectId
        record = event_records.find_one({"_id": ObjectId(event_id)})
        if not record:
            return redirect(url_for("events_page", error="missing"))
        event_records.update_one(
            {"_id": record["_id"]},
            {"$set": {"status": status, "updated_at": now_utc(), "updated_by": int(user["id"])}},
        )
        bot = BOT.get("instance")
        if bot and not bot.is_closed() and record.get("channel_id") and record.get("message_id"):
            async def update_announcement():
                status_labels = {
                    "upcoming": "🟢 Upcoming", "ongoing": "🟠 Ongoing",
                    "ended": "⚫ Ended", "cancelled": "🔴 Cancelled",
                }
                updated_view = discord.ui.LayoutView(timeout=None)
                container = discord.ui.Container(accent_colour=discord.Color.from_rgb(140, 72, 226))
                if record.get("image_url"):
                    container.add_item(discord.ui.MediaGallery(
                        discord.MediaGalleryItem(
                            media=record["image_url"],
                            description=f"{record.get('title', 'Event')} event image",
                        )
                    ))
                    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
                container.add_item(discord.ui.TextDisplay(content="# 🎃 Events"))
                container.add_item(discord.ui.TextDisplay(content=f"## {record.get('title', 'Untitled event')}"))
                if record.get("location"):
                    container.add_item(discord.ui.TextDisplay(content=f"**Location:** {record['location']}"))
                container.add_item(discord.ui.TextDisplay(content=f"**Description:** {record.get('description', '')}"))
                container.add_item(discord.ui.TextDisplay(content=f"**When:** {record.get('date_time', 'Time to be announced')}"))
                container.add_item(discord.ui.TextDisplay(content=f"**Status:** {status_labels[status]}"))
                if record.get("notes"):
                    container.add_item(discord.ui.TextDisplay(content=f"**Notes:** {record['notes']}"))
                container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
                container.add_item(discord.ui.ActionRow(discord.ui.Button(
                    style=discord.ButtonStyle.link,
                    label="View Event",
                    url="https://halloween-discord-bot-production.up.railway.app/events",
                )))
                updated_view.add_item(container)
                try:
                    channel = bot.get_channel(int(record["channel_id"]))
                    if channel is None:
                        channel = await bot.fetch_channel(int(record["channel_id"]))
                    message = await channel.fetch_message(int(record["message_id"]))
                    await message.edit(view=updated_view)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
                    log.warning("Event status saved, but the original Discord announcement could not be updated.")
            asyncio.run_coroutine_threadsafe(update_announcement(), bot.loop)
    except Exception:
        log.exception("Could not update event status")
        return redirect(url_for("events_page", error="missing"))
    return redirect(url_for("events_page", saved="status"))


@app.post("/events/manage/remove")
def event_manage_remove():
    user = session.get("discord_user")
    if not user or not is_admin(user.get("id", 0)):
        return "Forbidden", 403
    sent, expected = request.form.get("csrf_token", ""), session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        return redirect(url_for("events_page", error="csrf"))
    event_id = request.form.get("event_id", "").strip()
    try:
        from bson import ObjectId
        record = event_records.find_one({"_id": ObjectId(event_id)})
    except Exception:
        record = None
    if not record:
        return redirect(url_for("events_page", error="missing"))
    # Remove the stored event and best-effort delete the matching Discord announcement.
    event_records.delete_one({"_id": record["_id"]})
    bot = BOT.get("instance")
    if bot and not bot.is_closed() and record.get("channel_id") and record.get("message_id"):
        async def delete_announcement():
            try:
                channel = bot.get_channel(int(record["channel_id"]))
                if channel is None:
                    channel = await bot.fetch_channel(int(record["channel_id"]))
                message = await channel.fetch_message(int(record["message_id"]))
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
                log.warning("Event record removed; Discord announcement could not be deleted.")
        asyncio.run_coroutine_threadsafe(delete_announcement(), bot.loop)
    return redirect(url_for("events_page", saved="removed"))


def _create_oauth_transaction(flow, consent=None):
    """Persist OAuth state server-side so authorization survives session-cookie/proxy changes."""
    state = secrets.token_urlsafe(32)
    oauth_transactions.insert_one({
        "_id": state,
        "flow": flow,
        "consent": consent or {},
        "created_at": now_utc(),
    })
    return state


def _consume_oauth_transaction(state):
    if not state:
        return None
    return oauth_transactions.find_one_and_delete({"_id": state})


@app.get("/login")
def login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not OAUTH2_REDIRECT_URI:
        return "Discord OAuth2 is not configured on this deployment.", 503
    try:
        state = _create_oauth_transaction("dashboard")
    except PyMongoError:
        log.exception("Could not create Discord OAuth transaction")
        return "Discord OAuth2 is temporarily unavailable. Please try again.", 503
    params = {"client_id": DISCORD_CLIENT_ID, "redirect_uri": OAUTH2_REDIRECT_URI, "response_type": "code", "scope": "identify guilds", "state": state}
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))


@app.get("/oauth/callback")
def oauth_callback():
    state = request.args.get("state")
    try:
        transaction = _consume_oauth_transaction(state)
    except PyMongoError:
        log.exception("Could not read Discord OAuth transaction")
        return "Discord OAuth2 is temporarily unavailable. Please try again.", 503
    if not transaction:
        if request.args.get("error"):
            return redirect(url_for("authorize_page", error="denied"))
        return "This Discord authorization request has expired or is invalid. Please start again.", 400
    flow = transaction.get("flow", "dashboard")
    consent = transaction.get("consent") or {}
    if request.args.get("error"):
        if flow == "authorize":
            return redirect(url_for("authorize_page", error="denied"))
        if flow == "survey":
            return redirect(url_for("survey_page", error="oauth_denied"))
        return redirect(url_for("profile_page"))
    code = request.args.get("code")
    if not code:
        return "Missing Discord OAuth2 authorization code.", 400
    token = requests.post("https://discord.com/api/oauth2/token", data={"client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": OAUTH2_REDIRECT_URI}, timeout=10)
    if not token.ok:
        log.error("Discord OAuth2 token exchange failed: %s %s", token.status_code, token.text[:300])
        return "Discord OAuth2 sign-in failed.", 502
    access_token = token.json().get("access_token")
    if not access_token:
        return "Discord OAuth2 did not return an access token.", 502
    headers = {"Authorization": f"Bearer {access_token}"}
    if flow == "authorize":
        return finish_bot_authorization(headers, consent=consent)
    me_response = requests.get("https://discord.com/api/users/@me", headers=headers, timeout=10)
    guild_response = requests.get("https://discord.com/api/users/@me/guilds", headers=headers, timeout=10)
    if not me_response.ok or not guild_response.ok:
        return "Discord account information could not be loaded.", 502
    me = me_response.json()
    guilds = guild_response.json()
    compact_guilds = [
        {
            "id": str(guild.get("id")),
            "name": guild.get("name", "Unnamed Server"),
            "permissions": str(guild.get("permissions", "0")),
        }
        for guild in guilds
        if guild.get("id")
    ]
    session.permanent = True
    session["discord_user"] = me
    session["discord_guilds"] = compact_guilds
    if compact_guilds:
        session["selected_guild_id"] = compact_guilds[0]["id"]
    else:
        session.pop("selected_guild_id", None)
    if flow == "survey":
        return redirect(url_for("survey_page"))
    return redirect(url_for("profile_page"))

def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.get("/authorize")
def authorize_page():
    """First-use consent screen: accept Terms + Privacy and enable DMs, then sign in with Discord."""
    return render_template(
        "authorize.html",
        csrf_token=_csrf_token(),
        error=request.args.get("error"),
        configured=bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and OAUTH2_REDIRECT_URI),
    )


@app.post("/authorize")
def authorize_submit():
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        return redirect(url_for("authorize_page", error="expired"))
    if request.form.get("agree_terms") != "on" or request.form.get("enable_dms") != "on":
        return redirect(url_for("authorize_page", error="consent"))
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET or not OAUTH2_REDIRECT_URI:
        return redirect(url_for("authorize_page", error="config"))

    consent = {"tos_version": TOS_VERSION, "dms_enabled": True}
    try:
        state = _create_oauth_transaction("authorize", consent=consent)
    except PyMongoError:
        log.exception("Could not create authorization OAuth transaction")
        return redirect(url_for("authorize_page", error="unavailable"))
    params = {"client_id": DISCORD_CLIENT_ID, "redirect_uri": OAUTH2_REDIRECT_URI, "response_type": "code", "scope": "identify", "state": state}
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))



def send_authorization_dm(user_id):
    """Best-effort welcome DM that proves DMs work. Runs the send on the bot's event loop."""
    bot_ = BOT.get("instance")
    if bot_ is None or not bot_.is_ready():
        return False

    async def _send():
        user = bot_.get_user(int(user_id)) or await bot_.fetch_user(int(user_id))
        await user.send(view=authorized_dm_view())

    try:
        _run_on_bot_loop(bot_, _send(), 10)
        return True
    except Exception as exc:
        log.info("Authorization DM to %s was not delivered: %s", user_id, exc.__class__.__name__)
        return False


def finish_bot_authorization(headers, consent=None):
    consent = consent or {}
    if not consent.get("dms_enabled") or consent.get("tos_version") != TOS_VERSION:
        return redirect(url_for("authorize_page", error="expired"))
    me_response = requests.get("https://discord.com/api/users/@me", headers=headers, timeout=10)
    if not me_response.ok:
        return "Discord account information could not be loaded.", 502
    me = me_response.json()
    if not me.get("id"):
        return "Discord did not return your account.", 502
    username = me.get("global_name") or me.get("username") or str(me["id"])
    try:
        record_user_authorization(me["id"], me.get("username") or username)
    except PyMongoError:
        log.exception("Could not save authorization for %s", me.get("id"))
        return "We could not save your authorization right now. Please try again in a moment.", 503
    dm_ok = send_authorization_dm(me["id"])
    try:
        user_authorizations.update_one(
            {"_id": int(me["id"])},
            {"$set": {"dm_delivered": dm_ok, "dm_checked_at": now_utc()}},
        )
    except PyMongoError:
        log.warning("Could not record DM delivery result for %s", me.get("id"))
    session["authorized_result"] = {"username": username, "dm_ok": dm_ok}
    return redirect(url_for("authorized_page"))


@app.get("/authorize/done")
def authorized_page():
    result = session.get("authorized_result")
    if not result:
        return redirect(url_for("authorize_page"))
    return render_template("authorized.html", username=result.get("username"), dm_ok=result.get("dm_ok"))


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
    """Return a bot-connected guild that the signed-in Discord user actually belongs to."""
    bot_guild_list = bot_guilds()
    user_guild_ids = {str(g.get("id")) for g in session.get("discord_guilds", [])}
    guilds = [g for g in bot_guild_list if str(g.get("id")) in user_guild_ids]
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


def migrate_legacy_shop_items(guild_id=None):
    """Move legacy role-only listings into the unified shop collection."""
    query = {} if guild_id in (None, -1) else {"guild_id": int(guild_id)}
    legacy = list(role_shop_items.find(query))
    for listing in legacy:
        gid = int(listing["guild_id"]); rid = int(listing["role_id"])
        item_id = f"role-{gid}-{rid}"
        shop_items.update_one({"guild_id": gid, "item_id": item_id}, {"$setOnInsert": {
            "guild_id": gid, "item_id": item_id, "name": listing.get("role_name", f"Role {rid}"),
            "description": "Server role", "emoji": "🎭", "price": int(listing.get("price", 1)),
            "type": "role", "enabled": True, "required_role_ids": [], "requirement_mode": "any",
            "reward": {"role_id": rid, "role_name": listing.get("role_name", f"Role {rid}")},
            "created_at": listing.get("updated_at") or now_utc(), "updated_at": now_utc(),
        }}, upsert=True)
    if legacy:
        role_shop_items.delete_many(query)


def get_shop_items_for_guild(guild_id, enabled_only=True):
    migrate_legacy_shop_items(guild_id)
    query = {"guild_id": int(guild_id)}
    if enabled_only:
        query["$or"] = [
            {"enabled": True},
            {"enabled": False, "show_when_disabled": True},
        ]
    return list(shop_items.find(query).sort("price", 1))


CUSTOM_EMOJI_RE = re.compile(r"^<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>$")
REQUIREMENT_MODES = {"everyone", "any", "all"}


def normalize_item_emoji(raw):
    """Accepts a unicode emoji or a Discord custom emoji like <:name:123> / <a:name:123>."""
    raw = (raw or "").strip()
    if not raw:
        return "🎃"
    if CUSTOM_EMOJI_RE.match(raw):
        return raw
    if len(raw) <= 16 and not any(ch in raw for ch in "<>:@#"):
        return raw
    raise ValueError("invalid emoji")


def emoji_html(value):
    """Jinja filter: custom Discord emoji become an <img>, unicode emoji are escaped text."""
    value = (value or "🎃").strip()
    match = CUSTOM_EMOJI_RE.match(value)
    if match:
        animated, name, eid = match.groups()
        ext = "gif" if animated else "webp"
        return Markup(f'<img class="emoji-img" src="https://cdn.discordapp.com/emojis/{eid}.{ext}?size=64" alt=":{escape(name)}:" title=":{escape(name)}:" loading="lazy">')
    return escape(value)


def emoji_plain(value):
    """Unicode-only version for places that cannot render custom emoji (autocomplete labels, etc.)."""
    value = (value or "🎃").strip()
    return "" if CUSTOM_EMOJI_RE.match(value) else value


def emoji_for_button(value):
    try:
        return discord.PartialEmoji.from_str((value or "🎃").strip())
    except Exception:
        return None


app.jinja_env.filters["emoji"] = emoji_html


def item_requirement_mode(item):
    mode = str(item.get("requirement_mode", "any")).lower()
    if mode not in REQUIREMENT_MODES:
        mode = "any"
    required = [rid for rid in item.get("required_role_ids", []) if str(rid).isdigit()]
    return "everyone" if mode == "everyone" or not required else mode


def member_meets_shop_requirements(member, item):
    mode = item_requirement_mode(item)
    if mode == "everyone":
        return True
    required = {int(role_id) for role_id in item.get("required_role_ids", []) if str(role_id).isdigit()}
    owned = {role.id for role in getattr(member, "roles", [])}
    return required.issubset(owned) if mode == "all" else bool(required & owned)


def shop_requirement_text(guild, item):
    if item_requirement_mode(item) == "everyone":
        return "Everyone can buy"
    roles = [guild.get_role(int(rid)) for rid in item.get("required_role_ids", []) if str(rid).isdigit()]
    roles = [role for role in roles if role]
    if not roles:
        return "Everyone can buy"
    joiner = " AND " if item_requirement_mode(item) == "all" else " OR "
    return "Requires " + joiner.join(role.mention for role in roles)


class OwnedShopView(discord.ui.LayoutView):
    """Private, paginated Components V2 shop menu."""

    ITEMS_PER_PAGE = 5

    def __init__(self, owner_id, guild, items, member, clan_perks=None, page=0, timeout=600):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.guild = guild
        self.items = list(items)
        self.member = member
        self.clan_perks = clan_perks or {}
        self.page = max(0, min(int(page), self.total_pages - 1))
        self.message = None
        self._build_page()

    @property
    def total_pages(self):
        return max(1, (len(self.items) + self.ITEMS_PER_PAGE - 1) // self.ITEMS_PER_PAGE)

    def _page_items(self):
        start = self.page * self.ITEMS_PER_PAGE
        return self.items[start:start + self.ITEMS_PER_PAGE]

    def _new_page_view(self, page):
        return OwnedShopView(
            self.owner_id, self.guild, self.items, self.member,
            self.clan_perks, page=page,
        )

    def _build_page(self):
        page_items = self._page_items()
        summary = [
            "# 🛒 Halloween Candy Shop",
            f"<@{self.owner_id}>, spend your 🍬 Candy below. Only you can use these buttons.",
            f"**Page {self.page + 1} of {self.total_pages}** · {len(self.items)} item(s) available",
        ]

        for item_data in page_items:
            display_price, discounted = effective_shop_price(
                self.guild.id, self.owner_id, item_data, self.clan_perks,
            )
            eligible = member_meets_shop_requirements(self.member, item_data)
            requirement = shop_requirement_text(self.guild, item_data)
            status = "✅ Eligible" if eligible else "🔒 Locked"
            price_text = f"{display_price:,} 🍬"
            if discounted:
                price_text += f" (clan price; was {int(item_data.get('price', 0)):,})"
            summary.append(
                f"\n## {item_data.get('emoji', '🎃')} {item_data.get('name', 'Shop Item')} — {price_text}"
                f"\n{item_data.get('description', '')}\n{status} • {requirement}"
            )

        if not self.items:
            summary.append("\nNo shop items are currently configured.")
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(summary)[:3900])))

        if self.total_pages > 1:
            options = []
            # Discord select menus allow at most 25 options. Use a moving window
            # so the current page remains selectable even in very large catalogs.
            window_start = max(0, min(self.page - 12, self.total_pages - 25))
            window_end = min(self.total_pages, window_start + 25)
            for page_index in range(window_start, window_end):
                options.append(discord.SelectOption(
                    label=f"Page {page_index + 1}",
                    value=str(page_index),
                    default=(page_index == self.page),
                ))
            page_select = discord.ui.Select(
                placeholder=f"Jump to page {self.page + 1} of {self.total_pages}",
                min_values=1, max_values=1, options=options,
            )

            async def select_page(interaction: discord.Interaction):
                new_view = self._new_page_view(int(page_select.values[0]))
                await interaction.response.edit_message(view=new_view)
                new_view.message = interaction.message

            page_select.callback = select_page
            select_row = discord.ui.ActionRow()
            select_row.add_item(page_select)
            self.add_item(discord.ui.Container(select_row))

        buttons = []
        for item_data in page_items:
            offsale = not item_data.get("enabled", True)
            disabled = offsale or not member_meets_shop_requirements(self.member, item_data)
            if item_data.get("type") == "role":
                role_id = item_data.get("reward", {}).get("role_id")
                role = self.guild.get_role(int(role_id)) if role_id else None
                if not role or role in getattr(self.member, "roles", []):
                    disabled = True
            button_label = (
                f"Offsale: {item_data.get('name', 'Item')}" if offsale
                else f"Buy {item_data.get('name', 'Item')}"
            )
            button = discord.ui.Button(
                label=button_label[:80],
                emoji=emoji_for_button(item_data.get("emoji")),
                style=discord.ButtonStyle.secondary if offsale else (
                    discord.ButtonStyle.success if not disabled else discord.ButtonStyle.secondary
                ),
                disabled=disabled,
            )

            async def callback(btn_interaction, item_id=str(item_data["item_id"])):
                item_now = await db(lambda: shop_items.find_one({
                    "guild_id": btn_interaction.guild.id,
                    "item_id": item_id,
                    "enabled": True,
                }))
                if item_now:
                    paid_price, discounted = effective_shop_price(
                        btn_interaction.guild.id, btn_interaction.user.id, item_now,
                    )
                    price_text = f"{paid_price:,} 🍬"
                    if discounted:
                        price_text += " (10% clan discount)"
                    prompt = f"Purchase **{item_now.get('name', 'Shop Item')}** for **{price_text}**?"
                else:
                    prompt = "This shop item is no longer available. Run **/shop** to refresh the catalog."
                confirm_view = OwnedPurchaseConfirmationView(
                    btn_interaction.user.id, btn_interaction.guild.id, item_id,
                )
                if item_now is None:
                    for child in confirm_view.children:
                        child.disabled = True
                await btn_interaction.response.send_message(
                    prompt, view=confirm_view, ephemeral=True,
                )

            button.callback = callback
            buttons.append(button)

        if buttons:
            purchase_row = discord.ui.ActionRow()
            for button in buttons:
                purchase_row.add_item(button)
            self.add_item(discord.ui.Container(purchase_row))

        if self.total_pages > 1:
            previous_button = discord.ui.Button(
                label="Previous Page", emoji="⬅️",
                style=discord.ButtonStyle.secondary, disabled=(self.page == 0),
            )
            next_button = discord.ui.Button(
                label="Next Page", emoji="➡️",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.total_pages - 1),
            )

            async def go_previous(interaction: discord.Interaction):
                new_view = self._new_page_view(self.page - 1)
                await interaction.response.edit_message(view=new_view)
                new_view.message = interaction.message

            async def go_next(interaction: discord.Interaction):
                new_view = self._new_page_view(self.page + 1)
                await interaction.response.edit_message(view=new_view)
                new_view.message = interaction.message

            previous_button.callback = go_previous
            next_button.callback = go_next
            navigation_row = discord.ui.ActionRow()
            navigation_row.add_item(previous_button)
            navigation_row.add_item(next_button)
            self.add_item(discord.ui.Container(navigation_row))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"🚫 **This isn't yours!** That shop menu belongs to <@{self.owner_id}>. "
                "Use **/shop** to open your own.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class OwnedPurchaseConfirmationView(discord.ui.View):
    def __init__(self, owner_id, guild_id, item_id):
        super().__init__(timeout=180)
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.item_id = str(item_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🚫 These purchase buttons belong to someone else. Use **/shop** to browse and purchase for yourself.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    @discord.ui.button(label="Confirm purchase", style=discord.ButtonStyle.success)
    async def confirm_purchase(self, interaction, button):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This purchase can only be completed in its original server.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        controls = await db(get_member_controls, self.guild_id, self.owner_id)
        result = (
            "blocked"
            if controls["blocked_from_candy"]
            else await db(purchase_shop_item, self.guild_id, self.owner_id, self.item_id)
        )
        item_now = await db(
            lambda: shop_items.find_one({
                "guild_id": self.guild_id,
                "item_id": self.item_id,
            })
        )
        messages = {
            **SHOP_RESULT_MESSAGES,
            "blocked": "You are not allowed to participate in Candy activities in this server.",
        }
        embed = discord.Embed(
            title="🛒 Purchase Complete" if result == "success" else "🎃 Shop",
            description=messages.get(result, "The purchase could not be completed."),
            color=discord.Color.green() if result == "success" else discord.Color.red(),
        )
        if item_now:
            paid_price, discounted = effective_shop_price(
                self.guild_id,
                self.owner_id,
                item_now,
            )
            embed.add_field(
                name="Item",
                value=f"{item_now.get('emoji', '🎃')} {item_now.get('name', 'Shop Item')}",
                inline=False,
            )
            embed.add_field(
                name="Price",
                value=f"{paid_price:,} 🍬" + (" (10% clan discount)" if discounted else ""),
                inline=True,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_purchase(self, interaction, button):
        await interaction.response.edit_message(
            content="Purchase cancelled.",
            embed=None,
            view=None,
        )


SHOP_RESULT_MESSAGES = {
    "success": "Purchase complete.",
    "unavailable": "That shop item is no longer available.",
    "offline": "The bot is currently offline from this server.",
    "member_missing": "I could not find you as a server member.",
    "requirements": "You do not meet this item's role requirements.",
    "owned": "You already have that role.",
    "permissions": "I can't give that role right now. A server admin needs to give the bot the Manage Roles permission and move the bot's role above the reward role. You were not charged.",
    "not_enough": "You do not have enough Candy.",
    "assignment_failed": "Discord would not apply the role, so you were not charged. Please try again.",
    "purchase_failed": "The purchase could not be completed.",
}


def trivia_result_view(result, question, reward=None, balance=None):
    ui = discord.ui
    if result == "correct":
        text = (
            f"## ✅ Correct answer!\nYou earned **{int(reward):,} 🍬** for this question.\n"
            f"**New balance:** {int(balance):,} 🍬"
        )
        color = 0x22C55E
    elif result == "already_claimed":
        text = "## 🎃 Reward already claimed\nYou already earned Candy for this question."
        color = 0xF59E0B
    elif result == "already_tried":
        text = "## 🎃 One try only\nYou already used your try on this question. Run **/trivia** for a different one!"
        color = 0xF59E0B
    elif result == "incorrect":
        text = "## ❌ Not quite\nThat wasn't the answer, and you've used your one try on this question. Run **/trivia** for another!"
        color = 0xEF4444
    else:
        text = "## ⚠️ Question unavailable\nThis trivia question is no longer active."
        color = 0xEF4444
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay(f"{text}\n\n-# {question.get('question','Trivia')}"),
        accent_colour=color,
    ))
    return view


async def complete_trivia_attempt(interaction, question_id, answer_index=None, answer_text=None):
    guild = interaction.guild
    if guild is None:
        return "unavailable", None, None
    controls = await db(get_member_controls, guild.id, interaction.user.id)
    if controls["blocked_from_candy"]:
        return "blocked", None, None
    # Anyone can answer a posted question, including members who have never used the bot.
    # The reward update below needs an existing record, so make sure they have one.
    await db(ensure_user, guild.id, interaction.user.id, interaction.user.name, interaction.user.display_name)
    question = await db(
        trivia_questions.find_one,
        {"question_id": str(question_id), "enabled": True},
    )
    result, balance = await db(
        resolve_trivia_answer,
        guild.id,
        interaction.user.id,
        question_id,
        answer_index,
        answer_text,
    )
    if result == "correct" and question:
        await db(
            log_activity,
            "trivia_reward",
            interaction.user.id,
            interaction.user.name,
            guild.id,
            int(question.get("reward", 0)),
            {"question_id": str(question_id), "question": question.get("question", "")},
        )
    return result, balance, question


class TriviaAnswerModal(discord.ui.Modal):
    def __init__(self, question):
        super().__init__(title="Answer Trivia")
        self.question_id = str(question["question_id"])
        self.reward = int(question.get("reward", 0))
        self.question_text = str(question.get("question", "Trivia question"))
        text_style = (
            discord.TextStyle.paragraph
            if question.get("question_type") == "long" else discord.TextStyle.short
        )
        self.answer_input = discord.ui.TextInput(
            label="Your answer",
            placeholder="Type your answer here",
            style=text_style,
            min_length=1,
            max_length=1000 if text_style == discord.TextStyle.paragraph else 200,
        )
        self.add_item(self.answer_input)

    async def on_submit(self, interaction):
        result, balance, question = await complete_trivia_attempt(
            interaction, self.question_id, answer_text=self.answer_input.value,
        )
        if result == "blocked":
            view = candy_notice_view("🚫 Not allowed", "You are not allowed to participate in Candy activities in this server.")
        else:
            view = trivia_result_view(result, question or {"question": self.question_text}, self.reward, balance)
        await interaction.response.send_message(view=view, ephemeral=True)


def trivia_question_view(starter_id, question):
    """Public trivia card. Anyone in the server can answer; rewards are tracked per member."""
    ui = discord.ui
    view = ui.LayoutView(timeout=None)
    question_type = question.get("question_type")
    title = "Multiple choice" if question_type == "multiple_choice" else (
        "Short response" if question_type == "short" else "Long response"
    )
    text = (
        f"## 🧠 Trivia • {title}\n{question.get('question','')}\n\n"
        f"🏆 Reward: **{int(question.get('reward',0)):,} 🍬**\n"
        f"-# Started by <@{int(starter_id)}> • anyone can answer • run **/trivia** for your own question\n"
        "-# One try per member • Candy is awarded once per question in each reward cycle."
    )
    components = [ui.TextDisplay(text)]
    if question_type == "multiple_choice":
        for start in range(0, len(question.get("options", [])), 5):
            row = ui.ActionRow()
            for index, option in enumerate(question["options"][start:start + 5], start=start):
                button = ui.Button(label=option[:80], style=discord.ButtonStyle.secondary)

                async def callback(button_interaction, selected_index=index):
                    result, balance, current_question = await complete_trivia_attempt(
                        button_interaction, question["question_id"], answer_index=selected_index,
                    )
                    if result == "blocked":
                        result_view = candy_notice_view(
                            "🚫 Not allowed",
                            "You are not allowed to participate in Candy activities in this server.",
                        )
                    else:
                        result_view = trivia_result_view(
                            result, current_question or question,
                            question.get("reward"), balance,
                        )
                    await button_interaction.response.send_message(view=result_view, ephemeral=True)

                button.callback = callback
                row.add_item(button)
            components.append(row)
    else:
        button = ui.Button(label="Write your answer", style=discord.ButtonStyle.primary)

        async def open_answer_modal(button_interaction):
            await button_interaction.response.send_modal(TriviaAnswerModal(question))

        button.callback = open_answer_modal
        components.append(ui.ActionRow(button))
    view.add_item(ui.Container(*components, accent_colour=0x7C3AED))
    return view


def _run_on_bot_loop(bot, coro, timeout):
    return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout=timeout)


def get_fresh_member(guild_id, user_id):
    """Always ask Discord for the member's current roles. The cache can be stale because the bot
    does not run with the privileged Server Members intent."""
    bot = BOT.get("instance")
    if bot is None or bot.is_closed():
        return None
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return None
    try:
        return _run_on_bot_loop(bot, guild.fetch_member(int(user_id)), 8)
    except discord.NotFound:
        return None
    except Exception:
        log.exception("Could not fetch a fresh member %s in guild %s; using cache", user_id, guild_id)
        return guild.get_member(int(user_id))


def bot_can_assign(guild, role):
    """(ok, reason). Used both at save time and at purchase time."""
    bot = BOT.get("instance")
    me = guild.me or (guild.get_member(bot.user.id) if bot and bot.user else None)
    if role is None or role.is_default() or role.managed:
        return False, "unassignable"
    if me is None:
        return True, None  # cannot tell; let Discord decide
    if not me.guild_permissions.manage_roles:
        return False, "missing_manage_roles"
    if role >= me.top_role:
        return False, "role_above_bot"
    return True, None


def _refund_purchase(guild_id, user_id, price, item_id, purchase_id):
    purchase_match = {"item_id": item_id, "purchase_id": purchase_id}
    users.update_one({"guild_id": int(guild_id), "user_id": int(user_id)},
                     {"$inc": {"balance": price},
                      "$pull": {"inventory": purchase_match}})


def purchase_shop_item(guild_id, user_id, item_id):
    item = shop_items.find_one({"guild_id": int(guild_id), "item_id": str(item_id), "enabled": True})
    if not item: return "unavailable"
    bot = BOT.get("instance")
    guild = bot.get_guild(int(guild_id)) if bot and not bot.is_closed() else None
    if not guild: return "offline"
    member = get_fresh_member(guild_id, user_id)
    if member is None: return "member_missing"
    if not member_meets_shop_requirements(member, item): return "requirements"
    original_price = int(item.get("price", 0))
    clan_perks = member_clan_perks(guild_id, user_id)
    price, clan_discount_applied = effective_shop_price(
        guild_id, user_id, item, clan_perks,
    )
    if price < 1: return "unavailable"

    role = None
    if str(item.get("type", "custom")).lower() == "role":
        role_id = item.get("reward", {}).get("role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None: return "unavailable"
        if role in member.roles: return "owned"
        ok, reason = bot_can_assign(guild, role)
        if not ok:
            log.warning("Shop role %s (%s) cannot be assigned in guild %s: %s", role.id, role.name, guild_id, reason)
            return "unavailable" if reason == "unassignable" else "permissions"

    purchased_at = now_utc()
    purchase_id = secrets.token_urlsafe(12)
    inventory_entry = {"item_id": item["item_id"], "name": item.get("name", "Shop Item"),
                       "emoji": item.get("emoji", "🎃"), "description": item.get("description", ""),
                       "price": price, "type": item.get("type", "custom"), "purchased_at": purchased_at,
                       "purchase_id": purchase_id}
    if clan_discount_applied:
        inventory_entry["original_price"] = original_price
        inventory_entry["discount_percent"] = CLAN_DISCOUNT_PERCENT
    if role is not None:
        inventory_entry["role_id"] = role.id
    charged = False
    try:
        result = users.find_one_and_update(
            {"guild_id": int(guild_id), "user_id": int(user_id), "balance": {"$gte": price}},
            {"$inc": {"balance": -price}, "$push": {"inventory": inventory_entry}},
            return_document=ReturnDocument.AFTER,
        )
        if result is None: return "not_enough"
        charged = True

        if role is not None:
            try:
                # atomic=False sends one "add this role" request. The default (atomic=True) rewrites the
                # member's whole role list from the cache, which is stale without the Members intent and
                # can fail or even strip roles.
                _run_on_bot_loop(bot, member.add_roles(role, atomic=False,
                                 reason=f"Shop purchase: {item.get('name', role.name)}"), 15)
            except discord.Forbidden:
                log.error("Discord refused the role (403): guild=%s user=%s role=%s (%s). Bot role must be above it and have Manage Roles.",
                          guild_id, user_id, role.id, role.name)
                _refund_purchase(guild_id, user_id, price, item["item_id"], purchase_id)
                return "permissions"
            except Exception:
                log.exception("Shop role assignment failed: guild=%s user=%s role=%s (%s) item=%s",
                              guild_id, user_id, role.id, role.name, item.get("item_id"))
                _refund_purchase(guild_id, user_id, price, item["item_id"], purchase_id)
                return "assignment_failed"
            try:  # best-effort confirmation; a failed lookup must not undo a successful grant
                verified = _run_on_bot_loop(bot, guild.fetch_member(int(user_id)), 10)
                if role.id not in {r.id for r in verified.roles}:
                    log.error("Discord accepted add_roles but role %s is not on member %s.", role.id, user_id)
                    _refund_purchase(guild_id, user_id, price, item["item_id"], purchase_id)
                    return "assignment_failed"
            except Exception:
                log.warning("Could not re-check member %s after granting role %s; trusting Discord's success.", user_id, role.id)
    except Exception:
        log.exception("Shop purchase failed")
        if charged:
            _refund_purchase(guild_id, user_id, price, item["item_id"], purchase_id)
        return "purchase_failed"

    try:
        log_activity("shop_purchase", user_id, member.name, guild_id, price,
                     {"item_id": item["item_id"], "item": item.get("name"),
                      "purchase_id": purchase_id, "type": item.get("type", "custom"),
                      "original_price": original_price,
                      "clan_discount_percent": CLAN_DISCOUNT_PERCENT if clan_discount_applied else 0})
    except Exception:
        log.exception("Purchase succeeded but the activity log entry failed")
    return "success"


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


def get_bot_members_for_display(member_keys):
    bot = BOT.get("instance")
    if bot is None or bot.is_closed():
        return {}
    keys = {(int(guild_id), int(user_id)) for guild_id, user_id in member_keys}

    async def fetch_member(guild_id, user_id, semaphore):
        guild = bot.get_guild(guild_id)
        if guild is None:
            return (guild_id, user_id), None
        member = guild.get_member(user_id)
        if member is not None:
            return (guild_id, user_id), member
        async with semaphore:
            try:
                return (guild_id, user_id), await guild.fetch_member(user_id)
            except discord.NotFound:
                return (guild_id, user_id), None
            except Exception:
                log.exception("Could not fetch Discord member %s in guild %s", user_id, guild_id)
                return (guild_id, user_id), None

    async def fetch_all():
        semaphore = asyncio.Semaphore(5)
        return await asyncio.gather(
            *(fetch_member(guild_id, user_id, semaphore) for guild_id, user_id in keys)
        )

    try:
        results = _run_on_bot_loop(bot, fetch_all(), 15)
        return dict(results)
    except Exception:
        log.exception("Could not load Discord members for leaderboard badge display")
        return {}


DAILY_REWARD = 100
ARCANE_BOT_IDS = {1217870452253397082, 437808476106784770}
ARCANE_LEVEL_BASE_BONUS = 250
ARCANE_LEVEL_BONUS_PER_LEVEL = 5
ARCANE_LEVEL_PATTERN = re.compile(
    r"<@!?(\d+)>.{0,120}?(?:has\s+reached|reached|advanced\s+to|leveled\s+up\s+to|levelled\s+up\s+to|"
    r"level(?:ed)?\s+up\s+to|promoted\s+to|is\s+now|are\s+now)\s+level\s*[*_`]*\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)

TRICK_OR_TREAT_MIN = 25
TRICK_OR_TREAT_MAX = 150
MAX_TRICK_JACKPOT_ENTRIES = 20
MAX_TRICK_JACKPOT_ODDS = 1_000_000_000
MAX_TRICK_JACKPOT_REWARD = 1_000_000_000


def is_admin(user_id):
    return int(user_id) in ADMIN_USER_IDS


def valid_gradient_color(value, fallback):
    value = str(value or "")
    return value.lower() if HEX_COLOR_RE.fullmatch(value) else fallback


def get_badge_configs(guild_id):
    document = presentation_settings.find_one(
        {"guild_id": int(guild_id)},
        {"badges": 1},
    ) or {}
    configs = []
    saved_badges = document.get("badges", [])
    if not isinstance(saved_badges, list):
        return configs
    for item in saved_badges:
        if not isinstance(item, dict):
            continue
        target_type = item.get("target_type", "role")
        target_id = str(item.get("target_id", item.get("role_id", "")))
        badge_type = item.get("badge_type")
        if (target_type not in {"role", "user"} or not re.fullmatch(r"[0-9]+", target_id)
                or badge_type not in BADGE_TYPES):
            continue
        label, emoji_name, emoji_id = BADGE_TYPES[badge_type]
        configs.append({
            "target_type": target_type,
            "target_id": target_id,
            "target_name": str(item.get("target_name") or item.get("role_name")
                               or f"{'Role' if target_type == 'role' else 'User'} {target_id}"),
            "badge_type": badge_type,
            "badge_label": label,
            "emoji": f"<:{emoji_name}:{emoji_id}>",
            "icon_url": f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=64",
            "gradient_start": valid_gradient_color(item.get("gradient_start"), "#8b5cf6"),
            "gradient_end": valid_gradient_color(item.get("gradient_end"), "#f97316"),
            "gradient_enabled": item.get("gradient_enabled", True) is True,
        })
    return configs


def badge_presentation_for_member(user_id, member, configs, preferred_target=None):
    if not configs:
        return [], None, [], False
    role_positions = {
        str(role.id): role.position for role in getattr(member, "roles", [])
    }
    matching_user_badges = [
        config for config in configs
        if config["target_type"] == "user" and config["target_id"] == str(user_id)
    ]
    matching_role_badges = sorted(
        (config for config in configs
         if config["target_type"] == "role" and config["target_id"] in role_positions),
        key=lambda config: role_positions[config["target_id"]],
    )
    matching = sorted(
        matching_user_badges + matching_role_badges,
        key=lambda config: (
            1 if config["target_type"] == "user" else 0,
            role_positions.get(config["target_id"], 0),
        ),
        reverse=True,
    )
    if matching_user_badges:
        selected = matching_user_badges[0]
        options = []
        user_badge_override = True
    else:
        options = [{
            "target_type": config["target_type"],
            "target_id": config["target_id"],
            "label": f"{config['target_name']} — {config['badge_label']}",
        } for config in matching_role_badges]
        selected = next(
            (config for config in matching_role_badges
             if preferred_target == f"{config['target_type']}:{config['target_id']}"),
            matching_role_badges[0] if matching_role_badges else None,
        )
        if preferred_target == "none":
            selected = None
        user_badge_override = False
    badges = [{
        "icon_url": config["icon_url"],
        "label": config["badge_label"],
        "emoji": config["emoji"],
        "badge_type": config["badge_type"],
    } for config in [selected] if selected]
    gradient = None
    gradient_configs = [config for config in matching if config["gradient_enabled"]]
    if gradient_configs:
        gradient = {
            "start": gradient_configs[0]["gradient_start"],
            "end": gradient_configs[0]["gradient_end"],
        }
    return badges, gradient, options, user_badge_override


def get_member_badge_preference(guild_id, user_id):
    preference = member_badge_preferences.find_one(
        {"guild_id": int(guild_id), "user_id": int(user_id)},
        {"target": 1},
    )
    return preference.get("target") if preference else None


def leaderboard_member_badges(guild_id, rows):
    configs = get_badge_configs(guild_id)
    if not configs:
        return {}
    member_keys = [
        (int(guild_id), int(row["user_id"]))
        for row in rows
        if row.get("user_id") is not None
    ]
    members = get_bot_members_for_display(member_keys)
    badges_by_user = {}
    for row in rows:
        user_id = int(row["user_id"])
        badges, _, _, _ = badge_presentation_for_member(
            user_id,
            members.get((int(guild_id), user_id)),
            configs,
            get_member_badge_preference(guild_id, user_id),
        )
        badges_by_user[user_id] = badges
    return badges_by_user


def member_badges_for_display(guild_id, user_id, member):
    return badge_presentation_for_member(
        user_id,
        member,
        get_badge_configs(guild_id),
        get_member_badge_preference(guild_id, user_id),
    )[0]


def is_command_enabled(command_name):
    record = command_access.find_one({"command_name": command_name})
    return record is None or record.get("enabled", True)


def user_authorization_status(user_id):
    """'ok' = authorized with Discord, accepted the current Terms/Privacy version and enabled DMs.
    'outdated' = accepted an older version. 'missing' = never authorized."""
    record = user_authorizations.find_one({"_id": int(user_id)})
    if not record or not record.get("dms_enabled"):
        return "missing"
    if record.get("tos_version") != TOS_VERSION:
        return "outdated"
    return "ok"


def record_user_authorization(user_id, username):
    now = now_utc()
    user_authorizations.update_one(
        {"_id": int(user_id)},
        {
            "$set": {
                "username": username,
                "tos_version": TOS_VERSION,
                "terms_accepted_at": now,
                "privacy_accepted_at": now,
                "dms_enabled": True,
                "authorized_at": now,
            },
            "$setOnInsert": {"first_authorized_at": now},
        },
        upsert=True,
    )


def public_base_url():
    """Base URL of the web app, used for the authorize link. PUBLIC_BASE_URL wins; otherwise it is
    derived from OAUTH2_REDIRECT_URI so no extra config is needed."""
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    parsed = urlparse(OAUTH2_REDIRECT_URI or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None



def get_economy_config():
    record = economy_config.find_one({"_id": "global"}) or {}
    jackpots = []
    saved_jackpots = record.get("trick_or_treat_jackpots", [])
    if not isinstance(saved_jackpots, list):
        saved_jackpots = []
    for item in saved_jackpots:
        if not isinstance(item, dict):
            continue
        try:
            odds = int(item.get("odds", 0))
            reward = int(item.get("reward", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= odds <= MAX_TRICK_JACKPOT_ODDS and 1 <= reward <= MAX_TRICK_JACKPOT_REWARD:
            jackpots.append({"odds": odds, "reward": reward})
    return {
        "daily_reward": int(record.get("daily_reward", DAILY_REWARD)),
        "trick_or_treat_min": int(record.get("trick_or_treat_min", TRICK_OR_TREAT_MIN)),
        "trick_or_treat_max": int(record.get("trick_or_treat_max", TRICK_OR_TREAT_MAX)),
        "trick_or_treat_jackpots": jackpots[:MAX_TRICK_JACKPOT_ENTRIES],
        "trivia_looping": bool(record.get("trivia_looping", False)),
        "trivia_shuffle": bool(record.get("trivia_shuffle", True)),
    }


def set_economy_config(daily_reward, trick_or_treat_min, trick_or_treat_max, trick_or_treat_jackpots):
    economy_config.update_one(
        {"_id": "global"},
        {"$set": {
            "daily_reward": int(daily_reward),
            "trick_or_treat_min": int(trick_or_treat_min),
            "trick_or_treat_max": int(trick_or_treat_max),
            "trick_or_treat_jackpots": trick_or_treat_jackpots,
            "updated_at": now_utc(),
        }},
        upsert=True,
    )


def get_clan_config(guild_id):
    record = clan_config.find_one({"guild_id": int(guild_id)}) or {}
    try:
        discount_threshold = int(record.get("discount_threshold", CLAN_DEFAULT_DISCOUNT_THRESHOLD))
        daily_threshold = int(record.get("daily_threshold", CLAN_DEFAULT_DAILY_THRESHOLD))
    except (TypeError, ValueError):
        discount_threshold = CLAN_DEFAULT_DISCOUNT_THRESHOLD
        daily_threshold = CLAN_DEFAULT_DAILY_THRESHOLD
    return {
        "discount_threshold": max(0, min(discount_threshold, 1_000_000_000)),
        "daily_threshold": max(0, min(daily_threshold, 1_000_000_000)),
    }


def set_clan_config(guild_id, discount_threshold, daily_threshold, updated_by):
    clan_config.update_one(
        {"guild_id": int(guild_id)},
        {"$set": {
            "guild_id": int(guild_id),
            "discount_threshold": int(discount_threshold),
            "daily_threshold": int(daily_threshold),
            "updated_at": now_utc(),
            "updated_by": int(updated_by),
        }},
        upsert=True,
    )


def clan_progress(guild_id, clan):
    config = get_clan_config(guild_id)
    earned = 0
    members = list(users.find(
        {"guild_id": int(guild_id), "clan_id": str(clan["clan_id"])},
        {"candy_earned_total": 1, "clan_candy_earned_at_join": 1},
    ))
    for member in members:
        total = int(member.get("candy_earned_total", 0) or 0)
        joined_at = int(member.get("clan_candy_earned_at_join", total) or 0)
        earned += max(0, total - joined_at)
    return {
        "earned": earned,
        "member_count": len(members),
        "discount_threshold": config["discount_threshold"],
        "daily_threshold": config["daily_threshold"],
        "discount_unlocked": earned >= config["discount_threshold"],
        "daily_unlocked": earned >= config["daily_threshold"],
    }


def clan_member_profile(guild_id, member_record):
    member_record = member_record or {}
    clan_id = member_record.get("clan_id")
    if not clan_id:
        return None
    clan = clans.find_one({
        "guild_id": int(guild_id),
        "clan_id": str(clan_id),
        "status": "active",
    })
    if clan is None:
        return None
    earned_total = int(member_record.get("candy_earned_total", 0) or 0)
    earned_at_join = int(member_record.get("clan_candy_earned_at_join", earned_total) or 0)
    return {
        "name": clan.get("name", "Unknown Clan"),
        "contributed": max(0, earned_total - earned_at_join),
        **clan_verification_presentation(clan.get("verification_badge")),
    }


def member_clan_perks(guild_id, user_id):
    member_record = users.find_one({"guild_id": int(guild_id), "user_id": int(user_id)}) or {}
    clan_id = member_record.get("clan_id")
    clan = clans.find_one({"guild_id": int(guild_id), "clan_id": str(clan_id)}) if clan_id else None
    if clan is None:
        return None
    progress = clan_progress(guild_id, clan)
    return {"clan": clan, **progress}


def effective_shop_price(guild_id, user_id, item, clan_perks=None):
    price = max(0, int(item.get("price", 0) or 0))
    if item.get("clan_discount_excluded", False):
        return price, False
    perks = clan_perks if clan_perks is not None else member_clan_perks(guild_id, user_id)
    if not perks or not perks["discount_unlocked"]:
        return price, False
    return max(1, (price * (100 - CLAN_DISCOUNT_PERCENT) + 50) // 100), True


def clan_member_assign(guild_id, user_id, clan):
    if users.find_one({"guild_id": int(guild_id), "user_id": int(user_id)}) is None:
        ensure_user(guild_id, user_id)
    return users.find_one_and_update(
        {
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "$or": [
                {"clan_id": {"$exists": False}},
                {"clan_id": None},
                {"clan_id": ""},
            ],
        },
        [{"$set": {
            "clan_id": str(clan["clan_id"]),
            "clan_candy_earned_at_join": {"$ifNull": ["$candy_earned_total", 0]},
            "clan_joined_at": now_utc(),
        }}],
        return_document=ReturnDocument.AFTER,
    )


def clan_member_clear(guild_id, user_id):
    return users.update_one(
        {"guild_id": int(guild_id), "user_id": int(user_id)},
        {"$unset": {
            "clan_id": "",
            "clan_candy_earned_at_join": "",
            "clan_joined_at": "",
        }},
    )


def clan_withdraw_pending_membership_requests(guild_id, user_id):
    response = {"status": "withdrawn", "responded_at": now_utc()}
    query = {"guild_id": int(guild_id), "user_id": int(user_id), "status": "pending"}
    clan_join_requests.update_many(query, {"$set": response})
    clan_invites.update_many(
        {"guild_id": int(guild_id), "target_id": int(user_id), "status": "pending"},
        {"$set": response},
    )


def clan_member_departure(guild_id, user_id, clan_id):
    clan = clans.find_one({
        "guild_id": int(guild_id),
        "clan_id": str(clan_id),
        "status": "active",
        "owner_id": int(user_id),
    })
    if clan is None:
        return
    next_owner = users.find({
        "guild_id": int(guild_id),
        "clan_id": str(clan_id),
        "user_id": {"$ne": int(user_id)},
    }).sort("clan_joined_at", 1).limit(1)
    next_owner = next(iter(next_owner), None)
    if next_owner:
        clans.update_one(
            {"guild_id": int(guild_id), "clan_id": str(clan_id), "owner_id": int(user_id)},
            {"$set": {"owner_id": int(next_owner["user_id"]), "updated_at": now_utc()}},
        )
        send_clan_dm(next_owner["user_id"], f"You are now the creator of **{clan['name']}**.")
        return
    clans.delete_one({"guild_id": int(guild_id), "clan_id": str(clan_id), "owner_id": int(user_id)})
    clan_join_requests.update_many(
        {"guild_id": int(guild_id), "clan_id": str(clan_id), "status": "pending"},
        {"$set": {"status": "withdrawn"}},
    )
    clan_invites.update_many(
        {"guild_id": int(guild_id), "clan_id": str(clan_id), "status": "pending"},
        {"$set": {"status": "withdrawn"}},
    )


def clan_image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def roll_trick_or_treat_jackpots(jackpots):
    """Independently roll each configured jackpot; return the total and every prize won."""
    wins = [
        {"odds": int(jackpot["odds"]), "reward": int(jackpot["reward"])}
        for jackpot in jackpots
        if random.randint(1, int(jackpot["odds"])) == 1
    ]
    return sum(jackpot["reward"] for jackpot in wins), wins


def normalize_trivia_answer(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def trivia_answer_matches(answer, accepted_answers):
    normalized_answer = normalize_trivia_answer(answer)
    if not normalized_answer:
        return False
    for accepted in accepted_answers:
        normalized_accepted = normalize_trivia_answer(accepted)
        if not normalized_accepted:
            continue
        if normalized_accepted in normalized_answer:
            return True
        if len(normalized_answer) >= 3 and normalized_answer in normalized_accepted:
            return True
    return False


def resolve_trivia_answer(guild_id, user_id, question_id, answer_index=None, answer_text=None):
    question = trivia_questions.find_one({"question_id": str(question_id), "enabled": True})
    if question is None:
        return "unavailable", None
    # One try per member per question. The attempt is recorded atomically *before* the answer is
    # judged, so a double click or a second tab can never get a second guess.
    attempt = users.find_one_and_update(
        {
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "trivia_claims": {"$ne": str(question_id)},
            "trivia_attempts": {"$ne": str(question_id)},
        },
        {"$addToSet": {"trivia_attempts": str(question_id)}},
        return_document=ReturnDocument.AFTER,
    )
    if attempt is None:
        record = users.find_one({"guild_id": int(guild_id), "user_id": int(user_id)})
        if record is None:
            return "unavailable", None
        balance = int(record.get("balance", 0))
        if str(question_id) in (record.get("trivia_claims") or []):
            return "already_claimed", balance
        return "already_tried", balance
    question_type = question.get("question_type")
    if question_type == "multiple_choice":
        try:
            correct = int(answer_index) == int(question.get("correct_index", -1))
        except (TypeError, ValueError):
            correct = False
    else:
        correct = trivia_answer_matches(answer_text, question.get("accepted_answers", []))
    if not correct:
        return "incorrect", None

    reward = int(question.get("reward", 0))
    updated = users.find_one_and_update(
        {
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "trivia_claims": {"$ne": str(question_id)},
        },
        {"$inc": {
            "balance": reward,
            "candy_earned_total": reward,
        }, "$addToSet": {"trivia_claims": str(question_id)}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        record = users.find_one({"guild_id": int(guild_id), "user_id": int(user_id)})
        if record and str(question_id) in (record.get("trivia_claims") or []):
            return "already_claimed", int(record.get("balance", 0))
        return "unavailable", None
    return "correct", int(updated.get("balance", 0))


def trivia_answered_ids(record):
    """Question ids this member is finished with in the current cycle: earned Candy OR used their one try."""
    record = record or {}
    return set(record.get("trivia_claims") or []) | set(record.get("trivia_attempts") or [])


def reset_trivia_claims_for_loop(guild_id, user_id, record, active_question_ids):
    claimed = set(record.get("trivia_claims") or []) | set(record.get("trivia_attempts") or [])
    if not active_question_ids or not all(question_id in claimed for question_id in active_question_ids):
        return None
    cycle = int(record.get("trivia_cycle", 0))
    query = {"guild_id": int(guild_id), "user_id": int(user_id)}
    if cycle == 0:
        query["$or"] = [{"trivia_cycle": 0}, {"trivia_cycle": {"$exists": False}}]
    else:
        query["trivia_cycle"] = cycle
    if "trivia_claims" in record:
        query["trivia_claims"] = record["trivia_claims"]
    else:
        query["trivia_claims"] = {"$exists": False}
    return users.find_one_and_update(
        query,
        {"$set": {"trivia_claims": [], "trivia_attempts": []}, "$inc": {"trivia_cycle": 1}},
        return_document=ReturnDocument.AFTER,
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


def calculate_daily_reward(base_reward, role_ids, guild_id, clan_daily_bonus=False):
    boosts = get_daily_boosts(guild_id)
    role_ids = {int(role_id) for role_id in role_ids}
    matching = [boost for boost in boosts if int(boost.get("role_id", 0)) in role_ids]
    boost = max(matching, key=lambda item: float(item.get("multiplier", 1))) if matching else None
    multiplier = float(boost.get("multiplier", 1)) if boost else 1.0
    if clan_daily_bonus:
        multiplier *= CLAN_DAILY_MULTIPLIER
    return max(1, int(round(int(base_reward) * multiplier))), boost


# =====================================================================
#  /give LIMITS
#  Per-server config: an on/off switch plus any number of rules. A rule is
#  "members may give at most N Candy per hour, day or week", applied
#  to everyone or only to members holding one of the chosen roles. Windows are
#  rolling (last 60 minutes / 24 hours / 7 days), measured from the activity
#  log. If several rules apply to a member, every one of them must pass.
# =====================================================================
GIVE_LIMIT_PERIODS = {
    "hour": ("Per hour", timedelta(hours=1)),
    "day": ("Per day", timedelta(days=1)),
    "week": ("Per week", timedelta(weeks=1)),
}
GIVE_LIMIT_MAX_RULES = 20
LIMIT_KINDS = {
    "give": {"coll": give_limits, "action": "give", "log": "give_limits", "max": 100000000},
}


def get_limit_config(kind, guild_id):
    record = LIMIT_KINDS[kind]["coll"].find_one({"guild_id": int(guild_id)}) or {}
    rules = []
    for rule in record.get("rules", []):
        if rule.get("period") not in GIVE_LIMIT_PERIODS:
            continue
        rules.append({
            "id": str(rule.get("id", "")),
            "amount": int(rule.get("amount", 0)),
            "period": rule["period"],
            "scope": "roles" if rule.get("scope") == "roles" else "everyone",
            "role_ids": [int(r) for r in rule.get("role_ids", [])],
        })
    return {"enabled": bool(record.get("enabled", False)), "rules": rules}


def set_limit_enabled(kind, guild_id, enabled, updated_by=None):
    LIMIT_KINDS[kind]["coll"].update_one(
        {"guild_id": int(guild_id)},
        {"$set": {"guild_id": int(guild_id), "enabled": bool(enabled),
                  "updated_at": now_utc(), "updated_by": int(updated_by) if updated_by else None}},
        upsert=True,
    )


def set_limit_rules(kind, guild_id, rules, valid_role_ids, updated_by=None):
    """Replace the saved rules. Raises ValueError on anything invalid."""
    if len(rules) > GIVE_LIMIT_MAX_RULES:
        raise ValueError("Too many rules.")
    valid_role_ids = {int(r) for r in valid_role_ids}
    cleaned = []
    for rule in rules:
        amount = int(rule["amount"])
        if amount < 1 or amount > LIMIT_KINDS[kind]["max"]:
            raise ValueError("Limit amount out of range.")
        if rule["period"] not in GIVE_LIMIT_PERIODS:
            raise ValueError("Unknown period.")
        scope = rule["scope"]
        if scope not in {"everyone", "roles"}:
            raise ValueError("Unknown effect.")
        role_ids = []
        if scope == "roles":
            role_ids = sorted({int(r) for r in rule.get("role_ids", [])})
            if not role_ids or not set(role_ids) <= valid_role_ids:
                raise ValueError("Pick at least one valid role.")
        cleaned.append({"id": secrets.token_hex(4), "amount": amount, "period": rule["period"],
                        "scope": scope, "role_ids": role_ids})
    LIMIT_KINDS[kind]["coll"].update_one(
        {"guild_id": int(guild_id)},
        {"$set": {"guild_id": int(guild_id), "rules": cleaned,
                  "updated_at": now_utc(), "updated_by": int(updated_by) if updated_by else None},
         "$setOnInsert": {"enabled": False}},
        upsert=True,
    )
    return cleaned


def check_limits(kind, guild_id, user_id, role_ids, pending=1):
    """Check a pending action against the server's limit rules.

    `pending` is the Candy amount being given.
    Returns (blocked, statuses). `statuses` has one entry per rule that applies to this member, each with
    limit / used / remaining / period / resets_at. `blocked` is the most restrictive failing entry, or None.
    `statuses` is empty when limits are off or no rule applies to the member.
    """
    spec = LIMIT_KINDS[kind]
    config = get_limit_config(kind, guild_id)
    if not config["enabled"] or not config["rules"]:
        return None, []
    pending = int(pending)
    role_ids = {int(r) for r in role_ids}
    now = now_utc()
    statuses = []
    for rule in config["rules"]:
        if rule["scope"] == "roles" and not role_ids.intersection(rule["role_ids"]):
            continue
        window = GIVE_LIMIT_PERIODS[rule["period"]][1]
        entries = list(activity_logs.find(
            {"action": spec["action"], "guild_id": int(guild_id), "user_id": int(user_id), "created_at": {"$gte": now - window}},
            {"amount": 1, "created_at": 1},
        ).sort("created_at", 1))
        used = sum(int(e.get("amount") or 0) for e in entries)
        limit = rule["amount"]
        status = {"limit": limit, "used": used, "remaining": max(0, limit - used), "period": rule["period"],
                  "exceeds": used + pending > limit, "too_big": pending > limit, "resets_at": None}
        if status["exceeds"] and not status["too_big"]:
            need, freed = used + pending - limit, 0
            for entry in entries:
                freed += int(entry.get("amount") or 0)
                if freed >= need:
                    created = entry["created_at"]
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    status["resets_at"] = created + window
                    break
        statuses.append(status)
    failing = [st for st in statuses if st["exceeds"]]
    blocked = min(failing, key=lambda st: st["remaining"]) if failing else None
    return blocked, statuses


def get_give_limits(guild_id):
    return get_limit_config("give", guild_id)


def check_give_limits(guild_id, user_id, role_ids, amount):
    return check_limits("give", guild_id, user_id, role_ids, amount)


def get_give_tax_percent(guild_id):
    record = give_tax_settings.find_one({"guild_id": int(guild_id)}) or {}
    return max(0, min(100, int(record.get("percent", 0) or 0)))


def set_give_tax_percent(guild_id, percent, updated_by=None):
    percent = int(percent)
    if not 0 <= percent <= 100:
        raise ValueError("Give tax percentage must be between 0 and 100.")
    give_tax_settings.update_one(
        {"guild_id": int(guild_id)},
        {"$set": {
            "guild_id": int(guild_id),
            "percent": percent,
            "updated_at": now_utc(),
            "updated_by": int(updated_by) if updated_by else None,
        }},
        upsert=True,
    )
    return percent


def calculate_give_tax(amount, percent):
    amount, percent = int(amount), int(percent)
    return (amount * percent + 99) // 100


# =====================================================================
#  /trickortreat COOLDOWN
#  Everyone waits TRICK_COOLDOWN_DEFAULT_SECONDS (1 minute) between trick-or-treats. Admins can turn on
#  custom cooldowns per server: each rule is a length of time that applies to everyone or only to members with
#  chosen roles. If a member matches several rules, the SHORTEST one wins (so a role can get a faster cooldown).
#  A member who matches no rule, or a server with custom cooldowns switched off, uses the default.
# =====================================================================
TRICK_COOLDOWN_DEFAULT_SECONDS = 60
TRICK_COOLDOWN_MAX_SECONDS = 7 * 24 * 3600
TRICK_COOLDOWN_UNITS = {"seconds": ("Seconds", 1), "minutes": ("Minutes", 60), "hours": ("Hours", 3600)}


def split_cooldown_seconds(total):
    """Turn a number of seconds into the largest whole unit, e.g. 120 -> (2, 'minutes')."""
    for unit in ("hours", "minutes"):
        size = TRICK_COOLDOWN_UNITS[unit][1]
        if total % size == 0:
            return total // size, unit
    return total, "seconds"


def get_trick_cooldowns(guild_id):
    record = trick_cooldowns.find_one({"guild_id": int(guild_id)}) or {}
    rules = []
    for rule in record.get("rules", []):
        seconds = int(rule.get("seconds", 0))
        if seconds < 1:
            continue
        amount, unit = split_cooldown_seconds(seconds)
        rules.append({
            "id": str(rule.get("id", "")), "seconds": seconds, "amount": amount, "period": unit,
            "scope": "roles" if rule.get("scope") == "roles" else "everyone",
            "role_ids": [int(r) for r in rule.get("role_ids", [])],
        })
    return {"enabled": bool(record.get("enabled", False)), "rules": rules}


def set_trick_cooldowns_enabled(guild_id, enabled, updated_by=None):
    trick_cooldowns.update_one(
        {"guild_id": int(guild_id)},
        {"$set": {"guild_id": int(guild_id), "enabled": bool(enabled),
                  "updated_at": now_utc(), "updated_by": int(updated_by) if updated_by else None}},
        upsert=True,
    )


def set_trick_cooldown_rules(guild_id, rules, valid_role_ids, updated_by=None):
    """Replace the saved cooldown rules. Raises ValueError on anything invalid."""
    if len(rules) > GIVE_LIMIT_MAX_RULES:
        raise ValueError("Too many cooldowns.")
    valid_role_ids = {int(r) for r in valid_role_ids}
    cleaned = []
    for rule in rules:
        if rule["period"] not in TRICK_COOLDOWN_UNITS:
            raise ValueError("Unknown time unit.")
        seconds = int(rule["amount"]) * TRICK_COOLDOWN_UNITS[rule["period"]][1]
        if seconds < 1 or seconds > TRICK_COOLDOWN_MAX_SECONDS:
            raise ValueError("Cooldown out of range.")
        scope = rule["scope"]
        if scope not in {"everyone", "roles"}:
            raise ValueError("Unknown effect.")
        role_ids = []
        if scope == "roles":
            role_ids = sorted({int(r) for r in rule.get("role_ids", [])})
            if not role_ids or not set(role_ids) <= valid_role_ids:
                raise ValueError("Pick at least one valid role.")
        cleaned.append({"id": secrets.token_hex(4), "seconds": seconds, "scope": scope, "role_ids": role_ids})
    trick_cooldowns.update_one(
        {"guild_id": int(guild_id)},
        {"$set": {"guild_id": int(guild_id), "rules": cleaned,
                  "updated_at": now_utc(), "updated_by": int(updated_by) if updated_by else None},
         "$setOnInsert": {"enabled": False}},
        upsert=True,
    )
    return cleaned


def resolve_trick_cooldown(guild_id, role_ids):
    """Cooldown in seconds for a member with these roles."""
    config = get_trick_cooldowns(guild_id)
    if not config["enabled"]:
        return TRICK_COOLDOWN_DEFAULT_SECONDS
    role_ids = {int(r) for r in role_ids}
    applicable = [rule["seconds"] for rule in config["rules"]
                  if rule["scope"] == "everyone" or role_ids.intersection(rule["role_ids"])]
    return min(applicable) if applicable else TRICK_COOLDOWN_DEFAULT_SECONDS


# =====================================================================
#  LOGGING SYSTEM
#  Events are queued, rendered once as a Components V2 "spec", then
#  delivered to a channel (discord.ui.LayoutView) or a webhook (raw JSON
#  with the IS_COMPONENTS_V2 flag). Delivery results are saved so the
#  dashboard can show exactly why something failed.
# =====================================================================
COMPONENTS_V2_FLAG = 1 << 15

LOG_CATEGORY_META = {
    "economy":         {"emoji": "🍬", "label": "Economy",         "color": 0xF97316, "desc": "Daily rewards, trick-or-treat, Candy transfers"},
    "rewards":         {"emoji": "⭐", "label": "Rewards",         "color": 0xFACC15, "desc": "Arcane level-up bonuses"},
    "shop":            {"emoji": "🛒", "label": "Shop",            "color": 0x7C3AED, "desc": "Item purchases"},
    "member_activity": {"emoji": "👤", "label": "Member activity", "color": 0x3B82F6, "desc": "Member blocks and leaderboard controls"},
    "admin":           {"emoji": "🛡️", "label": "Admin",           "color": 0xEC4899, "desc": "Manual Candy changes, boosts, settings"},
    "errors":          {"emoji": "⚠️", "label": "Errors",          "color": 0xEF4444, "desc": "Failures worth a look"},
    "system":          {"emoji": "⚙️", "label": "System",          "color": 0x6B7280, "desc": "Everything else, including test logs"},
}
LOG_CATEGORIES = tuple(LOG_CATEGORY_META)

# action -> (category, emoji, title, amount sign: +1 / -1 / 0)
LOG_EVENTS = {
    "daily":               ("economy", "🎁", "Daily Reward", +1),
    "trick_or_treat":      ("economy", "🎃", "Trick or Treat", +1),
    "give":                ("economy", "🤝", "Candy Transfer", 0),
    "buy":                 ("shop", "🛒", "Shop Purchase", 0),
    "web_buy":             ("shop", "🛒", "Shop Purchase (Web)", 0),
    "shop_purchase":       ("shop", "🛒", "Shop Purchase", 0),
    "shop_purchase_removed": ("shop", "🗑️", "Shop Purchase Removed", 0),
    "trivia_reward":        ("economy", "🧠", "Trivia Reward", +1),
    "trivia_question_saved": ("admin", "🧠", "Trivia Question Saved", 0),
    "trivia_question_removed": ("admin", "🗑️", "Trivia Question Removed", 0),
    "trivia_looping":       ("admin", "🔄", "Trivia Looping Updated", 0),
    "trivia_shuffle":       ("admin", "🔀", "Trivia Shuffle Updated", 0),
    "clan_perks_updated":   ("admin", "🏰", "Clan Perks Updated", 0),
    "clan_member_admin":    ("admin", "🏰", "Clan Membership Changed", 0),
    "arcane_level_bonus":  ("rewards", "⭐", "Arcane Level Bonus", +1),
    "member_control":      ("member_activity", "🧭", "Member Control Changed", 0),
    "admin_add":           ("admin", "➕", "Candy Added by Admin", +1),
    "admin_subtract":      ("admin", "➖", "Candy Removed by Admin", -1),
    "admin_reset_candy":   ("admin", "🧹", "Candy Balance Reset", -1),
    "daily_boost":         ("admin", "⚡", "Daily Boost Set", 0),
    "daily_boost_removed": ("admin", "⚡", "Daily Boost Removed", 0),
    "give_limits":         ("admin", "🎁", "Give Limits Updated", 0),
    "trick_cooldown":      ("admin", "🎃", "Trick-or-Treat Cooldown Updated", 0),
    "economy_settings":    ("admin", "⚙️", "Economy Settings Changed", 0),
    "logging_test":        ("system", "🧪", "Logging Test", 0),
}

DETAIL_LABELS = {
    "changed_by": "Changed by", "recipient_id": "Recipient", "boost_role": "Boost role",
    "role_id": "Role", "role_name": "Role name", "control": "Control", "level": "Level",
    "trick_or_treat_min": "Trick-or-treat min", "trick_or_treat_max": "Trick-or-treat max",
    "discount_threshold": "Clan discount threshold", "daily_threshold": "Clan daily threshold",
    "daily_reward": "Daily reward", "rule_count": "Rules", "item": "Item",
    "item_id": "Item ID", "purchase_id": "Purchase ID", "refunded": "Refunded",
    "removed_by": "Removed by", "question_id": "Question ID", "question_type": "Question type",
    "reward": "Candy reward", "question": "Question", "source": "Source",
    "role_removed": "Role removed",
}
DETAIL_HIDDEN = {"source_bot_id", "test", "recipient"}
SOURCE_LABELS = {"web_users": "Web dashboard (Users)", "web_daily": "Web dashboard (Daily)"}


def event_info(action):
    """Metadata for an action; unknown actions fall back to a sensible category."""
    action = action or ""
    if action in LOG_EVENTS:
        category, emoji, title, sign = LOG_EVENTS[action]
    else:
        category = "errors" if action.startswith("error") else "system"
        emoji = "⚠️" if category == "errors" else "📌"
        title, sign = action.replace("_", " ").title() or "Event", 0
    meta = LOG_CATEGORY_META[category]
    return {"category": category, "emoji": emoji, "title": title, "sign": sign,
            "color": meta["color"], "category_label": meta["label"]}


def log_category(action):
    return event_info(action)["category"]


def actions_in_category(category):
    return [a for a, v in LOG_EVENTS.items() if v[0] == category]


# ---------- webhook helpers ----------
WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(\d{15,25})/([A-Za-z0-9_\-]{30,})/?(?:\?.*)?$"
)


def normalize_webhook_url(url):
    """Return a clean https://discord.com/api/webhooks/ID/TOKEN[?thread_id=N] URL, or None.
    Only real Discord webhook hosts are accepted, so the server can never be pointed at arbitrary URLs."""
    url = (url or "").strip()
    match = WEBHOOK_RE.match(url)
    if not match:
        return None
    wid, token = match.groups()
    clean = f"https://discord.com/api/webhooks/{wid}/{token}"
    thread = parse_qs(urlparse(url).query).get("thread_id", [None])[0]
    if thread and thread.isdigit():
        clean += f"?thread_id={thread}"
    return clean


def mask_webhook(url):
    match = WEBHOOK_RE.match(url or "")
    if not match:
        return "invalid webhook"
    return f"…/webhooks/{match.group(1)}/••••{match.group(2)[-4:]}"


def verify_webhook(url):
    """Ask Discord whether the webhook really exists. Returns its name or raises ValueError(message)."""
    base = url.split("?")[0]
    try:
        response = requests.get(base, timeout=6)
    except requests.RequestException:
        raise ValueError("Could not reach Discord to verify that webhook. Try again in a moment.")
    if response.status_code == 200:
        return response.json().get("name") or "Webhook"
    if response.status_code in (401, 404):
        raise ValueError("Discord says that webhook does not exist (it may have been deleted or the URL is incomplete).")
    raise ValueError(f"Discord rejected that webhook (HTTP {response.status_code}).")


def describe_webhook_error(status, body):
    message = ""
    try:
        data = json.loads(body)
        message = data.get("message", "")
        if data.get("errors"):
            message += " " + json.dumps(data["errors"])[:200]
    except (ValueError, TypeError):
        message = (body or "")[:120]
    hints = {
        401: "Webhook token is invalid.",
        403: "Webhook is not allowed to post there.",
        404: "Webhook was deleted or the URL is wrong.",
    }
    hint = hints.get(status, "")
    return f"HTTP {status}: {hint} {message}".strip()


# ---------- config ----------
_CFG_CACHE = {}
_CFG_TTL = 10


def get_logging_config(guild_id, fresh=False):
    key = str(guild_id)
    cached = _CFG_CACHE.get(key)
    if cached and not fresh and time.monotonic() - cached[0] < _CFG_TTL:
        return cached[1]
    record = logging_config.find_one({"_id": key}) or {}
    cfg = {
        "enabled": bool(record.get("enabled", True)),
        "mode": record.get("mode", "simple"),
        "main": record.get("main") or {"type": "channel", "channel_id": ""},
        "advanced": record.get("advanced") or {},
        "delivery": record.get("delivery") or {},
        "updated_at": record.get("updated_at"),
    }
    _CFG_CACHE[key] = (time.monotonic(), cfg)
    return cfg


def invalidate_logging_cache(guild_id):
    _CFG_CACHE.pop(str(guild_id), None)


def resolve_destination(cfg, category):
    """Returns (key, destination) or (None, None) when this category should not be delivered."""
    if cfg["mode"] == "advanced":
        override = cfg["advanced"].get(category)
        if override:
            if override.get("type") == "off":
                return None, None
            if override.get("type") in {"channel", "webhook"}:
                return category, override
    main = cfg["main"]
    if main.get("type") == "channel" and main.get("channel_id"):
        return "main", main
    if main.get("type") == "webhook" and main.get("url"):
        return "main", main
    return None, None


def all_destinations(cfg):
    """Every distinct destination currently configured (used by the test button)."""
    found = []
    main_key, main_dest = resolve_destination({**cfg, "mode": "simple"}, "system")
    if main_dest:
        found.append((main_key, main_dest))
    if cfg["mode"] == "advanced":
        for category in LOG_CATEGORIES:
            override = cfg["advanced"].get(category)
            if override and override.get("type") in {"channel", "webhook"}:
                found.append((category, override))
    return found


def record_delivery(guild_id, key, ok, message):
    try:
        logging_config.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"delivery.{key}": {"ok": bool(ok), "message": str(message)[:300], "at": now_utc()}}},
            upsert=True,
        )
        invalidate_logging_cache(guild_id)
    except PyMongoError:
        log.exception("Could not store log delivery status.")


# ---------- rendering (Components V2) ----------
def format_details(details):
    if not details:
        return []
    if not isinstance(details, dict):
        return [("Details", str(details)[:300])]
    rows = []
    for key, value in details.items():
        if value is None or key in DETAIL_HIDDEN:
            continue
        label = DETAIL_LABELS.get(key, key.replace("_", " ").title())
        text = str(value)
        if key in {"changed_by", "recipient_id"} and text.isdigit():
            text = f"<@{text}>"
        elif key == "role_id" and text.isdigit():
            text = f"<@&{text}>"
        elif key == "enabled":
            text = "Enabled" if value else "Disabled"
        elif key == "source":
            text = SOURCE_LABELS.get(text, text)
        elif key == "multiplier":
            text = f"×{value}"
        elif key == "control":
            text = text.replace("_", " ")
        rows.append((label, text[:200]))
    return rows


def activity_summary(item):
    """Plain-text one-liner for the dashboard's activity list."""
    parts = [f"{label}: {value}" for label, value in format_details(item.get("details"))]
    return " · ".join(parts)


def build_log_spec(event, thumbnail_url=None):
    info = event_info(event["action"])
    primary = []
    user_id, username = event.get("user_id"), event.get("username")
    if user_id:
        who = f"<@{user_id}>"
        if username and not str(username).startswith("<@"):
            who += f" (`{username}`)"
        primary.append(("Member", who))
    elif username and not str(username).startswith("<@"):
        primary.append(("Member", f"`{username}`"))
    amount = event.get("amount")
    if amount is not None:
        sign = {1: "+", -1: "−"}.get(info["sign"], "")
        primary.append(("Amount", f"**{sign}{int(amount):,}** 🍬"))
    if event["action"] == "logging_test":
        secondary = [("Status", "Delivery is working ✅")]
    else:
        secondary = format_details(event.get("details"))
    created = event.get("created") or now_utc()
    return {
        "emoji": info["emoji"], "title": info["title"], "color": info["color"],
        "category_label": info["category_label"], "primary": primary, "secondary": secondary,
        "thumbnail": thumbnail_url, "timestamp": int(created.timestamp()),
    }


def _lines(rows):
    return "\n".join(f"**{k}:** {v}" for k, v in rows)


def spec_to_components(spec):
    """Raw Components V2 JSON (used for webhooks)."""
    head = f"## {spec['emoji']} {spec['title']}"
    if spec["primary"]:
        head += "\n" + _lines(spec["primary"])
    text = lambda content: {"type": 10, "content": content[:3900]}
    separator = {"type": 14, "divider": True, "spacing": 1}
    children = []
    if spec["thumbnail"]:
        children.append({"type": 9, "components": [text(head)],
                         "accessory": {"type": 11, "media": {"url": spec["thumbnail"]}}})
    else:
        children.append(text(head))
    if spec["secondary"]:
        children += [separator, text(_lines(spec["secondary"]))]
    children += [separator, text(f"-# {spec['category_label']} • <t:{spec['timestamp']}:f> • Aureolis Logs")]
    return [{"type": 17, "accent_color": spec["color"], "components": children}]


def spec_to_view(spec):
    """The same layout as a discord.py LayoutView (used for channels)."""
    head = f"## {spec['emoji']} {spec['title']}"
    if spec["primary"]:
        head += "\n" + _lines(spec["primary"])
    ui = discord.ui
    items = []
    if spec["thumbnail"]:
        items.append(ui.Section(ui.TextDisplay(head[:3900]), accessory=ui.Thumbnail(spec["thumbnail"])))
    else:
        items.append(ui.TextDisplay(head[:3900]))
    if spec["secondary"]:
        items += [ui.Separator(), ui.TextDisplay(_lines(spec["secondary"])[:3900])]
    items += [ui.Separator(), ui.TextDisplay(f"-# {spec['category_label']} • <t:{spec['timestamp']}:f> • Aureolis Logs")]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=spec["color"]))
    return view


# ---------- delivery ----------
async def send_via_webhook(url, spec):
    base, _, query = url.partition("?")
    params = {"wait": "true", "with_components": "true"}
    thread = parse_qs(query).get("thread_id", [None])[0]
    if thread:
        params["thread_id"] = thread
    payload = {
        "username": "Aureolis Logs",
        "flags": COMPONENTS_V2_FLAG,
        "components": spec_to_components(spec),
        "allowed_mentions": {"parse": []},
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session_:
            for attempt in range(2):
                async with session_.post(base, params=params, json=payload) as response:
                    body = await response.text()
                    if response.status in (200, 204):
                        return True, "Delivered via webhook"
                    if response.status == 429 and attempt == 0:
                        try:
                            wait = float(json.loads(body).get("retry_after", 1))
                        except (ValueError, TypeError):
                            wait = 1.0
                        await asyncio.sleep(min(wait, 5))
                        continue
                    return False, describe_webhook_error(response.status, body)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return False, f"Network error: {exc.__class__.__name__}"
    return False, "Webhook is rate limited, try again shortly."


async def send_via_channel(guild_id, channel_id, spec):
    bot = BOT.get("instance")
    if bot is None or bot.is_closed() or not bot.is_ready():
        return False, "Bot is not connected to Discord right now."
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except discord.HTTPException:
            return False, "Channel not found. It may have been deleted or the bot cannot see it."
    guild = getattr(channel, "guild", None)
    if guild is not None and not channel.permissions_for(guild.me).send_messages:
        return False, f"Missing Send Messages permission in #{channel.name}."
    try:
        await channel.send(view=spec_to_view(spec), allowed_mentions=discord.AllowedMentions.none())
        return True, "Delivered to channel"
    except discord.Forbidden:
        return False, f"Discord refused the message in #{getattr(channel, 'name', channel_id)} (permissions)."
    except discord.HTTPException as exc:
        return False, f"Discord error {exc.status}: {str(exc.text)[:150]}"


def resolve_avatar(guild_id, user_id):
    bot = BOT.get("instance")
    if not user_id or bot is None or bot.is_closed():
        return None
    guild = bot.get_guild(int(guild_id))
    member = guild.get_member(int(user_id)) if guild else None
    return member.display_avatar.url if member else None


async def send_to_destination(dest, event, thumbnail=True):
    avatar = resolve_avatar(event["guild_id"], event.get("user_id")) if thumbnail else None
    spec = build_log_spec(event, avatar)
    if dest["type"] == "webhook":
        return await send_via_webhook(dest.get("url", ""), spec)
    return await send_via_channel(event["guild_id"], dest.get("channel_id", 0), spec)


async def deliver_log(event):
    cfg = await asyncio.to_thread(get_logging_config, event["guild_id"])
    if not cfg["enabled"]:
        return
    key, dest = resolve_destination(cfg, log_category(event["action"]))
    if not dest:
        return
    ok, message = await send_to_destination(dest, event)
    await asyncio.to_thread(record_delivery, event["guild_id"], key, ok, message)
    if not ok:
        log.warning("Log delivery to %s failed for guild %s: %s", key, event["guild_id"], message)


async def send_test_logs(guild_id, user):
    """Sends a test log to every configured destination and reports each result."""
    cfg = await asyncio.to_thread(get_logging_config, guild_id, True)
    destinations = all_destinations(cfg)
    if not destinations:
        return [("—", False, "No destination is configured yet. Save your settings first.")]
    results = []
    for key, dest in destinations:
        event = {"guild_id": int(guild_id), "action": "logging_test", "user_id": user.get("id"),
                 "username": user.get("username"), "amount": None, "details": {"test": True}, "created": now_utc()}
        ok, message = await send_to_destination(dest, event)
        await asyncio.to_thread(record_delivery, guild_id, key, ok, message)
        results.append((key, ok, message))
    return results


LOG_QUEUE = None
LOG_LOOP = None
_LOG_WORKER_TASK = None


async def log_worker():
    while True:
        event = await LOG_QUEUE.get()
        try:
            await deliver_log(event)
        except Exception:
            log.exception("Unexpected error while delivering a log.")
        finally:
            LOG_QUEUE.task_done()
        await asyncio.sleep(0.4)  # gentle pacing keeps webhooks/channels under rate limits


def start_log_worker():
    global LOG_QUEUE, LOG_LOOP, _LOG_WORKER_TASK
    LOG_LOOP = asyncio.get_running_loop()
    LOG_QUEUE = asyncio.Queue(maxsize=500)
    _LOG_WORKER_TASK = asyncio.create_task(log_worker())


def queue_discord_log(guild_id, action, user_id, username, amount, details):
    if not guild_id or LOG_QUEUE is None or LOG_LOOP is None:
        return
    event = {"guild_id": int(guild_id), "action": action, "user_id": user_id, "username": username,
             "amount": amount, "details": details, "created": now_utc()}

    def put():
        try:
            LOG_QUEUE.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("Log queue is full; dropped a %s event.", action)

    try:
        LOG_LOOP.call_soon_threadsafe(put)
    except RuntimeError:
        log.exception("Could not queue Discord activity log.")


def log_activity(action, user_id=None, username=None, guild_id=None, amount=None, details=None):
    activity_logs.insert_one({
        "action": action, "category": log_category(action),
        "user_id": int(user_id) if user_id is not None else None,
        "username": username, "guild_id": int(guild_id) if guild_id is not None else None,
        "amount": amount, "details": details, "created_at": now_utc(),
    })
    queue_discord_log(guild_id, action, int(user_id) if user_id is not None else None, username, amount, details)



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
    question_id = request.args.get("edit_trivia", "").strip()
    edit_trivia = trivia_questions.find_one({"question_id": question_id}) if question_id else None
    trivia_listing = list(trivia_questions.find({}).sort("created_at", -1))
    try:
        total_users = users.count_documents({})
        total_candy = sum((doc.get("balance", 0) or 0) for doc in users.find({}, {"balance": 1}))
        recent = list(activity_logs.find({"action": {"$in": ["daily", "trick_or_treat", "give", "buy", "web_buy", "shop_purchase", "shop_purchase_removed", "trivia_reward"]}}).sort("created_at", -1).limit(12))
    except PyMongoError:
        total_users, total_candy, recent = 0, 0, []
    return render_template(
        "economy.html", config=config, total_users=total_users, total_candy=total_candy,
        recent=recent, trivia_questions=trivia_listing, edit_trivia=edit_trivia,
        trivia_looping=config["trivia_looping"], trivia_shuffle=config["trivia_shuffle"],
    )

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
        odds_values = request.form.getlist("trick_jackpot_odds")
        reward_values = request.form.getlist("trick_jackpot_reward")
        if len(odds_values) != len(reward_values) or len(odds_values) > MAX_TRICK_JACKPOT_ENTRIES:
            raise ValueError
        jackpots = []
        for odds_value, reward_value in zip(odds_values, reward_values):
            odds_value = odds_value.strip()
            reward_value = reward_value.strip()
            if not odds_value and not reward_value:
                continue
            odds = int(odds_value)
            reward = int(reward_value)
            if not 1 <= odds <= MAX_TRICK_JACKPOT_ODDS:
                raise ValueError
            if not 1 <= reward <= MAX_TRICK_JACKPOT_REWARD:
                raise ValueError
            jackpots.append({"odds": odds, "reward": reward})
    except (TypeError, ValueError):
        return redirect(url_for("economy_page", error="invalid"))
    set_economy_config(daily_reward, minimum, maximum, jackpots)
    log_activity(
        "economy_settings",
        user_id=user["id"],
        username=user.get("username"),
        details={
            "daily_reward": daily_reward,
            "trick_or_treat_min": minimum,
            "trick_or_treat_max": maximum,
            "trick_or_treat_jackpot_count": len(jackpots),
        },
    )
    return redirect(url_for("economy_page", saved="1"))


@app.post("/economy/trivia/save")
def economy_trivia_save():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    question_id = request.form.get("question_id", "").strip()
    try:
        question = request.form.get("question", "").strip()
        question_type = request.form.get("question_type", "").strip()
        reward = int(request.form.get("reward", "0"))
        options = [line.strip() for line in request.form.get("options", "").splitlines() if line.strip()]
        accepted_answers = list(dict.fromkeys(
            line.strip() for line in request.form.get("accepted_answers", "").splitlines() if line.strip()
        ))
        if not question or len(question) > 1000 or reward < 1 or reward > 1000000:
            raise ValueError
        if question_type == "multiple_choice":
            if len(options) < 2 or len(options) > 5 or any(len(option) > 100 for option in options):
                raise ValueError
            correct_answer = normalize_trivia_answer(request.form.get("correct_answer", ""))
            correct_index = next(
                (index for index, option in enumerate(options)
                 if normalize_trivia_answer(option) == correct_answer),
                None,
            )
            if correct_index is None or len(set(normalize_trivia_answer(option) for option in options)) != len(options):
                raise ValueError
            accepted_answers = []
        elif question_type in {"short", "long"}:
            if not accepted_answers or len(accepted_answers) > 20 or any(len(answer) > 200 for answer in accepted_answers):
                raise ValueError
            options = []
            correct_index = None
        else:
            raise ValueError
        question_data = {
            "question": question,
            "question_type": question_type,
            "options": options,
            "correct_index": correct_index,
            "accepted_answers": accepted_answers,
            "reward": reward,
            "updated_at": now_utc(),
            "updated_by": int(user["id"]),
        }
        if question_id:
            result = trivia_questions.update_one(
                {"question_id": question_id},
                {"$set": question_data},
            )
            if not result.matched_count:
                return redirect(url_for("economy_page", error="trivia_not_found"))
        else:
            question_id = secrets.token_urlsafe(10)
            trivia_questions.insert_one({
                "question_id": question_id,
                "enabled": True,
                "created_at": now_utc(),
                **question_data,
            })
    except (KeyError, TypeError, ValueError):
        return redirect(url_for("economy_page", error="trivia_invalid"))
    log_activity(
        "trivia_question_saved",
        user_id=user["id"],
        username=user.get("username"),
        details={"question_id": question_id, "question_type": question_type, "reward": reward},
    )
    return redirect(url_for("economy_page", trivia_saved="1"))


@app.post("/economy/trivia/toggle")
def economy_trivia_toggle():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    question_id = request.form.get("question_id", "").strip()
    enabled_value = request.form.get("enabled")
    if not question_id or enabled_value not in {"0", "1"}:
        return redirect(url_for("economy_page", error="trivia_invalid"))
    result = trivia_questions.update_one(
        {"question_id": question_id},
        {"$set": {"enabled": enabled_value == "1", "updated_at": now_utc(), "updated_by": int(user["id"])}},
    )
    if not result.matched_count:
        return redirect(url_for("economy_page", error="trivia_not_found"))
    return redirect(url_for("economy_page", trivia_saved="1"))


@app.post("/economy/trivia/loop")
def economy_trivia_loop():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    enabled_value = request.form.get("enabled")
    if enabled_value not in {"0", "1"}:
        return redirect(url_for("economy_page", error="trivia_invalid"))
    enabled = enabled_value == "1"
    economy_config.update_one(
        {"_id": "global"},
        {"$set": {"trivia_looping": enabled, "updated_at": now_utc()}},
        upsert=True,
    )
    log_activity(
        "trivia_looping",
        user_id=user["id"],
        username=user.get("username"),
        details={"enabled": enabled},
    )
    return redirect(url_for("economy_page", trivia_saved="1"))
@app.post("/economy/trivia/shuffle")
def economy_trivia_shuffle():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    enabled_value = request.form.get("enabled")
    if enabled_value not in {"0", "1"}:
        return redirect(url_for("economy_page", error="trivia_invalid"))
    enabled = enabled_value == "1"
    economy_config.update_one(
        {"_id": "global"},
        {"$set": {"trivia_shuffle": enabled, "updated_at": now_utc()}},
        upsert=True,
    )
    log_activity(
        "trivia_shuffle",
        user_id=user["id"],
        username=user.get("username"),
        details={"enabled": enabled},
    )
    return redirect(url_for("economy_page", trivia_saved="1"))


@app.post("/economy/trivia/remove")
def economy_trivia_remove():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    question_id = request.form.get("question_id", "").strip()
    if not question_id:
        return redirect(url_for("economy_page", error="trivia_invalid"))
    result = trivia_questions.delete_one({"question_id": question_id})
    if not result.deleted_count:
        return redirect(url_for("economy_page", error="trivia_not_found"))
    users.update_many({}, {"$pull": {"trivia_claims": question_id, "trivia_attempts": question_id}})
    log_activity(
        "trivia_question_removed",
        user_id=user["id"],
        username=user.get("username"),
        details={"question_id": question_id},
    )
    return redirect(url_for("economy_page", trivia_saved="1"))


def _logging_flash(kind, text):
    session["log_flash"] = {"kind": kind, "text": text}


class LogConfigError(ValueError):
    pass


def parse_destination(form, prefix, guild, previous, allow_inherit):
    default = "inherit" if allow_inherit else "channel"
    typ = form.get(f"{prefix}_type", default)
    if allow_inherit and typ == "inherit":
        return None
    if allow_inherit and typ == "off":
        return {"type": "off"}
    if typ == "channel":
        cid = form.get(f"{prefix}_channel_id", "").strip()
        channel = guild.get_channel(int(cid)) if cid.isdigit() else None
        if channel is None or not hasattr(channel, "send") or isinstance(channel, discord.abc.PrivateChannel):
            raise LogConfigError("Pick a text channel the bot can see.")
        if not channel.permissions_for(guild.me).send_messages:
            raise LogConfigError(f"The bot cannot send messages in #{channel.name}. Give it Send Messages there first.")
        return {"type": "channel", "channel_id": cid}
    if typ == "webhook":
        raw = form.get(f"{prefix}_webhook_url", "").strip()
        if raw:
            url = normalize_webhook_url(raw)
            if not url:
                raise LogConfigError("That is not a valid Discord webhook URL. It should look like https://discord.com/api/webhooks/ID/TOKEN.")
            name = verify_webhook(url)
            return {"type": "webhook", "url": url, "name": name}
        if previous and previous.get("type") == "webhook" and previous.get("url"):
            return previous
        raise LogConfigError("Paste a webhook URL for the webhook destination.")
    raise LogConfigError("Unknown destination type.")


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

    category = request.args.get("cat", "")
    query = {"guild_id": int(guild_id)} if guild_id else None
    logs = []
    if query is not None:
        if category in LOG_CATEGORIES:
            query["action"] = {"$in": actions_in_category(category) or ["__none__"]}
            if category == "system":
                query["action"] = {"$nin": [a for a, v in LOG_EVENTS.items() if v[0] != "system"]}
            if category == "errors":
                query["action"] = {"$regex": "^error"}
        logs = list(activity_logs.find(query).sort("created_at", -1).limit(100))

    config = get_logging_config(guild_id, fresh=True) if guild_id else {
        "enabled": True, "mode": "simple", "main": {"type": "channel", "channel_id": ""}, "advanced": {}, "delivery": {}}
    channels = []
    if bot and not bot.is_closed() and guild_id:
        guild = bot.get_guild(int(guild_id))
        if guild:
            channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]

    # Webhook URLs are secrets: never send them to the browser, only a masked label.
    masked = {}
    for key, dest in [("main", config["main"])] + list(config["advanced"].items()):
        if dest and dest.get("type") == "webhook":
            masked[key] = f"{dest.get('name') or 'Webhook'} · {mask_webhook(dest.get('url', ''))}"

    flash = session.pop("log_flash", None)
    return render_template(
        "logging.html", logs=logs, logging_config=config, logging_guilds=guilds,
        logging_guild_id=str(guild_id), logging_channels=channels, log_categories=LOG_CATEGORIES,
        category_meta=LOG_CATEGORY_META, active_category=category, masked_webhooks=masked,
        flash=flash, event_info=event_info, activity_summary=activity_summary,
    )


@app.post("/logging/settings")
def logging_settings():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id_raw = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id_raw)
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if guild is None:
            raise LogConfigError("The bot is not in that server right now.")
        mode = request.form.get("mode", "simple")
        if mode not in {"simple", "advanced"}:
            raise LogConfigError("Unknown logging mode.")
        previous = get_logging_config(guild_id, fresh=True)
        main = parse_destination(request.form, "main", guild, previous.get("main"), allow_inherit=False)
        advanced = {}
        for category in LOG_CATEGORIES:
            dest = parse_destination(request.form, category, guild, previous.get("advanced", {}).get(category), allow_inherit=True)
            if dest:
                advanced[category] = dest
        logging_config.update_one(
            {"_id": str(guild_id)},
            {"$set": {"guild_id": guild_id, "enabled": request.form.get("enabled") == "on", "mode": mode,
                      "main": main, "advanced": advanced, "updated_at": now_utc()}},
            upsert=True,
        )
        invalidate_logging_cache(guild_id)
        _logging_flash("ok", "Logging settings saved. Use “Send test log” to confirm delivery.")
    except LogConfigError as exc:
        _logging_flash("err", str(exc))
    except ValueError as exc:
        _logging_flash("err", str(exc) or "Those values are not valid.")
    except PyMongoError:
        log.exception("Could not save logging settings")
        _logging_flash("err", "The database is unavailable, settings were not saved.")
    return redirect(url_for("logging_page", guild=guild_id_raw))


@app.post("/logging/test")
def logging_test():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id = request.form.get("guild_id", "")
    try:
        bot = BOT.get("instance")
        if not bot or bot.is_closed():
            raise RuntimeError("The bot is not connected to Discord right now.")
        future = asyncio.run_coroutine_threadsafe(send_test_logs(int(guild_id), user), bot.loop)
        results = future.result(timeout=30)
        labels = {"main": "Main"}
        summary = " • ".join(
            f"{'✓' if ok else '✗'} {labels.get(key, LOG_CATEGORY_META.get(key, {}).get('label', key))}: {message}"
            for key, ok, message in results
        )
        _logging_flash("ok" if all(ok for _, ok, _ in results) else "err", summary)
    except Exception as exc:
        log.exception("Log test failed")
        _logging_flash("err", f"Test failed: {exc}" if isinstance(exc, RuntimeError) else "Test failed. Check the Railway logs.")
    return redirect(url_for("logging_page", guild=guild_id))


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
            clan_profile=None,
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
    badges, name_gradient, badge_options, user_badge_override = [], None, [], False
    if guild:
        guild_id = int(guild["id"])
        profile = users.find_one({"guild_id": guild_id, "user_id": int(user["id"])})
        clan_profile = clan_member_profile(guild_id, profile)
        badge_configs = get_badge_configs(guild_id)
        needs_role_member = any(config["target_type"] == "role" for config in badge_configs)
        member = get_bot_member(guild_id, user["id"]) if needs_role_member else None
        badge_preference = get_member_badge_preference(guild_id, user["id"])
        badges, name_gradient, badge_options, user_badge_override = badge_presentation_for_member(
            user["id"], member, badge_configs, badge_preference
        )
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
        clan_profile=clan_profile if guild else None,
        rank=rank,
        account_created=account_created,
        next_daily=next_daily,
        inventory_value=inventory_value,
        badges=badges,
        name_gradient=name_gradient,
        badge_options=badge_options,
        badge_preference=badge_preference,
        user_badge_override=user_badge_override,
    )


@app.post("/profile/badge")
def profile_badge_save():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    guild_id_raw = request.form.get("guild_id", "")
    selection = request.form.get("badge_selection", "")
    try:
        guild_id = int(guild_id_raw)
        if str(guild_id) not in {
            str(guild.get("id")) for guild in session.get("discord_guilds", [])
        }:
            raise ValueError
        configs = get_badge_configs(guild_id)
        if any(config["target_type"] == "user" and config["target_id"] == str(user["id"])
               for config in configs):
            raise ValueError
        member = get_bot_member(guild_id, user["id"])
        _, _, options, _ = badge_presentation_for_member(user["id"], member, configs)
        valid_selections = {
            f"{option['target_type']}:{option['target_id']}" for option in options
        }
        if selection != "none" and selection not in valid_selections:
            raise ValueError
        member_badge_preferences.update_one(
            {"guild_id": guild_id, "user_id": int(user["id"])},
            {"$set": {"target": selection, "updated_at": now_utc()}},
            upsert=True,
        )
    except (TypeError, ValueError):
        return redirect(url_for("profile_page", error="badge"))
    return redirect(url_for("profile_page", saved="badge"))


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
            current_clan_id = record.get("clan_id")
            record["clan"] = clans.find_one({
                "guild_id": int(record.get("guild_id", 0)),
                "clan_id": current_clan_id,
                "status": "active",
            }) if current_clan_id else None
            record["available_clans"] = list(clans.find(
                {"guild_id": int(record.get("guild_id", 0)), "status": "active"},
                {"clan_id": 1, "name": 1},
            ).sort("name", 1))
            record["purchases"] = []
            for purchase in record.get("inventory", []) or []:
                if not isinstance(purchase, dict):
                    continue
                purchase = dict(purchase)
                purchased_at = purchase.get("purchased_at")
                purchase["purchase_timestamp_ms"] = (
                    int(as_utc(purchased_at).timestamp() * 1000)
                    if isinstance(purchased_at, datetime) else None
                )
                record["purchases"].append(purchase)

    return render_template(
        "users.html",
        user=user,
        avatar_url=discord_avatar_url(user),
        search=search,
        users=global_users,
    )


def notify_candy_change_from_web(guild_id, member_id, kind, amount, new_balance, admin):
    """DM the member after a dashboard Candy change. Best effort: it never breaks the save."""
    bot = BOT.get("instance")
    if bot is None or bot.is_closed():
        return
    guild = bot.get_guild(int(guild_id))

    async def send():
        target = bot.get_user(int(member_id)) or await bot.fetch_user(int(member_id))
        return await dm_candy_change(
            target, kind, guild.name if guild else "the server", amount, new_balance, admin["id"],
            admin.get("global_name") or admin.get("username") or "An admin", discord_avatar_url(admin))

    try:
        if _run_on_bot_loop(bot, send(), 10) is False:
            log.info("Could not DM member %s about a dashboard Candy change (DMs closed or blocked).", member_id)
    except Exception:
        log.warning("Dashboard Candy change DM failed for member %s.", member_id, exc_info=True)


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
        notify_candy_change_from_web(guild_id, member_id, action, amount, result["balance"], user)
        return redirect(url_for("users_page", q=request.form.get("q", ""), saved=action))
    except (TypeError, ValueError):
        return redirect(url_for("users_page", q=request.form.get("q", ""), error="invalid"))


@app.post("/users/clan")
def users_clan():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    search = request.form.get("q", "")
    try:
        guild_id = int(request.form.get("guild_id", ""))
        member_id = int(request.form.get("member_id", ""))
        clan_id = request.form.get("clan_id", "").strip()
        member_record = users.find_one({"guild_id": guild_id, "user_id": member_id}) or {}
        previous_clan_id = member_record.get("clan_id")
        if clan_id and clan_id == previous_clan_id:
            saved = "clan_assigned"
        else:
            if clan_id:
                bot = BOT.get("instance")
                guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
                clan = clans.find_one({
                    "guild_id": guild_id,
                    "clan_id": clan_id,
                    "status": "active",
                })
                if guild is None or clan is None or get_bot_member(guild_id, member_id) is None:
                    raise ValueError
            if previous_clan_id:
                clan_member_departure(guild_id, member_id, previous_clan_id)
                clan_member_clear(guild_id, member_id)
            if clan_id:
                if clan_member_assign(guild_id, member_id, clan) is None:
                    raise ValueError
                clan_withdraw_pending_membership_requests(guild_id, member_id)
                saved = "clan_assigned"
            else:
                clan_member_clear(guild_id, member_id)
                saved = "clan_removed"
        log_activity(
            "clan_member_admin",
            user_id=member_id,
            guild_id=guild_id,
            details={
                "changed_by": user["id"],
                "previous_clan_id": previous_clan_id,
                "clan_id": clan_id or None,
            },
        )
    except (TypeError, ValueError):
        return redirect(url_for("users_page", q=search, error="clan"))
    return redirect(url_for("users_page", q=search, saved=saved))


@app.post("/users/purchase/remove")
def users_purchase_remove():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    search = request.form.get("q", "")
    try:
        guild_id = int(request.form.get("guild_id", ""))
        member_id = int(request.form.get("member_id", ""))
        item_id = request.form.get("item_id", "").strip()
        purchase_id = request.form.get("purchase_id", "").strip()
        refund = request.form.get("refund", "")
        if not item_id or refund not in {"0", "1"}:
            raise ValueError
        if purchase_id:
            purchase_match = {"item_id": item_id, "purchase_id": purchase_id}
            purchased_at_ms = None
        else:
            purchased_at_ms = int(request.form.get("purchased_at_ms", ""))
            purchase_match = {
                "item_id": item_id,
                "purchased_at": datetime.fromtimestamp(purchased_at_ms / 1000, timezone.utc),
            }
    except (TypeError, ValueError, OverflowError):
        return redirect(url_for("users_page", q=search, error="invalid"))

    member_record = users.find_one({"guild_id": guild_id, "user_id": member_id})
    if purchase_id:
        purchase = next(
            (entry for entry in (member_record or {}).get("inventory", [])
             if isinstance(entry, dict) and all(entry.get(key) == value for key, value in purchase_match.items())),
            None,
        )
    else:
        purchase = next(
            (entry for entry in (member_record or {}).get("inventory", [])
             if isinstance(entry, dict)
             and entry.get("item_id") == item_id
             and isinstance(entry.get("purchased_at"), datetime)
             and int(as_utc(entry["purchased_at"]).timestamp() * 1000) == purchased_at_ms),
            None,
        )
    if purchase is None:
        return redirect(url_for("users_page", q=search, error="purchase_missing"))

    price = int(purchase.get("price", 0) or 0)
    if price < 0:
        return redirect(url_for("users_page", q=search, error="invalid"))

    role_id = purchase.get("role_id")
    if purchase.get("type") == "role" and not role_id:
        current_shop_item = shop_items.find_one({"guild_id": guild_id, "item_id": item_id})
        role_id = (current_shop_item or {}).get("reward", {}).get("role_id")
    if role_id:
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if guild is None:
            return redirect(url_for("users_page", q=search, error="purchase_role_offline"))
        role = guild.get_role(int(role_id))
        if role is not None:
            try:
                member = _run_on_bot_loop(bot, guild.fetch_member(member_id), 10)
            except discord.NotFound:
                member = None
            except Exception:
                log.exception(
                    "Could not fetch member %s in guild %s before removing shop purchase %s.",
                    member_id, guild_id, purchase.get("purchase_id", item_id),
                )
                return redirect(url_for("users_page", q=search, error="purchase_role_remove"))
            if member is not None and role in member.roles:
                try:
                    _run_on_bot_loop(
                        bot,
                        member.remove_roles(role, atomic=False, reason="Shop purchase removed by an admin"),
                        15,
                    )
                    verified = _run_on_bot_loop(bot, guild.fetch_member(member_id), 10)
                    if role in verified.roles:
                        raise RuntimeError("Discord still reports the purchased role after removal.")
                except discord.NotFound:
                    pass
                except Exception:
                    log.exception(
                        "Could not revoke shop role %s from member %s in guild %s while removing purchase %s.",
                        role_id, member_id, guild_id, purchase.get("purchase_id", item_id),
                    )
                    return redirect(url_for("users_page", q=search, error="purchase_role_remove"))

    update = {"$pull": {"inventory": purchase_match}}
    if refund == "1":
        update["$inc"] = {"balance": price}
    updated = users.find_one_and_update(
        {"guild_id": guild_id, "user_id": member_id, "inventory": {"$elemMatch": purchase_match}},
        update,
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        return redirect(url_for("users_page", q=search, error="purchase_missing"))

    log_activity(
        "shop_purchase_removed",
        user_id=member_id,
        username=member_record.get("username"),
        guild_id=guild_id,
        amount=price if refund == "1" else 0,
        details={
            "item_id": item_id,
            "item": purchase.get("name", "Shop Item"),
            "purchase_id": purchase.get("purchase_id"),
            "refunded": refund == "1",
            "removed_by": int(user["id"]),
        },
    )
    if refund == "1" and price:
        notify_candy_change_from_web(guild_id, member_id, "add", price, updated["balance"], user)
    return redirect(url_for("users_page", q=search, saved="purchase_refunded" if refund == "1" else "purchase_removed"))


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
    event_announcement_config = (
        event_announcement_settings.find_one({"guild_id": int(selected_boost_guild_id)}) or {}
        if selected_boost_guild_id.isdigit() else {}
    )
    saved_daily_boosts = get_daily_boosts(int(selected_boost_guild_id)) if selected_boost_guild_id.isdigit() else []
    give_tax_percent = (
        get_give_tax_percent(int(selected_boost_guild_id))
        if selected_boost_guild_id.isdigit() else 0
    )
    limit_cards = {}
    for limit_kind in LIMIT_KINDS:
        config = get_limit_config(limit_kind, int(selected_boost_guild_id)) if selected_boost_guild_id.isdigit() else {"enabled": False, "rules": []}
        limit_cards[limit_kind] = {
            "enabled": config["enabled"],
            "rules": config["rules"],
            # Discord IDs are bigger than JavaScript can hold exactly, so the page gets them as strings.
            "rules_js": [{**rule, "role_ids": [str(r) for r in rule["role_ids"]]} for rule in config["rules"]],
        }
    cooldown_config = get_trick_cooldowns(int(selected_boost_guild_id)) if selected_boost_guild_id.isdigit() else {"enabled": False, "rules": []}
    limit_cards["trickortreat"] = {
        "enabled": cooldown_config["enabled"],
        "rules": cooldown_config["rules"],
        "rules_js": [{**rule, "role_ids": [str(r) for r in rule["role_ids"]]} for rule in cooldown_config["rules"]],
    }
    badge_configs = get_badge_configs(int(selected_boost_guild_id)) if selected_boost_guild_id.isdigit() else []
    clan_verification_clans = []
    if selected_boost_guild_id.isdigit():
        for clan in clans.find(
            {"guild_id": int(selected_boost_guild_id), "status": "active"},
            {"clan_id": 1, "name": 1, "verification_badge": 1},
        ).sort("name", 1):
            clan_verification_clans.append({
                "clan_id": clan["clan_id"],
                "name": clan["name"],
                **clan_verification_presentation(clan.get("verification_badge")),
            })
    clan_settings = get_clan_config(int(selected_boost_guild_id)) if selected_boost_guild_id.isdigit() else {
        "discount_threshold": CLAN_DEFAULT_DISCOUNT_THRESHOLD,
        "daily_threshold": CLAN_DEFAULT_DAILY_THRESHOLD,
    }
    return render_template(
        "access_control.html",
        user=user,
        avatar_url=discord_avatar_url(user),
        csrf_token=_csrf_token(),
        survey_banner_configured=bool(site_assets.find_one({"_id": "survey_banner"}, {"_id": 1})),
        event_announcement_channel_id=str(event_announcement_config.get("channel_id", "")),
        allowed=True,
        bot_guilds=bot_guilds,
        slash_commands=slash_commands,
        member_controls=list(member_controls.find({}).sort("updated_at", -1).limit(100)),
        boost_guild_id=selected_boost_guild_id,
        boost_roles=boost_roles,
        badge_configs=badge_configs,
        badge_types=[(key, value[0]) for key, value in BADGE_TYPES.items()],
        clan_verification_clans=clan_verification_clans,
        clan_verification_types=[
            (key, BADGE_TYPES[key][0]) for key in CLAN_VERIFICATION_TYPES
        ],
        clan_settings=clan_settings,
        give_tax_percent=give_tax_percent,
        daily_boosts=saved_daily_boosts,
        limit_cards=limit_cards,
        give_limit_periods=[(key, label) for key, (label, _) in GIVE_LIMIT_PERIODS.items()],
        give_limit_max_rules=GIVE_LIMIT_MAX_RULES,
        cooldown_units=[(key, label) for key, (label, _) in TRICK_COOLDOWN_UNITS.items()],
    )


@app.post("/access-control/events/save")
def access_control_events_save():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403

    sent, expected = request.form.get("csrf_token", ""), session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        return redirect(url_for("access_control_page", error="event_csrf"))

    guild_id_raw = request.form.get("guild_id", "").strip()
    channel_id_raw = request.form.get("channel_id", "").strip()
    try:
        guild_id = int(guild_id_raw)
        channel_id = int(channel_id_raw)
    except (TypeError, ValueError):
        return redirect(url_for("access_control_page", boost_guild=guild_id_raw, error="event_channel"))

    bot = BOT.get("instance")
    guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
    channel = bot.get_channel(channel_id) if bot and not bot.is_closed() else None
    if guild is None or channel is None or getattr(channel, "guild", None) is None or channel.guild.id != guild_id:
        return redirect(url_for("access_control_page", boost_guild=guild_id, error="event_channel"))

    if not isinstance(channel, (discord.TextChannel)):
        return redirect(url_for("access_control_page", boost_guild=guild_id, error="event_channel"))
    bot_member = guild.me
    if bot_member is None or not channel.permissions_for(bot_member).send_messages:
        return redirect(url_for("access_control_page", boost_guild=guild_id, error="event_permissions"))
    if not channel.permissions_for(bot_member).embed_links:
        return redirect(url_for("access_control_page", boost_guild=guild_id, error="event_permissions"))

    event_announcement_settings.update_one(
        {"guild_id": guild_id},
        {"$set": {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "updated_at": now_utc(),
            "updated_by": int(user["id"]),
        }},
        upsert=True,
    )
    return redirect(url_for("access_control_page", boost_guild=guild_id, saved="event_channel"))


@app.post("/access-control/badges/save")
def access_control_badge_save():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id)
        target_type = request.form.get("target_type", "")
        target_id = request.form.get("target_id", "").strip()
        badge_type = request.form.get("badge_type", "")
        gradient_start = request.form.get("gradient_start", "")
        gradient_end = request.form.get("gradient_end", "")
        gradient_enabled = request.form.get("gradient_enabled") == "on"
        edit_target_type = request.form.get("edit_target_type", "").strip()
        edit_target_id = request.form.get("edit_target_id", "").strip()
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        valid_target_id = (
            re.fullmatch(r"[0-9]+", target_id) if target_type == "role"
            else re.fullmatch(r"[0-9]{17,20}", target_id) if target_type == "user"
            else None
        )
        if guild is None or valid_target_id is None or badge_type not in BADGE_TYPES:
            raise ValueError
        if bool(edit_target_type) != bool(edit_target_id):
            raise ValueError
        if edit_target_type:
            edit_id_pattern = r"[0-9]+" if edit_target_type == "role" else r"[0-9]{17,20}" if edit_target_type == "user" else None
            if edit_id_pattern is None or not re.fullmatch(edit_id_pattern, edit_target_id):
                raise ValueError
        target_name = f"User {target_id}"
        if target_type == "role":
            role = guild.get_role(int(target_id))
            if role is None or role.is_default():
                raise ValueError
            target_name = role.name
        elif target_id == "0":
            raise ValueError
        if gradient_enabled and (
                not HEX_COLOR_RE.fullmatch(gradient_start)
                or not HEX_COLOR_RE.fullmatch(gradient_end)):
            raise ValueError
        if not gradient_enabled:
            gradient_start = valid_gradient_color(gradient_start, "#8b5cf6")
            gradient_end = valid_gradient_color(gradient_end, "#f97316")
    except (TypeError, ValueError):
        return redirect(url_for("access_control_page", boost_guild=guild_id, error="badge"))

    badge_configs = [{
        "target_type": item["target_type"],
        "target_id": item["target_id"],
        "target_name": item["target_name"],
        "badge_type": item["badge_type"],
        "gradient_start": item["gradient_start"],
        "gradient_end": item["gradient_end"],
        "gradient_enabled": item["gradient_enabled"],
    } for item in get_badge_configs(guild_id)
      if (item["target_type"], item["target_id"]) not in {
          (target_type, target_id),
          (edit_target_type, edit_target_id),
      }]
    badge_configs.append({
        "target_type": target_type,
        "target_id": target_id,
        "target_name": target_name,
        "badge_type": badge_type,
        "gradient_start": gradient_start.lower(),
        "gradient_end": gradient_end.lower(),
        "gradient_enabled": gradient_enabled,
    })
    presentation_settings.update_one(
        {"guild_id": guild_id},
        {"$set": {
            "badges": badge_configs,
            "updated_at": now_utc(),
            "updated_by": int(user["id"]),
        }},
        upsert=True,
    )
    return redirect(url_for("access_control_page", boost_guild=guild_id, saved="badge"))


@app.post("/access-control/badges/remove")
def access_control_badge_remove():
    user = session.get("discord_user")
    if not user or not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id)
        target_type = request.form.get("target_type", "role")
        target_id = request.form.get("target_id", request.form.get("role_id", ""))
        valid_target_id = (
            re.fullmatch(r"[0-9]+", target_id) if target_type == "role"
            else re.fullmatch(r"[0-9]{17,20}", target_id) if target_type == "user"
            else None
        )
        if valid_target_id is None:
            raise ValueError
    except (TypeError, ValueError):
        return redirect(url_for("access_control_page", error="badge"))
    configs = [{
        "target_type": item["target_type"],
        "target_id": item["target_id"],
        "target_name": item["target_name"],
        "badge_type": item["badge_type"],
        "gradient_start": item["gradient_start"],
        "gradient_end": item["gradient_end"],
        "gradient_enabled": item["gradient_enabled"],
    } for item in get_badge_configs(guild_id)
      if (item["target_type"], item["target_id"]) != (target_type, target_id)]
    presentation_settings.update_one(
        {"guild_id": guild_id},
        {"$set": {"badges": configs, "updated_at": now_utc(), "updated_by": int(user["id"])}},
    )
    return redirect(url_for("access_control_page", boost_guild=guild_id, saved="badge_removed"))


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


@app.post("/access-control/clan-settings")
def access_control_clan_settings():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id_raw = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id_raw)
        bot = BOT.get("instance")
        if not bot or bot.is_closed() or bot.get_guild(guild_id) is None:
            raise ValueError
        discount_threshold = int(request.form.get("discount_threshold", ""))
        daily_threshold = int(request.form.get("daily_threshold", ""))
        if not 0 <= discount_threshold <= 1_000_000_000:
            raise ValueError
        if not 0 <= daily_threshold <= 1_000_000_000:
            raise ValueError
        set_clan_config(guild_id, discount_threshold, daily_threshold, user["id"])
        log_activity(
            "clan_perks_updated",
            user_id=user["id"],
            username=user.get("username"),
            guild_id=guild_id,
            details={
                "discount_threshold": discount_threshold,
                "daily_threshold": daily_threshold,
            },
        )
    except (TypeError, ValueError):
        return redirect(url_for(
            "access_control_page", boost_guild=guild_id_raw, clan_error="settings",
        ))
    return redirect(url_for(
        "access_control_page", boost_guild=guild_id, clan_saved="1",
    ))


@app.post("/access-control/clan-verification/save")
def access_control_clan_verification_save():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id_raw = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id_raw)
        clan_id = request.form.get("clan_id", "").strip()
        badge_type = request.form.get("badge_type", "")
        bot = BOT.get("instance")
        if (
            not bot
            or bot.is_closed()
            or bot.get_guild(guild_id) is None
            or not clan_id
            or badge_type not in CLAN_VERIFICATION_TYPES
        ):
            raise ValueError
        clan = clans.find_one_and_update(
            {
                "guild_id": guild_id,
                "clan_id": clan_id,
                "status": "active",
            },
            {"$set": {
                "verification_badge": badge_type,
                "verification_updated_at": now_utc(),
                "verification_updated_by": int(user["id"]),
            }},
            return_document=ReturnDocument.AFTER,
        )
        if clan is None:
            raise ValueError
        log_activity(
            "clan_verification_updated",
            user_id=user["id"],
            username=user.get("username"),
            guild_id=guild_id,
            details={"clan_id": clan_id, "badge_type": badge_type},
        )
    except (TypeError, ValueError):
        return redirect(url_for(
            "access_control_page",
            boost_guild=guild_id_raw,
            clan_verification_error="invalid",
        ))
    return redirect(url_for(
        "access_control_page",
        boost_guild=guild_id,
        clan_verification_saved="1",
    ))


@app.post("/access-control/clan-verification/remove")
def access_control_clan_verification_remove():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id_raw = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id_raw)
        clan_id = request.form.get("clan_id", "").strip()
        bot = BOT.get("instance")
        if (
            not bot
            or bot.is_closed()
            or bot.get_guild(guild_id) is None
            or not clan_id
        ):
            raise ValueError
        result = clans.update_one(
            {
                "guild_id": guild_id,
                "clan_id": clan_id,
                "status": "active",
                "verification_badge": {"$in": list(CLAN_VERIFICATION_TYPES)},
            },
            {"$unset": {
                "verification_badge": "",
                "verification_updated_at": "",
                "verification_updated_by": "",
            }},
        )
        if not result.modified_count:
            raise ValueError
        log_activity(
            "clan_verification_removed",
            user_id=user["id"],
            username=user.get("username"),
            guild_id=guild_id,
            details={"clan_id": clan_id},
        )
    except (TypeError, ValueError):
        return redirect(url_for(
            "access_control_page",
            boost_guild=guild_id_raw,
            clan_verification_error="invalid",
        ))
    return redirect(url_for(
        "access_control_page",
        boost_guild=guild_id,
        clan_verification_saved="1",
    ))


@app.post("/access-control/give-tax")
def access_control_give_tax():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id_raw = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id_raw)
        bot = BOT.get("instance")
        if not bot or bot.is_closed() or bot.get_guild(guild_id) is None:
            raise ValueError
        percent = int(request.form.get("percent", ""))
        set_give_tax_percent(guild_id, percent, user["id"])
        log_activity(
            "give_tax_updated",
            user_id=user["id"],
            username=user.get("username"),
            guild_id=guild_id,
            details={"percent": percent},
        )
    except (TypeError, ValueError):
        return redirect(url_for(
            "access_control_page",
            boost_guild=guild_id_raw,
            give_tax_error="invalid",
        ))
    return redirect(url_for(
        "access_control_page",
        boost_guild=guild_id,
        give_tax_saved="1",
    ))


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


def _limits_redirect(kind, guild_id, **params):
    return redirect(url_for("access_control_page", boost_guild=guild_id, **params) + f"#limits-{kind}")


@app.post("/access-control/limits/<kind>/toggle")
def access_control_limits_toggle(kind):
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    if kind not in LIMIT_KINDS:
        return "Not found", 404
    guild_id = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id)
        enabled = request.form.get("enabled") == "1"
        bot = BOT.get("instance")
        if not (bot and not bot.is_closed() and bot.get_guild(guild_id)):
            raise ValueError
        set_limit_enabled(kind, guild_id, enabled, user["id"])
        log_activity(LIMIT_KINDS[kind]["log"], user_id=user["id"], username=user.get("username"), guild_id=guild_id,
                     details={"enabled": enabled, "changed_by": user["id"]})
    except (TypeError, ValueError):
        return _limits_redirect(kind, guild_id, error=f"limit_{kind}")
    return _limits_redirect(kind, guild_id, saved=f"limits_{kind}")


@app.post("/access-control/limits/<kind>/save")
def access_control_limits_save(kind):
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    if kind not in LIMIT_KINDS:
        return "Not found", 404
    guild_id = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id)
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if guild is None:
            raise ValueError
        rules = []
        for index in request.form.getlist("rule"):
            if not index.isdigit():
                raise ValueError
            rules.append({
                "amount": request.form.get(f"amount_{index}", ""),
                "period": request.form.get(f"period_{index}", ""),
                "scope": request.form.get(f"scope_{index}", ""),
                "role_ids": request.form.getlist(f"roles_{index}"),
            })
        valid_roles = [role.id for role in guild.roles if not role.is_default()]
        cleaned = set_limit_rules(kind, guild_id, rules, valid_roles, user["id"])
        log_activity(LIMIT_KINDS[kind]["log"], user_id=user["id"], username=user.get("username"), guild_id=guild_id,
                     details={"rule_count": len(cleaned), "changed_by": user["id"]})
    except (TypeError, ValueError):
        return _limits_redirect(kind, guild_id, error=f"limit_{kind}")
    return _limits_redirect(kind, guild_id, saved=f"limits_{kind}")


@app.post("/access-control/cooldown/toggle")
def access_control_cooldown_toggle():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id)
        enabled = request.form.get("enabled") == "1"
        bot = BOT.get("instance")
        if not (bot and not bot.is_closed() and bot.get_guild(guild_id)):
            raise ValueError
        set_trick_cooldowns_enabled(guild_id, enabled, user["id"])
        log_activity("trick_cooldown", user_id=user["id"], username=user.get("username"), guild_id=guild_id,
                     details={"enabled": enabled, "changed_by": user["id"]})
    except (TypeError, ValueError):
        return _limits_redirect("trickortreat", guild_id, error="limit_trickortreat")
    return _limits_redirect("trickortreat", guild_id, saved="limits_trickortreat")


@app.post("/access-control/cooldown/save")
def access_control_cooldown_save():
    user = session.get("discord_user")
    if not user:
        return redirect(url_for("login"))
    if not is_admin(user["id"]):
        return "Forbidden", 403
    guild_id = request.form.get("guild_id", "")
    try:
        guild_id = int(guild_id)
        bot = BOT.get("instance")
        guild = bot.get_guild(guild_id) if bot and not bot.is_closed() else None
        if guild is None:
            raise ValueError
        rules = []
        for index in request.form.getlist("rule"):
            if not index.isdigit():
                raise ValueError
            rules.append({
                "amount": request.form.get(f"amount_{index}", ""),
                "period": request.form.get(f"period_{index}", ""),
                "scope": request.form.get(f"scope_{index}", ""),
                "role_ids": request.form.getlist(f"roles_{index}"),
            })
        valid_roles = [role.id for role in guild.roles if not role.is_default()]
        cleaned = set_trick_cooldown_rules(guild_id, rules, valid_roles, user["id"])
        log_activity("trick_cooldown", user_id=user["id"], username=user.get("username"), guild_id=guild_id,
                     details={"rule_count": len(cleaned), "changed_by": user["id"]})
    except (TypeError, ValueError):
        return _limits_redirect("trickortreat", guild_id, error="limit_trickortreat")
    return _limits_redirect("trickortreat", guild_id, saved="limits_trickortreat")


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
    return render_template("statistics.html", total_users=total_users, total_candy=total_candy, command_count=registered_command_count(9), bot_status=bot_status, db_ok=db_ok)

@app.get("/daily")
def daily_page():
    user = session.get("discord_user")
    guilds = []
    guild = None
    profile = None
    next_daily = None
    daily_reward = get_economy_config()["daily_reward"]
    boost = None
    reward = daily_reward
    daily_bonus = False
    if user:
        # Only show servers that both the bot and the signed-in user share.
        guilds = [g for g in bot_guilds()
                  if str(g["id"]) in {str(x.get("id")) for x in session.get("discord_guilds", [])}]
        guild = selected_bot_guild()
        if guild:
            guild_id = int(guild["id"])
            user_id = int(user["id"])
            profile = users.find_one({"guild_id": guild_id, "user_id": user_id})
            if profile and profile.get("last_daily"):
                candidate = profile["last_daily"]
                # MongoDB may return older timestamps as naive datetimes.
                # Normalize them to UTC before comparing with now_utc().
                if candidate.tzinfo is None:
                    candidate = candidate.replace(tzinfo=timezone.utc)
                candidate = candidate + timedelta(hours=24)
                if candidate > now_utc():
                    next_daily = candidate
            member = get_bot_member(guild_id, user_id)
            if member:
                perks = member_clan_perks(guild_id, user_id)
                daily_bonus = bool(perks and perks["daily_unlocked"])
                reward, boost = calculate_daily_reward(
                    daily_reward, [role.id for role in member.roles], guild_id, daily_bonus
                )
    return render_template(
        "daily.html",
        user=user,
        guild=guild,
        profile=profile,
        next_daily=next_daily,
        daily_reward=reward,
        base_daily_reward=daily_reward,
        daily_boost=boost,
        clan_daily_bonus=daily_bonus,
        daily_guilds=guilds,
        daily_error=request.args.get("error"),
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

    # Do not allow a dashboard claim unless the user is actually a member
    # of the selected server. This also prevents awarding Candy to an
    # account in a server they cannot access.
    member = get_bot_member(guild_id, user_id)
    if member is None:
        return redirect(url_for("daily_page", guild=guild_id, error="not_member"))

    config = get_economy_config()
    perks = member_clan_perks(guild_id, user_id)
    daily_bonus = bool(perks and perks["daily_unlocked"])
    reward, boost = calculate_daily_reward(
        config["daily_reward"], [role.id for role in member.roles], guild_id, daily_bonus
    )
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
        {"$set": {"last_daily": now}, "$inc": {
            "balance": reward,
            "candy_earned_total": reward,
        }},
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
        details={
            "boost_role": boost.get("role_name") if boost else None,
            "multiplier": boost.get("multiplier") if boost else 1,
            "clan_multiplier": CLAN_DAILY_MULTIPLIER if daily_bonus else 1,
            "source": "web_daily",
        },
    )
    return redirect(url_for("daily_page", guild=guild_id, claimed="1"))


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
        badge_configs_by_guild = {}
        badge_member_keys = []
        for doc in top_users:
            guild_id = int(doc.get("guild_id", 0))
            if guild_id not in badge_configs_by_guild:
                badge_configs_by_guild[guild_id] = get_badge_configs(guild_id)
            if (any(config["target_type"] == "role" for config in badge_configs_by_guild[guild_id])
                    and doc.get("user_id") is not None):
                badge_member_keys.append((guild_id, doc["user_id"]))
        badge_members = get_bot_members_for_display(badge_member_keys)
        for doc in top_users:
            user_id = doc.get("user_id")
            guild_id = int(doc.get("guild_id", 0))
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

            badge_configs = badge_configs_by_guild[guild_id]
            member = badge_members.get((guild_id, int(user_id))) if user_id is not None else None
            preferred_badge = (
                get_member_badge_preference(guild_id, user_id)
                if user_id is not None else None
            )
            badges, name_gradient, _, _ = badge_presentation_for_member(
                user_id, member, badge_configs, preferred_badge
            )
            leaderboard.append({
                "name": name,
                "balance": doc.get("balance", 0) or 0,
                "avatar_url": avatar_url,
                "badges": badges,
                "name_gradient": name_gradient,
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


def as_utc(value):
    """MongoDB can hand back naive datetimes (they are UTC). Calling .timestamp() on a naive
    datetime would use the host's local timezone, so always normalise before building Discord timestamps."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def registered_command_count(default=0):
    bot = BOT.get("instance")
    try:
        return len(bot.tree.get_commands()) if bot is not None else default
    except Exception:
        return default


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
            "trivia_claims": [],
            "trivia_attempts": [],
            "trivia_cycle": 0,
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


def add_candy(guild_id: int, user_id: int, amount: int, counts_as_earned=False):
    increments = {"balance": amount}
    if counts_as_earned and int(amount) > 0:
        increments["candy_earned_total"] = int(amount)
    return users.find_one_and_update(
        {"guild_id": guild_id, "user_id": user_id},
        {"$inc": increments},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def admin_candy_view(kind, member, amount, new_balance, admin, reason=None):
    """Components V2 card for /add and /subtract. kind is 'add' or 'subtract'."""
    adding = kind == "add"
    ui = discord.ui
    head = (
        f"## {'➕ Candy Added' if adding else '➖ Candy Subtracted'}\n"
        f"**Member:** {member.mention}\n"
        f"**Amount:** {'+' if adding else '−'}{int(amount):,} 🍬\n"
        f"**New balance:** {int(new_balance):,} 🍬"
    )
    items = [ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(member.display_avatar.url))]
    if reason:
        items += [ui.Separator(), ui.TextDisplay(f"**Reason**\n> {reason}"[:1000])]
    items += [ui.Separator(), ui.TextDisplay(f"-# By {admin.mention} • <t:{int(now_utc().timestamp())}:f>")]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0x22C55E if adding else 0xEF4444))
    return view


def candy_dm_view(kind, server_name, amount, new_balance, admin_name, admin_avatar=None, reason=None):
    """Components V2 card DMed to a member whose Candy was changed by an admin. kind is 'add' or 'subtract'."""
    adding = kind == "add"
    ui = discord.ui
    head = (
        f"## {'➕ You received Candy' if adding else '➖ Candy was taken'}\n"
        f"**Server:** {server_name}\n"
        f"**Amount:** {'+' if adding else '−'}{int(amount):,} 🍬\n"
        f"**New balance:** {int(new_balance):,} 🍬\n"
        f"**By:** {admin_name}"
    )
    items = [ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(admin_avatar)) if admin_avatar else ui.TextDisplay(head)]
    if reason:
        items += [ui.Separator(), ui.TextDisplay(f"**Reason**\n> {reason}"[:1000])]
    items += [ui.Separator(), ui.TextDisplay(f"-# <t:{int(now_utc().timestamp())}:f>")]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0x22C55E if adding else 0xEF4444))
    return view


async def dm_candy_change(target, kind, server_name, amount, new_balance, admin_id, admin_name, admin_avatar=None, reason=None):
    """Best-effort DM telling a member their Candy was added/subtracted by an admin.

    Returns None if there was nothing to send (a bot, or an admin changing their own balance),
    True if the DM was delivered, False if Discord refused it (DMs closed, blocked, etc.).
    """
    if getattr(target, "bot", False) or int(target.id) == int(admin_id):
        return None
    try:
        await target.send(view=candy_dm_view(kind, server_name, amount, new_balance, admin_name, admin_avatar, reason))
        return True
    except discord.HTTPException:
        return False


def authorization_required_view(status, url):
    """Ephemeral card shown the first time someone uses a slash command."""
    ui = discord.ui
    if status == "outdated":
        head = (
            "## 🎃 We updated our terms\n"
            "Please review and accept the updated **Terms of Service** and **Privacy Policy** to keep using Aureolis."
        )
    else:
        head = (
            "## 🎃 Authorize to continue\n"
            "Before you can use Aureolis commands, you need to:\n"
            "• Authorize with Discord\n"
            "• Agree to the **Terms of Service** and **Privacy Policy**\n"
            "• Enable **DMs** so the bot can message you"
        )
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay(head),
        ui.Separator(),
        ui.ActionRow(ui.Button(label="Authorize with Discord", url=url)),
        ui.TextDisplay("-# It takes about 30 seconds. Run your command again once you're done."),
        accent_colour=0xF97316,
    ))
    return view


def authorized_dm_view():
    """Welcome DM sent right after authorizing. Proves DMs work."""
    ui = discord.ui
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay(
            "## ✅ You're authorized\n"
            "Thanks for agreeing to the Terms of Service and Privacy Policy. DMs are on, so I can "
            "message you here, for example when an admin adds or removes Candy from your balance.\n"
            "Head back to the server and run your command again. 🍬"
        ),
        accent_colour=0x22C55E,
    ))
    return view


def candy_notice_view(title, text, ok=False):
    """Small Components V2 notice for errors and denials."""
    ui = discord.ui
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(f"## {title}\n{text}"), accent_colour=0x22C55E if ok else 0xEF4444))
    return view


RANK_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
GIVE_PERIOD_WORDS = {"hour": "hour", "day": "day", "week": "week"}


def give_candy_view(giver, member, amount, limit_status=None, tax=0, tax_percent=0):
    """Components V2 card for /give."""
    ui = discord.ui
    head = (
        "## 🤝 Candy Gift\n"
        f"**From:** {giver.mention}\n"
        f"**To:** {member.mention}\n"
        f"**Amount:** {int(amount):,} 🍬"
    )
    if tax:
        head += (
            f"\n**Tax ({int(tax_percent)}%):** {int(tax):,} 🍬"
            f"\n**Total charged:** {int(amount) + int(tax):,} 🍬"
        )
    items = [ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(member.display_avatar.url))]
    if limit_status:
        word = GIVE_PERIOD_WORDS[limit_status["period"]]
        left = max(0, limit_status["limit"] - limit_status["used"] - int(amount))
        items += [ui.Separator(), ui.TextDisplay(f"-# Give limit: {left:,} 🍬 left this {word}")]
    items += [ui.Separator(), ui.TextDisplay(f"-# <t:{int(now_utc().timestamp())}:f>")]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0xF97316))
    return view


def give_limit_blocked_view(status, amount):
    """Private notice when /give is stopped by a limit rule."""
    word = GIVE_PERIOD_WORDS[status["period"]]
    if status["too_big"]:
        text = (f"You can give at most **{status['limit']:,} 🍬** per {word}, "
                f"so **{int(amount):,} 🍬** can never fit in one gift.")
    else:
        text = (f"You've given **{status['used']:,} / {status['limit']:,} 🍬** in the last {word}. "
                f"You can give **{status['remaining']:,} 🍬** more right now.")
        if status["resets_at"]:
            text += f"\nEnough room frees up <t:{int(status['resets_at'].timestamp())}:R>."
    return candy_notice_view("⏳ Give limit reached", text)


TRICK_OR_TREAT_LINES = (
    "You knocked on a creaky old door and it swung open…",
    "A friendly ghost dropped a handful of candy in your bag.",
    "The witch next door was feeling generous tonight.",
    "A skeleton in a top hat handed you a full-size bar.",
    "You found a bowl on the porch with a note: \"Take a few!\"",
    "A pumpkin-headed neighbour tossed you a treat.",
)


def trick_or_treat_view(member, reward, new_balance, next_at, jackpot_rewards=None):
    """Components V2 card for a successful /trickortreat."""
    ui = discord.ui
    jackpot_rewards = jackpot_rewards or []
    jackpot_text = "".join(
        f"\n🎉 Jackpot! +{int(prize['reward']):,} 🍬 (**1 in {int(prize['odds']):,}**)"
        for prize in jackpot_rewards
    )
    base_reward = int(reward) - sum(int(prize["reward"]) for prize in jackpot_rewards)
    head = (
        "## 🎃 Trick or Treat!\n"
        f"{random.choice(TRICK_OR_TREAT_LINES)}\n"
        f"**Found:** +{base_reward:,} 🍬{jackpot_text}\n"
        f"**New balance:** {int(new_balance):,} 🍬"
    )
    items = [
        ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(member.display_avatar.url)),
        ui.Separator(),
        ui.TextDisplay(f"-# Next trick-or-treat <t:{int(next_at.timestamp())}:R>"),
    ]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0xF97316))
    return view


def balance_candy_view(member, balance, server_name, clan_profile=None, badges=None):
    """Components V2 card for /balance, showing the member's own badges."""
    ui = discord.ui
    clan_text = (
        f"🏰 **{discord.utils.escape_markdown(clan_profile['name'])}**"
        if clan_profile else "**🏰 Clan:** Not in a clan"
    )
    badge_text = " ".join(
        f"{badge['emoji']} **{badge['label']}**" for badge in badges or []
    )
    head = (
        "## 🍬 Candy Balance\n"
        f"**Member:** {member.mention}\n"
        + (f"**Badges:** {badge_text}\n" if badge_text else "")
        + f"{clan_text}\n"
        + f"**Balance:** {int(balance):,} 🍬\n"
        + f"**🍬 Clan contribution:** {int(clan_profile['contributed']) if clan_profile else 0:,} 🍬"
    )
    items = [
        ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(member.display_avatar.url)),
        ui.Separator(),
        ui.TextDisplay(f"-# {server_name} • <t:{int(now_utc().timestamp())}:f>"),
    ]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0xF97316))
    return view


def daily_claimed_view(member, reward, boost, new_balance, next_at, clan_daily_bonus=False):
    """Components V2 card for a successful /daily."""
    ui = discord.ui
    head = (
        "## 🎁 Daily Reward Claimed\n"
        f"**Received:** +{int(reward):,} 🍬\n"
        f"**New balance:** {int(new_balance):,} 🍬"
    )
    if boost:
        head += f"\n⚡ **{float(boost['multiplier']):g}× boost** from **{boost['role_name']}**"
    if clan_daily_bonus:
        head += f"\n🏰 **{CLAN_DAILY_MULTIPLIER:g}× clan daily bonus**"
    ts = int(next_at.timestamp())
    items = [
        ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(member.display_avatar.url)),
        ui.Separator(),
        ui.TextDisplay(f"⏰ **Next gift:** <t:{ts}:R>\n-# <t:{ts}:F>"),
    ]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0xF97316))
    return view


def daily_cooldown_view(member, next_at, next_reward, boost=None):
    """Components V2 card shown when /daily is used before the 24h cooldown is over."""
    ui = discord.ui
    ts = int(next_at.timestamp())
    head = (
        "## ⏰ Daily Already Claimed\n"
        "You've already opened today's gift.\n"
        f"**Next gift:** <t:{ts}:R>\n"
        f"**Ready at:** <t:{ts}:F>"
    )
    reward_text = f"🎁 **Next reward:** +{int(next_reward):,} 🍬"
    if boost:
        reward_text += f"\n⚡ **{float(boost['multiplier']):g}× boost** from **{boost['role_name']}**"
    items = [
        ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(member.display_avatar.url)),
        ui.Separator(),
        ui.TextDisplay(reward_text),
        ui.Separator(),
        ui.TextDisplay("-# Daily rewards can be claimed once every 24 hours."),
    ]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0xF59E0B))
    return view


def profile_candy_view(
    member, user, rank, next_daily=None, is_self=True, clan_profile=None, badges=None
):
    """Components V2 card for /profile."""
    ui = discord.ui
    inventory = user.get("inventory", []) or []
    badge_text = " ".join(
        f"{badge['emoji']} **{badge['label']}**" for badge in badges or []
    )
    personal_badge_text = f"**Badges:** {badge_text}\n" if badge_text else ""
    clan_text = (
        f"🏰 **{discord.utils.escape_markdown(clan_profile['name'])}**"
        if clan_profile else "**🏰 Clan:** Not in a clan"
    )
    head = (
        f"## 🎃 {member.display_name}'s Profile\n"
        + personal_badge_text
        + f"**🍬 Candy:** {int(user.get('balance', 0)):,}\n"
        + f"**🏆 Rank:** {'#' + format(rank, ',') if rank else '—'}\n"
        + f"**🎒 Items:** {len(inventory):,}\n"
        + f"{clan_text}\n"
        + f"**🍬 Clan contribution:** {int(clan_profile['contributed']) if clan_profile else 0:,} 🍬"
    )
    items = [ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(member.display_avatar.url))]
    if inventory:
        shown = [f"{i.get('emoji', '🎃')} {i.get('name', 'Shop Item')}" for i in inventory[-8:][::-1]]
        extra = len(inventory) - len(shown)
        text = "**Latest items**\n" + "\n".join(shown) + (f"\n-# and {extra} more" if extra > 0 else "")
        items += [ui.Separator(), ui.TextDisplay(text[:1000])]
    if next_daily and next_daily > now_utc():
        subject = "Your" if is_self else f"{member.display_name}'s"
        daily_text = f"⏰ {subject} next daily reward <t:{int(next_daily.timestamp())}:R>"
    else:
        daily_text = (
            "🎁 Your daily reward is ready — use **/daily**"
            if is_self
            else f"🎁 {member.display_name}'s daily reward is ready"
        )
    items += [ui.Separator(), ui.TextDisplay(daily_text)]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0xF97316))
    return view


def leaderboard_candy_view(guild, rows, viewer, viewer_rank=None, viewer_balance=None):
    """Components V2 card for /leaderboard. `rows` are user documents, best first."""
    ui = discord.ui
    lines = []
    for index, row in enumerate(rows, start=1):
        marker = RANK_MEDALS.get(index, f"**{index}.**")
        badges = " ".join(
            f"{badge['emoji']} **{badge['label']}**"
            for badge in row.get("badges", [])
        )
        badge_text = f" {badges}" if badges else ""
        lines.append(f"{marker} <@{row['user_id']}>{badge_text} — **{int(row['balance']):,} 🍬**")
    head = f"## 🏆 Candy Leaderboard\n-# {guild.name}"
    icon = guild.icon.url if guild.icon else None
    items = [ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(icon)) if icon else ui.TextDisplay(head)]
    items += [ui.Separator(), ui.TextDisplay("\n".join(lines)[:3500])]
    footer = f"-# Top {len(rows)}"
    if viewer_rank:
        footer += f" • You: #{viewer_rank:,} with {int(viewer_balance):,} 🍬"
    items += [ui.Separator(), ui.TextDisplay(footer)]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0xF97316))
    return view


def clan_command_view(guild, clan, progress, members, owner_id, show_leaderboard=False):
    ui = discord.ui
    clan_name = discord.utils.escape_markdown(str(clan.get("name", "Clan")))
    verification = clan_verification_presentation(clan.get("verification_badge"))
    verification_badge = f" {verification['verification_emoji']}" if verification else ""
    icon_url = None
    base_url = public_base_url()
    if clan.get("icon_data") and base_url:
        icon_url = (
            f"{base_url}/clans/{int(guild.id)}/"
            f"{quote(str(clan['clan_id']), safe='')}/icon"
        )

    if show_leaderboard:
        lines = [
            f"**{index}.** <@{member['user_id']}>"
            + "".join(
                f" {badge['emoji']} **{badge['label']}**"
                for badge in member.get("badges", [])
            )
            + f" — **{int(member['contributed']):,} 🍬**"
            for index, member in enumerate(members, start=1)
        ]
        details = "\n".join(lines) if lines else "No members have contributed Candy yet."
        body = f"## 🏆 {clan_name}{verification_badge} Contribution Leaderboard\n-# {guild.name}\n\n{details}"
        button_label = "Show clan overview"
        button_style = discord.ButtonStyle.secondary
    else:
        discount_status = "Unlocked" if progress["discount_unlocked"] else "Locked"
        daily_status = "Unlocked" if progress["daily_unlocked"] else "Locked"
        body = (
            f"## 🏰 {clan_name}{verification_badge}\n"
            f"**Members:** {int(progress['member_count']):,}\n"
            f"**Shared Candy contributed:** {int(progress['earned']):,} 🍬\n\n"
            f"**10% shop discount:** {int(progress['earned']):,} / "
            f"{int(progress['discount_threshold']):,} 🍬 — {discount_status}\n"
            f"**1.25× daily bonus:** {int(progress['earned']):,} / "
            f"{int(progress['daily_threshold']):,} 🍬 — {daily_status}"
        )
        if clan.get("description"):
            description = discord.utils.escape_markdown(str(clan["description"]))
            body += f"\n\n{description[:700]}"
        button_label = "Show member leaderboard"
        button_style = discord.ButtonStyle.primary

    items = [
        ui.Section(ui.TextDisplay(body), accessory=ui.Thumbnail(icon_url))
        if icon_url else ui.TextDisplay(body),
    ]
    button = ui.Button(label=button_label, style=button_style)

    async def toggle_leaderboard(button_interaction):
        if button_interaction.user.id != int(owner_id):
            await button_interaction.response.send_message(
                "🚫 This clan card belongs to someone else. Use **/clan** to view a clan and use your own buttons.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if button_interaction.guild_id != int(guild.id):
            await button_interaction.response.send_message(
                "This clan card can only be used in its original server.",
                ephemeral=True,
            )
            return
        current_clan = await db(clans.find_one, {
            "guild_id": int(guild.id),
            "clan_id": str(clan["clan_id"]),
            "status": "active",
        })
        if current_clan is None:
            await button_interaction.response.send_message(
                "This clan is no longer available.",
                ephemeral=True,
            )
            return
        # Acknowledge the click before database work so the component cannot time out.
        await button_interaction.response.defer()
        try:
            current_progress, current_members = await asyncio.gather(
                db(clan_progress, guild.id, current_clan),
                db(clan_member_contribution_leaderboard, guild.id, current_clan, 10),
            )
            current_badges = await db(leaderboard_member_badges, guild.id, current_members)
            for member in current_members:
                member["badges"] = current_badges.get(int(member["user_id"]), [])
            await button_interaction.edit_original_response(
                view=clan_command_view(
                    guild,
                    current_clan,
                    current_progress,
                    current_members,
                    owner_id,
                    not show_leaderboard,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception(
                "Failed to toggle clan overview/leaderboard for clan %s in guild %s",
                clan.get("clan_id"),
                guild.id,
            )
            await button_interaction.followup.send(
                "I couldn't load that clan view just now. Please try again or use "
                "/clan_contributers_leaderboard.",
                ephemeral=True,
            )

    button.callback = toggle_leaderboard
    items.extend([ui.Separator(), ui.ActionRow(button)])
    if clan.get("banner_data"):
        if not base_url:
            log.warning(
                "Clan %s has a banner but PUBLIC_BASE_URL is not configured; "
                "the banner cannot be shown in the /clan Components V2 card.",
                clan.get("clan_id"),
            )
        else:
            banner_url = f"{base_url}/clans/{int(guild.id)}/{quote(str(clan.get('clan_id')), safe='')}/banner"
            media_gallery = getattr(ui, "MediaGallery", None)
            # In discord.py, MediaGalleryItem is exported from discord, not discord.ui.
            media_item = getattr(discord, "MediaGalleryItem", None)
            if media_gallery and media_item:
                try:
                    items.append(media_gallery(media_item(media=banner_url)))
                except (TypeError, ValueError, AttributeError):
                    log.warning(
                        "Could not render clan banner in Components V2 for clan %s",
                        clan.get("clan_id"),
                        exc_info=True,
                    )
            else:
                log.warning(
                    "This discord.py build does not expose Components V2 media gallery support; "
                    "cannot render clan banner for clan %s.",
                    clan.get("clan_id"),
                )
    view = ui.LayoutView(timeout=600)
    view.add_item(ui.Container(*items, accent_colour=0x7C3AED))
    return view


def clan_leaderboard_view(guild, ranked_clans, viewer_rank=None, total_clans=None):
    """Components V2 card ranking active clans by their members' shared Candy contribution."""
    ui = discord.ui
    lines = []
    for index, row in enumerate(ranked_clans, start=1):
        clan = row["clan"]
        progress = row["progress"]
        marker = RANK_MEDALS.get(index, f"**{index}.**")
        clan_name = discord.utils.escape_markdown(str(clan.get("name", "Clan")))
        verification = clan_verification_presentation(clan.get("verification_badge"))
        badge = f" {verification['verification_emoji']}" if verification else ""
        member_count = int(progress.get("member_count", 0) or 0)
        earned = int(progress.get("earned", 0) or 0)
        lines.append(
            f"{marker} 🏰 **{clan_name}**{badge} — **{earned:,} 🍬**"
            f" • {member_count:,} member{'s' if member_count != 1 else ''}"
        )

    head = f"## 🏆 Clan Leaderboard\n-# {guild.name}"
    icon = guild.icon.url if guild.icon else None
    items = [ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(icon)) if icon else ui.TextDisplay(head)]
    items += [ui.Separator(), ui.TextDisplay("\n".join(lines)[:3500])]
    footer = f"Top {len(ranked_clans):,} of {int(total_clans if total_clans is not None else len(ranked_clans)):,} active clans"
    if viewer_rank:
        footer += f" • Your clan: #{viewer_rank:,}"
    items += [ui.Separator(), ui.TextDisplay(footer)]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=0x7C3AED))
    return view


def clean_reason(reason):
    reason = (reason or "").strip()
    return reason[:200] or None


intents = discord.Intents.default()
# Arcane level-up rewards read the level-up message, so Message Content is required.
# Enabled by default. If the Message Content intent is not switched on in the Discord Developer Portal,
# main() falls back to running without it (embed-style level-ups still work) instead of crashing.
ARCANE_LEVEL_BONUS_ENABLED = os.getenv("ARCANE_LEVEL_BONUS_ENABLED", "true").lower() == "true"
ARCANE_ANNOUNCE = os.getenv("ARCANE_ANNOUNCE", "true").lower() == "true"
intents.message_content = ARCANE_LEVEL_BONUS_ENABLED
log.info(
    "Arcane diagnostics: ARCANE_LEVEL_BONUS_ENABLED=%s | message_content_intent=%s",
    os.getenv("ARCANE_LEVEL_BONUS_ENABLED", "<unset>"),
    intents.message_content,
)


class AccessControlledTree(app_commands.CommandTree):
    def __init__(self, client):
        # Slash commands belong to the server install only. Without this, Discord falls back to the app's
        # Developer Portal defaults, which can offer them as "external" user-installed commands
        # (replies then show only to the person who ran it, and they aren't a normal server command).
        try:
            super().__init__(
                client,
                allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False),
                allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
            )
        except (TypeError, AttributeError) as exc:
            log.warning("Could not restrict commands to server installs (%s: %s); using Discord's defaults.", type(exc).__name__, exc)
            super().__init__(client)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        if command is None:
            return True

        if not await db(is_command_enabled, command.name):
            await interaction.response.send_message(
                f"🚫 **/{command.name}** is currently disabled by the bot administrator.",
                ephemeral=True,
            )
            return False

        # First-use Discord authorization is disabled. Slash commands are available
        # immediately after the normal command-access check above. The legacy OAuth
        # authorization routes remain available for dashboard/admin flows.
        return True


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

        log.info(
            "Arcane diagnostics: author_id=%s guild_id=%s channel_id=%s content_len=%s embeds=%s message_content_intent=%s",
            message.author.id,
            message.guild.id,
            getattr(message.channel, "id", None),
            len(message.content or ""),
            len(message.embeds),
            self.bot.intents.message_content,
        )

        # Arcane can send the level-up text either as normal message content
        # or inside an embed, so inspect both formats.
        if not message.content and not message.embeds:
            log.warning(
                "Arcane message arrived with no readable content. The Message Content intent is probably "
                "not enabled in the Discord Developer Portal (Bot > Privileged Gateway Intents)."
            )
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
        matched_text = None
        for text in message_texts:
            # Normalize harmless formatting differences Arcane/Discord can add,
            # such as non-breaking spaces, zero-width characters, or line breaks.
            normalized_text = (
                (text or "")
                .replace("\u200b", "")
                .replace("\ufeff", "")
                .replace("\u00a0", " ")
            )
            normalized_text = re.sub(r"\s+", " ", normalized_text).strip()
            candidate = ARCANE_LEVEL_PATTERN.search(normalized_text)
            if candidate:
                match = candidate
                matched_text = normalized_text
                break
        if not match:
            log.info(
                "Arcane level-up message did not match in guild %s. content=%r normalized=%r embeds=%s",
                message.guild.id,
                message.content,
                re.sub(r"\s+", " ", (message.content or "").replace("\u200b", "").replace("\ufeff", "").replace("\u00a0", " ")).strip(),
                len(message.embeds),
            )
            return
        log.info(
            "Arcane level-up message matched in guild %s: %r",
            message.guild.id,
            matched_text,
        )

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
        updated = await db(add_candy, guild_id, user_id, bonus, True)
        await db(
            log_activity,
            "arcane_level_bonus",
            user_id,
            None,
            guild_id,
            bonus,
            {"level": level, "source_bot_id": message.author.id},
        )
        log.info(
            "Awarded %s Candy to user %s for Arcane level %s in guild %s. New balance: %s",
            bonus,
            user_id,
            level,
            guild_id,
            updated.get("balance", 0),
        )

        if ARCANE_ANNOUNCE:
            try:
                await message.channel.send(
                    f"🍬 <@{user_id}> earned **{bonus:,} Candy** for reaching level **{level}**!",
                    allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)]),
                )
            except discord.Forbidden:
                log.warning("Missing Send Messages permission in channel %s (guild %s) to announce Arcane bonus.", getattr(message.channel, "id", None), guild_id)
            except discord.HTTPException:
                log.exception("Could not announce Arcane level bonus.")

    @app_commands.command(name="balance", description="Check your or another member's Candy balance.")
    @app_commands.describe(member="The member whose balance you want to view.")
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        if not interaction.guild:
            await interaction.response.send_message(view=candy_notice_view("Server only", "This command can only be used in a server."), ephemeral=True)
            return
        target = member or interaction.user
        user = await db(ensure_user, interaction.guild.id, target.id, target.name, target.display_name)
        clan_profile = await db(clan_member_profile, interaction.guild.id, user)
        badges = await db(member_badges_for_display, interaction.guild.id, target.id, target)
        await interaction.response.send_message(
            view=balance_candy_view(
                target, user["balance"], interaction.guild.name, clan_profile, badges=badges,
            ),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="trivia", description="Answer a trivia question to earn Candy.")
    @app_commands.guild_only()
    async def trivia(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                view=candy_notice_view("Server only", "Trivia can only be played in a server."),
                ephemeral=True,
            )
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        controls = await db(get_member_controls, guild_id, user_id)
        if controls["blocked_from_candy"]:
            await interaction.response.send_message(
                view=candy_notice_view(
                    "🚫 Not allowed",
                    "You are not allowed to participate in Candy activities in this server.",
                ),
                ephemeral=True,
            )
            return
        member_record = await db(
            ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name,
        )
        questions = await db(lambda: list(trivia_questions.find({"enabled": True})))
        if not questions:
            await interaction.response.send_message(
                view=candy_notice_view("🧠 No trivia yet", "There are no active trivia questions right now."),
                ephemeral=True,
            )
            return
        config = await db(get_economy_config)
        questions.sort(key=lambda question: (
            str(question.get("created_at") or ""),
            str(question.get("question_id") or ""),
        ))
        claimed = trivia_answered_ids(member_record)
        available = [question for question in questions if question.get("question_id") not in claimed]
        if not available:
            if not config["trivia_looping"]:
                await interaction.response.send_message(
                    view=candy_notice_view(
                        "🎉 All caught up",
                        "You have already answered every active trivia question.",
                        ok=True,
                    ),
                    ephemeral=True,
                )
                return
            active_question_ids = [str(question["question_id"]) for question in questions]
            refreshed = await db(
                reset_trivia_claims_for_loop,
                guild_id,
                user_id,
                member_record,
                active_question_ids,
            )
            member_record = refreshed or await db(
                users.find_one, {"guild_id": guild_id, "user_id": user_id},
            ) or member_record
            claimed = trivia_answered_ids(member_record)
            available = [question for question in questions if question.get("question_id") not in claimed]
            if not available:
                await interaction.response.send_message(
                    view=candy_notice_view(
                        "🔄 Trivia round is refreshing",
                        "Run **/trivia** again in a moment to start the next round.",
                    ),
                    ephemeral=True,
                )
                return
        question = random.choice(available) if config["trivia_shuffle"] else available[0]
        await interaction.response.send_message(
            view=trivia_question_view(user_id, question),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="daily", description="Claim your daily Candy reward.")
    async def daily(self, interaction: discord.Interaction):
        notice = candy_notice_view
        if not interaction.guild:
            await interaction.response.send_message(view=notice("Server only", "This command can only be used in a server."), ephemeral=True)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        controls = await db(get_member_controls, guild_id, user_id)
        if controls["blocked_from_candy"]:
            await interaction.response.send_message(view=notice("🚫 Not allowed", "You are not allowed to participate in Candy activities in this server."), ephemeral=True)
            return
        now = now_utc()
        await db(ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name)
        config = get_economy_config()
        perks = await db(member_clan_perks, guild_id, user_id)
        daily_bonus = bool(perks and perks["daily_unlocked"])
        reward, boost = await db(
            calculate_daily_reward,
            config["daily_reward"],
            [role.id for role in getattr(interaction.user, "roles", [])],
            guild_id,
            daily_bonus,
        )
        cutoff = now - timedelta(hours=24)
        updated = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": user_id,
             "$or": [{"last_daily": {"$exists": False}}, {"last_daily": {"$lte": cutoff}}]},
            {"$set": {"last_daily": now}, "$inc": {
                "balance": reward,
                "candy_earned_total": reward,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            # Still on cooldown: show when the next gift unlocks (and what it will be worth).
            user = await db(get_user, guild_id, user_id)
            last = as_utc((user or {}).get("last_daily"))
            next_at = (last + timedelta(hours=24)) if last else (now + timedelta(hours=24))
            await interaction.response.send_message(
                view=daily_cooldown_view(interaction.user, next_at, reward, boost), ephemeral=True)
            return
        await db(log_activity, "daily", interaction.user.id, interaction.user.name, guild_id, reward,
                 {"boost_role": boost.get("role_name") if boost else None,
                  "multiplier": boost.get("multiplier") if boost else 1,
                  "clan_multiplier": CLAN_DAILY_MULTIPLIER if daily_bonus else 1})
        await interaction.response.send_message(
            view=daily_claimed_view(
                interaction.user, reward, boost, updated["balance"],
                now + timedelta(hours=24), daily_bonus,
            ),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="trickortreat", description="Go trick-or-treating for a random Candy reward.")
    async def trick_or_treat(self, interaction: discord.Interaction):
        notice = candy_notice_view
        if not interaction.guild:
            await interaction.response.send_message(view=notice("Server only", "This command can only be used in a server."), ephemeral=True)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        controls = await db(get_member_controls, guild_id, user_id)
        if controls["blocked_from_candy"]:
            await interaction.response.send_message(view=notice("🚫 Not allowed", "You are not allowed to participate in Candy activities in this server."), ephemeral=True)
            return
        # The cooldown is 1 minute by default; the dashboard (Access Control) can change it per server and per role.
        cooldown = await db(resolve_trick_cooldown, guild_id, [role.id for role in getattr(interaction.user, "roles", [])])
        now = now_utc()
        await db(ensure_user, guild_id, user_id, interaction.user.name, interaction.user.display_name)
        config = get_economy_config()
        base_reward = random.randint(config["trick_or_treat_min"], config["trick_or_treat_max"])
        jackpot_bonus, jackpot_wins = roll_trick_or_treat_jackpots(config["trick_or_treat_jackpots"])
        reward = base_reward + jackpot_bonus
        updated = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": user_id,
             "$or": [{"last_trick_or_treat": {"$exists": False}}, {"last_trick_or_treat": {"$lte": now - timedelta(seconds=cooldown)}}]},
            {"$set": {"last_trick_or_treat": now}, "$inc": {
                "balance": reward,
                "candy_earned_total": reward,
            }},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            user = await db(get_user, guild_id, user_id)
            last = user["last_trick_or_treat"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            await interaction.response.send_message(
                view=notice("🏠 No more Candy yet", f"The neighbours are out of treats. Try again <t:{int((last + timedelta(seconds=cooldown)).timestamp())}:R>."),
                ephemeral=True)
            return
        await db(
            log_activity,
            "trick_or_treat",
            interaction.user.id,
            interaction.user.name,
            guild_id,
            reward,
            {"base_reward": base_reward, "jackpots": jackpot_wins} if jackpot_wins else None,
        )
        await interaction.response.send_message(
            view=trick_or_treat_view(
                interaction.user,
                reward,
                updated["balance"],
                now + timedelta(seconds=cooldown),
                jackpot_wins,
            ),
            allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="add", description="Admin: add Candy to a member's balance.")
    @app_commands.describe(member="Member receiving Candy.", amount="Amount of Candy to add.",
                           reason="Optional reason, shown on the card and in the logs.")
    async def add(self, interaction: discord.Interaction, member: discord.Member,
                  amount: app_commands.Range[int, 1, 100000000],
                  reason: Optional[app_commands.Range[str, 1, 200]] = None):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(view=candy_notice_view("🚫 Admins only", "Only Aureolis admins can use this command."), ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message(view=candy_notice_view("Server only", "This command can only be used in a server."), ephemeral=True)
            return
        reason = clean_reason(reason)
        await db(ensure_user, interaction.guild.id, member.id, member.name, member.display_name)
        updated = await db(users.find_one_and_update, {"guild_id": interaction.guild.id, "user_id": member.id},
            {"$inc": {"balance": int(amount)}}, return_document=ReturnDocument.AFTER)
        details = {"changed_by": interaction.user.id}
        if reason: details["reason"] = reason
        await db(log_activity, "admin_add", member.id, member.name, interaction.guild.id, int(amount), details)
        await interaction.response.send_message(
            view=admin_candy_view("add", member, amount, updated["balance"], interaction.user, reason),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]))
        dm_ok = await dm_candy_change(member, "add", interaction.guild.name, amount, updated["balance"],
                                      interaction.user.id, interaction.user.display_name, interaction.user.display_avatar.url, reason)
        if dm_ok is False:
            await interaction.followup.send(
                view=candy_notice_view("📪 Couldn't send a DM",
                                       f"{member.mention} has DMs closed or has blocked the bot, so they weren't notified. The Candy change itself went through."),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="subtract", description="Admin: subtract Candy from a member's balance.")
    @app_commands.describe(member="Member losing Candy.", amount="Amount of Candy to subtract.",
                           reason="Optional reason, shown on the card and in the logs.")
    async def subtract(self, interaction: discord.Interaction, member: discord.Member,
                       amount: app_commands.Range[int, 1, 100000000],
                       reason: Optional[app_commands.Range[str, 1, 200]] = None):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(view=candy_notice_view("🚫 Admins only", "Only Aureolis admins can use this command."), ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message(view=candy_notice_view("Server only", "This command can only be used in a server."), ephemeral=True)
            return
        reason = clean_reason(reason)
        await db(ensure_user, interaction.guild.id, member.id, member.name, member.display_name)
        updated = await db(users.find_one_and_update, {"guild_id": interaction.guild.id, "user_id": member.id, "balance": {"$gte": int(amount)}},
            {"$inc": {"balance": -int(amount)}}, return_document=ReturnDocument.AFTER)
        if not updated:
            current = await db(get_user, interaction.guild.id, member.id)
            await interaction.response.send_message(
                view=candy_notice_view("❌ Not enough Candy",
                                       f"{member.mention} only has **{(current or {}).get('balance', 0):,} 🍬 Candy**, so that amount cannot be subtracted."),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        details = {"changed_by": interaction.user.id}
        if reason: details["reason"] = reason
        await db(log_activity, "admin_subtract", member.id, member.name, interaction.guild.id, int(amount), details)
        await interaction.response.send_message(
            view=admin_candy_view("subtract", member, amount, updated["balance"], interaction.user, reason),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]))
        dm_ok = await dm_candy_change(member, "subtract", interaction.guild.name, amount, updated["balance"],
                                      interaction.user.id, interaction.user.display_name, interaction.user.display_avatar.url, reason)
        if dm_ok is False:
            await interaction.followup.send(
                view=candy_notice_view("📪 Couldn't send a DM",
                                       f"{member.mention} has DMs closed or has blocked the bot, so they weren't notified. The Candy change itself went through."),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="resetcandy", description="Admin: reset a member's Candy and Candy access settings.")
    @app_commands.describe(member="The member whose Candy data should be reset.",
                           reason="Optional reason, shown in the audit log.")
    async def resetcandy(self, interaction: discord.Interaction, member: discord.Member,
                         reason: Optional[app_commands.Range[str, 1, 200]] = None):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                view=candy_notice_view("🚫 Admins only", "Only Aureolis admins can use this command."),
                ephemeral=True,
            )
            return
        if not interaction.guild:
            await interaction.response.send_message(
                view=candy_notice_view("Server only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild.id
        reason = clean_reason(reason)
        await db(ensure_user, guild_id, member.id, member.name, member.display_name)
        previous = await db(get_user, guild_id, member.id) or {}
        previous_balance = int(previous.get("balance", 0) or 0)

        updated = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": member.id},
            {"$set": {"balance": 0, "candy_earned_total": 0}},
            return_document=ReturnDocument.AFTER,
        )
        # Reset both server-scoped restrictions so the member can earn Candy and appear
        # on the leaderboard again. Keep an audit trail of who performed the reset.
        await db(
            member_controls.update_one,
            {"guild_id": guild_id, "user_id": member.id},
            {"$set": {
                "guild_id": guild_id,
                "user_id": member.id,
                "blocked_from_candy": False,
                "leaderboard_excluded": False,
                "updated_at": now_utc(),
                "updated_by": interaction.user.id,
            }},
            upsert=True,
        )

        details = {
            "changed_by": interaction.user.id,
            "previous_balance": previous_balance,
            "controls_reset": ["blocked_from_candy", "leaderboard_excluded"],
        }
        if reason:
            details["reason"] = reason
        await db(
            log_activity, "admin_reset_candy", member.id, member.name,
            guild_id, previous_balance, details,
        )

        await interaction.followup.send(
            view=candy_notice_view(
                "🧹 Candy Reset Complete",
                f"Reset {member.mention}'s Candy balance to **0** and lifetime Candy earned to **0**. "
                "They can participate in Candy activities again and will be included on the Candy leaderboard. "
                "Their shop inventory and clan membership were left unchanged.",
                ok=True,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="give", description="Give Candy to another member.")
    @app_commands.describe(member="The member receiving Candy.", amount="Amount of Candy to give.")
    async def give(self, interaction: discord.Interaction, member: discord.Member,
                   amount: app_commands.Range[int, 1, 100000000]):
        notice = candy_notice_view
        if not interaction.guild:
            await interaction.response.send_message(view=notice("Server only", "This command can only be used in a server."), ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message(view=notice("🤖 Not a person", "You can't give Candy to a bot."), ephemeral=True)
            return
        sender_controls = await db(get_member_controls, interaction.guild.id, interaction.user.id)
        recipient_controls = await db(get_member_controls, interaction.guild.id, member.id)
        if sender_controls["blocked_from_candy"]:
            await interaction.response.send_message(view=notice("🚫 Not allowed", "You are not allowed to participate in Candy activities in this server."), ephemeral=True)
            return
        if recipient_controls["blocked_from_candy"]:
            await interaction.response.send_message(view=notice("🚫 Not allowed", "That member is not allowed to participate in Candy activities in this server."), ephemeral=True)
            return
        if member.id == interaction.user.id:
            await interaction.response.send_message(view=notice("🍬 Nice try", "You can't give Candy to yourself."), ephemeral=True)
            return
        blocked, statuses = await db(
            check_give_limits, interaction.guild.id, interaction.user.id,
            [role.id for role in getattr(interaction.user, "roles", [])], int(amount))
        if blocked:
            await interaction.response.send_message(view=give_limit_blocked_view(blocked, amount), ephemeral=True)
            return
        tax_percent = await db(get_give_tax_percent, interaction.guild.id)
        tax = calculate_give_tax(amount, tax_percent)
        total_cost = int(amount) + tax
        sender = await db(ensure_user, interaction.guild.id, interaction.user.id, interaction.user.name, interaction.user.display_name)
        debited = await db(
            users.find_one_and_update,
            {"guild_id": interaction.guild.id, "user_id": interaction.user.id, "balance": {"$gte": total_cost}},
            {"$inc": {"balance": -total_cost}},
            return_document=ReturnDocument.AFTER,
        )
        if not debited:
            await interaction.response.send_message(
                view=notice(
                    "❌ Not enough Candy",
                    f"You have **{sender['balance']:,} 🍬**, but the gift and tax require **{total_cost:,} 🍬**.",
                ),
                ephemeral=True,
            )
            return
        await db(ensure_user, interaction.guild.id, member.id, member.name, member.display_name)
        await db(add_candy, interaction.guild.id, member.id, amount)
        await db(
            log_activity,
            "give",
            interaction.user.id,
            interaction.user.name,
            interaction.guild.id,
            amount,
            {
                "recipient_id": member.id,
                "recipient": member.name,
                "tax_percent": tax_percent,
                "tax_amount": tax,
                "total_charged": total_cost,
            },
        )
        tightest = min(statuses, key=lambda s: s["remaining"]) if statuses else None
        await interaction.response.send_message(
            view=give_candy_view(
                interaction.user, member, amount, tightest, tax, tax_percent,
            ),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[member]))

    @app_commands.command(name="leaderboard", description="Show the server Candy leaderboard.")
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                view=candy_notice_view("Server only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        # Acknowledge immediately: MongoDB and badge lookups can exceed Discord's
        # interaction response deadline.
        await interaction.response.defer(thinking=True)
        guild_id = interaction.guild.id

        def load():
            excluded = [r["user_id"] for r in member_controls.find(
                {"guild_id": guild_id, "leaderboard_excluded": True}, {"user_id": 1})]
            top = list(users.find({"guild_id": guild_id, "user_id": {"$nin": excluded}}).sort("balance", -1).limit(10))
            viewer_rank = viewer_balance = None
            if interaction.user.id not in excluded and not any(r["user_id"] == interaction.user.id for r in top):
                mine = users.find_one({"guild_id": guild_id, "user_id": interaction.user.id})
                if mine:
                    viewer_balance = int(mine.get("balance", 0))
                    viewer_rank = users.count_documents(
                        {"guild_id": guild_id, "user_id": {"$nin": excluded}, "balance": {"$gt": viewer_balance}}) + 1
            return top, viewer_rank, viewer_balance

        try:
            top_users, viewer_rank, viewer_balance = await db(load)
            if not top_users:
                await interaction.followup.send(
                    view=candy_notice_view("🍬 Candy Leaderboard", "Nobody has earned Candy yet!", ok=True)
                )
                return
            badges_by_user = await db(leaderboard_member_badges, guild_id, top_users)
            for row in top_users:
                row["badges"] = badges_by_user.get(int(row["user_id"]), [])
            await interaction.followup.send(
                view=leaderboard_candy_view(
                    interaction.guild, top_users, interaction.user, viewer_rank, viewer_balance
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Main Candy leaderboard command failed")
            await interaction.followup.send(
                view=candy_notice_view(
                    "Leaderboard unavailable",
                    "I couldn't load the Candy leaderboard just now. Please try again in a moment.",
                ),
                ephemeral=True,
            )

    @app_commands.command(name="clan_announcement", description="DM an announcement to your current clan members.")
    @app_commands.describe(message="Announcement to send to members of the clan you own.")
    async def clan_announcement(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 1500],
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                view=candy_notice_view("Server only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = int(interaction.guild.id)
        clan = await db(clans.find_one, {
            "guild_id": guild_id,
            "owner_id": int(interaction.user.id),
            "status": "active",
        })
        if clan is None:
            await interaction.followup.send(
                view=candy_notice_view(
                    "🚫 Clan owners only",
                    "You must be the current owner of an active clan to use **/clan_announcement**.",
                ),
                ephemeral=True,
            )
            return

        message = str(message).strip()
        if not message:
            await interaction.followup.send(
                view=candy_notice_view("Empty announcement", "Please provide a message to send to your clan."),
                ephemeral=True,
            )
            return

        member_ids = await db(lambda: [
            int(record["user_id"])
            for record in users.find(
                {
                    "guild_id": guild_id,
                    "clan_id": str(clan["clan_id"]),
                    "user_id": {"$ne": int(interaction.user.id)},
                },
                {"user_id": 1},
            )
            if record.get("user_id") is not None
        ])
        if not member_ids:
            await interaction.followup.send(
                view=candy_notice_view(
                    "📣 No members to notify",
                    "Your clan currently has no other members to DM.",
                    ok=True,
                ),
                ephemeral=True,
            )
            return

        clan_name = str(clan.get("name", "Clan"))
        embed = discord.Embed(
            title=f"📣 Announcement from {clan_name}"[:256],
            description=message,
            colour=0x7C3AED,
        )
        embed.set_author(
            name=f"{interaction.user.display_name} • {interaction.guild.name}"[:256],
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_footer(text="Sent to current members of this clan.")

        async def notify_member(user_id):
            try:
                recipient = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await recipient.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except (discord.Forbidden, discord.NotFound):
                return False
            except discord.HTTPException:
                log.warning("Clan announcement DM failed for user %s in clan %s.", user_id, clan.get("clan_id"), exc_info=True)
                return False
            except Exception:
                log.warning("Unexpected clan announcement DM failure for user %s.", user_id, exc_info=True)
                return False

        results = await asyncio.gather(*(notify_member(user_id) for user_id in member_ids))
        sent = sum(1 for result in results if result)
        failed = len(results) - sent
        summary = f"Announcement sent to **{sent:,}** of **{len(member_ids):,}** current clan members."
        if failed:
            summary += f"\n📪 **{failed:,}** member(s) could not be reached (for example, DMs may be closed)."
        await interaction.followup.send(
            view=candy_notice_view("📣 Clan announcement complete", summary, ok=(sent > 0)),
            ephemeral=True,
        )

    @app_commands.command(name="clan_leaderboard", description="Rank the server's clans by shared Candy contribution.")
    async def clan_leaderboard(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                view=candy_notice_view("Server only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        guild_id = int(interaction.guild.id)
        active_clans = await db(lambda: list(clans.find({
            "guild_id": guild_id,
            "status": "active",
        })))
        if not active_clans:
            await interaction.followup.send(
                view=candy_notice_view("🏰 Clan Leaderboard", "There are no active clans in this server yet.", ok=True),
            )
            return

        progress_rows = await asyncio.gather(*(
            db(clan_progress, guild_id, clan) for clan in active_clans
        ))
        ranked_clans = [
            {"clan": clan, "progress": progress}
            for clan, progress in zip(active_clans, progress_rows)
        ]
        ranked_clans.sort(key=lambda row: (
            -int(row["progress"].get("earned", 0)),
            str(row["clan"].get("name", "")).casefold(),
            str(row["clan"].get("clan_id", "")),
        ))

        member_record = await db(users.find_one, {
            "guild_id": guild_id,
            "user_id": int(interaction.user.id),
        })
        viewer_clan_id = str(member_record.get("clan_id")) if member_record and member_record.get("clan_id") else None
        viewer_rank = next((
            index for index, row in enumerate(ranked_clans, start=1)
            if viewer_clan_id and str(row["clan"].get("clan_id")) == viewer_clan_id
        ), None)
        await interaction.followup.send(
            view=clan_leaderboard_view(interaction.guild, ranked_clans[:10], viewer_rank, len(ranked_clans)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="clan", description="View your clan or another clan's progress.")
    @app_commands.describe(clan_name="Optional clan name to look up; leave empty to view your clan.")
    @app_commands.autocomplete(clan_name=clan_name_autocomplete)
    async def clan(self, interaction: discord.Interaction, clan_name: Optional[str] = None):
        if interaction.guild is None:
            await interaction.response.send_message(
                view=candy_notice_view("Server only", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        # Acknowledge immediately so slower database/leaderboard work cannot time out.
        await interaction.response.defer(thinking=True)
        guild_id = interaction.guild.id
        if clan_name and clan_name.strip():
            normalized_name = " ".join(clan_name.split()).casefold()
            clan = await db(clans.find_one, {
                "guild_id": guild_id,
                "normalized_name": normalized_name,
                "status": "active",
            })
            if clan is None:
                await interaction.followup.send(
                    view=candy_notice_view(
                        "🏰 Clan not found",
                        f"No active clan named **{discord.utils.escape_markdown(clan_name.strip())}** exists in this server.",
                    ),
                    ephemeral=True,
                )
                return
        else:
            member_record = await db(users.find_one, {
                "guild_id": guild_id,
                "user_id": interaction.user.id,
            })
            clan_id = member_record.get("clan_id") if member_record else None
            clan = await db(clans.find_one, {
                "guild_id": guild_id,
                "clan_id": str(clan_id),
                "status": "active",
            }) if clan_id else None
            if clan is None:
                await interaction.followup.send(
                    view=candy_notice_view(
                        "🏰 No clan yet",
                        "You aren't in a clan in this server. Join one or start a creation request on the **Clans** page, or use `/clan clan_name` to look up another clan.",
                    ),
                    ephemeral=True,
                )
                return

        progress, members = await asyncio.gather(
            db(clan_progress, guild_id, clan),
            db(clan_member_contribution_leaderboard, guild_id, clan, 10),
        )
        badges_by_user = await db(leaderboard_member_badges, guild_id, members)
        for member in members:
            member["badges"] = badges_by_user.get(int(member["user_id"]), [])
        await interaction.followup.send(
            view=clan_command_view(
                interaction.guild,
                clan,
                progress,
                members,
                interaction.user.id,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="clan_contributers_leaderboard",
        description="Show a clan's top Candy contributors.",
    )
    @app_commands.describe(
        clan_name="Optional clan name; leave empty to view your own clan's leaderboard.",
    )
    @app_commands.autocomplete(clan_name=clan_name_autocomplete)
    async def clan_contributers_leaderboard(
        self,
        interaction: discord.Interaction,
        clan_name: Optional[str] = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                view=candy_notice_view(
                    "Server only",
                    "This command can only be used in a server.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        guild_id = interaction.guild.id
        if clan_name and clan_name.strip():
            normalized_name = " ".join(clan_name.split()).casefold()
            clan = await db(clans.find_one, {
                "guild_id": guild_id,
                "normalized_name": normalized_name,
                "status": "active",
            })
            if clan is None:
                await interaction.followup.send(
                    view=candy_notice_view(
                        "🏰 Clan not found",
                        f"No active clan named **{discord.utils.escape_markdown(clan_name.strip())}** exists in this server.",
                    ),
                    ephemeral=True,
                )
                return
        else:
            member_record = await db(users.find_one, {
                "guild_id": guild_id,
                "user_id": interaction.user.id,
            })
            clan_id = member_record.get("clan_id") if member_record else None
            clan = await db(clans.find_one, {
                "guild_id": guild_id,
                "clan_id": str(clan_id),
                "status": "active",
            }) if clan_id else None
            if clan is None:
                await interaction.followup.send(
                    view=candy_notice_view(
                        "🏰 No clan yet",
                        "You aren't in a clan in this server. Join one first, or use "
                        "/clan_contributers_leaderboard clan_name to view another clan's leaderboard.",
                    ),
                    ephemeral=True,
                )
                return

        progress, members = await asyncio.gather(
            db(clan_progress, guild_id, clan),
            db(clan_member_contribution_leaderboard, guild_id, clan, 10),
        )
        badges_by_user = await db(leaderboard_member_badges, guild_id, members)
        for member in members:
            member["badges"] = badges_by_user.get(int(member["user_id"]), [])

        await interaction.followup.send(
            view=clan_command_view(
                interaction.guild,
                clan,
                progress,
                members,
                interaction.user.id,
                show_leaderboard=True,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="event-announcement",
        description="Post an upcoming event announcement to the configured channel (panel admins only).",
    )
    @app_commands.describe(
        title="Short event title",
        date_time="When the event starts, including timezone (for example: Oct 31 at 7 PM ET)",
        description="What the event is about",
        location="Optional location or event location URL",
        notes="Optional extra notes",
        image_url="Optional HTTPS image URL to display at the top",
    )
    async def event_announcement(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        date_time: app_commands.Range[str, 1, 100],
        description: app_commands.Range[str, 1, 1500],
        location: Optional[app_commands.Range[str, 1, 300]] = None,
        notes: Optional[app_commands.Range[str, 1, 1000]] = None,
        image_url: Optional[str] = None,
    ):
        if not is_admin(interaction.user.id):
            await interaction.response.send_message(
                "🔒 This command is restricted to Aureolis panel admins.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        config = await db(event_announcement_settings.find_one, {"guild_id": interaction.guild.id})
        channel_id = config.get("channel_id") if config else None
        if not channel_id:
            await interaction.response.send_message(
                "⚙️ No event announcement channel is configured for this server. An Aureolis panel admin must set it in **Access Control → Event Announcements** first.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if not isinstance(channel, (discord.TextChannel)):
            await interaction.response.send_message(
                "⚠️ The configured event channel is unavailable. Please reselect it in Access Control.",
                ephemeral=True,
            )
            return
        if channel.guild.id != interaction.guild.id:
            await interaction.response.send_message(
                "⚠️ The configured event channel belongs to a different server. Please fix it in Access Control.",
                ephemeral=True,
            )
            return

        image_url = (image_url or "").strip()
        if image_url:
            parsed_image = urlparse(image_url)
            if parsed_image.scheme != "https" or not parsed_image.netloc or len(image_url) > 500:
                await interaction.response.send_message(
                    "⚠️ The image URL must be a valid HTTPS URL under 500 characters.",
                    ephemeral=True,
                )
                return

        # Discord Components V2 event card.
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Color.from_rgb(140, 72, 226))
        if image_url:
            container.add_item(discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=image_url, description=f"{title.strip()} event image")
            ))
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(content="# 🎃 Events"))
        container.add_item(discord.ui.TextDisplay(content=f"## {title.strip()}"))
        if location and location.strip():
            container.add_item(discord.ui.TextDisplay(content=f"**Location:** {location.strip()}"))
        container.add_item(discord.ui.TextDisplay(content=f"**Description:** {description.strip()}"))
        container.add_item(discord.ui.TextDisplay(content=f"**When:** {date_time.strip()}"))
        container.add_item(discord.ui.TextDisplay(content="**Status:** 🟢 Upcoming"))
        if notes and notes.strip():
            container.add_item(discord.ui.TextDisplay(content=f"**Notes:** {notes.strip()}"))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(discord.ui.Button(
            style=discord.ButtonStyle.link,
            label="View Event",
            url="https://halloween-discord-bot-production.up.railway.app/events",
        )))
        view.add_item(container)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sent_message = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await db(event_records.insert_one, {
                "guild_id": interaction.guild.id,
                "channel_id": channel.id,
                "message_id": sent_message.id,
                "title": title.strip(),
                "date_time": date_time.strip(),
                "description": description.strip(),
                "location": (location or "").strip(),
                "notes": (notes or "").strip(),
                "image_url": image_url,
                "status": "upcoming",
                "created_by": interaction.user.id,
                "created_at": now_utc(),
                "updated_at": now_utc(),
            })
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not post event announcement in channel %s", channel.id)
            await interaction.followup.send(
                "❌ I couldn't post there. Check the bot's **View Channel**, **Send Messages**, and **Embed Links** permissions.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Event announcement posted in {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="shop", description="Browse the Halloween Candy shop and buy items.")
    async def shop(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        if (await db(get_member_controls, interaction.guild.id, interaction.user.id))["blocked_from_candy"]:
            await interaction.response.send_message(
                "🚫 You are not allowed to participate in Candy activities in this server.",
                ephemeral=True,
            )
            return

        await db(
            ensure_user, interaction.guild.id, interaction.user.id,
            interaction.user.name, interaction.user.display_name,
        )
        items = await db(get_shop_items_for_guild, interaction.guild.id, True)
        member = await db(get_fresh_member, interaction.guild.id, interaction.user.id)
        if member is None:
            member = interaction.user
        clan_perks = await db(member_clan_perks, interaction.guild.id, interaction.user.id)
        view = OwnedShopView(
            interaction.user.id, interaction.guild, items, member, clan_perks, page=0,
        )
        await interaction.response.send_message(
            view=view, allowed_mentions=discord.AllowedMentions.none(),
        )
        view.message = await interaction.original_response()

    @app_commands.command(name="profile", description="View your or another member's Candy profile and inventory.")
    @app_commands.describe(member="The member whose profile and inventory you want to view.")
    async def profile(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        if not interaction.guild:
            await interaction.response.send_message(view=candy_notice_view("Server only", "This command can only be used in a server."), ephemeral=True)
            return
        guild_id = interaction.guild.id
        target = member or interaction.user
        user = await db(ensure_user, guild_id, target.id, target.name, target.display_name)
        clan_profile = await db(clan_member_profile, guild_id, user)
        badges = await db(member_badges_for_display, guild_id, target.id, target)
        rank = await db(users.count_documents, {"guild_id": guild_id, "balance": {"$gt": int(user.get("balance", 0))}}) + 1
        last_daily = user.get("last_daily")
        next_daily = None
        if last_daily:
            if last_daily.tzinfo is None:
                last_daily = last_daily.replace(tzinfo=timezone.utc)
            next_daily = last_daily + timedelta(hours=24)
        await interaction.response.send_message(
            view=profile_candy_view(
                target,
                user,
                rank,
                next_daily,
                is_self=target.id == interaction.user.id,
                clan_profile=clan_profile,
                badges=badges,
            ),
            allowed_mentions=discord.AllowedMentions.none())


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
    start_log_worker()
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
        except discord.PrivilegedIntentsRequired:
            await bot.close()
            if intents.message_content:
                log.error(
                    "Message Content intent is NOT enabled in the Discord Developer Portal. "
                    "Starting without it: Arcane plain-text level-ups cannot be read until you enable it "
                    "(Developer Portal > Bot > Privileged Gateway Intents > Message Content Intent)."
                )
                intents.message_content = False
                continue
            d.update(state="error", last_error="Privileged intents not enabled")
            await park_forever("A privileged intent is required but not enabled in the Developer Portal.")
        except discord.LoginFailure:
            await bot.close()
            d.update(state="error", last_error="Invalid Discord token")
            await park_forever("Discord rejected DISCORD_TOKEN (invalid token).")
        except discord.HTTPException as exc:
            await bot.close()
            if exc.status != 429:
                d.update(state="error", last_error=f"Discord HTTP {exc.status}")
                await park_forever(f"Discord returned HTTP {exc.status} during login ({str(exc.text)[:200]!r}).")
            info = describe_rate_limit(exc)
            wait = retry_delay
            if info["retry_after"]:
                wait = max(wait, min(info["retry_after"] + 5, 3600))
            log.warning(
                "Discord 429 during login: %s | headers=%s | body=%r",
                info["label"], info["headers"], info["body"],
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
        except Exception as exc:
            await bot.close()
            d.update(state="error", last_error=exc.__class__.__name__)
            log.exception("Unexpected error while running the bot")
            await park_forever("Unexpected error.")


if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    asyncio.run(main())