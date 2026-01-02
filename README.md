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
- Multi-keyword AND search (all keywords must match)
- Optional filters:
  - `@username` (first token)
  - `r:<N>` / `likes:<N>` / `reactions>=<N>`
  - trailing page number
- Docker-based deployment
- History import from Telegram Desktop JSON export

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
