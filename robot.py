from telegram.ext import Updater
from threading import Thread
import asyncio
import datetime
import logging
import os

from user_handlers import bot_help, chat_start, chat_stop, chat_delete, chatid_get, msg_ai, msg_store, faq_admin
from user_jobs.commands_set import set_bot_commands
from user_jobs.faq_learn import run_faq_learn
from user_jobs.embed_updater import run_embed_update
from userbot import run_telethon
from utils import is_userbot_mode, get_text_func

logging.basicConfig(format="%(asctime)s - %(threadName)s - %(levelname)s - %(message)s",
                    level=logging.INFO)
_ = get_text_func()

bot_token = os.getenv("BOT_TOKEN")
updater = Updater(token=bot_token, workers=8)
dispatcher = updater.dispatcher

job = updater.job_queue
job.run_once(set_bot_commands, 30)

# Weekly FAQ auto-learn job — every Sunday at 03:00 UTC
# Using a fixed time avoids re-triggering on every bot restart (first=120 was problematic)
_next_sunday_3am = datetime.time(3, 0, tzinfo=datetime.timezone.utc)
job.run_repeating(run_faq_learn, interval=604800, first=_next_sunday_3am)

# Hourly embedding updater: indexes new messages into ChromaDB
job.run_repeating(run_embed_update, interval=3600, first=300)

dispatcher.add_handler(msg_ai.handler)
dispatcher.add_handler(faq_admin.handler)
dispatcher.add_handler(chat_start.handler)
dispatcher.add_handler(chat_stop.handler)
dispatcher.add_handler(chat_delete.handler)
dispatcher.add_handler(bot_help.handler)
dispatcher.add_handler(chatid_get.handler)

if not is_userbot_mode():
    dispatcher.add_handler(msg_store.handler)

def run_telethon_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_telethon())

if __name__ == "__main__":
    if is_userbot_mode():
        telethon_thread = Thread(target=run_telethon_thread, name="Thread-userbot")
        telethon_thread.start()
        logging.info(_("userbot start..."))

    mode_env = os.getenv("BOT_MODE")
    if mode_env == "webhook":
        url_path = os.getenv("URL_PATH")
        hook_url = os.getenv("HOOK_URL")
        updater.start_webhook(listen="0.0.0.0", port=9968, url_path=url_path, webhook_url=hook_url)
    else:
        updater.start_polling(
            poll_interval=1.0,       # check for updates every 1s
            timeout=20,              # long-poll timeout (seconds)
            drop_pending_updates=True,  # ignore updates queued while bot was down
        )

    logging.info(_("robot start..."))
    updater.idle()
