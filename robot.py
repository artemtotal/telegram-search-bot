from telegram.ext import Updater
from threading import Thread
import asyncio
import datetime
import logging
import os

from user_handlers import (
    anonymous_posts,
    bot_help,
    chat_start,
    chat_stop,
    chat_delete,
    chatid_get,
    msg_ai,
    msg_store,
    faq_admin,
)
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


def _next_sunday_3am_utc(now=None):
    """Return the next Sunday at 03:00 UTC as an aware datetime."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    else:
        now = now.astimezone(datetime.timezone.utc)

    days_until_sunday = (6 - now.weekday()) % 7
    first_run = (now + datetime.timedelta(days=days_until_sunday)).replace(
        hour=3, minute=0, second=0, microsecond=0,
    )
    if first_run <= now:
        first_run += datetime.timedelta(days=7)
    return first_run


# Weekly FAQ auto-learn job — every Sunday at 03:00 UTC
# An explicit datetime is required because a time-only value means the next
# 03:00 on any day, not specifically Sunday.
job.run_repeating(run_faq_learn, interval=604800, first=_next_sunday_3am_utc())

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

# Anonymous posting runs in separate groups so message indexing remains active.
dispatcher.add_handler(anonymous_posts.private_start_handler, group=1)
dispatcher.add_handler(anonymous_posts.bind_topic_handler, group=1)
dispatcher.add_handler(anonymous_posts.list_topics_handler, group=1)
dispatcher.add_handler(anonymous_posts.reset_user_handler, group=1)
dispatcher.add_handler(anonymous_posts.callback_handler, group=1)
dispatcher.add_handler(anonymous_posts.private_text_handler, group=1)
dispatcher.add_handler(anonymous_posts.forum_observer_handler, group=2)

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
