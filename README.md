# 🎃 Halloween Discord Bot

A seasonal Discord bot for a 30-day Halloween event featuring 🍬 Candy, events, rewards, and a Halloween finale.

## Planned features

- 🍬 Persistent Candy currency
- 🎁 Daily and event rewards
- 🎃 Halloween events and registration
- 🛒 Candy shop and inventory
- 🏆 Candy leaderboard
- ⏰ Halloween countdown
- 🤖 Slash commands and interactive buttons
- ☁️ Render-friendly deployment

## Status

🚧 Initial development — October Halloween event season.

## First-use authorization

Every slash command is gated. The first time someone uses one, the bot replies (ephemeral) with an
**Authorize with Discord** button. That opens `/authorize`, where they:

1. tick **Terms of Service + Privacy Policy**,
2. tick **Enable DMs**,
3. sign in with Discord (`identify` scope only).

The result is stored in the `user_authorizations` collection (`_id` = Discord user ID) and the bot sends a
welcome DM to confirm DMs work. Nothing else is needed in the Discord Developer Portal: the flow reuses the
existing `/oauth/callback` redirect URI.

- `PUBLIC_BASE_URL` (optional): base URL for the authorize link. Defaults to the origin of `OAUTH2_REDIRECT_URI`.
- To make everyone re-accept after you edit the legal pages, bump `TOS_VERSION` in `bot.py`.
