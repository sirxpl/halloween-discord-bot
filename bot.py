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
from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from flask_session import Session
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv
from urllib.parse import urlencode, urlparse, parse_qs
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
shop_items = db["shop_items"]
role_shop_items = db["role_shop_items"]
role_shop_claims = db["role_shop_claims"]
arcane_level_rewards = db["arcane_level_rewards"]

ADMIN_USER_IDS = {777341204047331348, 793723672225382452, 931543094086750299}

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
    public_endpoints = {"static", "home", "dashboard", "login", "oauth_callback", "logout", "status_page", "status_json", "health", "terms_page", "privacy_page"}
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


@app.get("/shop")
def shop_page():
    guild = selected_guild()
    guild_id = int(guild["id"]) if guild else -1
    migrate_legacy_shop_items(guild_id)
    items = list(shop_items.find({"guild_id": guild_id, "enabled": True}).sort("price", 1))
    result = request.args.get("role_result")
    purchase_message = SHOP_RESULT_MESSAGES.get(result) if result else None
    return render_template("shop.html", items=items, guild=guild, purchase_message=purchase_message,
                           purchase_ok=(result == "success"), requirement_mode=item_requirement_mode)


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
    errors = {
        "invalid": "That item configuration is not valid. Check the name and price.",
        "role_required": "Pick the Discord role this item should give.",
        "role_unassignable": "That role cannot be given by the bot. Choose a normal role (not @everyone or a bot-managed role).",
        "role_above_bot": "The bot cannot give that role yet. In Server Settings → Roles, give the bot Manage Roles and drag the bot's role ABOVE the reward role, then try again.",
        "requirements_empty": "Pick at least one required role, or set “Who can buy this?” to Everyone.",
        "emoji_invalid": "That emoji is not valid. Use a normal emoji, or pick one of the server's custom emojis.",
    }
    return render_template("shop_panel.html", user=user, avatar_url=discord_avatar_url(user),
                           bot_guilds=guilds, guild_id=gid, roles=roles, listings=listings, emojis=emojis,
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
        requirement_mode = request.form.get("requirement_mode", "everyone").lower()
        required_role_ids = [int(x) for x in request.form.getlist("required_role_ids") if str(x).isdigit()]
        role_id_raw = request.form.get("role_id", "").strip()
        role_id = int(role_id_raw) if role_id_raw.isdigit() else None
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
        item_id = secrets.token_urlsafe(10)
        now = now_utc()
        shop_items.insert_one({"guild_id": gid, "item_id": item_id, "name": name,
            "description": description or "A Halloween shop item.", "emoji": emoji, "price": price,
            "type": item_type, "enabled": enabled, "required_role_ids": required_role_ids,
            "requirement_mode": requirement_mode, "reward": reward,
            "created_at": now, "updated_at": now, "updated_by": int(user["id"])})
    except ShopConfigError as exc:
        return redirect(url_for("shop_panel_page", guild=gid, error=exc.code))
    except (KeyError, TypeError, ValueError):
        return redirect(url_for("shop_panel_page", guild=gid, error="invalid"))
    return redirect(url_for("shop_panel_page", guild=gid, saved="1"))


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
    result = purchase_shop_item(int(guild["id"]), int(user["id"]), item_id)
    return redirect(url_for("shop_page", guild=guild["id"], role_result=result))


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
    if enabled_only: query["enabled"] = True
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
    """The /shop menu belongs to whoever ran the command. Other people get a private 'not yours' reply."""

    def __init__(self, owner_id, timeout=600):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.message = None

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
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


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


def _refund_purchase(guild_id, user_id, price, item_id, purchased_at):
    users.update_one({"guild_id": int(guild_id), "user_id": int(user_id)},
                     {"$inc": {"balance": price},
                      "$pull": {"inventory": {"item_id": item_id, "purchased_at": purchased_at}}})


def purchase_shop_item(guild_id, user_id, item_id):
    item = shop_items.find_one({"guild_id": int(guild_id), "item_id": str(item_id), "enabled": True})
    if not item: return "unavailable"
    bot = BOT.get("instance")
    guild = bot.get_guild(int(guild_id)) if bot and not bot.is_closed() else None
    if not guild: return "offline"
    member = get_fresh_member(guild_id, user_id)
    if member is None: return "member_missing"
    if not member_meets_shop_requirements(member, item): return "requirements"
    price = int(item.get("price", 0))
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
    inventory_entry = {"item_id": item["item_id"], "name": item.get("name", "Shop Item"),
                       "emoji": item.get("emoji", "🎃"), "description": item.get("description", ""),
                       "price": price, "type": item.get("type", "custom"), "purchased_at": purchased_at}
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
                _refund_purchase(guild_id, user_id, price, item["item_id"], purchased_at)
                return "permissions"
            except Exception:
                log.exception("Shop role assignment failed: guild=%s user=%s role=%s (%s) item=%s",
                              guild_id, user_id, role.id, role.name, item.get("item_id"))
                _refund_purchase(guild_id, user_id, price, item["item_id"], purchased_at)
                return "assignment_failed"
            try:  # best-effort confirmation; a failed lookup must not undo a successful grant
                verified = _run_on_bot_loop(bot, guild.fetch_member(int(user_id)), 10)
                if role.id not in {r.id for r in verified.roles}:
                    log.error("Discord accepted add_roles but role %s is not on member %s.", role.id, user_id)
                    _refund_purchase(guild_id, user_id, price, item["item_id"], purchased_at)
                    return "assignment_failed"
            except Exception:
                log.warning("Could not re-check member %s after granting role %s; trusting Discord's success.", user_id, role.id)
    except Exception:
        log.exception("Shop purchase failed")
        if charged:
            _refund_purchase(guild_id, user_id, price, item["item_id"], purchased_at)
        return "purchase_failed"

    try:
        log_activity("shop_purchase", user_id, member.name, guild_id, price,
                     {"item_id": item["item_id"], "item": item.get("name"), "type": item.get("type", "custom")})
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
    "arcane_level_bonus":  ("rewards", "⭐", "Arcane Level Bonus", +1),
    "member_control":      ("member_activity", "🧭", "Member Control Changed", 0),
    "admin_add":           ("admin", "➕", "Candy Added by Admin", +1),
    "admin_subtract":      ("admin", "➖", "Candy Removed by Admin", -1),
    "daily_boost":         ("admin", "⚡", "Daily Boost Set", 0),
    "daily_boost_removed": ("admin", "⚡", "Daily Boost Removed", 0),
    "economy_settings":    ("admin", "⚙️", "Economy Settings Changed", 0),
    "logging_test":        ("system", "🧪", "Logging Test", 0),
}

