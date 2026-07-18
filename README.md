# Telegram Search Bot — Artem Fork (based on Taosky)

Self-hosted Telegram bot that records messages from enabled chats and provides inline search.  
This repository is a fork of `Taosky/telegram-search-bot` with an upgraded inline search handler and a deployment setup optimized for SQLite performance on Docker Desktop (Windows/WSL2).

## Why this fork (vs upstream)

This fork modifies the message search handler and provides a deployment pattern that is more practical for large, busy chats.

- Reactions-based ranking: results are sorted by `reactions_total` (DESC, NULL treated as 0) and then by message date (DESC).  
  Upstream sorts only by date.
- Reactions filter in query: supports `r:<N>`, `likes:<N>`, or `reactions>=<N>` to show only messages with at least N reactions.
- Username-first queries: supports `@username keywords` where `@username` is the first token.  
  User resolution is performed via `User.username` (case-insensitive `ilike`) rather than full name.
- Cleaner result cards: inline result description includes reactions + metadata; the posted content includes message text and a clickable link.
- Compatibility fallback: if your DB schema does not contain `reactions_total`, the bot automatically falls back to date-only sorting.
- Performance (SQLite on Windows): the recommended Compose setup stores `/app/config` (including `bot.db`) inside Docker’s Linux filesystem via a named volume, instead of a bind mount to an NTFS path.  
  For SQLite workloads this is typically **dramatically faster** on Docker Desktop/WSL2 compared to running the database on an NTFS-mounted host directory.

### Important behavioral difference (access control)

Upstream checks whether the inline querying user is a member of each enabled chat (via `get_chat_member`) and filters results accordingly.
This fork currently searches across all chats where `Chat.enable == 1` and does not run membership checks in bot mode.

If you need upstream-style access filtering, restore the membership check or enforce restrictions via `.config.json` / admin rules.

## Features

- Record messages from enabled Telegram groups/supergroups
- Inline search from any chat (Inline Mode)
- Anonymous questions sent from private chat to a selected forum topic
- Captcha, weekly per-user cooldown, duplicate/contact filtering, admin block controls
- One-hour self-service deletion and private notifications about replies
- Multi-keyword AND search (all keywords must match)
- Optional filters:
  - `@username` (first token)
  - `r:<N>` / `likes:<N>` / `reactions>=<N>`
  - trailing page number
- Docker-based deployment
- History import from Telegram Desktop JSON export

## Anonymous questions

Users open a private chat with the bot and send `/start` or `/anonymous`. The flow is:

1. solve a simple button captcha;
2. choose a forum topic;
3. send one text question;
4. preview and confirm it.

The group sees a post from the bot without the author's name. The bot keeps the Telegram user ID for abuse handling and tells the user about this before submission. Replies to the group post are mirrored to the author's private chat.

Default anti-spam rules:

- one submission per Telegram user every 7 days, including a post deleted by its author;
- text only, 15–1500 characters;
- links, `@username` values, e-mail addresses and phone numbers are rejected;
- exact duplicate text from the previous 30 days is rejected;
- three failed captcha attempts cause a 15-minute lock;
- an administrator can delete and block an author from the private moderation notification.

The author can delete a published question through the bot during the first hour. This does not reset the weekly cooldown.

### Topic discovery and administration

The bot automatically discovers forum topics when it receives new messages in them. A newly discovered topic may temporarily be shown as `Тема #<thread_id>` until Telegram sends a topic creation event or an administrator names it manually.

If the advertising bot's SQLite database is available in this container as a read-only file, the search bot can import the exact topics previously bound with the advertising bot's `/bind` command:

```env
ANON_TOPIC_SOURCE_DB=/adbot/sqlite.db
```

Mount the advertising bot's whole data directory at `/adbot` with `:ro` so SQLite can also read its WAL file, then point the variable to `/adbot/sqlite.db`. The import reads only the `topics` table and does not modify the advertising bot database.

```yaml
volumes:
  - /host/path/telegram-ad-bot-data:/adbot:ro
```

Run this command inside a forum topic to add or rename it:

```text
/anon_topic Название темы
```

Admin commands:

```text
/anon_topics          list discovered topics and thread IDs
/anon_reset USER_ID   unblock a user and reset their cooldown/captcha lock
```

Configure the feature through environment variables:

```env
ADMIN_ID=123456789
ANON_TARGET_CHAT_ID=-1001234567890
ANON_TOPIC_SOURCE_DB=/adbot/sqlite.db
ANON_COOLDOWN_DAYS=7
ANON_DELETE_MINUTES=60
ANON_MIN_LENGTH=15
ANON_MAX_LENGTH=1500
```

If `ANON_TARGET_CHAT_ID=0`, the first enabled chat in `bot.db` is used. Set it explicitly when the bot indexes more than one chat. The bot needs permission to send messages in topics and delete its own messages.

## Search syntax

Use the bot in inline mode:

`@YourBot <query>`

Supported query format:

- `@username` as the first token (optional): `@YourBot @john hello`
- reactions filter (optional): `r:<N>` / `likes:<N>` / `reactions>=<N>`
- page number as the last token (optional): `@YourBot hello 2`

Examples:

- `@YourBot berlin`
- `@YourBot @john berlin`
- `@YourBot berlin r:5`
- `@YourBot @john berlin r:3 2`

Sorting:
- `reactions_total` DESC (if present), then `date` DESC.

## Requirements

- Docker + Docker Compose
- Telegram Bot token from @BotFather
- Inline Mode enabled in bot settings

## Quick start / Advanced usage

- Quick start:  
  https://github.com/artemtotal/telegram-search-bot/blob/master/docs/en/quick-start.md

- Advanced usage:  
  https://github.com/artemtotal/telegram-search-bot/blob/master/docs/en/advanced-use.md



## Data persistence

Runtime data lives in `/app/config` inside the container:

- `bot.db` (SQLite database)
- `.config.json` (optional access control/admin config)
- `Caddyfile` (optional, if you use webhook reverse proxy)

Persist `/app/config` using a Docker named volume (recommended on Windows/WSL2 for SQLite performance) or a host directory.

## Security notes

- Never commit `BOT_TOKEN` or `USER_BOT_API_HASH` to Git.
- Use `.env` for secrets.
- If a token ever leaked, rotate it (BotFather / my.telegram.org).
