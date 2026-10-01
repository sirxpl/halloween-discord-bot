import os
import random
import threading
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify
from pymongo import MongoClient, ReturnDocument
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
PORT = int(os.getenv("PORT", "10000"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not configured.")

# MongoDB
mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
db = mongo["halloween_bot"]
users = db["users"]
settings = db["settings"]

# Health endpoint for Render's web service.
app = Flask(__name__)


@app.get("/")
def health():
    return jsonify({"status": "online", "service": "halloween-discord-bot"})


@app.get("/health")
def health_check():
    try:
        mongo.admin.command("ping")
        return jsonify({"status": "ok", "database": "connected"}), 200
    except Exception:
        return jsonify({"status": "error", "database": "unavailable"}), 503


def run_web_server():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


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
bot = commands.Bot(command_prefix="!", intents=intents)


class HalloweenBot(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_

    @app_commands.command(name="balance", description="Check your Candy balance.")
    async def balance(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "🍬 This command can only be used in a server.", ephemeral=True
            )
            return

        user = ensure_user(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(
            f"🍬 **{interaction.user.display_name}** has **{user['balance']:,} Candy**."
        )

    @app_commands.command(name="daily", description="Claim your daily Candy reward.")
    async def daily(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "🍬 This command can only be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        now = now_utc()

        ensure_user(guild_id, user_id)

        # Atomic cooldown check: only one daily claim can succeed per 24 hours.
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
            {
                "$set": {"last_daily": now},
                "$inc": {"balance": 100},
            },
            return_document=ReturnDocument.AFTER,
        )

        if not updated:
            user = get_user(guild_id, user_id)
            next_claim = user["last_daily"] + timedelta(hours=24)
            timestamp = int(next_claim.timestamp())
            await interaction.response.send_message(
                f"⏰ You already claimed your daily Candy. Try again <t:{timestamp}:R>.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🎃 You claimed your daily reward: **+100 🍬 Candy**!"
        )

    @app_commands.command(
        name="trickortreat",
        description="Go trick-or-treating for a random Candy reward.",
    )
    async def trick_or_treat(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "🍬 This command can only be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        user_id = interaction.user.id
        now = now_utc()
        ensure_user(guild_id, user_id)

        # A simple 1-hour cooldown for the first version.
        cutoff = now - timedelta(hours=1)
        reward = random.randint(25, 150)

        updated = users.find_one_and_update(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "$or": [
                    {"last_trick_or_treat": {"$exists": False}},
                    {"last_trick_or_treat": {"$lte": cutoff}},
                ],
            },
            {
                "$set": {"last_trick_or_treat": now},
                "$inc": {"balance": reward},
            },
            return_document=ReturnDocument.AFTER,
        )

        if not updated:
            user = get_user(guild_id, user_id)
            next_claim = user["last_trick_or_treat"] + timedelta(hours=1)
            timestamp = int(next_claim.timestamp())
            await interaction.response.send_message(
                f"🏠 No more Candy yet! Try again <t:{timestamp}:R>.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🎃 **Trick or treat!** You found **{reward:,} 🍬 Candy**!"
        )

    @app_commands.command(name="give", description="Give Candy to another member.")
    @app_commands.describe(member="The member receiving Candy.", amount="Amount of Candy to give.")
    async def give(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        if not interaction.guild:
            await interaction.response.send_message(
                "🍬 This command can only be used in a server.", ephemeral=True
            )
            return

        if member.bot:
            await interaction.response.send_message(
                "🤖 You can't give Candy to a bot.", ephemeral=True
            )
            return

        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "🍬 You can't give Candy to yourself.", ephemeral=True
            )
            return

        if amount <= 0:
            await interaction.response.send_message(
                "❌ The amount must be greater than 0.", ephemeral=True
            )
            return

        sender = ensure_user(interaction.guild.id, interaction.user.id)

        # Atomic debit prevents negative balances.
        debited = users.find_one_and_update(
            {
                "guild_id": interaction.guild.id,
                "user_id": interaction.user.id,
                "balance": {"$gte": amount},
            },
            {"$inc": {"balance": -amount}},
            return_document=ReturnDocument.AFTER,
        )

        if not debited:
            await interaction.response.send_message(
                f"❌ You don't have enough Candy. You currently have **{sender['balance']:,} 🍬**.",
                ephemeral=True,
            )
            return

        ensure_user(interaction.guild.id, member.id)
        add_candy(interaction.guild.id, member.id, amount)

        await interaction.response.send_message(
            f"🍬 {interaction.user.mention} gave **{amount:,} Candy** to {member.mention}!"
        )

    @app_commands.command(name="leaderboard", description="Show the server Candy leaderboard.")
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "🍬 This command can only be used in a server.", ephemeral=True
            )
            return

        top_users = list(
            users.find({"guild_id": interaction.guild.id})
            .sort("balance", -1)
            .limit(10)
        )

        if not top_users:
            await interaction.response.send_message(
                "🍬 Nobody has earned Candy yet!"
            )
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
            await interaction.response.send_message(
                "🍬 This command can only be used in a server.", ephemeral=True
            )
            return

        user = ensure_user(interaction.guild.id, interaction.user.id)
        inventory = user.get("inventory", [])

        embed = discord.Embed(
            title=f"🎃 {interaction.user.display_name}'s Profile",
            color=discord.Color.orange(),
        )
        embed.add_field(name="🍬 Candy", value=f"{user['balance']:,}", inline=True)
        embed.add_field(name="🎒 Inventory", value=str(len(inventory)), inline=True)
        await interaction.response.send_message(embed=embed)


async def setup_bot():
    await bot.add_cog(HalloweenBot(bot))


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Connected to {len(bot.guilds)} guild(s).")


async def main():
    await setup_bot()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()

    import asyncio

    asyncio.run(main())
