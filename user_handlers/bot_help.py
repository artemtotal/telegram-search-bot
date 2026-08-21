# coding: utf-8
from telegram.ext import CommandHandler
from utils import auto_delete


@auto_delete
def get_help(update, context):
    help_text = (
        "🤖 *Команди бота:*\n\n"
        "/start — головне меню\n"
        "/anonymous — анонімне запитання в чат\n"
        "/housing — моніторинг житла (пошук квартир)\n"
        "/dps_document — перевірка черги ДП Документ\n"
        "/chat_id — дізнатися ID поточного чату\n\n"
        "Для адміністраторів груп:\n"
        "/stop — зупинити бота в групі\n"
        "/delete — видалити збережені повідомлення (після /stop)\n"
    )
    sent_message = context.bot.send_message(update.effective_chat.id, text=help_text, disable_notification=True,
                                    parse_mode='markdown')
    return sent_message


handler = CommandHandler('help', get_help)


