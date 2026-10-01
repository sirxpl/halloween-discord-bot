import os
import logging
import random
import threading
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify, render_template
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from dotenv import load_dotenv

load_dotenv()

discord.utils.setup_logging(level=logging.INFO)
log = logging.getLogger("halloween-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
PORT = int(os.getenv("PORT", "10000"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not configured.")

mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=2500)
db = mongo["halloween_bot"]
users = db["users"]
settings = db["settings"]

app = Flask(__name__)


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
        status = "Online"
    except PyMongoError:
        log.exception("Dashboard could not reach MongoDB")
        total_users, total_candy, status = 0, 0, "Database unavailable"
    return render_template(
        "dashboard.html",
        total_users=total_users,
        total_candy=total_candy,
        command_count=6,
        bot_status=status,
    )


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


def get_user(guild_id: int, user_id: int):
    return users.find_one({"guild_id": guild_id, "user_id": user_id})


def ensure_user(guild_id: int, user_id: int):
    return users.find_one_and_update(
        {"guild_id": guild_id, "user_id": user_id},
        {
            "$setOnInsert": {
                "guild_id": guild_id,
                "user_id": user_id,
                "balance": 0,
                "inventory": [],
                "created_at": now_utc(),
            }
        },
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
        user = await db(ensure_user, interaction.guild.id, interaction.user.id)
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
        await db(ensure_user, guild_id, user_id)
        cutoff = now - timedelta(hours=24)
        updated = await db(
            users.find_one_and_update,
            {"guild_id": guild_id, "user_id": user_id,
             "$or": [{"last_daily": {"$exists": False}}, {"last_daily": {"$lte": cutoff}}]},
            {"$set": {"last_daily": now}, "$inc": {"balance": 100}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            user = await db(get_user, guild_id, user_id)
            timestamp = int((user["last_daily"] + timedelta(hours=24)).timestamp())
            await interaction.response.send_message(
                f"⏰ You already claimed your daily Candy. Try again <t:{timestamp}:R>.", ephemeral=True
            )
            return
        await interaction.response.send_message("🎃 You claimed your daily reward: **+100 🍬 Candy**!")

    @app_commands.command(name="trickortreat", description="Go trick-or-treating for a random Candy reward.")
    async def trick_or_treat(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        now = now_utc()
        await db(ensure_user, guild_id, user_id)
        cutoff = now - timedelta(hours=1)
        reward = random.randint(25, 150)
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
        sender = await db(ensure_user, interaction.guild.id, interaction.user.id)
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
        await db(ensure_user, interaction.guild.id, member.id)
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
            name = member.display_name if member else f"User {user['user_id']}"
            lines.append(f"**{index}.** {name} — **{user['balance']:,} 🍬**")
        embed = discord.Embed(
            title="🍬 Candy Leaderboard",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="profile", description="View your Candy profile.")
    async def profile(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("🍬 This command can only be used in a server.", ephemeral=True)
            return
        user = await db(ensure_user, interaction.guild.id, interaction.user.id)
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
    async def on_ready():
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


async def main():
    await check_database()
    retry_delay = 30
    while True:
        bot = create_bot()
        await setup_bot(bot)
        try:
            await bot.start(DISCORD_TOKEN)
            return
        except discord.HTTPException as exc:
            if exc.status != 429:
                await bot.close()
                raise
            log.warning(
                "Discord returned HTTP 429 during login. "
                "Waiting %ss before creating a fresh bot and retrying.", retry_delay
            )
            await bot.close()
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)
        except discord.LoginFailure:
            await bot.close()
            raise
        except Exception:
            await bot.close()
            raise


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(main())
