## Quick Start

Run this project in the simplest way possible.  
For import/history, userbot, webhook, backups and troubleshooting see [advanced-use.md](advanced-use.md).

---

### Create a Telegram Bot

1. Chat with [@BotFather](https://t.me/botfather). Create a bot and copy the `BOT_TOKEN`.

2. Enable Inline Mode:  
   Bot Settings → **Inline Mode** → enable.  
   Set inline placeholder to: `@{user} [keywords] {page}`

3. Disable [Privacy Mode](https://core.telegram.org/bots#privacy-mode):  
   Bot Settings → **Group Privacy** → **Turn off**

4. Add the bot to your target group/supergroup and grant permission to read messages.  
   (Userbot mode does not require adding the bot — see [advanced-use.md](advanced-use.md#userbot-mode).)

---

### Configure Docker Compose (no .env)

This fork recommends storing `/app/config` (SQLite DB + configs) in a **Docker named volume**.  
On Docker Desktop (Windows/WSL2) this is typically much faster than binding the database to an NTFS host directory.

1. Create a directory:

```bash
mkdir -p tgbot && cd tgbot
