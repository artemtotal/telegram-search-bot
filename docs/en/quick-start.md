## Quick Start

Run this project in the simplest way possible.  
For advanced scenarios (history import, userbot mode, webhook, backups), please refer to  
https://github.com/artemtotal/telegram-search-bot/blob/master/docs/en/advanced-use.md

---

## Create Bot

1. Chat with [@BotFather](https://t.me/botfather).  
   Follow the steps to create a bot and record the `BOT_TOKEN`.

2. Enable Inline Mode:  
   Go to **Bot Settings** → **Inline Mode** → enable.  
   Edit the inline placeholder and set it to:  
   `@{user} [keywords] {page}`

3. Disable [Privacy Mode](https://core.telegram.org/bots#privacy-mode):  
   Go to **Bot Settings** → **Group Privacy** → **Turn off**.

4. Configure other options according to your preferences.  
   Add the bot to the target group and grant permission to read messages.  
   (Userbot mode does not require adding the bot to the group, see Advanced Usage.)

---

## Configure Docker Compose

This fork stores the database and configuration inside a **Docker named volume**  
(`/app/config`).  
On Docker Desktop (Windows / WSL2) this is significantly faster than storing SQLite
on an NTFS-mounted host directory.


---

## Configure Docker Compose

1. Create a new directory to store configuration and database files:

```bash
mkdir tgbot && cd tgbot
```

2. Download the Docker Compose configuration file:

```bash
wget https://github.com/artemtotal/telegram-search-bot/raw/master/docker-compose.yml
```

> ⚠️ Note: this file contains placeholders.
> Do **not** commit it with real secrets.

3. Edit `docker-compose.yml` and set your values:

* Replace `BOT_TOKEN` with the token received from @BotFather
* Change `LANG` to `en_US` (or another available language)
* Do not change other options unless you understand their purpose

4. Start the bot in background:

```bash
docker compose up -d
```

5. View logs to ensure the bot is running correctly:

```bash
docker compose logs -f tgbot
```

6. To update the bot to the latest version:

```bash
docker compose pull
docker compose up -d --remove-orphans
```

---

## Important Notes on Storage Performance

This fork stores the database and configuration inside a **Docker named volume**
(`/app/config`).

On Docker Desktop (Windows / WSL2), this approach is **tens of times faster**
than storing the SQLite database on an NTFS-mounted host directory and is
strongly recommended.

---