DETAIL_LABELS = {
    "changed_by": "Changed by", "recipient_id": "Recipient", "boost_role": "Boost role",
    "role_id": "Role", "role_name": "Role name", "control": "Control", "level": "Level",
    "trick_or_treat_min": "Trick-or-treat min", "trick_or_treat_max": "Trick-or-treat max",
    "daily_reward": "Daily reward",
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
    try:
        total_users = users.count_documents({})
        total_candy = sum((doc.get("balance", 0) or 0) for doc in users.find({}, {"balance": 1}))
        recent = list(activity_logs.find({"action": {"$in": ["daily", "trick_or_treat", "give", "buy", "web_buy", "shop_purchase"]}}).sort("created_at", -1).limit(12))
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
                reward, boost = calculate_daily_reward(
                    daily_reward, [role.id for role in member.roles], guild_id
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
    reward, boost = calculate_daily_reward(
        config["daily_reward"], [role.id for role in member.roles], guild_id
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
        details={
            "boost_role": boost.get("role_name") if boost else None,
            "multiplier": boost.get("multiplier") if boost else 1,
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


def candy_notice_view(title, text, ok=False):
    """Small Components V2 notice for errors and denials."""
    ui = discord.ui
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(f"## {title}\n{text}"), accent_colour=0x22C55E if ok else 0xEF4444))
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
        updated = await db(add_candy, guild_id, user_id, bonus)
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

    @app_commands.command(name="shop", description="Browse the Halloween Candy shop and buy items.")
    async def shop(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True); return
        if (await db(get_member_controls, interaction.guild.id, interaction.user.id))["blocked_from_candy"]:
            await interaction.response.send_message("🚫 You are not allowed to participate in Candy activities in this server.", ephemeral=True); return
        await db(ensure_user, interaction.guild.id, interaction.user.id, interaction.user.name, interaction.user.display_name)
        items = await db(get_shop_items_for_guild, interaction.guild.id, True)
        member = interaction.user  # carries the member's current roles
        view = OwnedShopView(interaction.user.id)
        summary = ["# 🛒 Halloween Candy Shop", f"<@{interaction.user.id}>, spend your 🍬 Candy below. Only you can use these buttons."]
        for item_data in items:
            eligible = member_meets_shop_requirements(member, item_data)
            requirement = shop_requirement_text(interaction.guild, item_data)
            status = "✅ Eligible" if eligible else "🔒 Locked"
            summary.append(f"\n## {item_data.get('emoji','🎃')} {item_data.get('name','Shop Item')} — {int(item_data.get('price',0)):,} 🍬\n{item_data.get('description','')}\n{status} • {requirement}")
        if not items: summary.append("\nNo shop items are currently configured.")
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(summary)[:3900])))
        buttons = []
        for item_data in items[:25]:
            disabled = not member_meets_shop_requirements(member, item_data)
            if item_data.get("type") == "role":
                role_id = item_data.get("reward", {}).get("role_id")
                role = interaction.guild.get_role(int(role_id)) if role_id else None
                if not role or role in getattr(member, "roles", []): disabled = True
            button = discord.ui.Button(label=f"Buy {item_data.get('name','Item')}"[:80],
                                       emoji=emoji_for_button(item_data.get("emoji")),
                                       style=discord.ButtonStyle.success if not disabled else discord.ButtonStyle.secondary,
                                       disabled=disabled)

            async def callback(btn_interaction, item_id=str(item_data["item_id"])):
                await btn_interaction.response.defer(ephemeral=True)
                if (await db(get_member_controls, btn_interaction.guild.id, btn_interaction.user.id))["blocked_from_candy"]:
                    await btn_interaction.followup.send("🚫 You are not allowed to participate in Candy activities in this server.", ephemeral=True); return
                result = await db(purchase_shop_item, btn_interaction.guild.id, btn_interaction.user.id, item_id)
                item_now = await db(lambda: shop_items.find_one({"guild_id": btn_interaction.guild.id, "item_id": item_id}))
                embed = discord.Embed(title="🛒 Purchase Complete" if result == "success" else "🎃 Shop",
                                      description=SHOP_RESULT_MESSAGES.get(result, "Purchase failed."),
                                      color=discord.Color.green() if result == "success" else discord.Color.red())
                if item_now:
                    embed.add_field(name="Item", value=f"{item_now.get('emoji','🎃')} {item_now.get('name','Shop Item')}", inline=False)
                    embed.add_field(name="Price", value=f"{int(item_now.get('price',0)):,} 🍬", inline=True)
                await btn_interaction.followup.send(embed=embed, ephemeral=True)
            button.callback = callback
            buttons.append(button)
        for start in range(0, len(buttons), 5):
            row = discord.ui.ActionRow()
            for button in buttons[start:start + 5]: row.add_item(button)
            view.add_item(discord.ui.Container(row))
        await interaction.response.send_message(view=view, allowed_mentions=discord.AllowedMentions.none())
        view.message = await interaction.original_response()

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
